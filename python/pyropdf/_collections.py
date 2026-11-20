"""Portable collections, collection items, navigators, and named pages."""

from __future__ import annotations

import enum
import math
import secrets
from collections.abc import Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import KW_ONLY, dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple, cast

from pyropdf import _core
from pyropdf._content import Color
from pyropdf._errors import LimitError, MalformedPdfError, StalePageError
from pyropdf._metadata import _require_version, pdf_date
from pyropdf._objects import Array, Dictionary, Name, Real, Stream, String, _catalog, _encode_text_string, _Handle

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._limit_codes import LimitCode
    from pyropdf._page import Page

#: Maximum folder hierarchy depth and total folder count in ``Catalog["Collection"]["Folders"]``.
_MAX_FOLDER_DEPTH = 32
_MAX_FOLDERS = 4096
#: Maximum entries in ``CollectionSchema``, ``CollectionSort``, and ``CollectionItem``.
_MAX_SCHEMA_FIELDS = 4096
_MAX_SORT_KEYS = 4096
_MAX_CI_FIELDS = 4096
#: Maximum layout names in ``Navigator.layout``.
_MAX_LAYOUT_NAMES = 64
#: Maximum ASCII byte length of a PDF name key or layout name.
_MAX_NAME_SIZE = 127
#: Maximum length of a PDF date string (``D:...``).
_MAX_DATE_SIZE = 64
#: Maximum UTF-8 byte length of text inputs written in one call.
_MAX_TEXT_BYTES = 1024 * 1024
#: Maximum magnitude of a finite PDF real stored in 32-bit float range.
_FLOAT32_MAX = 3.4028234663852886e38
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1
#: Bounds for ``Catalog["Names"]["Pages"]``.
_MAX_TREE_DEPTH = 32
_MAX_TREE_ENTRIES = 4096
_MAX_LEAF_PAIRS = 64
_RGB_LEN = 3
_PERCENT_MAX = 100.0

#: Standard fallback layout names permitted inside an array ``Navigator["Layout"]`` (ISO 32000-2 Table 160).
_STANDARD_NAVIGATOR_LAYOUTS = frozenset({"D", "T", "H", "FilmStrip", "FreeForm", "Linear", "Tree"})


def _page_cls() -> type[Page]:
    from pyropdf._page import Page  # noqa: PLC0415 - _page imports _links -> _document.

    return Page


def _is_non_bool_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_dict(value: object) -> bool:
    return isinstance(value, Dictionary) and not isinstance(value, Stream)


def _indirect_xref(value: object) -> int | None:
    if isinstance(value, _Handle) and value.is_indirect:
        return value.xref
    return None


def _dict_id(value: object) -> tuple[int, int] | None:
    if _is_dict(value):
        d = cast("Dictionary", value)
        if d.is_indirect and d.xref is not None and d.generation is not None:
            return (d.xref, d.generation)
    return None


def _check_text_budget(texts: Iterable[str], label: str, code: LimitCode = "text_input_size") -> None:
    total = 0
    for text in texts:
        if str.__len__(text) > _MAX_TEXT_BYTES - total:
            msg = f"{label} exceeds the {_MAX_TEXT_BYTES}-byte UTF-8 limit"
            raise LimitError(code, msg)
        total += len(str.encode(text, "utf-8", "surrogatepass"))
        if total > _MAX_TEXT_BYTES:
            msg = f"{label} exceeds the {_MAX_TEXT_BYTES}-byte UTF-8 limit"
            raise LimitError(code, msg)


def _validate_pdf_name(value: object, label: str, *, forbid_type: bool = False) -> str:
    if not isinstance(value, str):
        msg = f"{label} must be a str: {value!r}"
        raise TypeError(msg)
    plain = str.__str__(value)
    if (
        not (0 < len(plain) <= _MAX_NAME_SIZE)
        or not plain.isascii()
        or not plain.isprintable()
        or plain.startswith("/")
    ):
        msg = f"{label} must be a non-empty printable ASCII PDF name without a leading '/': {value!r}"
        raise ValueError(msg)
    if forbid_type and plain == "Type":
        msg = f"{label} cannot be 'Type'"
        raise ValueError(msg)
    return plain


def _is_pdf_date(value: str) -> bool:
    return (
        len("D:") < str.__len__(value) <= _MAX_DATE_SIZE
        and str.startswith(value, "D:")
        and str.isascii(value)
        and str.isprintable(value)
    )


def _validate_date_input(value: object, label: str) -> str:
    if isinstance(value, datetime):
        return pdf_date(value)
    if not isinstance(value, str):
        msg = f"{label} must be a PDF date string or datetime: {value!r}"
        raise TypeError(msg)
    plain = str.__str__(value)
    if not _is_pdf_date(plain):
        msg = f"{label} must be a PDF date string such as 'D:20260101120000Z': {value!r}"
        raise ValueError(msg)
    return plain


def _opaque_color(value: object, label: str) -> Color | None:
    if value is None:
        return None
    if not isinstance(value, Color):
        msg = f"{label} must be a Color or None: {value!r}"
        raise TypeError(msg)
    components = (value.red, value.green, value.blue)
    if (
        isinstance(value.alpha, bool)
        or any(isinstance(c, bool) or not isinstance(c, (int, float)) for c in (*components, value.alpha))
        or value.alpha != 1
        or not all(math.isfinite(c) and 0 <= c <= 1 for c in components)
    ):
        msg = f"{label} must be an opaque Color with RGB components from 0 to 1: {value!r}"
        raise ValueError(msg)
    return Color(float(value.red), float(value.green), float(value.blue))


def _require_collection_version(document: Document, catalog: Dictionary, minimum: tuple[int, int]) -> None:
    """Promote ``catalog['Version']`` to ``minimum`` and ensure PDF 2.0 trailer ``/ID`` when needed."""
    _require_version(document, catalog, minimum)
    if minimum >= (2, 0) and "ID" not in document.trailer:
        file_id = secrets.token_bytes(16)
        document.trailer["ID"] = [file_id, file_id]


class CollectionView(enum.StrEnum):
    """Initial view of a portable collection (``/View``, ISO 32000-2 Table 153)."""

    DETAILS = "D"
    """Multi-column tabular details view."""
    TILE = "T"
    """Tile view showing icons and field values."""
    HIDDEN = "H"
    """Collection view initially hidden."""
    CUSTOM = "C"
    """Custom presentation controlled by :attr:`Collection.navigator` (PDF 2.0)."""


class CollectionFieldKind(enum.StrEnum):
    """Data type or file-property binding of a :class:`CollectionField` (``/Subtype``, ISO 32000-2 Table 155)."""

    STRING = "S"
    """Text string field stored in a collection item."""
    DATE = "D"
    """Date field stored in a collection item."""
    NUMBER = "N"
    """Numeric field stored in a collection item."""
    FILENAME = "F"
    """File specification file name."""
    DESCRIPTION = "Desc"
    """File specification description."""
    MODIFIED_DATE = "ModDate"
    """Modification date of the file or folder."""
    CREATION_DATE = "CreationDate"
    """Creation date of the file or folder."""
    SIZE = "Size"
    """Uncompressed size of the embedded file."""
    COMPRESSED_SIZE = "CompressedSize"
    """Compressed stream length of the embedded file (PDF 2.0)."""


class CollectionSplitDirection(enum.StrEnum):
    """How a viewer splits its window between the collection navigator and document preview (Table 158)."""

    HORIZONTAL = "H"
    """Split the window horizontally."""
    VERTICAL = "V"
    """Split the window vertically."""
    NONE = "N"
    """Do not split the window."""


@dataclass(frozen=True, slots=True)
class CollectionField:
    """One schema column of a portable collection (``CollectionField``, ISO 32000-2 Table 155).

    ``kind`` selects whether the column reads a custom :attr:`Attachment.collection_item`
    entry (``STRING``, ``DATE``, ``NUMBER``) or a built-in file property, and
    ``name`` is the user-visible column title (``/N``).
    """

    kind: CollectionFieldKind
    name: str
    _: KW_ONLY
    order: int | None = None
    """Relative display order among schema columns (``/O``), or ``None``."""
    visible: bool = True
    """Whether the column is initially visible (``/V``)."""
    editable: bool = False
    """Whether the viewer lets users edit the field value (``/E``)."""

    def __post_init__(self) -> None:
        """Validate field kind, display name, order, and visibility flags."""
        if not isinstance(self.kind, str):
            msg = f"kind must be a CollectionFieldKind or str: {self.kind!r}"
            raise TypeError(msg)
        object.__setattr__(self, "kind", CollectionFieldKind(self.kind))
        if not isinstance(self.name, str):
            msg = f"name must be a str: {self.name!r}"
            raise TypeError(msg)
        _check_text_budget((self.name,), "collection field name")
        object.__setattr__(self, "name", str.__str__(self.name))
        if self.order is not None:
            if not _is_non_bool_int(self.order) or not (_INT64_MIN <= self.order <= _INT64_MAX):
                msg = f"order must be a 64-bit int or None: {self.order!r}"
                raise TypeError(msg) if not _is_non_bool_int(self.order) else ValueError(msg)
            object.__setattr__(self, "order", int(self.order))
        if not isinstance(self.visible, bool):
            msg = f"visible must be a bool: {self.visible!r}"
            raise TypeError(msg)
        if not isinstance(self.editable, bool):
            msg = f"editable must be a bool: {self.editable!r}"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class CollectionSortKey:
    """One sort column of a portable collection (``CollectionSort``, ISO 32000-2 Table 156)."""

    field: str
    """Key of the schema field to sort by (``/S``)."""
    ascending: bool = True
    """Whether to sort in ascending order (``/A``)."""

    def __post_init__(self) -> None:
        """Validate ``field`` and ``ascending``."""
        object.__setattr__(self, "field", _validate_pdf_name(self.field, "field", forbid_type=True))
        if not isinstance(self.ascending, bool):
            msg = f"ascending must be a bool: {self.ascending!r}"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionColors:
    """RGB interface colors of a portable collection (``CollectionColors``, ISO 32000-2 Table 157, PDF 2.0)."""

    background: Color | None = None
    """Collection view background color (``/Background``)."""
    card_background: Color | None = None
    """Tile card background color (``/CardBackground``)."""
    card_border: Color | None = None
    """Tile card border color (``/CardBorder``)."""
    primary_text: Color | None = None
    """Primary text color (``/PrimaryText``)."""
    secondary_text: Color | None = None
    """Secondary text color (``/SecondaryText``)."""

    def __post_init__(self) -> None:
        """Validate that each color is an opaque RGB :class:`Color` in ``0..=1``."""
        for attr in ("background", "card_background", "card_border", "primary_text", "secondary_text"):
            object.__setattr__(self, attr, _opaque_color(getattr(self, attr), attr))


@dataclass(frozen=True, slots=True)
class CollectionSplit:
    """Window split configuration of a portable collection (``CollectionSplit``, ISO 32000-2 Table 158, PDF 2.0)."""

    direction: CollectionSplitDirection = CollectionSplitDirection.HORIZONTAL
    """Split axis or ``NONE`` (``/Direction``)."""
    position: float | None = None
    """Percentage from ``0`` to ``100`` allotted to the navigator pane (``/Position``); ``None`` when ``NONE``."""

    def __post_init__(self) -> None:
        """Validate ``direction`` and ``position``."""
        if not isinstance(self.direction, str):
            msg = f"direction must be a CollectionSplitDirection or str: {self.direction!r}"
            raise TypeError(msg)
        direction = CollectionSplitDirection(self.direction)
        object.__setattr__(self, "direction", direction)
        if self.position is not None:
            if isinstance(self.position, bool) or not isinstance(self.position, (int, float)):
                msg = f"position must be a number or None: {self.position!r}"
                raise TypeError(msg)
            pos = float(self.position)
            if not math.isfinite(pos) or not (0.0 <= pos <= _PERCENT_MAX):
                msg = f"position must be between 0 and 100: {self.position!r}"
                raise ValueError(msg)
            if direction is CollectionSplitDirection.NONE:
                msg = "position must be None when direction is CollectionSplitDirection.NONE"
                raise ValueError(msg)
            object.__setattr__(self, "position", pos)


@dataclass(frozen=True, slots=True, init=False)
class Navigator:
    """Navigator presentation of a portable collection (``Navigator``, ISO 32000-2 Table 160, PDF 2.0).

    ``layout`` is either a single PDF layout name (such as ``"FilmStrip"``,
    ``"FreeForm"``, ``"Linear"``, ``"Tree"``, ``"D"``, ``"T"``, ``"H"``, or an
    extension layout name) or a tuple of two or more standard layout names in
    decreasing order of preference. A one-element sequence is normalized to its
    single name string.
    """

    layout: str | tuple[str, ...]

    def __init__(self, layout: str | Sequence[str]) -> None:
        """Validate and normalize ``layout``."""
        if isinstance(layout, str):
            object.__setattr__(self, "layout", _validate_pdf_name(layout, "layout"))
            return
        if not isinstance(layout, Sequence) or isinstance(layout, (bytes, bytearray, memoryview)):
            msg = f"layout must be a str or non-empty sequence of strings: {layout!r}"
            raise TypeError(msg)
        if not layout or len(layout) > _MAX_LAYOUT_NAMES:
            msg = f"layout must contain between 1 and {_MAX_LAYOUT_NAMES} layout names"
            raise ValueError(msg)
        names = tuple(_validate_pdf_name(item, "layout") for item in layout)
        if len(names) == 1:
            object.__setattr__(self, "layout", names[0])
            return
        invalid = [item for item in names if item not in _STANDARD_NAVIGATOR_LAYOUTS]
        if invalid:
            msg = f"multi-layout fallback arrays admit only {sorted(_STANDARD_NAVIGATOR_LAYOUTS)}: {invalid[0]!r}"
            raise ValueError(msg)
        object.__setattr__(self, "layout", names)


def _validate_item_scalar(value: object, label: str) -> str | int | float:
    if isinstance(value, datetime):
        return pdf_date(value)
    if isinstance(value, bool):
        msg = f"{label} must be a str, int, float, or datetime, not bool"
        raise TypeError(msg)
    if isinstance(value, int):
        if not (_INT64_MIN <= value <= _INT64_MAX):
            msg = f"{label} integer is outside 64-bit PDF range: {value}"
            raise ValueError(msg)
        return int(value)
    if isinstance(value, Real):
        if not math.isfinite(value) or abs(float(value)) > _FLOAT32_MAX:
            msg = f"{label} must be finite and within PDF real-number range: {value!r}"
            raise ValueError(msg)
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > _FLOAT32_MAX:
            msg = f"{label} must be finite and within PDF real-number range: {value!r}"
            raise ValueError(msg)
        return float(value)
    if isinstance(value, str):
        _check_text_budget((value,), label)
        return str.__str__(value)
    msg = f"{label} must be a str, int, float, or datetime: {value!r}"
    raise TypeError(msg)


@dataclass(frozen=True, slots=True, init=False)
class CollectionSubitem:
    """A collection item value paired with a display prefix (``CollectionSubitem``, ISO 32000-2 Table 47).

    ``value`` holds the string, number, or PDF date string (``/D``; passing a
    :class:`~datetime.datetime` formats it with :func:`pdf_date`), and ``prefix``
    holds the text prefix displayed before the formatted value (``/P``).
    """

    value: str | int | float | None
    prefix: str | None

    def __init__(
        self,
        value: str | float | datetime | None = None,
        prefix: str | None = None,
    ) -> None:
        """Validate ``value`` and ``prefix``."""
        checked_val = None if value is None else _validate_item_scalar(value, "value")
        checked_prefix: str | None = None
        if prefix is not None:
            if not isinstance(prefix, str):
                msg = f"prefix must be a str or None: {prefix!r}"
                raise TypeError(msg)
            checked_prefix = str.__str__(prefix)
        texts = [t for t in (checked_val if isinstance(checked_val, str) else None, checked_prefix) if t is not None]
        if texts:
            _check_text_budget(texts, "collection subitem")
        object.__setattr__(self, "value", checked_val)
        object.__setattr__(self, "prefix", checked_prefix)


def _validate_collection_item(
    mapping: object,
    *,
    error_code: LimitCode = "text_input_size",
) -> dict[str, str | int | float | CollectionSubitem]:
    """Validate a ``CollectionItem`` mapping before writing."""
    if not isinstance(mapping, Mapping):
        msg = f"collection_item must be a mapping: {type(mapping).__name__}"
        raise TypeError(msg)
    if len(mapping) > _MAX_CI_FIELDS:
        msg = f"collection_item holds at most {_MAX_CI_FIELDS} fields"
        raise ValueError(msg)
    checked: dict[str, str | int | float | CollectionSubitem] = {}
    texts: list[str] = []
    for raw_key, raw_val in mapping.items():
        key = _validate_pdf_name(raw_key, "collection_item key", forbid_type=True)
        texts.append(key)
        if isinstance(raw_val, CollectionSubitem):
            checked[key] = raw_val
            if isinstance(raw_val.value, str):
                texts.append(raw_val.value)
            if raw_val.prefix is not None:
                texts.append(raw_val.prefix)
        else:
            scalar = _validate_item_scalar(raw_val, f"collection_item[{key!r}]")
            checked[key] = scalar
            if isinstance(scalar, str):
                texts.append(scalar)
    _check_text_budget(texts, "collection_item", code=error_code)
    return checked


def _collection_item_dict(items: Mapping[str, str | int | float | CollectionSubitem]) -> dict[str, object]:
    """Serialize validated ``CollectionItem`` entries into a PDF dictionary."""
    out: dict[str, object] = {"Type": Name("CollectionItem")}
    for key, val in items.items():
        if isinstance(val, CollectionSubitem):
            sub: dict[str, object] = {"Type": Name("CollectionSubitem")}
            if val.value is not None:
                sub["D"] = val.value
            if val.prefix is not None:
                sub["P"] = val.prefix
            out[key] = sub
        else:
            out[key] = val
    return out


def _read_subitem(sub: Dictionary) -> CollectionSubitem:
    raw_d = sub.get("D")
    val: str | int | float | None = None
    if isinstance(raw_d, String):
        val = raw_d.text
    elif _is_non_bool_int(raw_d):
        val = int(cast("int", raw_d))
    elif isinstance(raw_d, Real) and math.isfinite(raw_d) and abs(float(raw_d)) <= _FLOAT32_MAX:
        val = raw_d
    elif isinstance(raw_d, float) and math.isfinite(raw_d) and abs(raw_d) <= _FLOAT32_MAX:
        val = float(raw_d)
    raw_p = sub.get("P")
    prefix = raw_p.text if isinstance(raw_p, String) else None
    return CollectionSubitem(val, prefix)


def _read_collection_item(raw: object) -> Mapping[str, str | int | float | CollectionSubitem] | None:
    """Read a ``CollectionItem`` dictionary into an immutable mapping, or ``None`` if absent."""
    if not _is_dict(raw):
        return None
    ci = cast("Dictionary", raw)
    if len(ci) > _MAX_CI_FIELDS + 1:
        code = "object_count"
        msg = f"CollectionItem exceeds the {_MAX_CI_FIELDS}-field safety limit"
        raise LimitError(code, msg)
    parsed: dict[str, str | int | float | CollectionSubitem] = {}
    for key in ci:
        if key == "Type":
            continue
        val = ci.get(key)
        if isinstance(val, String):
            parsed[key] = val.text
        elif _is_non_bool_int(val):
            parsed[key] = int(cast("int", val))
        elif isinstance(val, Real) and math.isfinite(val) and abs(float(val)) <= _FLOAT32_MAX:
            parsed[key] = val
        elif isinstance(val, float) and math.isfinite(val) and abs(val) <= _FLOAT32_MAX:
            parsed[key] = float(val)
        elif _is_dict(val):
            parsed[key] = _read_subitem(cast("Dictionary", val))
    return MappingProxyType(parsed)


def _collection_item_xrefs(raw_ci: object) -> list[int]:
    """Return indirect object numbers owned by ``raw_ci`` and its subitems."""
    if not _is_dict(raw_ci):
        return []
    ci = cast("Dictionary", raw_ci)
    xrefs: list[int] = []
    if (ci_xref := _indirect_xref(ci)) is not None:
        xrefs.append(ci_xref)
    xrefs.extend(sub_xref for key in ci if key != "Type" and (sub_xref := _indirect_xref(ci.get(key))) is not None)
    return xrefs


def _validate_folder_id_and_text(
    folder_id: object,
    name: object,
    description: object,
) -> tuple[int, str, str | None]:
    if not _is_non_bool_int(folder_id):
        msg = f"id must be an int: {folder_id!r}"
        raise TypeError(msg)
    checked_id = int(cast("int", folder_id))
    if checked_id < 0 or checked_id > _INT64_MAX:
        msg = f"id must be a non-negative 64-bit integer: {checked_id}"
        raise ValueError(msg)
    if not isinstance(name, str):
        msg = f"name must be a str: {name!r}"
        raise TypeError(msg)
    plain_name = str.__str__(name)
    if not plain_name:
        msg = "folder name must not be empty"
        raise ValueError(msg)
    checked_desc: str | None = None
    if description is not None:
        if not isinstance(description, str):
            msg = f"description must be a str or None: {description!r}"
            raise TypeError(msg)
        checked_desc = str.__str__(description)
    texts = [plain_name] if checked_desc is None else [plain_name, checked_desc]
    _check_text_budget(texts, "collection folder text")
    return checked_id, plain_name, checked_desc


def _validate_folder_children(children: object) -> tuple[CollectionFolder, ...]:
    if isinstance(children, (str, bytes, bytearray, memoryview)) or not isinstance(children, Iterable):
        msg = "children must be an iterable of CollectionFolder instances"
        raise TypeError(msg)
    direct_children: list[CollectionFolder] = []
    for child in children:
        if not isinstance(child, CollectionFolder):
            msg = f"children must contain CollectionFolder instances: {child!r}"
            raise TypeError(msg)
        if len(direct_children) >= _MAX_FOLDERS:
            msg = f"collection folder hierarchy exceeds the {_MAX_FOLDERS}-folder limit"
            raise ValueError(msg)
        direct_children.append(child)
    return tuple(direct_children)


@dataclass(frozen=True, slots=True, init=False)
class CollectionFolder:
    """A folder node in a portable collection hierarchy (``CollectionFolder``, ISO 32000-2 Table 159, PDF 2.0).

    Folder hierarchies are validated, read, and written iteratively with an
    explicit stack up to 32 levels and 4,096 folders, with distinct
    non-negative integer ``id`` values across the tree.
    """

    id: int
    name: str
    description: str | None
    created: str | None
    modified: str | None
    collection_item: Mapping[str, str | int | float | CollectionSubitem] | None
    children: tuple[CollectionFolder, ...]

    def __init__(  # noqa: PLR0913 - mirrors ISO 32000-2 Table 159 folder entries.
        self,
        id: int,  # noqa: A002 - PDF Table 159 names the folder identifier ID.
        name: str,
        *,
        description: str | None = None,
        created: str | datetime | None = None,
        modified: str | datetime | None = None,
        collection_item: Mapping[str, str | int | float | datetime | CollectionSubitem] | None = None,
        children: Iterable[CollectionFolder] = (),
    ) -> None:
        """Validate folder attributes and verify iterative tree bounds and distinct IDs."""
        checked_id, plain_name, checked_desc = _validate_folder_id_and_text(id, name, description)
        checked_created = None if created is None else _validate_date_input(created, "created")
        checked_modified = None if modified is None else _validate_date_input(modified, "modified")
        checked_ci = None if collection_item is None else MappingProxyType(_validate_collection_item(collection_item))
        child_tuple = _validate_folder_children(children)
        _validate_folder_subtree(checked_id, child_tuple)

        object.__setattr__(self, "id", checked_id)
        object.__setattr__(self, "name", plain_name)
        object.__setattr__(self, "description", checked_desc)
        object.__setattr__(self, "created", checked_created)
        object.__setattr__(self, "modified", checked_modified)
        object.__setattr__(self, "collection_item", checked_ci)
        object.__setattr__(self, "children", child_tuple)


def _validate_folder_subtree(root_id: int, direct_children: tuple[CollectionFolder, ...]) -> None:
    """Validate depth (<= 32), total count (<= 4,096), and distinct IDs iteratively without recursion."""
    seen_ids: set[int] = {root_id}
    count = 1
    stack: list[tuple[CollectionFolder, int]] = [(child, 2) for child in reversed(direct_children)]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_FOLDER_DEPTH:
            msg = f"collection folder hierarchy exceeds the {_MAX_FOLDER_DEPTH}-level depth limit"
            raise ValueError(msg)
        count += 1
        if count > _MAX_FOLDERS:
            msg = f"collection folder hierarchy exceeds the {_MAX_FOLDERS}-folder limit"
            raise ValueError(msg)
        if node.id in seen_ids:
            msg = f"duplicate collection folder id: {node.id}"
            raise ValueError(msg)
        seen_ids.add(node.id)
        stack.extend((grandchild, depth + 1) for grandchild in reversed(node.children))


def _validate_initial_document(initial_document: object) -> str | None:
    if initial_document is None:
        return None
    if not isinstance(initial_document, str):
        msg = f"initial_document must be a str or None: {initial_document!r}"
        raise TypeError(msg)
    if not initial_document:
        msg = "initial_document must not be empty"
        raise ValueError(msg)
    _check_text_budget((initial_document,), "initial_document")
    return str.__str__(initial_document)


def _validate_optional_instance(value: object, kind: type, label: str) -> None:
    if value is not None and not isinstance(value, kind):
        msg = f"{label} must be a {kind.__name__} or None: {value!r}"
        raise TypeError(msg)


@dataclass(frozen=True, slots=True, init=False)
class Collection:
    """A portable collection dictionary on ``Document.collection`` (``Collection``, ISO 32000-2 Table 153).

    Setting ``view=CollectionView.CUSTOM`` requires a :class:`Navigator`.
    Writing PDF 2.0 collection features (``CUSTOM`` view, ``navigator``,
    ``colors``, ``split``, ``root_folder``, or ``COMPRESSED_SIZE`` schema
    fields) promotes the catalog's ``/Version`` to ``2.0``; base collections
    promote older documents to ``1.7``.
    """

    view: CollectionView
    initial_document: str | None
    schema: Mapping[str, CollectionField]
    sort: tuple[CollectionSortKey, ...]
    colors: CollectionColors | None
    split: CollectionSplit | None
    navigator: Navigator | None
    root_folder: CollectionFolder | None

    def __init__(  # noqa: PLR0913 - mirrors ISO 32000-2 Table 153 portable collection entries.
        self,
        *,
        view: CollectionView = CollectionView.DETAILS,
        initial_document: str | None = None,
        schema: Mapping[str, CollectionField] | Iterable[tuple[str, CollectionField]] = (),
        sort: Iterable[CollectionSortKey] = (),
        colors: CollectionColors | None = None,
        split: CollectionSplit | None = None,
        navigator: Navigator | None = None,
        root_folder: CollectionFolder | None = None,
    ) -> None:
        """Validate portable collection settings, schema, sort keys, navigator, and folder hierarchy."""
        if not isinstance(view, str):
            msg = f"view must be a CollectionView or str: {view!r}"
            raise TypeError(msg)
        checked_view = CollectionView(view)
        checked_initial = _validate_initial_document(initial_document)
        checked_schema = _validate_schema(schema)
        checked_sort = _validate_sort(sort)

        _validate_optional_instance(colors, CollectionColors, "colors")
        _validate_optional_instance(split, CollectionSplit, "split")
        _validate_optional_instance(navigator, Navigator, "navigator")
        if checked_view is CollectionView.CUSTOM and navigator is None:
            msg = "CollectionView.CUSTOM requires a navigator"
            raise ValueError(msg)
        if root_folder is not None:
            _validate_optional_instance(root_folder, CollectionFolder, "root_folder")
            _validate_folder_subtree(root_folder.id, root_folder.children)

        object.__setattr__(self, "view", checked_view)
        object.__setattr__(self, "initial_document", checked_initial)
        object.__setattr__(self, "schema", MappingProxyType(checked_schema))
        object.__setattr__(self, "sort", checked_sort)
        object.__setattr__(self, "colors", colors)
        object.__setattr__(self, "split", split)
        object.__setattr__(self, "navigator", navigator)
        object.__setattr__(self, "root_folder", root_folder)


def _validate_schema(
    schema: Mapping[str, CollectionField] | Iterable[tuple[str, CollectionField]],
) -> dict[str, CollectionField]:
    if isinstance(schema, (str, bytes, bytearray, memoryview)):
        msg = "schema must be a mapping or iterable of (key, CollectionField) pairs"
        raise TypeError(msg)
    items = schema.items() if isinstance(schema, Mapping) else schema
    checked: dict[str, CollectionField] = {}
    texts: list[str] = []
    for item in items:
        if isinstance(item, (str, bytes)) or not isinstance(item, tuple) or len(item) != 2:  # noqa: PLR2004
            msg = f"schema entry must be a (key, CollectionField) pair: {item!r}"
            raise TypeError(msg)
        raw_key, field = item
        key = _validate_pdf_name(raw_key, "schema key", forbid_type=True)
        if not isinstance(field, CollectionField):
            msg = f"schema[{key!r}] must be a CollectionField: {field!r}"
            raise TypeError(msg)
        if len(checked) >= _MAX_SCHEMA_FIELDS and key not in checked:
            msg = f"schema holds at most {_MAX_SCHEMA_FIELDS} fields"
            raise ValueError(msg)
        checked[key] = field
        texts.extend((key, field.name))
    _check_text_budget(texts, "collection schema")
    return checked


def _validate_sort(sort: Iterable[CollectionSortKey]) -> tuple[CollectionSortKey, ...]:
    if isinstance(sort, (str, bytes, bytearray, memoryview)) or not isinstance(sort, Iterable):
        msg = "sort must be an iterable of CollectionSortKey instances"
        raise TypeError(msg)
    keys: list[CollectionSortKey] = []
    for item in sort:
        if not isinstance(item, CollectionSortKey):
            msg = f"sort items must be CollectionSortKey instances: {item!r}"
            raise TypeError(msg)
        if len(keys) >= _MAX_SORT_KEYS:
            msg = f"sort holds at most {_MAX_SORT_KEYS} keys"
            raise ValueError(msg)
        keys.append(item)
    return tuple(keys)


def _collection_minimum_version(collection: Collection) -> tuple[int, int]:
    if (
        collection.view is CollectionView.CUSTOM
        or collection.navigator is not None
        or collection.colors is not None
        or collection.split is not None
        or collection.root_folder is not None
        or any(f.kind is CollectionFieldKind.COMPRESSED_SIZE for f in collection.schema.values())
    ):
        return (2, 0)
    return (1, 7)


def _folder_dict_entries(
    folder: CollectionFolder,
    handles: list[Dictionary],
    parent_idx: int | None,
    child_idxs: list[int],
    next_idx: int | None,
) -> dict[str, object]:
    entries: dict[str, object] = {
        "Type": Name("Folder"),
        "ID": folder.id,
        "Name": folder.name,
    }
    if parent_idx is not None:
        entries["Parent"] = handles[parent_idx]
    if child_idxs:
        entries["Child"] = handles[child_idxs[0]]
    if next_idx is not None:
        entries["Next"] = handles[next_idx]
    if folder.description is not None:
        entries["Desc"] = folder.description
    if folder.created is not None:
        entries["CreationDate"] = folder.created
    if folder.modified is not None:
        entries["ModDate"] = folder.modified
    if folder.collection_item is not None:
        entries["CI"] = _collection_item_dict(folder.collection_item)
    return entries


def _write_folder_tree(document: Document, root: CollectionFolder) -> Dictionary:
    """Write ``root`` and all descendants as indirect ``CollectionFolder`` dictionaries without recursion."""
    nodes: list[CollectionFolder] = [root]
    parent_of: list[int | None] = [None]
    children_of: list[list[int]] = [[]]
    next_of: list[int | None] = [None]
    stack: list[int] = [0]
    while stack:
        idx = stack.pop()
        child_idxs: list[int] = []
        for child in nodes[idx].children:
            c_idx = len(nodes)
            nodes.append(child)
            parent_of.append(idx)
            children_of.append([])
            next_of.append(None)
            child_idxs.append(c_idx)
        for pos in range(len(child_idxs) - 1):
            next_of[child_idxs[pos]] = child_idxs[pos + 1]
        children_of[idx] = child_idxs
        stack.extend(reversed(child_idxs))

    xrefs = [document.objects.add({}) for _ in range(len(nodes))]
    handles = [document.objects[xref] for xref in xrefs]
    for idx, folder in enumerate(nodes):
        document.objects[xrefs[idx]] = _folder_dict_entries(
            folder, handles, parent_of[idx], children_of[idx], next_of[idx]
        )
    return handles[0]


def _collect_folder_xrefs(raw_root: object) -> list[int]:
    """Collect indirect object numbers of a ``/Folders`` hierarchy iteratively with cycle detection."""
    if not _is_dict(raw_root):
        return []
    xrefs: list[int] = []
    seen: set[tuple[int, int]] = set()
    stack: list[Dictionary] = [cast("Dictionary", raw_root)]
    steps = 0
    while stack and steps < _MAX_FOLDERS:
        folder = stack.pop()
        pair = _dict_id(folder)
        if pair is not None:
            if pair in seen:
                continue
            seen.add(pair)
            xrefs.append(pair[0])
        steps += 1
        xrefs.extend(_collection_item_xrefs(folder.get("CI")))
        if (thumb_xref := _indirect_xref(folder.get("Thumb"))) is not None:
            xrefs.append(thumb_xref)
        for link_key in ("Next", "Child"):
            linked = folder.get(link_key)
            if _is_dict(linked):
                stack.append(cast("Dictionary", linked))
    return xrefs


def _collect_collection_xrefs(raw_col: object) -> list[int]:
    """Collect indirect object numbers owned by ``Catalog['Collection']``."""
    if not _is_dict(raw_col):
        return []
    col = cast("Dictionary", raw_col)
    xrefs: list[int] = []
    if (col_xref := _indirect_xref(col)) is not None:
        xrefs.append(col_xref)
    for key in ("Schema", "Navigator", "Colors", "Sort", "Split"):
        sub = col.get(key)
        if (sub_xref := _indirect_xref(sub)) is not None:
            xrefs.append(sub_xref)
        if key == "Schema" and _is_dict(sub):
            schema_dict = cast("Dictionary", sub)
            xrefs.extend(
                f_xref
                for field_key in schema_dict
                if field_key != "Type" and (f_xref := _indirect_xref(schema_dict.get(field_key))) is not None
            )
    xrefs.extend(_collect_folder_xrefs(col.get("Folders")))
    return xrefs


def _serialize_colors(colors: CollectionColors) -> dict[str, object]:
    entries: dict[str, object] = {"Type": Name("CollectionColors")}
    for attr, pdf_key in (
        ("background", "Background"),
        ("card_background", "CardBackground"),
        ("card_border", "CardBorder"),
        ("primary_text", "PrimaryText"),
        ("secondary_text", "SecondaryText"),
    ):
        color: Color | None = getattr(colors, attr)
        if color is not None:
            entries[pdf_key] = [color.red, color.green, color.blue]
    return entries


def _serialize_schema(schema: Mapping[str, CollectionField]) -> dict[str, object]:
    schema_dict: dict[str, object] = {"Type": Name("CollectionSchema")}
    for key, field in schema.items():
        field_dict: dict[str, object] = {
            "Type": Name("CollectionField"),
            "Subtype": Name(field.kind.value),
            "N": field.name,
            "V": field.visible,
            "E": field.editable,
        }
        if field.order is not None:
            field_dict["O"] = field.order
        schema_dict[key] = field_dict
    return schema_dict


def _serialize_sort(sort: tuple[CollectionSortKey, ...]) -> dict[str, object]:
    if len(sort) == 1:
        return {
            "Type": Name("CollectionSort"),
            "S": Name(sort[0].field),
            "A": sort[0].ascending,
        }
    return {
        "Type": Name("CollectionSort"),
        "S": [Name(item.field) for item in sort],
        "A": [item.ascending for item in sort],
    }


def _serialize_split(split: CollectionSplit) -> dict[str, object]:
    split_dict: dict[str, object] = {
        "Type": Name("CollectionSplit"),
        "Direction": Name(split.direction.value),
    }
    if split.position is not None:
        split_dict["Position"] = split.position
    return split_dict


def write_collection(document: Document, collection: Collection) -> None:
    """Write ``collection`` to ``Catalog['Collection']`` in a single transaction."""
    if not isinstance(collection, Collection):
        msg = f"collection must be a Collection: {type(collection).__name__}"
        raise TypeError(msg)
    minimum = _collection_minimum_version(collection)
    with document.transaction("collection"):
        catalog = _catalog(document)
        retired = _collect_collection_xrefs(catalog.get("Collection"))
        _require_collection_version(document, catalog, minimum)

        entries: dict[str, object] = {
            "Type": Name("Collection"),
            "View": Name(collection.view.value),
        }
        if collection.initial_document is not None:
            entries["D"] = collection.initial_document
        if collection.schema:
            entries["Schema"] = _serialize_schema(collection.schema)
        if collection.sort:
            entries["Sort"] = _serialize_sort(collection.sort)
        if collection.colors is not None:
            entries["Colors"] = _serialize_colors(collection.colors)
        if collection.split is not None:
            entries["Split"] = _serialize_split(collection.split)
        if collection.navigator is not None:
            layout = collection.navigator.layout
            layout_val: object = Name(layout) if isinstance(layout, str) else [Name(x) for x in layout]
            nav_xref = document.objects.add({"Type": Name("Navigator"), "Layout": layout_val})
            entries["Navigator"] = document.objects[nav_xref]
        if collection.root_folder is not None:
            entries["Folders"] = _write_folder_tree(document, collection.root_folder)

        col_xref = document.objects.add(entries)
        catalog["Collection"] = document.objects[col_xref]
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


def delete_collection(document: Document) -> None:
    """Remove ``Catalog['Collection']`` and free unreferenced collection objects in one transaction."""
    catalog = _catalog(document)
    if "Collection" not in catalog:
        return
    with document.transaction("collection"):
        catalog = _catalog(document)
        if "Collection" not in catalog:
            return
        retired = _collect_collection_xrefs(catalog.get("Collection"))
        del catalog["Collection"]
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


def _read_schema(raw: object) -> dict[str, CollectionField]:
    if not _is_dict(raw):
        return {}
    schema_dict = cast("Dictionary", raw)
    if len(schema_dict) > _MAX_SCHEMA_FIELDS + 1:
        code = "object_count"
        msg = f"CollectionSchema exceeds the {_MAX_SCHEMA_FIELDS}-field safety limit"
        raise LimitError(code, msg)
    fields: dict[str, CollectionField] = {}
    for key in schema_dict:
        if key == "Type":
            continue
        raw_field = schema_dict.get(key)
        if not _is_dict(raw_field):
            msg = f"CollectionSchema[{key!r}] is not a dictionary"
            raise MalformedPdfError(msg)
        fd = cast("Dictionary", raw_field)
        raw_subtype = fd.get("Subtype")
        raw_n = fd.get("N")
        if not isinstance(raw_subtype, Name) or not isinstance(raw_n, String):
            msg = f"CollectionField {key!r} is missing /Subtype or /N"
            raise MalformedPdfError(msg)
        try:
            kind = CollectionFieldKind(str(raw_subtype))
        except ValueError as err:
            msg = f"unknown CollectionField /Subtype: {str(raw_subtype)!r}"
            raise MalformedPdfError(msg) from err
        raw_o = fd.get("O")
        order = int(cast("int", raw_o)) if _is_non_bool_int(raw_o) else None
        raw_v = fd.get("V")
        visible = raw_v if isinstance(raw_v, bool) else True
        raw_e = fd.get("E")
        editable = raw_e if isinstance(raw_e, bool) else False
        fields[key] = CollectionField(kind, raw_n.text, order=order, visible=visible, editable=editable)
    return fields


def _ascending_at(raw_a: object, a_list: list[object] | None, idx: int) -> bool:
    if isinstance(raw_a, bool):
        return raw_a
    if a_list is not None and idx < len(a_list) and isinstance(a_list[idx], bool):
        return cast("bool", a_list[idx])
    return True


def _read_sort(raw: object) -> tuple[CollectionSortKey, ...]:
    if not _is_dict(raw):
        return ()
    sd = cast("Dictionary", raw)
    raw_s = sd.get("S")
    raw_a = sd.get("A")
    a_list = list(raw_a) if isinstance(raw_a, Array) else None
    if isinstance(raw_s, Name):
        if not str(raw_s) or str(raw_s) == "Type":
            return ()
        return (CollectionSortKey(str(raw_s), _ascending_at(raw_a, a_list, 0)),)
    if isinstance(raw_s, Array):
        if len(raw_s) > _MAX_SORT_KEYS:
            code = "object_count"
            msg = f"CollectionSort /S exceeds the {_MAX_SORT_KEYS}-key safety limit"
            raise LimitError(code, msg)
        return tuple(
            CollectionSortKey(str(item), _ascending_at(raw_a, a_list, idx))
            for idx, item in enumerate(raw_s)
            if isinstance(item, Name) and str(item) and str(item) != "Type"
        )
    return ()


def _read_rgb_array(raw: object) -> Color | None:
    if not isinstance(raw, Array) or len(raw) != _RGB_LEN:
        return None
    comps: list[float] = []
    for item in raw:
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            or not (0 <= item <= 1)
        ):
            return None
        comps.append(float(item))
    return Color(comps[0], comps[1], comps[2])


def _read_colors(raw: object) -> CollectionColors | None:
    if not _is_dict(raw):
        return None
    cd = cast("Dictionary", raw)
    return CollectionColors(
        background=_read_rgb_array(cd.get("Background")),
        card_background=_read_rgb_array(cd.get("CardBackground")),
        card_border=_read_rgb_array(cd.get("CardBorder")),
        primary_text=_read_rgb_array(cd.get("PrimaryText")),
        secondary_text=_read_rgb_array(cd.get("SecondaryText")),
    )


def _read_split(raw: object) -> CollectionSplit | None:
    if not _is_dict(raw):
        return None
    sd = cast("Dictionary", raw)
    raw_dir = sd.get("Direction")
    direction = CollectionSplitDirection.HORIZONTAL
    if isinstance(raw_dir, Name):
        try:
            direction = CollectionSplitDirection(str(raw_dir))
        except ValueError:
            direction = CollectionSplitDirection.HORIZONTAL
    position: float | None = None
    if direction is not CollectionSplitDirection.NONE:
        raw_pos = sd.get("Position")
        if (
            isinstance(raw_pos, (int, float))
            and not isinstance(raw_pos, bool)
            and math.isfinite(raw_pos)
            and 0.0 <= float(raw_pos) <= _PERCENT_MAX
        ):
            position = float(raw_pos)
    return CollectionSplit(direction, position)


def _read_navigator(raw: object) -> Navigator | None:
    if not _is_dict(raw):
        return None
    nd = cast("Dictionary", raw)
    raw_layout = nd.get("Layout")
    if isinstance(raw_layout, Name):
        text = str(raw_layout)
        if not text:
            msg = "Navigator /Layout name is empty"
            raise MalformedPdfError(msg)
        return Navigator(text)
    if isinstance(raw_layout, Array):
        if not raw_layout or len(raw_layout) > _MAX_LAYOUT_NAMES:
            msg = "Navigator /Layout array is empty or exceeds safety limit"
            raise MalformedPdfError(msg)
        names = [str(item) for item in raw_layout if isinstance(item, Name) and str(item)]
        if len(names) != len(raw_layout):
            msg = "Navigator /Layout array contains a non-name entry"
            raise MalformedPdfError(msg)
        try:
            return Navigator(names)
        except ValueError as err:
            raise MalformedPdfError(str(err)) from err
    msg = "Navigator is missing a valid /Layout entry"
    raise MalformedPdfError(msg)


class _FolderRecord(NamedTuple):
    folder_id: int
    name: str
    description: str | None
    created: str | None
    modified: str | None
    collection_item: Mapping[str, str | int | float | CollectionSubitem] | None
    child_indices: list[int]


def _parse_folder_scalar(fd: Dictionary) -> _FolderRecord:
    raw_id = fd.get("ID")
    raw_name = fd.get("Name")
    if not _is_non_bool_int(raw_id) or cast("int", raw_id) < 0:
        msg = "CollectionFolder is missing a non-negative integer /ID"
        raise MalformedPdfError(msg)
    if not isinstance(raw_name, String) or not raw_name.text:
        msg = "CollectionFolder is missing a non-empty /Name"
        raise MalformedPdfError(msg)
    raw_desc = fd.get("Desc")
    desc = raw_desc.text if isinstance(raw_desc, String) else None
    raw_created = fd.get("CreationDate")
    created = raw_created.text if isinstance(raw_created, String) and _is_pdf_date(raw_created.text) else None
    raw_modified = fd.get("ModDate")
    modified = raw_modified.text if isinstance(raw_modified, String) and _is_pdf_date(raw_modified.text) else None
    ci = _read_collection_item(fd.get("CI"))
    return _FolderRecord(int(cast("int", raw_id)), raw_name.text, desc, created, modified, ci, [])


def _build_folders_bottom_up(records: list[_FolderRecord]) -> CollectionFolder:
    built: list[CollectionFolder | None] = [None] * len(records)
    for idx in range(len(records) - 1, -1, -1):
        rec = records[idx]
        folder = object.__new__(CollectionFolder)
        object.__setattr__(folder, "id", rec.folder_id)
        object.__setattr__(folder, "name", rec.name)
        object.__setattr__(folder, "description", rec.description)
        object.__setattr__(folder, "created", rec.created)
        object.__setattr__(folder, "modified", rec.modified)
        object.__setattr__(folder, "collection_item", rec.collection_item)
        object.__setattr__(folder, "children", tuple(cast("CollectionFolder", built[c]) for c in rec.child_indices))
        built[idx] = folder
    return cast("CollectionFolder", built[0])


def _read_folder_tree(raw_root: object) -> CollectionFolder | None:
    """Read ``Collection['Folders']`` iteratively up to 32 levels and 4,096 folders with cycle detection."""
    if not _is_dict(raw_root):
        return None
    root_dict = cast("Dictionary", raw_root)
    root_rec = _parse_folder_scalar(root_dict)
    records: list[_FolderRecord] = [root_rec]
    seen_nodes: set[tuple[int, int]] = set()
    if (root_pair := _dict_id(root_dict)) is not None:
        seen_nodes.add(root_pair)
    used_ids: set[int] = {root_rec.folder_id}

    stack: list[tuple[int, Dictionary, int]] = [(0, root_dict, 1)]
    while stack:
        parent_idx, parent_dict, depth = stack.pop()
        cur = parent_dict.get("Child")
        if _is_dict(cur) and depth + 1 > _MAX_FOLDER_DEPTH:
            code = "object_depth"
            msg = f"CollectionFolder hierarchy exceeds the {_MAX_FOLDER_DEPTH}-level safety limit"
            raise LimitError(code, msg)
        to_visit: list[tuple[int, Dictionary, int]] = []
        while _is_dict(cur):
            child_dict = cast("Dictionary", cur)
            if (pair := _dict_id(child_dict)) is not None:
                if pair in seen_nodes:
                    break
                seen_nodes.add(pair)
            if len(records) >= _MAX_FOLDERS:
                code = "object_count"
                msg = f"CollectionFolder hierarchy exceeds the {_MAX_FOLDERS}-folder safety limit"
                raise LimitError(code, msg)
            rec = _parse_folder_scalar(child_dict)
            if rec.folder_id in used_ids:
                msg = f"duplicate CollectionFolder /ID: {rec.folder_id}"
                raise MalformedPdfError(msg)
            used_ids.add(rec.folder_id)
            child_idx = len(records)
            records.append(rec)
            records[parent_idx].child_indices.append(child_idx)
            to_visit.append((child_idx, child_dict, depth + 1))
            cur = child_dict.get("Next")
        stack.extend(reversed(to_visit))

    return _build_folders_bottom_up(records)


def read_collection(document: Document) -> Collection | None:
    """Read ``Catalog['Collection']`` into a :class:`Collection`, or ``None`` when absent."""
    raw_col = _catalog(document).get("Collection")
    if not _is_dict(raw_col):
        return None
    col = cast("Dictionary", raw_col)
    raw_view = col.get("View")
    view = CollectionView.DETAILS
    if isinstance(raw_view, Name):
        try:
            view = CollectionView(str(raw_view))
        except ValueError as err:
            msg = f"unknown Collection /View: {str(raw_view)!r}"
            raise MalformedPdfError(msg) from err

    raw_d = col.get("D")
    initial_doc = raw_d.text if isinstance(raw_d, String) and raw_d.text else None
    schema = _read_schema(col.get("Schema"))
    sort = _read_sort(col.get("Sort"))
    colors = _read_colors(col.get("Colors"))
    split = _read_split(col.get("Split"))
    navigator = _read_navigator(col.get("Navigator")) if "Navigator" in col else None
    if view is CollectionView.CUSTOM and navigator is None:
        msg = "Collection /View /C requires a /Navigator dictionary"
        raise MalformedPdfError(msg)
    root_folder = _read_folder_tree(col.get("Folders"))
    return Collection(
        view=view,
        initial_document=initial_doc,
        schema=schema,
        sort=sort,
        colors=colors,
        split=split,
        navigator=navigator,
        root_folder=root_folder,
    )


class _NamedPageEntry(NamedTuple):
    raw_key: bytes
    name: str
    page_pair: tuple[int, int]


def _pages_tree_root(document: Document) -> Dictionary | None:
    names = _catalog(document).get("Names")
    if not _is_dict(names):
        return None
    root = cast("Dictionary", names).get("Pages")
    return cast("Dictionary", root) if _is_dict(root) else None


def _has_named_pages(document: Document) -> bool:
    """Return whether ``Catalog['Names']['Pages']`` is present."""
    names = _catalog(document).get("Names")
    return _is_dict(names) and "Pages" in cast("Dictionary", names)


def _scan_named_page_pairs(
    core: _core._Document,
    raw_names: object,
    unique: dict[bytes, _NamedPageEntry],
    scanned: int,
) -> int:
    if not isinstance(raw_names, Array):
        return scanned
    items = list(raw_names)
    for key_obj, val_obj in zip(items[::2], items[1::2], strict=False):
        if not isinstance(key_obj, String):
            continue
        scanned += 1
        if scanned > _MAX_TREE_ENTRIES:
            code = "object_count"
            msg = f"the Pages name tree exceeds the {_MAX_TREE_ENTRIES}-entry safety limit"
            raise LimitError(code, msg)
        target_pair = _dict_id(val_obj)
        if target_pair is not None and _core.page_index(core, target_pair[0], target_pair[1]) is not None:
            raw_key = bytes(key_obj)
            unique.setdefault(raw_key, _NamedPageEntry(raw_key, key_obj.text, target_pair))
    return scanned


def _walk_named_pages(document: Document) -> list[_NamedPageEntry]:
    """Walk ``Catalog['Names']['Pages']`` iteratively up to 32 levels and 4,096 entries."""
    root = _pages_tree_root(document)
    if root is None:
        return []
    core = document._core()  # noqa: SLF001
    seen: set[tuple[int, int]] = set()
    stack: list[tuple[object, int]] = [(root, 1)]
    unique: dict[bytes, _NamedPageEntry] = {}
    scanned = 0
    while stack:
        node, depth = stack.pop()
        if not _is_dict(node):
            continue
        nd = cast("Dictionary", node)
        if (pair := _dict_id(nd)) is not None:
            if pair in seen:
                continue
            seen.add(pair)
        if depth > _MAX_TREE_DEPTH:
            code = "object_depth"
            msg = f"the Pages name tree exceeds the {_MAX_TREE_DEPTH}-level safety limit"
            raise LimitError(code, msg)
        scanned = _scan_named_page_pairs(core, nd.get("Names"), unique, scanned)
        raw_kids = nd.get("Kids")
        if isinstance(raw_kids, Array):
            stack.extend((kid, depth + 1) for kid in reversed(list(raw_kids)))
    return [unique[k] for k in sorted(unique)]


def _collect_name_tree_xrefs(root: object) -> list[int]:
    """Collect indirect object numbers of a name tree's root and ``/Kids`` nodes iteratively."""
    if not _is_dict(root):
        return []
    xrefs: list[int] = []
    seen: set[tuple[int, int]] = set()
    stack: list[object] = [root]
    steps = 0
    while stack and steps <= _MAX_TREE_ENTRIES:
        node = stack.pop()
        if not _is_dict(node):
            continue
        nd = cast("Dictionary", node)
        pair = _dict_id(nd)
        if pair is not None:
            if pair in seen:
                continue
            seen.add(pair)
            xrefs.append(pair[0])
        steps += 1
        raw_kids = nd.get("Kids")
        if isinstance(raw_kids, Array):
            if (kids_xref := _indirect_xref(raw_kids)) is not None:
                xrefs.append(kids_xref)
            stack.extend(list(raw_kids))
        if (names_xref := _indirect_xref(nd.get("Names"))) is not None:
            xrefs.append(names_xref)
    return xrefs


def _build_canonical_pages_tree(document: Document, entries: list[_NamedPageEntry]) -> Dictionary:
    """Write sorted ``entries`` as canonical 64-pair ``/Names`` / ``/Limits`` / ``/Kids`` nodes."""
    if len(entries) <= _MAX_LEAF_PAIRS:
        flat: list[object] = []
        for entry in entries:
            flat.extend((entry.raw_key, Dictionary(document, entry.page_pair)))
        root_xref = document.objects.add({"Names": flat})
        return document.objects[root_xref]

    level_nodes: list[tuple[Dictionary, bytes, bytes]] = []
    for start in range(0, len(entries), _MAX_LEAF_PAIRS):
        chunk = entries[start : start + _MAX_LEAF_PAIRS]
        low, high = chunk[0].raw_key, chunk[-1].raw_key
        flat_leaf: list[object] = []
        for entry in chunk:
            flat_leaf.extend((entry.raw_key, Dictionary(document, entry.page_pair)))
        leaf_xref = document.objects.add({"Limits": [low, high], "Names": flat_leaf})
        level_nodes.append((document.objects[leaf_xref], low, high))

    while len(level_nodes) > _MAX_LEAF_PAIRS:
        next_level: list[tuple[Dictionary, bytes, bytes]] = []
        for start in range(0, len(level_nodes), _MAX_LEAF_PAIRS):
            group = level_nodes[start : start + _MAX_LEAF_PAIRS]
            low, high = group[0][1], group[-1][2]
            mid_xref = document.objects.add({"Kids": [h for h, _, _ in group], "Limits": [low, high]})
            next_level.append((document.objects[mid_xref], low, high))
        level_nodes = next_level

    root_xref = document.objects.add({"Kids": [h for h, _, _ in level_nodes]})
    return document.objects[root_xref]


def _write_named_pages_entries(document: Document, entries: list[_NamedPageEntry]) -> None:
    """Write ``entries`` to ``Catalog['Names']['Pages']`` inside the active transaction."""
    catalog = _catalog(document)
    raw_names = catalog.get("Names")
    names_dict = cast("Dictionary", raw_names) if _is_dict(raw_names) else None
    retired = _collect_name_tree_xrefs(names_dict.get("Pages") if names_dict is not None else None)

    if not entries:
        if names_dict is not None and "Pages" in names_dict:
            del names_dict["Pages"]
            if len(names_dict) == 0:
                if (names_xref := _indirect_xref(names_dict)) is not None:
                    retired.append(names_xref)
                del catalog["Names"]
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001
        return

    entries_sorted = sorted(entries, key=lambda item: item.raw_key)
    if len(entries_sorted) > _MAX_TREE_ENTRIES:
        code = "object_count"
        msg = f"the Pages name tree's {_MAX_TREE_ENTRIES}-entry safety limit is reached"
        raise LimitError(code, msg)
    _require_version(document, catalog, (1, 3))
    new_root = _build_canonical_pages_tree(document, entries_sorted)
    if names_dict is None:
        names_xref = document.objects.add({"Pages": new_root})
        catalog["Names"] = document.objects[names_xref]
    else:
        names_dict["Pages"] = new_root
    if retired:
        _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


def _validate_page_name(name: object, *, allow_empty: bool = False) -> str:
    if not isinstance(name, str):
        msg = f"page name must be a str: {type(name).__name__}"
        raise TypeError(msg)
    if not allow_empty and not str.__len__(name):
        msg = "page name must not be empty"
        raise ValueError(msg)
    _check_text_budget((name,), "named page key")
    return str.__str__(name)


def _resolve_named_page_target(document: Document, target: object) -> tuple[int, int]:
    page_type = _page_cls()
    if isinstance(target, page_type):
        if target.document is not document:
            msg = "page belongs to another document"
            raise ValueError(msg)
        if _core.page_index(document._core(), target.xref, target._generation) is None:  # noqa: SLF001
            raise StalePageError("page is no longer in the document")  # noqa: EM101, TRY003
        return (target.xref, target._generation)  # noqa: SLF001
    if _is_non_bool_int(target):
        page = document.pages[cast("int", target)]
        return (page.xref, page._generation)  # noqa: SLF001
    msg = f"named page target must be a Page or int page index: {target!r}"
    raise TypeError(msg)


def _prepare_named_page_updates(
    document: Document,
    items: Iterable[tuple[object, object]],
) -> dict[str, tuple[bytes, tuple[int, int]]]:
    prepared: dict[str, tuple[bytes, tuple[int, int]]] = {}
    texts: list[str] = []
    for raw_name, raw_target in items:
        name = _validate_page_name(raw_name)
        texts.append(name)
        pair = _resolve_named_page_target(document, raw_target)
        prepared[name] = (_encode_text_string(name), pair)
    _check_text_budget(texts, "named page keys")
    return prepared


class NamedPages(MutableMapping[str, "Page"]):
    """The document's ``/Names /Pages`` name tree as a live mutable mapping (ISO 32000-2 Table 32).

    Keys are strings sorted in PDF name-tree byte order, and values read as live
    :class:`Page` handles. Assigning ``doc.named_pages[name] = page`` accepts a
    :class:`Page` of the same document or a zero-based page index (negative
    indexes count from the end). Deleting or selecting pages in ``doc.pages``
    prunes entries whose target page was removed in the same transaction.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap ``document``; use ``doc.named_pages``."""
        self._document = document

    def _entries_map(self) -> dict[str, Page]:
        self._document._ensure_open()  # noqa: SLF001
        page_type = _page_cls()
        return {
            entry.name: page_type(self._document, entry.page_pair[0], entry.page_pair[1])
            for entry in _walk_named_pages(self._document)
        }

    def __getitem__(self, name: str) -> Page:
        """Return the :class:`Page` named ``name``, or raise :class:`KeyError`."""
        checked = _validate_page_name(name, allow_empty=True)
        self._document._ensure_open()  # noqa: SLF001
        page_type = _page_cls()
        for entry in _walk_named_pages(self._document):
            if entry.name == checked:
                return page_type(self._document, entry.page_pair[0], entry.page_pair[1])
        raise KeyError(name)

    def __iter__(self) -> Iterator[str]:
        """Iterate named-page keys in tree order."""
        self._document._ensure_open()  # noqa: SLF001
        return iter([entry.name for entry in _walk_named_pages(self._document)])

    def __len__(self) -> int:
        """Return the number of named pages."""
        self._document._ensure_open()  # noqa: SLF001
        return len(_walk_named_pages(self._document))

    def __contains__(self, name: object) -> bool:
        """Return whether ``name`` names a live page of the document."""
        if not isinstance(name, str):
            return False
        checked = _validate_page_name(name, allow_empty=True)
        self._document._ensure_open()  # noqa: SLF001
        return any(entry.name == checked for entry in _walk_named_pages(self._document))

    def __setitem__(self, name: str, page: Page | int) -> None:
        """Bind ``name`` to ``page`` (a :class:`Page` or zero-based page index) in one transaction."""
        self.update([(name, page)])

    def __delitem__(self, name: str) -> None:
        """Remove ``name`` from ``/Names /Pages`` in one transaction, or raise :class:`KeyError`."""
        if not isinstance(name, str):
            raise KeyError(name)
        checked = _validate_page_name(name, allow_empty=True)
        self._document._ensure_open()  # noqa: SLF001
        with self._document.transaction("named pages"):
            current = _walk_named_pages(self._document)
            kept = [entry for entry in current if entry.name != checked]
            if len(kept) == len(current):
                raise KeyError(name)
            _write_named_pages_entries(self._document, kept)

    def update(self, other: object = (), /, **values: Page | int) -> None:
        """Insert or replace every named page of ``other`` and ``values`` in one transaction."""
        self._document._ensure_open()  # noqa: SLF001
        pairs: list[tuple[object, object]] = []
        if isinstance(other, Mapping):
            pairs.extend(other.items())
        elif isinstance(other, (str, bytes, bytearray, memoryview)) or not isinstance(other, Iterable):
            msg = f"update expects a mapping or iterable of pairs: {type(other).__name__}"
            raise TypeError(msg)
        else:
            for item in other:
                if isinstance(item, (str, bytes)) or not isinstance(item, tuple) or len(item) != 2:  # noqa: PLR2004
                    msg = f"update iterable item must be a (name, page) pair: {item!r}"
                    raise TypeError(msg)
                pairs.append(item)
        pairs.extend(values.items())
        prepared = _prepare_named_page_updates(self._document, pairs)
        if not prepared:
            return
        with self._document.transaction("named pages"):
            prepared = _prepare_named_page_updates(self._document, pairs)
            by_name = {entry.name: entry for entry in _walk_named_pages(self._document)}
            for key_name, (raw_key, page_pair) in prepared.items():
                by_name[key_name] = _NamedPageEntry(raw_key, key_name, page_pair)
            _write_named_pages_entries(self._document, list(by_name.values()))

    def clear(self) -> None:
        """Remove all named pages and free retired name-tree nodes in one transaction."""
        _delete_named_pages(self._document)

    def __repr__(self) -> str:
        """Return ``NamedPages({...})``."""
        return f"NamedPages({self._entries_map()!r})"


def _replace_named_pages(
    document: Document,
    pages: Mapping[str, Page | int] | Iterable[tuple[str, Page | int]],
) -> None:
    """Replace ``Catalog['Names']['Pages']`` with ``pages`` in one transaction."""
    if isinstance(pages, Mapping):
        raw_items: list[tuple[object, object]] = list(pages.items())
    elif isinstance(pages, (str, bytes, bytearray, memoryview)) or not isinstance(pages, Iterable):
        msg = f"named_pages must be a mapping or iterable of (name, page) pairs: {type(pages).__name__}"
        raise TypeError(msg)
    else:
        raw_items = []
        for item in pages:
            if isinstance(item, (str, bytes)) or not isinstance(item, tuple) or len(item) != 2:  # noqa: PLR2004
                msg = f"named_pages item must be a (name, page) pair: {item!r}"
                raise TypeError(msg)
            raw_items.append(item)
    prepared = _prepare_named_page_updates(document, raw_items)
    if not prepared and not _has_named_pages(document):
        return
    with document.transaction("named pages"):
        prepared = _prepare_named_page_updates(document, raw_items)
        entries = [_NamedPageEntry(raw_key, name, pair) for name, (raw_key, pair) in prepared.items()]
        _write_named_pages_entries(document, entries)


def _delete_named_pages(document: Document) -> None:
    """Remove ``Catalog['Names']['Pages']`` when present, freeing retired nodes."""
    document._ensure_open()  # noqa: SLF001
    if not _has_named_pages(document):
        return
    with document.transaction("named pages"):
        if not _has_named_pages(document):
            return
        _write_named_pages_entries(document, [])


def _prune_named_pages_for_removed_pages(document: Document, removed_pairs: set[tuple[int, int]]) -> None:
    """Prune ``Catalog['Names']['Pages']`` entries whose target page is in ``removed_pairs``."""
    if not removed_pairs or not _has_named_pages(document):
        return
    current = _walk_named_pages(document)
    kept = [entry for entry in current if entry.page_pair not in removed_pairs]
    if len(kept) != len(current) or not kept:
        _write_named_pages_entries(document, kept)


for _public in (
    Collection,
    CollectionColors,
    CollectionField,
    CollectionFieldKind,
    CollectionFolder,
    CollectionSortKey,
    CollectionSplit,
    CollectionSplitDirection,
    CollectionSubitem,
    CollectionView,
    NamedPages,
    Navigator,
):
    _public.__module__ = "pyropdf"

__all__ = [
    "Collection",
    "CollectionColors",
    "CollectionField",
    "CollectionFieldKind",
    "CollectionFolder",
    "CollectionSortKey",
    "CollectionSplit",
    "CollectionSplitDirection",
    "CollectionSubitem",
    "CollectionView",
    "NamedPages",
    "Navigator",
]
