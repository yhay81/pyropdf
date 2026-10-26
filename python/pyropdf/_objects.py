"""The low-level object model: names, strings, reals, and object handles.

``Document.objects``, ``Document.trailer``, ``Document.catalog``, and
``Page.obj`` expose the PDF object graph as Python values. Scalars are plain
values: ``None``, ``bool``, ``int``, :class:`Real`, :class:`Name`, and
:class:`String`. Arrays, dictionaries, and streams are handles that read and
write the document's current version; a handle names one indirect object, or
a direct container inside one, by object number, generation, and a path of
keys and indexes. Each access reads the handle's own object, so its cost does
not grow with the document or with unrelated edits (DESIGN.md section 10).
References resolve on read, and a reference to a missing object reads as
``None``, as ISO 32000-2 section 7.3.10 reads it. Every write is one
transaction, or part of the open one.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping, MutableMapping, MutableSequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Self, overload

from pyropdf import _core
from pyropdf._errors import EncryptedDocumentError, LimitError, StaleObjectError
from pyropdf._limits import _validate_optional_positive_int

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyropdf._core import _Document as _CoreDocument
    from pyropdf._document import Document

#: Default bound on the bytes that ``Stream.read`` returns.
_DEFAULT_MAX_STREAM_DATA_SIZE = 64 * 1024 * 1024

#: Nesting of lists and mappings accepted in one written value; the Rust
#: boundary repeats the check.
_MAX_VALUE_DEPTH = 32

#: The largest object number of a cross-reference table.
_MAX_OBJECT_NUMBER = 2**32 - 1

#: A real of PDF syntax: an optional sign, digits, and one decimal point.
_REAL_LEXEME = re.compile(rb"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+)")

_Root = tuple[int, int] | None
_Step = int | bytes
_Path = tuple[_Step, ...]


def _name_bytes(name: str) -> bytes:
    """Return the bytes of a name or dictionary key."""
    return name.encode("utf-8", "surrogateescape")


def _name_text(data: bytes) -> str:
    """Return a name's bytes as text, losslessly for non-UTF-8 bytes."""
    return data.decode("utf-8", "surrogateescape")


class Name(str):
    """A PDF name, such as ``/Type``, held without its leading slash.

    A name compares equal to the plain string of its text, so
    ``page.obj["Type"] == "Page"`` holds. Writing needs the type, though: a
    plain ``str`` value is written as a PDF text string, so write
    ``Name("Page")`` for a name. Bytes that are not UTF-8 are kept as
    surrogate escapes, so a name read from a file is written back unchanged.
    """

    __slots__ = ()

    def __new__(cls, name: str) -> Self:
        """Create a name from its text without the leading slash."""
        if not isinstance(name, str):
            msg = f"a Name is made from a str: {name!r}"
            raise TypeError(msg)
        if name.startswith("/"):
            msg = f"give a Name without its leading slash: Name({name[1:]!r})"
            raise ValueError(msg)
        return super().__new__(cls, name)

    def __repr__(self) -> str:
        """Return ``Name('Type')``."""
        return f"Name({str.__repr__(self)})"


class String(bytes):
    """A PDF string: its bytes, with :attr:`text` decoding a text string.

    Write a ``str`` to store a text string (ASCII as is, anything else as
    UTF-16BE), or ``bytes`` or a :class:`String` to store the bytes exactly.
    """

    __slots__ = ()

    @property
    def text(self) -> str:
        """Decode the bytes as a PDF text string.

        A UTF-16BE or UTF-8 byte-order mark selects that encoding; otherwise
        the bytes are PDFDocEncoding (ISO 32000-2 section 7.9.2.2).
        """
        return _core.decode_text_string(bytes(self))

    def __repr__(self) -> str:
        """Return ``String(b'...')``."""
        return f"String({bytes.__repr__(self)})"


class Real(float):
    """A PDF real: a ``float`` that keeps the digits it is written with.

    A real read from a document is a ``Real``, and so equal to, hashed as,
    and computed with as its ``float`` value; arithmetic returns a plain
    ``float``. :attr:`lexeme` is the bytes that the real is written with,
    when they are known, and travels with the value through copies, other
    documents, and ``pickle``. ``Real("35.68123456789012345")`` and
    ``Real(Decimal(...))`` keep digits beyond a ``float``'s precision;
    ``Real(1.5)`` has no lexeme and is written in the shortest form that
    reads back as its ``float``. As DESIGN.md section 10 says, assigning a
    ``Real`` writes its lexeme, a ``float`` its shortest form, and a
    ``Decimal`` its exact digits.
    """

    __slots__ = ("_lexeme",)
    _lexeme: bytes | None

    def __new__(cls, value: float | str | bytes | Decimal) -> Self:
        """Create a real from a ``float``, a ``Decimal``, or the text of a PDF real."""
        if isinstance(value, Real):
            return cls._make(float(value), value.lexeme)
        if isinstance(value, Decimal):
            return cls._make(float(value), _decimal_lexeme(value))
        if isinstance(value, (str, bytes)):
            lexeme = value.encode("ascii", "replace") if isinstance(value, str) else bytes(value)
            if _REAL_LEXEME.fullmatch(lexeme) is None:
                msg = f"not a PDF real, which has digits and one decimal point: {value!r}"
                raise ValueError(msg)
            return cls._make(float(lexeme), lexeme)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            msg = f"a Real is made from a float, a Decimal, or text: {value!r}"
            raise TypeError(msg)
        return cls._make(_finite(float(value)), None)

    @classmethod
    def _make(cls, value: float, lexeme: bytes | None) -> Self:
        """Return a real of ``value`` and ``lexeme`` without checking them."""
        real = float.__new__(cls, value)
        real._lexeme = lexeme  # noqa: SLF001 - the new instance's own slot.
        return real

    @property
    def lexeme(self) -> bytes | None:
        """Return the bytes that the real is written with, or ``None`` when only its ``float`` is known."""
        return self._lexeme

    def to_decimal(self) -> Decimal:
        """Return the exact decimal that the real is written as."""
        if self._lexeme is not None:
            return Decimal(self._lexeme.decode("ascii"))
        return Decimal(repr(float(self)))

    def __reduce__(self) -> tuple[type[Real], tuple[float | bytes]]:
        """Pickle the lexeme, or the ``float`` of a real without one."""
        return (Real, (float(self) if self._lexeme is None else self._lexeme,))

    def __repr__(self) -> str:
        """Return ``Real('35.6812345')``."""
        text = repr(float(self)) if self._lexeme is None else self._lexeme.decode("ascii")
        return f"Real({text!r})"


def _finite(value: float) -> float:
    """Refuse a value that is not finite, which PDF cannot write."""
    if not math.isfinite(value):
        msg = f"a PDF real must be finite: {value!r}"
        raise ValueError(msg)
    return value


def _decimal_lexeme(value: Decimal) -> bytes:
    """Return the exact digits of ``value`` in positional form, with a decimal point."""
    if not value.is_finite():
        msg = f"a PDF real must be finite: {value!r}"
        raise ValueError(msg)
    text = format(value, "f")
    return (text if "." in text else text + ".0").encode("ascii")


def _encode_text_string(text: str) -> bytes:
    """Return ``text`` as a PDF text string: ASCII as is, anything else as UTF-16BE."""
    if text.isascii():
        return text.encode("ascii")
    return b"\xfe\xff" + text.encode("utf-16-be", "surrogatepass")


class _Handle:
    """A view of one container in a document's object graph."""

    __slots__ = ("_document", "_path", "_root")

    def __init__(self, document: Document, root: _Root, path: _Path = ()) -> None:
        """Address ``path`` inside ``root``; ``None`` is the trailer."""
        self._document = document
        self._root = root
        self._path = path

    def _core(self) -> _CoreDocument:
        """Return the core document, refusing a closed one."""
        return self._document._core()  # noqa: SLF001

    def _describe(self) -> str:
        """Describe the addressed container for messages and reprs."""
        root = "trailer" if self._root is None else f"{self._root[0]} {self._root[1]} R"
        steps = "".join(f"[{step}]" if isinstance(step, int) else f"/{_name_text(step)}" for step in self._path)
        return root + steps

    @property
    def xref(self) -> int | None:
        """Return the object number of an indirect object, ``None`` for a direct value or the trailer."""
        if self._root is None or self._path:
            return None
        return self._root[0]

    @property
    def generation(self) -> int | None:
        """Return the generation number of an indirect object, ``None`` for a direct value or the trailer."""
        if self._root is None or self._path:
            return None
        return self._root[1]

    @property
    def is_indirect(self) -> bool:
        """Return whether this is an indirect object rather than a direct value inside one."""
        return self._root is not None and not self._path

    def _get(self, step: _Step) -> Any:  # noqa: ANN401 - a PDF value is any of several types.
        """Return the resolved value at ``step``."""
        return _decode(self._document, _core.object_get(self._core(), self._root, list(self._path), step), self, step)

    def _set(self, step: _Step, value: object) -> None:
        """Write ``value`` at ``step``."""
        encoded = _encode(self._document, value, 1)
        _core.object_set(self._core(), self._root, list(self._path), step, encoded)

    def _delete(self, step: _Step) -> None:
        """Remove ``step``."""
        _core.object_delete(self._core(), self._root, list(self._path), step)

    def __len__(self) -> int:
        """Return the number of entries or elements."""
        return _core.object_len(self._core(), self._root, list(self._path))

    def __eq__(self, other: object) -> bool:
        """Compare the document and the addressed container."""
        if not isinstance(other, _Handle):
            return NotImplemented
        return self._document is other._document and self._root == other._root and self._path == other._path

    def __hash__(self) -> int:
        """Hash the document and the addressed container."""
        return hash((id(self._document), self._root, self._path))

    def __repr__(self) -> str:
        """Return ``<Dictionary 12 0 R>`` or ``<Array 12 0 R/Kids>``."""
        return f"<{type(self).__name__} {self._describe()}>"


class Dictionary(_Handle, MutableMapping[str, Any]):
    """A PDF dictionary keyed by name text without the slash.

    Reading a key resolves references: an array, dictionary, or stream comes
    back as a handle, and anything else as a plain value. Assigning a value
    writes it at once, in one transaction. Plain values, lists, and mappings
    are written as direct objects; an indirect handle from the same document
    is written as a reference to it; and a direct handle is copied.
    """

    __slots__ = ()

    def __getitem__(self, key: str) -> Any:  # noqa: ANN401 - a PDF value is any of several types.
        """Return the resolved value of ``key``."""
        return self._get(_key(key))

    def __setitem__(self, key: str, value: object) -> None:
        """Write ``value`` under ``key``."""
        self._set(_key(key), value)

    def __delitem__(self, key: str) -> None:
        """Remove ``key``."""
        self._delete(_key(key))

    def __iter__(self) -> Iterator[str]:
        """Iterate over the keys as :class:`Name` values."""
        keys = _core.object_keys(self._core(), self._root, list(self._path))
        return iter([str.__new__(Name, _name_text(key)) for key in keys])

    def __contains__(self, key: object) -> bool:
        """Return whether ``key`` is present, without resolving its value."""
        if not isinstance(key, str):
            return False
        return _key(key) in _core.object_keys(self._core(), self._root, list(self._path))


class Stream(Dictionary):
    """A PDF stream: its dictionary, plus :meth:`read` and :meth:`write`."""

    __slots__ = ()

    def read(self, *, decode: bool = True, max_size: int | None = _DEFAULT_MAX_STREAM_DATA_SIZE) -> bytes:
        """Return the stream data, decoded through its filters unless ``decode`` is false.

        hayro-syntax decodes the filters, and a filter that it does not
        decode, such as an image filter, raises :class:`PdfError` instead of
        returning encoded bytes. ``max_size`` bounds the result with
        :class:`LimitError` code ``stream_data_size``, and the document's
        limits bound the decoding; ``None`` lifts ``max_size`` for trusted
        input.
        """
        _validate_optional_positive_int("max_size", max_size)
        if not isinstance(decode, bool):
            msg = f"decode must be a bool: {decode!r}"
            raise TypeError(msg)
        return _core.stream_read(self._core(), self._stream_root(), decode, max_size)

    def write(self, data: bytes, /, *, filter: Literal["FlateDecode"] | None = "FlateDecode") -> None:  # noqa: A002 - pymupdf and pikepdf name it filter.
        """Replace the stream data with ``data``, given decoded, in one transaction.

        ``filter="FlateDecode"`` compresses it; ``None`` stores it
        uncompressed. Either way the previous ``/Filter`` and ``/DecodeParms``
        are removed and ``/Length`` is updated.
        """
        if filter not in ("FlateDecode", None):
            msg = f"filter must be 'FlateDecode' or None: {filter!r}"
            raise ValueError(msg)
        _core.stream_write(self._core(), self._stream_root(), bytes(data), filter is not None)

    def _stream_root(self) -> tuple[int, int]:
        """Return the object that holds this stream, which is always indirect."""
        if self._root is None or self._path:
            msg = f"{self._describe()} is not a stream"
            raise StaleObjectError(msg)
        return self._root


class Array(_Handle, MutableSequence[Any]):
    """A PDF array; elements resolve and write as :class:`Dictionary` values do."""

    __slots__ = ()

    @overload
    def __getitem__(self, index: int) -> Any: ...  # noqa: ANN401 - a PDF value is any of several types.
    @overload
    def __getitem__(self, index: slice) -> list[Any]: ...
    def __getitem__(self, index: int | slice) -> Any:
        """Return the resolved element at ``index``, or a list for a slice."""
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        return self._get(self._position(index))

    @overload
    def __setitem__(self, index: int, value: object) -> None: ...
    @overload
    def __setitem__(self, index: slice, value: Iterable[Any]) -> None: ...
    def __setitem__(self, index: int | slice, value: object) -> None:
        """Write ``value`` at ``index``; slices are not supported."""
        if isinstance(index, slice):
            msg = "slice assignment is not supported; assign elements one by one"
            raise TypeError(msg)
        self._set(self._position(index), value)

    @overload
    def __delitem__(self, index: int) -> None: ...
    @overload
    def __delitem__(self, index: slice) -> None: ...
    def __delitem__(self, index: int | slice) -> None:
        """Remove the element at ``index``; slices are not supported."""
        if isinstance(index, slice):
            msg = "slice deletion is not supported; delete elements one by one"
            raise TypeError(msg)
        self._delete(self._position(index))

    def insert(self, index: int, value: object) -> None:
        """Insert ``value`` before ``index``, clamped as ``list.insert`` clamps it."""
        length = len(self)
        position = max(0, min(length, index + length if index < 0 else index))
        encoded = _encode(self._document, value, 1)
        _core.object_insert(self._core(), self._root, list(self._path), position, encoded)

    def _position(self, index: int) -> int:
        """Resolve a negative index and check the range."""
        if not isinstance(index, int) or isinstance(index, bool):
            msg = f"array indexes are integers: {index!r}"
            raise TypeError(msg)
        length = len(self)
        position = index + length if index < 0 else index
        if not 0 <= position < length:
            msg = f"array index {index} out of range"
            raise IndexError(msg)
        return position


class Objects(Mapping[int, Any]):
    """The indirect objects of a document, keyed by object number.

    Look up ``doc.objects[xref]`` to read an object, assign to replace it,
    and call :meth:`add` or :meth:`add_stream` to create one. An object that
    is an array, dictionary, or stream comes back as a handle; any other as
    its value. Assigning a handle here copies what it addresses, so
    ``doc.objects[target] = doc.objects[source]`` copies an object, whereas
    assigning an indirect handle inside a container writes a reference.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap a document; use ``Document.objects``."""
        self._document = document

    def _core(self) -> _CoreDocument:
        return _unlocked(self._document)

    def __getitem__(self, xref: int) -> Any:  # noqa: ANN401 - a PDF value is any of several types.
        """Return the object stored under ``xref``."""
        number = _object_number(xref)
        found = _core.object_lookup(self._core(), number)
        if found is None:
            raise KeyError(xref)
        generation, value = found
        if isinstance(value, tuple) and value[0] in _CONTAINERS:
            return _CONTAINERS[value[0]](self._document, (number, generation))
        return _decode(self._document, value, None, 0)

    def __setitem__(self, xref: int, value: object) -> None:
        """Replace the object stored under ``xref``, which must exist, in one transaction."""
        number = _object_number(xref)
        core = self._core()
        _core.object_replace(core, number, _encode(self._document, value, 0, top_level=True))

    def __iter__(self) -> Iterator[int]:
        """Iterate over object numbers in ascending order."""
        return iter(_core.object_numbers(self._core()))

    def __len__(self) -> int:
        """Return the number of object numbers in use."""
        return len(_core.object_numbers(self._core()))

    def __contains__(self, xref: object) -> bool:
        """Return whether an object is stored under ``xref``."""
        if not isinstance(xref, int) or isinstance(xref, bool) or not 0 <= xref <= _MAX_OBJECT_NUMBER:
            return False
        return _core.object_lookup(self._core(), xref) is not None

    def add(self, value: object) -> int:
        """Add ``value`` as a new indirect object, in one transaction, and return its object number."""
        core = self._core()
        number, _generation = _core.object_add(core, _encode(self._document, value, 0, top_level=True))
        return number

    def add_stream(
        self,
        data: bytes,
        /,
        dictionary: Mapping[str, Any] | None = None,
        *,
        filter: Literal["FlateDecode"] | None = "FlateDecode",  # noqa: A002 - pymupdf and pikepdf name it filter.
    ) -> int:
        """Add a stream of decoded ``data``, in one transaction, and return its object number.

        ``dictionary`` supplies further stream entries; ``/Length``,
        ``/Filter``, and ``/DecodeParms`` are set from the data and
        ``filter``, which compresses with ``"FlateDecode"`` or stores the
        data as given with ``None``.
        """
        if filter not in ("FlateDecode", None):
            msg = f"filter must be 'FlateDecode' or None: {filter!r}"
            raise ValueError(msg)
        core = self._core()
        encoded = _encode(self._document, {} if dictionary is None else dictionary, 0, top_level=True)
        number, _generation = _core.object_add_stream(core, encoded, bytes(data), filter is not None)
        return number

    def __repr__(self) -> str:
        """Return ``<Objects of N>``."""
        return f"<Objects of {len(self)}>"


def _unlocked(document: Document) -> _CoreDocument:
    """Return the core document, refusing a closed or locked one."""
    if document.needs_password:
        msg = "the document is encrypted; open it with password= or call unlock()"
        raise EncryptedDocumentError(msg)
    return document._core()  # noqa: SLF001


def _trailer(document: Document) -> Dictionary:
    """Return the trailer of an unlocked document."""
    _unlocked(document)
    return Dictionary(document, None)


def _catalog(document: Document) -> Dictionary:
    """Return the catalog that the trailer's ``/Root`` names."""
    catalog = _trailer(document).get("Root")
    if not isinstance(catalog, Dictionary) or not catalog.is_indirect:
        msg = "the trailer names no catalog dictionary"
        raise StaleObjectError(msg)
    return catalog


def _object_number(xref: object) -> int:
    """Validate an object number."""
    if not isinstance(xref, int) or isinstance(xref, bool):
        msg = f"object numbers are integers: {xref!r}"
        raise TypeError(msg)
    if not 0 <= xref <= _MAX_OBJECT_NUMBER:
        raise KeyError(xref)
    return xref


def _key(key: object) -> bytes:
    """Return the bytes of a dictionary key."""
    if not isinstance(key, str):
        msg = f"dictionary keys are str or Name: {key!r}"
        raise TypeError(msg)
    if key.startswith("/"):
        msg = f"give a key without its leading slash: {key[1:]!r}"
        raise ValueError(msg)
    return _name_bytes(key)


_CONTAINERS: dict[str, type[_Handle]] = {"a": Array, "d": Dictionary, "t": Stream}


def _decode(document: Document, value: object, parent: _Handle | None, step: _Step) -> Any:  # noqa: ANN401 - a PDF value is any of several types.
    """Wrap a tagged value that Rust read at ``step`` of ``parent``."""
    if not isinstance(value, tuple):
        return value
    tag = value[0]
    if tag == "f":
        return Real._make(value[1], value[2])
    if tag == "n":
        # A name read from a file may begin with an escaped slash, which
        # the constructor refuses as a likely mistake.
        return str.__new__(Name, _name_text(value[1]))
    if tag == "s":
        return String(value[1])
    indirect = value[1]
    kind = _CONTAINERS[tag]
    if indirect is not None:
        return kind(document, tuple(indirect))
    if parent is None:
        msg = "a direct container read without its parent"
        raise StaleObjectError(msg)
    return kind(document, parent._root, (*parent._path, step))  # noqa: SLF001


def _encode(document: Document, value: object, depth: int, *, top_level: bool = False) -> object:  # noqa: C901, PLR0911, PLR0912 - one branch per value kind.
    """Convert a Python value to the tagged form that Rust writes."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, Real):
        return ("f", float(value), value.lexeme)
    if isinstance(value, float):
        return ("f", _finite(value), None)
    if isinstance(value, Decimal):
        return ("f", float(value), _decimal_lexeme(value))
    if isinstance(value, Name):
        return ("n", _name_bytes(value))
    if isinstance(value, str):
        return ("s", _encode_text_string(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ("s", bytes(value))
    if isinstance(value, _Handle):
        if value._document is not document:  # noqa: SLF001
            msg = "a handle from another document cannot be written; copy the value instead"
            raise ValueError(msg)
        if value.is_indirect and not top_level:
            root = value._root  # noqa: SLF001
            assert root is not None  # noqa: S101 - an indirect handle has a root.
            return ("r", root[0], root[1])
        return ("c", value._root, list(value._path))  # noqa: SLF001
    if depth >= _MAX_VALUE_DEPTH:
        msg = f"a written value nests lists and mappings deeper than {_MAX_VALUE_DEPTH} levels"
        code = "object_value_depth"
        raise LimitError(code, msg)
    if isinstance(value, Mapping):
        return ("d", [(_key(key), _encode(document, item, depth + 1)) for key, item in value.items()])
    if isinstance(value, (list, tuple)):
        return ("a", [_encode(document, item, depth + 1) for item in value])
    msg = f"cannot write a {type(value).__name__} as a PDF object: {value!r}"
    raise TypeError(msg)


__all__ = ["Array", "Dictionary", "Name", "Objects", "Real", "Stream", "String"]
