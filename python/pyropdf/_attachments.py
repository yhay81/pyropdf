"""Document attachments: the ``Document.attachments`` mapping and its handles.

The attachments are the entries of the catalog's ``/EmbeddedFiles`` name tree
(ISO 32000-2, 7.11.4). Every access walks the tree within its bounds: 32
levels and 4,096 entries. Adding and deleting write the tree again as one
node whose ``/Names`` holds the entries sorted, as pylopdf does.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from pyropdf import _core
from pyropdf._errors import LimitError, PdfError, StaleObjectError
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Array, Dictionary, Name, Stream, String, _encode_text_string

if TYPE_CHECKING:
    from pyropdf._document import Document

_DEFAULT_MAX_EMBEDDED_FILE_SIZE = 64 * 1024 * 1024
_MAX_INPUT_TEXT_SIZE = 1024 * 1024
_MAX_TREE_DEPTH = 32
_MAX_TREE_ENTRIES = 4096
_MAX_INLINE_OBJECTS = 4096
#: The longest date string that a file's ``created`` and ``modified`` take.
_MAX_DATE_SIZE = 64


class _Entry(NamedTuple):
    """One entry of the name tree: its key as stored, as text, and its file specification."""

    key: bytes
    name: str
    filespec: Any


def _validate_text_size(texts: list[str], label: str) -> None:
    """Refuse texts that are longer together in UTF-8 than 1 MiB."""
    size = 0
    for text in texts:
        size += len(text.encode("utf-8", "surrogatepass"))
        if size > _MAX_INPUT_TEXT_SIZE:
            code = "embedded_file_input_size"
            msg = f"the {label} is longer than the {_MAX_INPUT_TEXT_SIZE}-byte limit in UTF-8"
            raise LimitError(code, msg)


def _validate_name(name: object) -> str:
    """Check a lookup name's type and its 1 MiB bound before walking the tree."""
    if not isinstance(name, str):
        msg = f"name must be a string: {type(name).__name__}"
        raise TypeError(msg)
    _validate_text_size([name], "attachment lookup name")
    return name


def _validate_data(data: bytes, max_size: int | None) -> bytes:
    """Check attachment data against ``max_size`` before copying it."""
    _validate_optional_positive_int("max_size", max_size)
    if max_size is not None and len(data) > max_size:
        code = "embedded_file_size"
        msg = f"attachment input is {len(data)} bytes, exceeding the {max_size}-byte limit"
        raise LimitError(code, msg)
    return bytes(data)


def _now() -> str:
    """Return the current time as a PDF date string in UTC."""
    return datetime.now(UTC).strftime("D:%Y%m%d%H%M%SZ")


def _text(value: object) -> str | None:
    """Return a text string's text, or ``None`` for another value."""
    return value.text if isinstance(value, String) else None


def _pairs(names: object) -> Iterator[_Entry]:
    """Return the entries of a node's ``/Names``, skipping a key that is not a string."""
    items = list(names) if isinstance(names, Array) else []
    for key, filespec in zip(items[::2], items[1::2], strict=False):
        if isinstance(key, String):
            yield _Entry(bytes(key), key.text, filespec)


def _walk(root: object) -> list[_Entry]:
    """Return the entries of the name tree at ``root`` in tree order, visiting each node once."""
    entries: list[_Entry] = []
    seen: set[int | None] = set()
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, Dictionary) or (node.is_indirect and node.xref in seen):
            continue
        if depth > _MAX_TREE_DEPTH:
            msg = f"the EmbeddedFiles name tree exceeds the {_MAX_TREE_DEPTH}-level safety limit"
            raise PdfError(msg)
        if node.is_indirect:
            seen.add(node.xref)
        for entry in _pairs(node.get("Names")):
            entries.append(entry)
            if len(entries) > _MAX_TREE_ENTRIES:
                msg = f"the EmbeddedFiles name tree exceeds the {_MAX_TREE_ENTRIES}-entry safety limit"
                raise PdfError(msg)
        kids = node.get("Kids")
        if isinstance(kids, Array):
            stack.extend((kid, depth + 1) for kid in reversed(list(kids)))
    return entries


def _check_inline(value: object) -> None:
    """Refuse a direct ``value`` whose copy would copy more than 4,096 values."""
    budget = _MAX_INLINE_OBJECTS
    pending = [value]
    while pending:
        item = pending.pop()
        budget -= 1
        if budget < 0:
            msg = f"an inline file specification exceeds the {_MAX_INLINE_OBJECTS}-object safety limit"
            raise PdfError(msg)
        if isinstance(item, Dictionary) and not item.is_indirect:
            pending.extend(item.values())
        elif isinstance(item, Array) and not item.is_indirect:
            pending.extend(item)


#: The version that a document without a header is held to, as pyropdf writes it.
_DEFAULT_VERSION = (1, 7)
#: The version that adds /Names/EmbeddedFiles, and the one that adds /UF.
_EMBEDDED_FILES_VERSION = (1, 4)
_UF_VERSION = (1, 7)


def _parse_version(text: str) -> tuple[int, int] | None:
    major, _, minor = text.partition(".")
    return (int(major), int(minor)) if major.isdigit() and minor.isdigit() else None


def _version(document: Document) -> tuple[int, int]:
    """Return the document's version: the later of its header and the catalog's ``/Version``."""
    header = _core.header_version(document._core())  # noqa: SLF001
    version = (_parse_version(header) if header is not None else None) or _DEFAULT_VERSION
    declared = document.catalog.get("Version")
    if isinstance(declared, Name):
        version = max(version, _parse_version(str(declared)) or version)
    return version


def _require(document: Document, minimum: tuple[int, int]) -> tuple[int, int]:
    """Raise the catalog's ``/Version`` to ``minimum`` when the document is older, and return its version."""
    version = _version(document)
    if version < minimum:
        document.catalog["Version"] = Name(f"{minimum[0]}.{minimum[1]}")
        return minimum
    return version


def _file(filespec: object) -> Stream | None:
    """Return the embedded file stream of a file specification, if it has one."""
    if not isinstance(filespec, Dictionary):
        return None
    files = filespec.get("EF")
    if not isinstance(files, Dictionary):
        return None
    for key in ("F", "UF"):
        stream = files.get(key)
        if isinstance(stream, Stream):
            return stream
    return None


class Attachment:
    """A handle on one embedded file of a document, as ``doc.attachments[name]`` returns it.

    Properties read the document on every access. A handle raises
    :class:`StaleObjectError` once its attachment has been deleted.
    """

    __slots__ = ("_document", "_name")

    def __init__(self, document: Document, name: str) -> None:
        """Create a handle; :class:`Attachments` does this."""
        self._document = document
        self._name = name

    def _filespec(self) -> Any:  # noqa: ANN401 - a file specification is any PDF value.
        """Return the file specification of this attachment, or raise when it no longer exists."""
        self._document._ensure_open()  # noqa: SLF001
        found = _lookup(self._document, self._name)
        if found is None:
            msg = f"the attachment {self._name!r} no longer exists"
            raise StaleObjectError(msg)
        return found.filespec

    def _params(self) -> Dictionary | None:
        stream = _file(self._filespec())
        params = stream.get("Params") if stream is not None else None
        return params if isinstance(params, Dictionary) else None

    @property
    def name(self) -> str:
        """Return the key under which the document lists the attachment."""
        return self._name

    @property
    def filename(self) -> str:
        """Return the viewer-facing file name (``/UF``, else ``/F``), or ``""``."""
        filespec = self._filespec()
        if not isinstance(filespec, Dictionary):
            return _text(filespec) or ""
        return _text(filespec.get("UF")) or _text(filespec.get("F")) or ""

    @property
    def description(self) -> str | None:
        """Return the description (``/Desc``), or ``None``."""
        filespec = self._filespec()
        return (_text(filespec.get("Desc")) or None) if isinstance(filespec, Dictionary) else None

    @property
    def size(self) -> int:
        """Return the uncompressed size that the file declares (``/Params /Size``), or -1, as pymupdf does."""
        params = self._params()
        size = params.get("Size") if params is not None else None
        return size if isinstance(size, int) and not isinstance(size, bool) else -1

    @property
    def created(self) -> str | None:
        """Return the creation date (``/Params /CreationDate``) as stored, or ``None``."""
        params = self._params()
        return (_text(params.get("CreationDate")) or None) if params is not None else None

    @property
    def modified(self) -> str | None:
        """Return the modification date (``/Params /ModDate``) as stored, or ``None``; :meth:`write` sets it."""
        params = self._params()
        return (_text(params.get("ModDate")) or None) if params is not None else None

    def read(self, *, max_size: int | None = _DEFAULT_MAX_EMBEDDED_FILE_SIZE) -> bytes:
        """Return the attachment's contents, as pymupdf's ``embfile_get`` does.

        ``max_size`` bounds the decoded data and defaults to 64 MiB, and the
        document's limits bound the decoding. Pass a larger positive integer
        for a known large attachment, or ``None`` only when intentionally
        accepting unbounded materialization. Oversized content raises
        :class:`LimitError` with code ``embedded_file_size``.
        """
        stream = _file(self._filespec())
        _validate_optional_positive_int("max_size", max_size)
        if stream is None:
            msg = f"the attachment {self._name!r} has no embedded file stream"
            raise PdfError(msg)
        filters = stream.get("Filter")
        if filters is not None and not isinstance(filters, Name | Array):
            msg = f"the attachment {self._name!r} has an invalid Filter"
            raise PdfError(msg)
        try:
            return stream.read(max_size=max_size)
        except LimitError as error:
            if error.code != "stream_data_size":
                raise
            code = "embedded_file_size"
            msg = f"the attachment {self._name!r} is larger than the {max_size}-byte limit"
            raise LimitError(code, msg) from error
        except PdfError as error:
            if isinstance(error, LimitError):
                raise
            msg = f"failed to decode attachment {self._name!r}: {error}"
            raise PdfError(msg) from error

    def write(self, data: bytes, /, *, max_size: int | None = _DEFAULT_MAX_EMBEDDED_FILE_SIZE) -> None:
        """Replace the attachment's contents, as pymupdf's ``embfile_upd`` does with ``buffer_``.

        The name, file name, description, and creation date stay; ``size``
        becomes the new length, ``modified`` the current time, and a stale
        ``/CheckSum`` is dropped. ``max_size`` bounds ``data`` and defaults to
        64 MiB, checked before the data is copied.
        """
        stream = _file(self._filespec())
        payload = _validate_data(data, max_size)
        if stream is None:
            msg = f"the attachment {self._name!r} has no embedded file stream"
            raise PdfError(msg)
        with self._document.transaction("attachment write"):
            stream.write(payload)
            if not isinstance(stream.get("Params"), Dictionary):
                stream["Params"] = {}
            params = stream["Params"]
            params["Size"] = len(payload)
            params["ModDate"] = _now()
            if "CheckSum" in params:
                del params["CheckSum"]

    def __eq__(self, other: object) -> bool:
        """Compare handles by document and name."""
        if not isinstance(other, Attachment):
            return NotImplemented
        return self._document is other._document and self._name == other._name

    def __hash__(self) -> int:
        """Hash consistently with equality."""
        return hash((id(self._document), self._name))

    def __repr__(self) -> str:
        """Return ``Attachment('name')``."""
        return f"Attachment({self._name!r})"


def _root(document: Document) -> Dictionary | None:
    """Return the root of the ``/EmbeddedFiles`` name tree, if the document has one."""
    names = document.catalog.get("Names")
    root = names.get("EmbeddedFiles") if isinstance(names, Dictionary) else None
    return root if isinstance(root, Dictionary) else None


def _entries(document: Document) -> list[_Entry]:
    """Return the entries of the document's name tree, each name once, sorted by key."""
    root = _root(document)
    unique: dict[bytes, _Entry] = {}
    for entry in _walk(root) if root is not None else []:
        unique.setdefault(entry.key, entry)
    return [unique[key] for key in sorted(unique)]


def _lookup(document: Document, name: str) -> _Entry | None:
    """Return the entry called ``name``, or ``None``."""
    return next((entry for entry in _entries(document) if entry.name == name), None)


def _writable(document: Document) -> list[_Entry]:
    """Return the entries of the name tree, refusing a tree that writing it again would copy beyond its bounds."""
    entries = _entries(document)
    for entry in entries:
        _check_inline(entry.filespec)
    return entries


def _rewrite(document: Document, entries: list[_Entry]) -> None:
    """Write the name tree as one node whose ``/Names`` holds ``entries``, sorted by key.

    Without entries the tree is removed, and so is a name dictionary that it
    leaves empty, since a name tree node has at least one entry.
    """
    if not entries:
        catalog = document.catalog
        names = catalog.get("Names")
        if isinstance(names, Dictionary) and "EmbeddedFiles" in names:
            del names["EmbeddedFiles"]
            if not len(names):
                del catalog["Names"]
        return
    pairs: list[object] = []
    for entry in sorted(entries, key=lambda entry: entry.key):
        pairs += [String(entry.key), entry.filespec]
    root = _root(document)
    if root is not None:
        root["Names"] = pairs
        for key in ("Kids", "Limits"):
            if key in root:
                del root[key]
        return
    catalog = document.catalog
    names = catalog.get("Names")
    if isinstance(names, Dictionary):
        names["EmbeddedFiles"] = {"Names": pairs}
    else:
        catalog["Names"] = {"EmbeddedFiles": {"Names": pairs}}


class Attachments(Mapping[str, Attachment]):
    """The embedded files of a document, keyed by name, as ``doc.attachments`` returns them.

    This live view replaces pymupdf's ``embfile_*`` methods: iterate it for
    the sorted names, index it for an :class:`Attachment`, ``add`` a file, and
    ``del`` one. The EmbeddedFiles name tree is read within its bounds on
    every access.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Create the view; :attr:`Document.attachments` does this."""
        self._document = document

    def _names(self) -> list[str]:
        self._document._ensure_open()  # noqa: SLF001
        return [entry.name for entry in _entries(self._document)]

    def __getitem__(self, name: str) -> Attachment:
        """Return the handle of the attachment called ``name``; raise :class:`KeyError` when absent."""
        _validate_name(name)
        self._document._ensure_open()  # noqa: SLF001
        if _lookup(self._document, name) is None:
            raise KeyError(name)
        return Attachment(self._document, name)

    def __iter__(self) -> Iterator[str]:
        """Iterate the attachment names in sorted order."""
        return iter(self._names())

    def __len__(self) -> int:
        """Return the number of attachments, as pymupdf's ``embfile_count`` does."""
        return len(self._names())

    def __contains__(self, name: object) -> bool:
        """Return whether an attachment is called ``name``."""
        if not isinstance(name, str):
            return False
        _validate_name(name)
        self._document._ensure_open()  # noqa: SLF001
        return _lookup(self._document, name) is not None

    def __delitem__(self, name: str) -> None:
        """Delete the attachment called ``name``; raise :class:`KeyError` when absent."""
        if name not in self:
            raise KeyError(name)
        entries = [entry for entry in _writable(self._document) if entry.name != name]
        with self._document.transaction("attachment delete"):
            _rewrite(self._document, entries)

    def add(
        self,
        name: str,
        data: bytes,
        /,
        *,
        filename: str | None = None,
        description: str | None = None,
        max_size: int | None = _DEFAULT_MAX_EMBEDDED_FILE_SIZE,
    ) -> Attachment:
        """Add an attachment and return its handle, as pymupdf's ``embfile_add`` does.

        ``name`` is the unique key; adding an existing name raises
        :class:`PdfError`. ``filename`` is the viewer-facing file name and
        defaults to ``name``; ``description`` is a description; both may hold
        any Unicode text. ``max_size`` bounds the data and defaults to 64 MiB,
        matching :meth:`Attachment.read`; ``None`` explicitly accepts
        unbounded input. The name, file name, and description together are
        bounded at 1 MiB in UTF-8 before the data is copied; refusals use
        ``embedded_file_input_size``.

        The file name is written as ``/UF`` only from PDF 1.7, the later of
        the header and the catalog's ``/Version``; a document older than PDF
        1.4, which has no embedded file tree, gets ``/Version /1.4``.
        Deleting the last attachment removes the tree.
        """
        self._document._ensure_open()  # noqa: SLF001
        if not isinstance(name, str):
            msg = f"name must be a string: {type(name).__name__}"
            raise TypeError(msg)
        if not name:
            msg = "name must be at least 1 character"
            raise ValueError(msg)
        for field, value in (("filename", filename), ("description", description)):
            if value is not None and not isinstance(value, str):
                msg = f"{field} must be a string or None: {type(value).__name__}"
                raise TypeError(msg)
        filename = name if filename is None else filename
        texts = [name, filename] if description is None else [name, filename, description]
        _validate_text_size(texts, "attachment name, filename, and description")
        payload = _validate_data(data, max_size)
        entries = _writable(self._document)
        if any(entry.name == name for entry in entries):
            msg = f"an attachment named {name!r} already exists"
            raise PdfError(msg)
        if len(entries) >= _MAX_TREE_ENTRIES:
            msg = f"the EmbeddedFiles name tree's {_MAX_TREE_ENTRIES}-entry safety limit is reached"
            raise PdfError(msg)
        objects = self._document.objects
        with self._document.transaction("attachment add"):
            dictionary = {"Type": Name("EmbeddedFile"), "Params": {"Size": len(payload)}}
            stream = objects[objects.add_stream(payload, dictionary)]
            # /UF names the file only from PDF 1.7, and the tree needs 1.4.
            unicode = _require(self._document, _EMBEDDED_FILES_VERSION) >= _UF_VERSION
            filespec: dict[str, object] = {"Type": Name("Filespec"), "F": filename}
            if unicode:
                filespec["UF"] = filename
            if description is not None:
                filespec["Desc"] = description
            filespec["EF"] = {"F": stream, "UF": stream} if unicode else {"F": stream}
            added = objects[objects.add(filespec)]
            _rewrite(self._document, [*entries, _Entry(_encode_text_string(name), name, added)])
        return Attachment(self._document, name)

    def __repr__(self) -> str:
        """Return the names, such as ``Attachments(['a.xml', 'b.txt'])``."""
        return f"Attachments({self._names()!r})"
