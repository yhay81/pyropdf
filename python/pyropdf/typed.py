"""Typed views of PDF objects, one per object type of the Arlington PDF Model.

DESIGN.md section 8: the descriptors that pyropdf-schema generates from the
model give each key of an object type its types, the object types it links
to, whether it is required or inheritable, its default, and its possible
values. A view reads and writes a :class:`~pyropdf.Dictionary`,
:class:`~pyropdf.Stream`, or :class:`~pyropdf.Array` handle by those rules::

    catalog = typed.Catalog(doc.catalog)
    root = catalog.Pages              # a PageTreeNodeRoot view
    page = typed.PageObject(doc.pages[0].obj)
    page.Rotate                       # inherited from the page tree, else 0
    page.Rotate = 90                  # checked against the declared types

Each object type is a subclass of :class:`View` named after it; a name that
begins with ``3D`` begins with ``ThreeD`` instead. An attribute reads or
writes a key whose name is an identifier, and indexing reads or writes any
key or array element. Reading converts a value to its declared type:

- ``integer`` and ``bitmask`` read as ``int``, ``number`` as ``int`` or
  :class:`~pyropdf.Real`, and ``boolean`` as ``bool``;
- ``name`` reads as :class:`~pyropdf.Name`; ``string-text``,
  ``string-ascii``, and ``date`` as ``str``; ``string`` and ``string-byte``
  as :class:`~pyropdf.String`;
- ``rectangle`` reads as a tuple of four floats as stored, and ``matrix`` as
  a :class:`~pyropdf.Matrix`;
- a container that links to object types reads as the view of the linked
  type that its ``/Type``-like keys match, a container that links to none
  as its handle, and ``name-tree`` and ``number-tree`` as their dictionary.

An inheritable key absent from its object is read from the nearest
``/Parent`` that holds it, and a key absent everywhere reads as its default
when the model gives one. An absent optional key reads as ``None`` through an
attribute, and a ``null`` value counts as absent (ISO 32000-2 section 7.3.7).
A required key that is absent, and a value of no declared type, raise
:class:`~pyropdf.PdfError`. Predicates of the model are not evaluated here;
the structural check evaluates them.

Writing takes the Python form of a declared type, or a view or handle for a
container type, and refuses anything else with ``TypeError``, or a name
outside the key's possible values with ``ValueError``. Assigning ``None`` to
an attribute removes the key, unless the key may be ``null``.

``typed.pyi``, which tools/arlington/generate.py writes, declares each view's
keys with their types for type checkers.
"""

from __future__ import annotations

import re
from functools import cache
from typing import Any, ClassVar, NamedTuple

from pyropdf import _core
from pyropdf._errors import LimitError, PdfError
from pyropdf._geometry import Matrix as _Matrix

# Aliased, since the model names object types Name and Stream.
from pyropdf._objects import Array as _Array
from pyropdf._objects import Dictionary as _Dictionary
from pyropdf._objects import Name as _Name
from pyropdf._objects import Real as _Real
from pyropdf._objects import Stream as _Stream
from pyropdf._objects import String as _String

#: The object types of the model, sorted.
TYPES: tuple[str, ...] = tuple(_core.schema_names())

#: The longest chain of ``/Parent`` links that inheritance follows: the default
#: ``DocumentLimits.max_object_depth``, which bounds the page tree's nesting.
_MAX_INHERITANCE = 64

#: Wrappers around a possible name that only gate it by version or extension.
_GATED = re.compile(r"(?:fn:(?:SinceVersion|BeforeVersion|Deprecated|IsPDFVersion|Extension)\([^,()]*,)*([^,()@]+)\)*")

_NUMBER_KINDS = frozenset({"integer", "bitmask", "number"})
_TEXT_KINDS = frozenset({"string-text", "string-ascii", "date"})
_BYTE_KINDS = frozenset({"string", "string-byte"})
_TREE_KINDS = frozenset({"name-tree", "number-tree"})
_RECTANGLE = 4
_ARRAY_KEY = re.compile(r"\d+\*?|\*")
_MATRIX = 6


class _Type(NamedTuple):
    """One declared type of a key."""

    kind: str
    links: tuple[str, ...]
    default: str | None
    names: frozenset[str] | None
    """The possible values of a ``name`` type, or ``None`` when any name is possible."""


class _Key(NamedTuple):
    """One key of an object type."""

    name: str
    inheritable: bool
    required: bool
    types: tuple[_Type, ...]


def _names(kind: str, values: list[str]) -> frozenset[str] | None:
    """Return the possible names of a ``name`` type, if every value is a plain name."""
    if kind != "name" or not values:
        return None
    names = [_GATED.fullmatch(value) for value in values]
    if not all(names):
        return None
    return frozenset(match.group(1) for match in names if match)


@cache
def _keys(name: str) -> dict[str, _Key]:
    """Return the keys of the object type ``name`` by name."""
    rows = _core.schema_object(name)
    if rows is None:
        msg = f"the Arlington PDF Model has no object type {name!r}"
        raise ValueError(msg)
    return {
        key: _Key(
            key,
            inheritable,
            required == "TRUE",
            tuple(_Type(kind, tuple(links), default, _names(kind, values)) for kind, links, default, values in rows),
        )
        for key, inheritable, required, rows in rows
    }


@cache
def _discriminators(name: str) -> dict[str, frozenset[str]]:
    """Return the keys of ``name`` whose one type is a name with known values, such as ``/Type``."""
    found = {}
    for key, described in _keys(name).items():
        if len(described.types) == 1 and described.types[0].names:
            found[key] = described.types[0].names
    return found


def _python_name(name: str) -> str:
    """Return the class name of an object type."""
    return "ThreeD" + name[2:] if name.startswith("3D") else name


_MODEL_NAMES = {_python_name(name): name for name in TYPES}
_classes: dict[str, type[View]] = {}


def _class(name: str) -> type[View]:
    """Return the view class of the object type ``name``, creating it once."""
    view = _classes.get(name)
    if view is None:
        python = _python_name(name)
        view = type(
            python, (_base(name),), {"__slots__": (), "_type": name, "__module__": __name__, "__qualname__": python}
        )
        _classes[name] = view
    return view


def __getattr__(name: str) -> type[View]:
    """Return the view class named ``name``."""
    model = _MODEL_NAMES.get(name)
    if model is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    return _class(model)


def __dir__() -> list[str]:
    """List the module's names, with every view class."""
    return sorted([*globals(), *_MODEL_NAMES])


_ABSENT: Any = object()


class View:
    """A typed view of one object: the base of the views of every object type."""

    __slots__ = ("_obj",)
    _type: ClassVar[str] = ""
    _handle: ClassVar[type[_Dictionary | _Array]]
    _obj: _Dictionary | _Array

    def __init__(self, obj: _Dictionary | _Array, /) -> None:
        """View ``obj`` as an object of this view's type."""
        if not self._type:
            msg = f"{type(self).__name__} is a base class; view an object through its type, such as typed.Catalog"
            raise TypeError(msg)
        if not isinstance(obj, self._handle):
            msg = f"a {type(self).__name__} views a {self._handle.__name__}, not {type(obj).__name__}"
            raise TypeError(msg)
        object.__setattr__(self, "_obj", obj)

    @property
    def obj(self) -> _Dictionary | _Array:
        """Return the viewed handle."""
        return self._obj

    def _key(self, key: str | int) -> tuple[str | int, _Key | None]:
        """Return the step of ``key`` in the handle and its descriptor."""
        keys = _keys(self._type)
        if isinstance(self._obj, _Array):
            index = int(key)
            if index < 0:
                index += len(self._obj)
            exact = keys.get(str(index))
            if exact is not None:
                return index, exact
            starts = sorted(int(name[:-1]) for name in keys if name.endswith("*") and name[:-1].isdigit())
            if starts and index >= starts[0]:
                return index, keys[f"{starts[0] + (index - starts[0]) % len(starts)}*"]
            return index, keys.get("*")
        if not isinstance(key, str):
            msg = f"a {type(self).__name__} is keyed by name, not {type(key).__name__}"
            raise TypeError(msg)
        return key, keys.get(key, keys.get("*"))

    def _where(self, key: str | int) -> str:
        """Name ``key`` of this object in messages."""
        return f"{self._type} {key if isinstance(key, int) else '/' + key}"

    def _raw(self, step: str | int, described: _Key | None) -> Any:  # noqa: ANN401 - a PDF value is any of several types.
        """Return the stored value at ``step``, inherited if the key is, or ``_ABSENT``."""
        node = self._obj
        if isinstance(node, _Array):
            assert isinstance(step, int)  # noqa: S101 - an array is indexed by position.
            return node[step] if -len(node) <= step < len(node) else _ABSENT
        assert isinstance(step, str)  # noqa: S101 - a dictionary is keyed by name.
        null = described is not None and any(typed.kind == "null" for typed in described.types)
        seen: set[_Dictionary] = set()
        for _ in range(_MAX_INHERITANCE):
            if step in node:
                value = node[step]
                if value is not None or null:
                    return value
            if described is None or not described.inheritable or node in seen:
                return _ABSENT
            seen.add(node)
            parent = node.get("Parent")
            if not isinstance(parent, _Dictionary):
                return _ABSENT
            node = parent
        msg = f"{self._where(step)} is inherited through more than {_MAX_INHERITANCE} /Parent links"
        code = "object_depth"
        raise LimitError(code, msg)

    def _read(self, key: str | int) -> Any:  # noqa: ANN401 - a PDF value is any of several types.
        """Return the typed value of ``key``, ``_ABSENT``, or raise for a required key."""
        step, described = self._key(key)
        value = self._raw(step, described)
        if described is None:
            return value
        if value is _ABSENT:
            for typed in described.types:
                default = _default(typed)
                if default is not _ABSENT:
                    return default
            if described.required:
                msg = f"{self._where(key)} is required and absent"
                raise PdfError(msg)
            return _ABSENT
        for typed in described.types:
            converted = _read(typed, value)
            if converted is not _ABSENT:
                return converted
        kinds = ", ".join(typed.kind for typed in described.types)
        msg = f"{self._where(key)} is {_kind_of(value)}, which is none of its types: {kinds}"
        raise PdfError(msg)

    def __getitem__(self, key: str | int) -> Any:  # noqa: ANN401 - a PDF value is any of several types.
        """Return the typed value of ``key``; raise ``KeyError`` when it is absent."""
        value = self._read(key)
        if value is _ABSENT:
            raise KeyError(key)
        return value

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - a PDF value is any of several types.
        """Return the typed value of the key ``name``, or ``None`` when it is absent."""
        if name.startswith("_") or name not in _keys(self._type):
            msg = f"{type(self).__name__} has no key {name!r}"
            raise AttributeError(msg)
        value = self._read(name)
        return None if value is _ABSENT else value

    def __setitem__(self, key: str | int, value: object) -> None:
        """Write ``value`` under ``key`` as one of its declared types."""
        step, described = self._key(key)
        if described is None:
            msg = f"{self._where(key)} is not a key of the model; write it through obj"
            raise KeyError(msg)
        written = _ABSENT
        for typed in described.types:
            written = _write(typed, value, self._where(key))
            if written is not _ABSENT:
                break
        if written is _ABSENT:
            kinds = ", ".join(typed.kind for typed in described.types)
            msg = f"{self._where(key)} takes {kinds}, not {type(value).__name__}"
            raise TypeError(msg)
        self._obj[step] = written  # ty: ignore[invalid-assignment]

    def __setattr__(self, name: str, value: object) -> None:
        """Write the key ``name``; ``None`` removes it unless it may be ``null``."""
        described = _keys(self._type).get(name)
        if name.startswith("_") or described is None:
            msg = f"{type(self).__name__} has no key {name!r}"
            raise AttributeError(msg)
        if value is None and all(typed.kind != "null" for typed in described.types):
            if name in self._obj:
                del self._obj[name]  # ty: ignore[invalid-argument-type]
            return
        self[name] = value

    def __delitem__(self, key: str | int) -> None:
        """Remove ``key``; raise ``KeyError`` when it is absent."""
        step, _ = self._key(key)
        if isinstance(self._obj, _Dictionary) and step not in self._obj:
            raise KeyError(key)
        del self._obj[step]  # ty: ignore[invalid-argument-type]

    def __delattr__(self, name: str) -> None:
        """Remove the key ``name``."""
        if name.startswith("_") or name not in _keys(self._type):
            msg = f"{type(self).__name__} has no key {name!r}"
            raise AttributeError(msg)
        if name in self._obj:
            del self._obj[name]  # ty: ignore[invalid-argument-type]

    def __contains__(self, key: object) -> bool:
        """Return whether ``key`` is stored in the object itself."""
        if isinstance(self._obj, _Array):
            return isinstance(key, int) and -len(self._obj) <= key < len(self._obj)
        return key in self._obj

    def __eq__(self, other: object) -> bool:
        """Compare the type and the viewed handle."""
        if not isinstance(other, View):
            return NotImplemented
        return self._type == other._type and self._obj == other._obj

    def __hash__(self) -> int:
        """Hash the type and the viewed handle."""
        return hash((self._type, self._obj))

    def __repr__(self) -> str:
        """Return ``<Catalog 1 0 R>``."""
        return f"<{type(self).__name__} {self._obj._describe()}>"  # noqa: SLF001


class DictionaryView(View):
    """The base of the views of dictionary types."""

    __slots__ = ()
    _handle = _Dictionary


class StreamView(DictionaryView):
    """The base of the views of stream types."""

    __slots__ = ()
    _handle = _Stream


class ArrayView(View):
    """The base of the views of array types."""

    __slots__ = ()
    _handle = _Array


def _base(name: str) -> type[View]:
    """Return the view base of an object type, as tools/arlington/generate.py chooses it.

    The model does not say whether an object type is a dictionary, a stream,
    or an array. An array's keys are positions, ``N*`` for repeated
    elements, or ``*``, and a stream's keys include Length, Filter, and
    DecodeParms.
    """
    names = set(_keys(name))
    if all(_ARRAY_KEY.fullmatch(key) for key in names) and ("Array" in name or any(key[0].isdigit() for key in names)):
        return ArrayView
    return StreamView if {"Length", "Filter", "DecodeParms"} <= names else DictionaryView


def _view(links: tuple[str, ...], obj: _Dictionary | _Array) -> View:
    """Return the view of ``obj`` as the linked type that its discriminating keys match best."""
    if len(links) == 1 or not isinstance(obj, _Dictionary):
        return _class(links[0])(obj)
    present = set(obj)
    values: dict[str, object] = {}
    best, score = links[0], -1
    for name in links:
        matched = 0
        for key, names in _discriminators(name).items():
            if key not in present:
                continue
            if key not in values:
                values[key] = obj[key]
            if values[key] not in names:
                matched = -1
                break
            matched += 1
        if matched > score:
            best, score = name, matched
    return _class(best)(obj)


def _kind_of(value: object) -> str:  # noqa: PLR0911 - one branch per kind.
    """Name the PDF type of a value read from a handle."""
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a number"
    if isinstance(value, _Name):
        return "a name"
    if isinstance(value, _String):
        return "a string"
    if isinstance(value, _Array):
        return "an array"
    if isinstance(value, _Stream):
        return "a stream"
    return "a dictionary" if isinstance(value, _Dictionary) else "null"


def _numbers(value: _Array, count: int) -> tuple[float, ...] | None:
    """Return the numbers of an array of ``count`` numbers, or ``None``."""
    if len(value) != count:
        return None
    items = list(value)
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in items):
        return None
    return tuple(float(item) for item in items)


def _read(typed: _Type, value: object) -> Any:  # noqa: ANN401, C901, PLR0911 - one branch per kind.
    """Return ``value`` as ``typed``, or ``_ABSENT`` when it is not of that type."""
    kind = typed.kind
    if kind == "boolean":
        return value if isinstance(value, bool) else _ABSENT
    if kind in _NUMBER_KINDS:
        if isinstance(value, bool):
            return _ABSENT
        return value if isinstance(value, int) or (kind == "number" and isinstance(value, _Real)) else _ABSENT
    if kind == "name":
        return value if isinstance(value, _Name) else _ABSENT
    if kind in _TEXT_KINDS:
        return value.text if isinstance(value, _String) else _ABSENT
    if kind in _BYTE_KINDS:
        return value if isinstance(value, _String) else _ABSENT
    if kind == "null":
        return None if value is None else _ABSENT
    if kind in {"rectangle", "matrix"}:
        numbers = _numbers(value, _RECTANGLE if kind == "rectangle" else _MATRIX) if isinstance(value, _Array) else None
        if numbers is None:
            return _ABSENT
        return numbers if kind == "rectangle" else _Matrix(*numbers)
    if kind in _TREE_KINDS:
        return value if isinstance(value, _Dictionary) and not isinstance(value, _Stream) else _ABSENT
    container = {"array": _Array, "stream": _Stream}.get(kind, _Dictionary)
    if not isinstance(value, container) or (kind == "dictionary" and isinstance(value, _Stream)):
        return _ABSENT
    return _view(typed.links, value) if typed.links else value


def _default(typed: _Type) -> Any:  # noqa: ANN401, PLR0911 - one branch per kind.
    """Return the default of ``typed`` when it is a plain value, else ``_ABSENT``."""
    default = typed.default
    if default is None:
        return _ABSENT
    if typed.kind in {"integer", "bitmask"} and re.fullmatch(r"-?\d+", default):
        return int(default)
    if typed.kind == "number" and re.fullmatch(r"-?\d+", default):
        return int(default)
    if typed.kind == "number" and re.fullmatch(r"-?(?:\d+\.\d*|\.\d+)", default):
        return _Real(default)
    if typed.kind == "boolean" and default in {"true", "false"}:
        return default == "true"
    if typed.kind == "name" and re.fullmatch(r"[A-Za-z0-9_.\-]+", default):
        return _Name(default)
    return _ABSENT


def _write(typed: _Type, value: object, where: str) -> Any:  # noqa: ANN401, C901, PLR0911, PLR0912 - one branch per kind.
    """Return the PDF value that writes ``value`` as ``typed``, or ``_ABSENT``."""
    kind = typed.kind
    if isinstance(value, View):
        if kind in {"array", "dictionary", "stream"} and value._type in typed.links:  # noqa: SLF001
            return value.obj
        return _ABSENT
    if isinstance(value, (_Dictionary, _Array)):
        if kind in {"array", "stream", "dictionary"} or kind in _TREE_KINDS:
            return value if _read(typed._replace(links=()), value) is not _ABSENT else _ABSENT
        return _ABSENT
    if kind == "boolean":
        return value if isinstance(value, bool) else _ABSENT
    if isinstance(value, bool):
        return _ABSENT
    if kind in {"integer", "bitmask"}:
        return value if isinstance(value, int) else _ABSENT
    if kind == "number":
        return value if isinstance(value, (int, float)) else _ABSENT
    if kind == "name" and isinstance(value, str):
        if typed.names is not None and value not in typed.names:
            msg = f"{where} is one of {', '.join(sorted(typed.names))}, not {value!r}"
            raise ValueError(msg)
        return _Name(value)
    if kind in _TEXT_KINDS and isinstance(value, str) and not isinstance(value, _Name):
        if kind == "string-ascii" and not value.isascii():
            msg = f"{where} is an ASCII string, not {value!r}"
            raise ValueError(msg)
        return value
    if kind in _BYTE_KINDS and isinstance(value, bytes):
        return _String(value)
    if kind == "null":
        return None if value is None else _ABSENT
    if kind in {"rectangle", "matrix"} and isinstance(value, (tuple, list)):
        count = _RECTANGLE if kind == "rectangle" else _MATRIX
        if len(value) == count and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
            return [float(item) for item in value]
    return _ABSENT


__all__ = ["TYPES", "ArrayView", "DictionaryView", "StreamView", "View"]
