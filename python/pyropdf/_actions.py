"""Interactive actions, document-level scripts, and additional-action triggers."""

from __future__ import annotations

import enum
import itertools
import math
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NamedTuple, NoReturn, TypeAlias, TypeGuard, cast

from pyropdf import _core
from pyropdf._errors import LimitError, StaleObjectError, StalePageError
from pyropdf._geometry import Point
from pyropdf._media_actions import GoTo3DView, RenditionAction, RichMediaExecute, media_action_dict, read_media_action
from pyropdf._metadata import _require_version
from pyropdf._objects import (
    Array,
    Dictionary,
    Name,
    Stream,
    String,
    _catalog,
    _encode_text_string,
    _Handle,
    _unlocked,
)

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._limit_codes import LimitCode
    from pyropdf._page import Page
    from pyropdf._targets import Destination, Target

#: Scripts stop at 1 MiB of UTF-8, read or written (``script_size``).
_MAX_SCRIPT_SIZE = 1024 * 1024
_MAX_ENCODED_SCRIPT_SIZE = 2 * _MAX_SCRIPT_SIZE + 3
#: Non-script text inputs written in one action or trigger edit stop at 1 MiB of UTF-8.
_MAX_ACTION_TEXT_BYTES = 1024 * 1024
#: Maximum entries in ``HideAction``, ``SetLayerStateAction``, ``SubmitFormAction``, or ``ResetFormAction``.
_MAX_ACTION_ITEMS = 4096
#: Maximum hops in an ``EmbeddedTarget`` chain (``/T``, ISO 32000-2 Table 205).
_MAX_EMBEDDED_TARGET_DEPTH = 32
#: Maximum depth, entries, and leaf fan-out in ``Catalog["Names"]["JavaScript"]``.
_MAX_TREE_DEPTH = 32
_MAX_TREE_ENTRIES = 4096
_MAX_LEAF_PAIRS = 64
#: Maximum ``/Parent`` depth when resolving qualified field names in ``/Fields``.
_MAX_FIELD_DEPTH = 32
#: Maximum magnitude of a finite PDF real stored in 32-bit float range.
_FLOAT32_MAX = 3.4028234663852886e38
#: Defined bits of ``ActionSubmitForm["Flags"]`` (bit 13 and bits 15..32 must stay clear).
_VALID_SUBMIT_FLAGS_MASK = 0x2FFF
_PAIR_LEN = 2

SubmitFormCharset: TypeAlias = Literal["utf-8", "utf-16", "Shift-JIS", "BigFive", "GBK", "UHC"]
_SUBMIT_CHARSETS = frozenset({"utf-8", "utf-16", "Shift-JIS", "BigFive", "GBK", "UHC"})

#: Minimum PDF version required when ``ActionHide["T"]`` references an annotation subtype.
_ANNOT_HIDE_MIN_VERSION: dict[str, tuple[int, int]] = {
    "FreeText": (1, 3),
    "Line": (1, 3),
    "Square": (1, 3),
    "Circle": (1, 3),
    "Highlight": (1, 3),
    "Underline": (1, 3),
    "StrikeOut": (1, 3),
    "Stamp": (1, 3),
    "Ink": (1, 3),
    "Popup": (1, 3),
    "FileAttachment": (1, 3),
    "TrapNetwork": (1, 3),
    "Squiggly": (1, 4),
    "PrinterMark": (1, 4),
    "Polygon": (1, 5),
    "PolyLine": (1, 5),
    "Caret": (1, 5),
    "Screen": (1, 5),
    "Watermark": (1, 6),
    "3D": (1, 6),
    "Redact": (1, 7),
    "Projection": (2, 0),
    "RichMedia": (2, 0),
}


def _is_non_bool_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_dict(value: object) -> TypeGuard[Dictionary]:
    return isinstance(value, Dictionary) and not isinstance(value, Stream)


def _indirect_xref(value: object) -> int | None:
    if isinstance(value, _Handle) and value.is_indirect:
        return value.xref
    return None


def _dict_id(value: object) -> tuple[int, int] | None:
    if _is_dict(value):
        d = value
        if d.is_indirect and d.xref is not None and d.generation is not None:
            return (d.xref, d.generation)
    return None


def _script_size(source: str) -> int:
    if str.__len__(source) > _MAX_SCRIPT_SIZE:
        return str.__len__(source)
    return len(str.encode(source, "utf-8", "surrogatepass"))


def _refuse_script(subject: str) -> NoReturn:
    code: LimitCode = "script_size"
    msg = f"{subject} exceed the {_MAX_SCRIPT_SIZE}-byte safety limit"
    raise LimitError(code, msg)


def _validate_script(value: object, label: str = "script") -> str:
    if not isinstance(value, str):
        msg = f"{label} must be a str: {type(value).__name__}"
        raise TypeError(msg)
    plain = str.__str__(value)
    if _script_size(plain) > _MAX_SCRIPT_SIZE:
        _refuse_script("a script's UTF-8 bytes")
    return plain


def _check_text_budget(texts: Iterable[str], label: str) -> None:
    total = 0
    code: LimitCode = "text_input_size"
    for text in texts:
        if str.__len__(text) > _MAX_ACTION_TEXT_BYTES - total:
            msg = f"{label} exceeds the {_MAX_ACTION_TEXT_BYTES}-byte UTF-8 limit"
            raise LimitError(code, msg)
        total += len(str.encode(text, "utf-8", "surrogatepass"))
        if total > _MAX_ACTION_TEXT_BYTES:
            msg = f"{label} exceeds the {_MAX_ACTION_TEXT_BYTES}-byte UTF-8 limit"
            raise LimitError(code, msg)


def _non_empty_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        msg = f"{label} must be a str: {value!r}"
        raise TypeError(msg)
    plain = str.__str__(value)
    if not plain:
        msg = f"{label} must not be empty"
        raise ValueError(msg)
    return plain


def _decode_byte_or_text_string(raw: String) -> str:
    data = bytes(raw)
    if data.startswith((b"\xfe\xff", b"\xef\xbb\xbf")):
        return raw.text
    return data.decode("utf-8", "replace")


def _javascript_source(action: object) -> str | None:
    """Return the source of a ``/S /JavaScript`` action dictionary, or ``None``."""
    if not _is_dict(action):
        return None
    action_dict = action
    if action_dict.get("S") != "JavaScript":
        return None
    source = action_dict.get("JS")
    if isinstance(source, Stream):
        try:
            data = source.read(max_size=_MAX_ENCODED_SCRIPT_SIZE)
        except LimitError as error:
            if error.code != "stream_data_size":
                raise
            _refuse_script("a script's bytes")
        source = String(data)
    elif not isinstance(source, String):
        return None
    elif len(source) > _MAX_ENCODED_SCRIPT_SIZE:
        _refuse_script("a script's bytes")
    text = source.text
    if _script_size(text) > _MAX_SCRIPT_SIZE:
        _refuse_script("a script's UTF-8 bytes")
    return text


@dataclass(frozen=True, slots=True)
class JavaScriptAction:
    """An action that runs an ECMAScript script (``/S /JavaScript``, ISO 32000-2 Table 221).

    pyropdf stores and reads scripts as data and never executes them. Each
    script is bounded at 1 MiB of UTF-8 (``LimitError.code == "script_size"``).
    """

    script: str

    def __post_init__(self) -> None:
        """Validate ``script`` and its UTF-8 size."""
        object.__setattr__(self, "script", _validate_script(self.script, "script"))


def _validate_non_neg_int_or_str(value: object, label: str) -> int | str | None:
    if value is None:
        return None
    if _is_non_bool_int(value):
        num = int(cast("int", value))
        if num < 0:
            msg = f"{label} index must not be negative: {num}"
            raise ValueError(msg)
        return num
    if isinstance(value, str):
        return _non_empty_str(value, label)
    msg = f"{label} must be a non-negative int, str, or None: {value!r}"
    raise TypeError(msg)


def _validate_embedded_target_chain(root: EmbeddedTarget) -> None:
    """Verify iteratively that ``root`` nests at most 32 hops and fits within 1 MiB of UTF-8."""
    hops = 0
    texts: list[str] = []
    current: EmbeddedTarget | None = root
    while current is not None:
        hops += 1
        if hops > _MAX_EMBEDDED_TARGET_DEPTH:
            msg = f"embedded target chain exceeds the {_MAX_EMBEDDED_TARGET_DEPTH}-hop limit"
            raise ValueError(msg)
        if current.embedded_file is not None:
            texts.append(current.embedded_file)
        if isinstance(current.annotation_page, str):
            texts.append(current.annotation_page)
        if isinstance(current.annotation, str):
            texts.append(current.annotation)
        current = current.next_target
    _check_text_budget(texts, "embedded target")


@dataclass(frozen=True, slots=True, kw_only=True)
class EmbeddedTarget:
    """One hop in an embedded go-to target path (``/T``, ISO 32000-2 Table 205).

    ``relationship`` is ``"parent"`` (``/R /P``) or ``"child"`` (``/R /C``). A
    child hop names either ``embedded_file`` in ``/Names /EmbeddedFiles``
    (``/N``) or a file-attachment annotation via ``annotation_page`` (``/P``)
    and ``annotation`` (``/A``). ``next_target`` chains up to 32 hops without
    recursion.
    """

    relationship: Literal["parent", "child"] = "child"
    embedded_file: str | None = None
    annotation_page: int | str | None = None
    annotation: int | str | None = None
    next_target: EmbeddedTarget | None = None

    def __post_init__(self) -> None:
        """Validate hop fields and iterative chain depth."""
        if not isinstance(self.relationship, str):
            msg = f"relationship must be 'parent' or 'child': {self.relationship!r}"
            raise TypeError(msg)
        if self.relationship not in {"parent", "child"}:
            msg = f"relationship must be 'parent' or 'child': {self.relationship!r}"
            raise ValueError(msg)
        object.__setattr__(self, "relationship", str.__str__(self.relationship))

        ef = None if self.embedded_file is None else _non_empty_str(self.embedded_file, "embedded_file")
        ap = _validate_non_neg_int_or_str(self.annotation_page, "annotation_page")
        annot = _validate_non_neg_int_or_str(self.annotation, "annotation")
        if (ap is None) != (annot is None):
            msg = "annotation_page and annotation must both be set or both be None"
            raise ValueError(msg)
        if self.relationship == "parent":
            if ef is not None or ap is not None:
                msg = "parent EmbeddedTarget cannot set embedded_file, annotation_page, or annotation"
                raise ValueError(msg)
        elif (ef is not None) == (ap is not None):
            msg = "child EmbeddedTarget requires either embedded_file or both annotation_page and annotation"
            raise ValueError(msg)

        if self.next_target is not None and not isinstance(self.next_target, EmbeddedTarget):
            msg = f"next_target must be an EmbeddedTarget or None: {self.next_target!r}"
            raise TypeError(msg)
        object.__setattr__(self, "embedded_file", ef)
        object.__setattr__(self, "annotation_page", ap)
        object.__setattr__(self, "annotation", annot)
        _validate_embedded_target_chain(self)


def _validate_zoom(zoom: object) -> float | None:
    if zoom is None:
        return None
    if isinstance(zoom, bool) or not isinstance(zoom, (int, float)):
        msg = f"zoom must be a number or None: {zoom!r}"
        raise TypeError(msg)
    val = float(zoom)
    if not math.isfinite(val) or val < 0 or val > _FLOAT32_MAX:
        msg = f"zoom must be finite, non-negative, and within PDF real-number range: {zoom!r}"
        raise ValueError(msg)
    return val


def _validate_embedded_page_and_view(
    page: object,
    point: Point | Sequence[float] | None,
    zoom: float | None,
    name: str | None,
) -> tuple[int, Point | None, float | None, str | None]:
    if not _is_non_bool_int(page):
        msg = f"page must be an int: {page!r}"
        raise TypeError(msg)
    checked_page = int(cast("int", page))
    if checked_page < 0:
        msg = f"page must not be negative: {checked_page}"
        raise ValueError(msg)
    checked_point = None if point is None else Point(point)
    if checked_point is not None and (abs(checked_point.x) > _FLOAT32_MAX or abs(checked_point.y) > _FLOAT32_MAX):
        msg = f"point coordinates exceed PDF real-number range: {checked_point!r}"
        raise ValueError(msg)
    checked_zoom = _validate_zoom(zoom)
    checked_name = None if name is None else _non_empty_str(name, "name")
    if checked_name is not None and (checked_point is not None or checked_zoom is not None):
        msg = "point and zoom cannot be combined with a named destination"
        raise ValueError(msg)
    return checked_page, checked_point, checked_zoom, checked_name


@dataclass(frozen=True, slots=True, init=False)
class EmbeddedDestination:
    """An embedded go-to action into an embedded or parent PDF file (``/S /GoToE``, ISO 32000-2 Table 204).

    At least one of ``target`` (``/T``) or ``file`` (``/F``) must be given.
    When ``name`` is set, ``/D`` names that destination instead of ``page``,
    ``point``, and ``zoom``.
    """

    page: int
    point: Point | None
    zoom: float | None
    name: str | None
    target: EmbeddedTarget | None
    file: str | None
    new_window: bool | None

    def __init__(  # noqa: PLR0913 - mirrors ISO 32000-2 Table 204 GoToE entries.
        self,
        *,
        page: int = 0,
        point: Point | Sequence[float] | None = None,
        zoom: float | None = None,
        name: str | None = None,
        target: EmbeddedTarget | None = None,
        file: str | None = None,
        new_window: bool | None = None,
    ) -> None:
        """Validate destination coordinates, target path, file specification, and window option."""
        checked_page, checked_point, checked_zoom, checked_name = _validate_embedded_page_and_view(
            page, point, zoom, name
        )
        if target is not None and not isinstance(target, EmbeddedTarget):
            msg = f"target must be an EmbeddedTarget or None: {target!r}"
            raise TypeError(msg)
        checked_file = None if file is None else _non_empty_str(file, "file")
        if target is None and checked_file is None:
            msg = "EmbeddedDestination requires at least one of target or file"
            raise ValueError(msg)
        if new_window is not None and not isinstance(new_window, bool):
            msg = f"new_window must be a bool or None: {new_window!r}"
            raise TypeError(msg)
        texts = [t for t in (checked_name, checked_file) if t is not None]
        _check_text_budget(texts, "embedded destination")

        object.__setattr__(self, "page", checked_page)
        object.__setattr__(self, "point", checked_point)
        object.__setattr__(self, "zoom", checked_zoom)
        object.__setattr__(self, "name", checked_name)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "file", checked_file)
        object.__setattr__(self, "new_window", new_window)


def _normalize_hide_targets(targets: object) -> tuple[str | int, ...]:
    if isinstance(targets, str) or _is_non_bool_int(targets):
        raw_items: list[object] = [targets]
    elif isinstance(targets, (bytes, bytearray, memoryview)) or not isinstance(targets, Iterable):
        msg = f"targets must be a field name, annotation xref, or iterable of them: {targets!r}"
        raise TypeError(msg)
    else:
        raw_items = list(itertools.islice(targets, _MAX_ACTION_ITEMS + 1))
    if not raw_items or len(raw_items) > _MAX_ACTION_ITEMS:
        msg = f"targets must contain between 1 and {_MAX_ACTION_ITEMS} entries"
        raise ValueError(msg)

    normalized: list[str | int] = []
    texts: list[str] = []
    for item in raw_items:
        if _is_non_bool_int(item):
            xref = int(cast("int", item))
            if xref <= 0:
                msg = f"annotation xref in HideAction must be positive: {xref}"
                raise ValueError(msg)
            normalized.append(xref)
        elif isinstance(item, str):
            field_name = _non_empty_str(item, "field name")
            texts.append(field_name)
            normalized.append(field_name)
        else:
            msg = f"each target in HideAction must be a str or positive int xref: {item!r}"
            raise TypeError(msg)
    _check_text_budget(texts, "HideAction targets")
    return tuple(normalized)


@dataclass(frozen=True, slots=True, init=False)
class HideAction:
    """An action that hides or shows annotations and form fields (``/S /Hide``, ISO 32000-2 Table 214).

    ``targets`` holds non-empty qualified field names or positive indirect
    annotation object numbers (``xref``), and ``hide`` sets ``/H`` (``True``
    hides them, ``False`` shows them).
    """

    targets: tuple[str | int, ...]
    hide: bool

    def __init__(self, targets: str | int | Iterable[str | int], hide: bool = True) -> None:  # noqa: FBT001, FBT002 - Table 214 /H defaults to true.
        """Validate ``targets`` and ``hide``."""
        if not isinstance(hide, bool):
            msg = f"hide must be a bool: {hide!r}"
            raise TypeError(msg)
        object.__setattr__(self, "targets", _normalize_hide_targets(targets))
        object.__setattr__(self, "hide", hide)


class LayerStateTransition(enum.StrEnum):
    """How a :class:`LayerStateChange` updates optional content groups (ISO 32000-2 Table 217)."""

    ON = "ON"
    """Turn the listed optional content groups on."""
    OFF = "OFF"
    """Turn the listed optional content groups off."""
    TOGGLE = "Toggle"
    """Toggle the visibility state of the listed optional content groups."""


@dataclass(frozen=True, slots=True, init=False)
class LayerStateChange:
    """One transition group inside a :class:`SetLayerStateAction` ``/State`` array (ISO 32000-2 Table 217)."""

    mode: LayerStateTransition
    layers: tuple[int, ...]

    def __init__(self, mode: LayerStateTransition | str, layers: int | Iterable[int]) -> None:
        """Validate ``mode`` and non-empty positive optional-content group ``layers``."""
        if not isinstance(mode, str):
            msg = f"mode must be a LayerStateTransition or str: {mode!r}"
            raise TypeError(msg)
        checked_mode = LayerStateTransition(mode)
        if _is_non_bool_int(layers):
            raw_layers: list[object] = [layers]
        elif isinstance(layers, (str, bytes, bytearray, memoryview)) or not isinstance(layers, Iterable):
            msg = f"layers must be a positive int xref or iterable of xrefs: {layers!r}"
            raise TypeError(msg)
        else:
            raw_layers = list(itertools.islice(layers, _MAX_ACTION_ITEMS + 1))
        if not raw_layers or len(raw_layers) > _MAX_ACTION_ITEMS:
            msg = f"layers must contain between 1 and {_MAX_ACTION_ITEMS} xrefs"
            raise ValueError(msg)
        checked_layers: list[int] = []
        for item in raw_layers:
            if not _is_non_bool_int(item):
                msg = f"layer xref must be an int: {item!r}"
                raise TypeError(msg)
            xref = int(cast("int", item))
            if xref <= 0:
                msg = f"layer xref must be positive: {xref}"
                raise ValueError(msg)
            checked_layers.append(xref)
        object.__setattr__(self, "mode", checked_mode)
        object.__setattr__(self, "layers", tuple(checked_layers))


@dataclass(frozen=True, slots=True, init=False)
class SetLayerStateAction:
    """An action that changes optional-content group states (``/S /SetOCGState``, ISO 32000-2 Table 217)."""

    changes: tuple[LayerStateChange, ...]
    preserve_radio_groups: bool

    def __init__(
        self,
        changes: Iterable[LayerStateChange],
        preserve_radio_groups: bool = True,  # noqa: FBT001, FBT002 - Table 217 /PreserveRB defaults to true.
    ) -> None:
        """Validate ``changes`` and ``preserve_radio_groups``."""
        if not isinstance(preserve_radio_groups, bool):
            msg = f"preserve_radio_groups must be a bool: {preserve_radio_groups!r}"
            raise TypeError(msg)
        if isinstance(changes, (str, bytes, bytearray, memoryview)) or not isinstance(changes, Iterable):
            msg = f"changes must be an iterable of LayerStateChange records: {changes!r}"
            raise TypeError(msg)
        items = list(itertools.islice(changes, _MAX_ACTION_ITEMS + 1))
        if not items or len(items) > _MAX_ACTION_ITEMS:
            msg = f"changes must contain between 1 and {_MAX_ACTION_ITEMS} LayerStateChange records"
            raise ValueError(msg)
        total_refs = 0
        for item in items:
            if not isinstance(item, LayerStateChange):
                msg = f"each change must be a LayerStateChange: {item!r}"
                raise TypeError(msg)
            total_refs += len(item.layers)
            if total_refs > _MAX_ACTION_ITEMS:
                msg = f"SetLayerStateAction cannot reference more than {_MAX_ACTION_ITEMS} layer entries"
                raise ValueError(msg)
        object.__setattr__(self, "changes", tuple(items))
        object.__setattr__(self, "preserve_radio_groups", preserve_radio_groups)


class SubmitFormFlags(enum.IntFlag):
    """Bit flags of a :class:`SubmitFormAction` (``/Flags``, ISO 32000-2 Table 240)."""

    INCLUDE_EXCLUDE = 1 << 0
    """Exclude rather than include the fields named in ``fields`` (bit 1)."""
    INCLUDE_NO_VALUE_FIELDS = 1 << 1
    """Submit fields even when they have no value (bit 2)."""
    EXPORT_FORMAT = 1 << 2
    """Submit in HTML Form format rather than FDF when ``XFDF`` and ``SUBMIT_PDF`` are clear (bit 3)."""
    GET_METHOD = 1 << 3
    """Use HTTP ``GET`` instead of ``POST`` for HTML Form submission (bit 4)."""
    SUBMIT_COORDINATES = 1 << 4
    """Include click coordinates for HTML Form submission (bit 5)."""
    XFDF = 1 << 5
    """Submit form data as XFDF (bit 6, PDF 1.4)."""
    INCLUDE_APPEND_SAVES = 1 << 6
    """Include incremental updates in submitted FDF (bit 7, PDF 1.4)."""
    INCLUDE_ANNOTATIONS = 1 << 7
    """Include markup annotations in submitted FDF (bit 8, PDF 1.4)."""
    SUBMIT_PDF = 1 << 8
    """Submit the entire document as PDF instead of FDF (bit 9, PDF 1.4)."""
    CANONICAL_FORMAT = 1 << 9
    """Convert date and time fields to standard canonical format (bit 10, PDF 1.4)."""
    EXCL_NON_USER_ANNOTS = 1 << 10
    """Exclude annotations not belonging to the current user (bit 11, PDF 1.4)."""
    EXCL_F_KEY = 1 << 11
    """Omit ``/F`` from the submitted FDF (bit 12, PDF 1.4)."""
    EMBED_FORM = 1 << 13
    """Embed the target file in the submitted FDF's ``/EmbeddedFDFs`` (bit 14, PDF 1.5)."""


_NO_SUBMIT_FLAGS = SubmitFormFlags(0)


def _normalize_field_names(fields: object, label: str) -> tuple[str, ...]:
    if isinstance(fields, (str, bytes, bytearray, memoryview)) or not isinstance(fields, Iterable):
        msg = f"{label} must be an iterable of field name strings: {fields!r}"
        raise TypeError(msg)
    items = list(itertools.islice(fields, _MAX_ACTION_ITEMS + 1))
    if len(items) > _MAX_ACTION_ITEMS:
        msg = f"{label} cannot contain more than {_MAX_ACTION_ITEMS} field names"
        raise ValueError(msg)
    checked = tuple(_non_empty_str(item, "field name") for item in items)
    _check_text_budget(checked, label)
    return checked


@dataclass(frozen=True, slots=True, init=False)
class SubmitFormAction:
    """An action that submits form field values to a URL (``/S /SubmitForm``, ISO 32000-2 Table 239)."""

    url: str
    fields: tuple[str, ...]
    flags: SubmitFormFlags
    charset: Literal["utf-8", "utf-16", "Shift-JIS", "BigFive", "GBK", "UHC"] | None

    def __init__(
        self,
        url: str,
        fields: Iterable[str] = (),
        flags: SubmitFormFlags = _NO_SUBMIT_FLAGS,
        charset: Literal["utf-8", "utf-16", "Shift-JIS", "BigFive", "GBK", "UHC"] | None = None,
    ) -> None:
        """Validate ``url``, ``fields``, ``flags``, and ``charset``."""
        checked_url = _non_empty_str(url, "url")
        checked_fields = _normalize_field_names(fields, "SubmitFormAction fields")
        _check_text_budget((checked_url, *checked_fields), "SubmitFormAction")
        if not _is_non_bool_int(flags):
            msg = f"flags must be a SubmitFormFlags value: {flags!r}"
            raise TypeError(msg)
        raw_flags = int(flags)
        if raw_flags < 0 or (raw_flags & ~_VALID_SUBMIT_FLAGS_MASK) != 0:
            msg = f"flags sets reserved undefined SubmitForm bits: {flags!r}"
            raise ValueError(msg)
        checked_charset: SubmitFormCharset | None = None
        if charset is not None:
            if not isinstance(charset, str):
                msg = f"charset must be one of {sorted(_SUBMIT_CHARSETS)} or None: {charset!r}"
                raise TypeError(msg)
            plain_cs = str.__str__(charset)
            if plain_cs not in _SUBMIT_CHARSETS:
                msg = f"charset must be one of {sorted(_SUBMIT_CHARSETS)}: {charset!r}"
                raise ValueError(msg)
            checked_charset = cast("SubmitFormCharset", plain_cs)

        object.__setattr__(self, "url", checked_url)
        object.__setattr__(self, "fields", checked_fields)
        object.__setattr__(self, "flags", SubmitFormFlags(raw_flags))
        object.__setattr__(self, "charset", checked_charset)


@dataclass(frozen=True, slots=True, init=False)
class ResetFormAction:
    """An action that resets form fields to their default values (``/S /ResetForm``, ISO 32000-2 Table 241).

    When ``fields`` is empty, every field in the document is reset; when
    ``exclude`` is ``True`` (``/Flags 1``), every field *except* ``fields`` is
    reset.
    """

    fields: tuple[str, ...]
    exclude: bool

    def __init__(self, fields: Iterable[str] = (), exclude: bool = False) -> None:  # noqa: FBT001, FBT002 - Table 242 /Flags bit 1 defaults to 0.
        """Validate ``fields`` and ``exclude``."""
        if not isinstance(exclude, bool):
            msg = f"exclude must be a bool: {exclude!r}"
            raise TypeError(msg)
        object.__setattr__(self, "fields", _normalize_field_names(fields, "ResetFormAction fields"))
        object.__setattr__(self, "exclude", exclude)


@dataclass(frozen=True, slots=True)
class ImportDataAction:
    """An action that imports FDF field values from a file (``/S /ImportData``, ISO 32000-2 Table 243)."""

    file: str

    def __post_init__(self) -> None:
        """Validate ``file``."""
        checked = _non_empty_str(self.file, "file")
        _check_text_budget((checked,), "ImportDataAction")
        object.__setattr__(self, "file", checked)


def _is_custom_target(target: object) -> bool:
    """Return whether ``target`` serializes via a Python-built action dictionary outside the Rust link/outline core."""
    from pyropdf._presentations import TransitionAction  # noqa: PLC0415
    from pyropdf._threads import ThreadAction  # noqa: PLC0415

    return isinstance(
        target,
        (
            GoTo3DView,
            RenditionAction,
            RichMediaExecute,
            TransitionAction,
            ThreadAction,
            JavaScriptAction,
            EmbeddedDestination,
            HideAction,
            SetLayerStateAction,
            SubmitFormAction,
            ResetFormAction,
            ImportDataAction,
        ),
    )


def _custom_target_budget_text(target: object) -> str | None:  # noqa: PLR0911 - one branch per action.
    """Return representative non-script text of a custom action for aggregate link, outline, or step UTF-8 checks."""
    match target:
        case GoTo3DView(view=str(view)):
            return view
        case RichMediaExecute(command=command, arguments=arg):
            return command + (arg if isinstance(arg, str) else "")
        case RenditionAction(script=str(script)):
            return script
        case EmbeddedDestination(name=name, file=file):
            parts = [p for p in (name, file) if p is not None]
            return "".join(parts) or None
        case HideAction(targets=targets):
            return "".join(item for item in targets if isinstance(item, str)) or None
        case SubmitFormAction(url=url, fields=fields):
            return "".join((url, *fields))
        case ResetFormAction(fields=fields):
            return "".join(fields) or None
        case ImportDataAction(file=file):
            return file
        case _:
            return None


def _serialize_embedded_target(root: EmbeddedTarget) -> dict[str, object]:
    """Build nested ``Target`` / ``TargetEmbedded`` dictionaries iteratively from bottom to top."""
    hops: list[EmbeddedTarget] = []
    current: EmbeddedTarget | None = root
    while current is not None:
        hops.append(current)
        current = current.next_target

    child_dict: dict[str, object] | None = None
    for hop in reversed(hops):
        entry: dict[str, object] = {"R": Name("P" if hop.relationship == "parent" else "C")}
        if hop.embedded_file is not None:
            entry["N"] = hop.embedded_file.encode("utf-8")
        if hop.annotation_page is not None:
            entry["P"] = (
                hop.annotation_page.encode("utf-8") if isinstance(hop.annotation_page, str) else hop.annotation_page
            )
        if hop.annotation is not None:
            entry["A"] = hop.annotation
        if child_dict is not None:
            entry["T"] = child_dict
        child_dict = entry
    assert child_dict is not None  # noqa: S101 - root has at least one hop.
    return child_dict


def _gotoe_action_dict(action: EmbeddedDestination) -> tuple[dict[str, object], tuple[int, int]]:
    entries: dict[str, object] = {"Type": Name("Action"), "S": Name("GoToE")}
    if action.name is not None:
        entries["D"] = action.name.encode("utf-8")
    elif action.point is None and action.zoom is None:
        entries["D"] = [action.page, Name("Fit")]
    else:
        left: float | None = None if action.point is None else action.point.x
        top: float | None = None if action.point is None else action.point.y
        entries["D"] = [action.page, Name("XYZ"), left, top, action.zoom]
    if action.file is not None:
        entries["F"] = action.file
    if action.target is not None:
        entries["T"] = _serialize_embedded_target(action.target)
    if action.new_window is not None:
        entries["NewWindow"] = action.new_window
    return entries, (1, 6)


def _resolve_annot_xref(document: Document, xref: int) -> tuple[Dictionary, tuple[int, int]]:
    core = _unlocked(document)
    found = _core.object_lookup(core, xref)
    if found is None or not isinstance(found[1], tuple) or found[1][0] != "d":
        msg = f"xref {xref} is not an indirect annotation dictionary in this document"
        raise ValueError(msg)
    annot_dict = Dictionary(document, (xref, found[0]))
    subtype = annot_dict.get("Subtype")
    subtype_name = str(subtype) if isinstance(subtype, Name) else ""
    if not subtype_name:
        msg = f"xref {xref} is not an annotation dictionary (missing /Subtype)"
        raise ValueError(msg)
    return annot_dict, _ANNOT_HIDE_MIN_VERSION.get(subtype_name, (1, 2))


def _hide_action_dict(document: Document, action: HideAction) -> tuple[dict[str, object], tuple[int, int]]:
    minimum = (1, 2)
    encoded: list[object] = []
    for item in action.targets:
        if isinstance(item, int):
            annot_dict, req = _resolve_annot_xref(document, item)
            minimum = max(minimum, req)
            encoded.append(annot_dict)
        else:
            encoded.append(item)
    t_value: object = encoded[0] if len(encoded) == 1 else encoded
    return (
        {
            "Type": Name("Action"),
            "S": Name("Hide"),
            "T": t_value,
            "H": action.hide,
        },
        minimum,
    )


def _resolve_ocg_xref(document: Document, xref: int, known_layers: set[int]) -> Dictionary:
    core = _unlocked(document)
    found = _core.object_lookup(core, xref)
    if found is None or not isinstance(found[1], tuple) or found[1][0] != "d":
        msg = f"xref {xref} is not an optional content group in this document"
        raise ValueError(msg)
    ocg_dict = Dictionary(document, (xref, found[0]))
    if xref not in known_layers and ocg_dict.get("Type") != "OCG":
        msg = f"xref {xref} is not an optional content group (/Type /OCG) in this document"
        raise ValueError(msg)
    return ocg_dict


def _set_ocg_state_action_dict(
    document: Document,
    action: SetLayerStateAction,
) -> tuple[dict[str, object], tuple[int, int]]:
    known_layers = {layer.xref for layer in document.layers}
    flat_state: list[object] = []
    for change in action.changes:
        flat_state.append(Name(change.mode.value))
        flat_state.extend(_resolve_ocg_xref(document, xref, known_layers) for xref in change.layers)
    return (
        {
            "Type": Name("Action"),
            "S": Name("SetOCGState"),
            "State": flat_state,
            "PreserveRB": action.preserve_radio_groups,
        },
        (1, 5),
    )


def _submit_form_action_dict(action: SubmitFormAction) -> tuple[dict[str, object], tuple[int, int]]:
    entries: dict[str, object] = {
        "Type": Name("Action"),
        "S": Name("SubmitForm"),
        "F": {"Type": Name("Filespec"), "FS": Name("URL"), "F": action.url},
    }
    if action.fields:
        entries["Fields"] = list(action.fields)
    raw_flags = int(action.flags)
    if raw_flags:
        entries["Flags"] = raw_flags
    if action.charset is not None:
        entries["CharSet"] = action.charset.encode("ascii")
    minimum = (1, 3)
    if raw_flags & int(SubmitFormFlags.EMBED_FORM):
        minimum = (1, 5)
    elif raw_flags & 0x0FE0:
        minimum = (1, 4)
    return entries, minimum


def _reset_form_action_dict(action: ResetFormAction) -> tuple[dict[str, object], tuple[int, int]]:
    entries: dict[str, object] = {"Type": Name("Action"), "S": Name("ResetForm")}
    if action.fields:
        entries["Fields"] = list(action.fields)
    if action.exclude:
        entries["Flags"] = 1
    return entries, (1, 2)


def _import_data_action_dict(action: ImportDataAction) -> tuple[dict[str, object], tuple[int, int]]:
    return (
        {
            "Type": Name("Action"),
            "S": Name("ImportData"),
            "F": {"Type": Name("Filespec"), "F": action.file},
        },
        (1, 3),
    )


def _custom_target_action_dict(  # noqa: C901, PLR0911 - dispatches one branch per custom action class.
    document: Document,
    target: object,
) -> tuple[dict[str, object], tuple[int, int]]:
    """Serialize a custom :data:`Target` into ``(action_dict, minimum_pdf_version)``."""
    from pyropdf._presentations import TransitionAction, _transition_action_dict  # noqa: PLC0415
    from pyropdf._threads import ThreadAction, _thread_action_dict  # noqa: PLC0415

    match target:
        case GoTo3DView() | RenditionAction() | RichMediaExecute():
            return media_action_dict(document, target)
        case TransitionAction():
            return _transition_action_dict(target)
        case ThreadAction():
            return _thread_action_dict(document, target)
        case JavaScriptAction(script=script):
            return {"Type": Name("Action"), "S": Name("JavaScript"), "JS": script}, (1, 3)
        case EmbeddedDestination():
            return _gotoe_action_dict(target)
        case HideAction():
            return _hide_action_dict(document, target)
        case SetLayerStateAction():
            return _set_ocg_state_action_dict(document, target)
        case SubmitFormAction():
            return _submit_form_action_dict(target)
        case ResetFormAction():
            return _reset_form_action_dict(target)
        case ImportDataAction():
            return _import_data_action_dict(target)
        case _:
            msg = f"unsupported custom target: {target!r}"
            raise TypeError(msg)


def _target_action_dict(
    document: Document,
    target: object,
) -> tuple[dict[str, object], tuple[int, int]]:
    """Serialize any :data:`Target` into ``(action_dict, minimum_pdf_version)``."""
    from pyropdf._presentations import _goto_action_dict  # noqa: PLC0415
    from pyropdf._targets import Destination, Launch, NamedAction, RemoteDestination, _text  # noqa: PLC0415

    if _is_custom_target(target):
        return _custom_target_action_dict(document, target)
    match target:
        case Destination(page=page_idx, point=point, zoom=zoom):
            return _goto_action_dict(document, page_idx, point, zoom), (1, 1)
        case str():
            uri = _text(target, "URI")
            _check_text_budget((uri,), "URI")
            return {"Type": Name("Action"), "S": Name("URI"), "URI": uri.encode("utf-8")}, (1, 1)
        case RemoteDestination(file=file, page=page_idx, name=name):
            _check_text_budget((file, *(p for p in (name,) if p is not None)), "RemoteDestination")
            remote_dest: object = name.encode("utf-8") if name else [page_idx, Name("Fit")]
            return {"Type": Name("Action"), "S": Name("GoToR"), "F": file, "D": remote_dest}, (1, 1)
        case Launch(file=file):
            _check_text_budget((file,), "Launch")
            return {"Type": Name("Action"), "S": Name("Launch"), "F": file}, (1, 1)
        case NamedAction(name=name):
            _check_text_budget((name,), "NamedAction")
            return {"Type": Name("Action"), "S": Name("Named"), "N": Name(name)}, (1, 2)
        case _:
            msg = (
                "a target is a Destination, RemoteDestination, EmbeddedDestination, Launch, "
                "NamedAction, TransitionAction, ThreadAction, JavaScriptAction, HideAction, "
                "SetLayerStateAction, SubmitFormAction, ResetFormAction, ImportDataAction, "
                f"or URI string: {type(target).__name__}"
            )
            raise TypeError(msg)


def _parse_embedded_hop_fields(
    hop_dict: Dictionary,
) -> tuple[Literal["parent", "child"], str | None, int | str | None, int | str | None] | None:
    raw_r = hop_dict.get("R")
    if not isinstance(raw_r, Name) or str(raw_r) not in {"P", "C"}:
        return None
    if str(raw_r) == "P":
        return ("parent", None, None, None)

    raw_n = hop_dict.get("N")
    ef = _decode_byte_or_text_string(raw_n) if isinstance(raw_n, String) and len(raw_n) > 0 else None

    raw_p = hop_dict.get("P")
    ap: int | str | None = None
    if _is_non_bool_int(raw_p) and int(cast("int", raw_p)) >= 0:
        ap = int(cast("int", raw_p))
    elif isinstance(raw_p, String) and len(raw_p) > 0:
        ap = _decode_byte_or_text_string(raw_p)

    raw_a = hop_dict.get("A")
    annot: int | str | None = None
    if _is_non_bool_int(raw_a) and int(cast("int", raw_a)) >= 0:
        annot = int(cast("int", raw_a))
    elif isinstance(raw_a, String) and raw_a.text:
        annot = raw_a.text

    if ap is not None and annot is not None:
        return ("child", None, ap, annot)
    if ef is not None:
        return ("child", ef, None, None)
    return None


def _read_embedded_target(raw_target: object, operation: _core._OperationBudget) -> EmbeddedTarget | None:
    """Read a ``Target`` / ``TargetEmbedded`` chain iteratively up to 32 hops."""
    if not _is_dict(raw_target):
        return None
    parsed_hops: list[tuple[Literal["parent", "child"], str | None, int | str | None, int | str | None]] = []
    seen: set[tuple[int, int]] = set()
    current: object = raw_target
    while current is not None:
        if not _is_dict(current):
            return None
        hop_dict = current
        if (pair := _dict_id(hop_dict)) is not None:
            if pair in seen:
                return None
            seen.add(pair)
        if len(parsed_hops) >= _MAX_EMBEDDED_TARGET_DEPTH:
            code: LimitCode = "object_depth"
            msg = f"embedded target /T exceeds the {_MAX_EMBEDDED_TARGET_DEPTH}-hop safety limit"
            raise LimitError(code, msg)
        fields = _parse_embedded_hop_fields(hop_dict)
        if fields is None:
            return None
        # Keep the parsed values, the visited object ID and the resulting
        # handle until this chain is complete.
        operation.charge("memory_size", 256 + sum(sys.getsizeof(value) for value in fields))
        parsed_hops.append(fields)
        current = hop_dict.get("T")

    built: EmbeddedTarget | None = None
    for rel, ef, ap, annot in reversed(parsed_hops):
        built = EmbeddedTarget(
            relationship=rel,
            embedded_file=ef,
            annotation_page=ap,
            annotation=annot,
            next_target=built,
        )
    return built


def _opt_finite(items: Array, idx: int) -> float | None:
    if idx >= len(items):
        return None
    raw = items[idx]
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) and abs(raw) <= _FLOAT32_MAX:
        return float(raw)
    return None


def _parse_embedded_dest_value(raw_d: object) -> tuple[int, Point | None, float | None, str | None] | None:
    if isinstance(raw_d, Name) and str(raw_d):
        return (0, None, None, str(raw_d))
    if isinstance(raw_d, String) and len(raw_d) > 0:
        return (0, None, None, _decode_byte_or_text_string(raw_d))
    if isinstance(raw_d, Array) and len(raw_d) >= 1:
        raw_page = raw_d[0]
        page_num = int(raw_page) if _is_non_bool_int(raw_page) and int(raw_page) >= 0 else 0
        fit = str(raw_d[1]) if len(raw_d) >= _PAIR_LEN and isinstance(raw_d[1], Name) else ""
        if fit == "XYZ":
            left = _opt_finite(raw_d, 2)
            top = _opt_finite(raw_d, 3)
            raw_zoom = _opt_finite(raw_d, 4)
            pt = Point(left or 0.0, top or 0.0) if left is not None or top is not None else None
            zm = raw_zoom if raw_zoom is not None and raw_zoom >= 0 else None
            return (page_num, pt, zm, None)
        return (page_num, None, None, None)
    return None


def _read_gotoe_action(action: Dictionary) -> EmbeddedDestination | None:
    from pyropdf._presentations import _read_file_spec  # noqa: PLC0415

    dest_parts = _parse_embedded_dest_value(action.get("D"))
    if dest_parts is None:
        return None
    page_num, pt, zm, dest_name = dest_parts
    file = _read_file_spec(action.get("F")) if "F" in action else None
    target = (
        _core.read_operation(action._core(), lambda operation: _read_embedded_target(action.get("T"), operation))  # noqa: SLF001
        if "T" in action
        else None
    )
    if file is None and target is None:
        return None
    raw_nw = action.get("NewWindow")
    nw = raw_nw if isinstance(raw_nw, bool) else None
    return EmbeddedDestination(
        page=page_num,
        point=pt,
        zoom=zm,
        name=dest_name,
        target=target,
        file=file,
        new_window=nw,
    )


def _read_hide_action(action: Dictionary) -> HideAction | None:
    raw_t = action.get("T")
    if isinstance(raw_t, Array):
        if len(raw_t) > _MAX_ACTION_ITEMS:
            code: LimitCode = "object_count"
            msg = f"HideAction /T exceeds the {_MAX_ACTION_ITEMS}-entry safety limit"
            raise LimitError(code, msg)
        candidates = list(raw_t)
    elif raw_t is not None:
        candidates = [raw_t]
    else:
        return None

    parsed: list[str | int] = []
    for entry in candidates:
        if isinstance(entry, String) and entry.text:
            parsed.append(entry.text)
        elif (xref := _indirect_xref(entry)) is not None and _is_dict(entry):
            parsed.append(xref)
    if not parsed:
        return None
    raw_h = action.get("H")
    hide = raw_h if isinstance(raw_h, bool) else True
    return HideAction(parsed, hide=hide)


def _read_set_ocg_state_action(action: Dictionary) -> SetLayerStateAction | None:
    raw_state = action.get("State")
    if not isinstance(raw_state, Array):
        return None
    if len(raw_state) > _MAX_ACTION_ITEMS * 2:
        code: LimitCode = "optional_content_count"
        msg = f"SetOCGState /State exceeds the {_MAX_ACTION_ITEMS * 2}-element safety limit"
        raise LimitError(code, msg)

    changes: list[LayerStateChange] = []
    current_mode: LayerStateTransition | None = None
    current_layers: list[int] = []
    for elem in raw_state:
        if isinstance(elem, Name):
            if current_mode is not None and current_layers:
                changes.append(LayerStateChange(current_mode, current_layers))
            current_layers = []
            try:
                current_mode = LayerStateTransition(str(elem))
            except ValueError:
                current_mode = None
        elif current_mode is not None and (xref := _indirect_xref(elem)) is not None and _is_dict(elem):
            current_layers.append(xref)
    if current_mode is not None and current_layers:
        changes.append(LayerStateChange(current_mode, current_layers))
    if not changes:
        return None
    raw_prb = action.get("PreserveRB")
    preserve = raw_prb if isinstance(raw_prb, bool) else True
    return SetLayerStateAction(changes, preserve_radio_groups=preserve)


def _qualified_field_name(field_dict: Dictionary) -> str | None:
    """Resolve a form field dictionary's qualified name iteratively up its ``/Parent`` chain."""
    parts: list[str] = []
    seen: set[tuple[int, int]] = set()
    current: object = field_dict
    steps = 0
    while _is_dict(current) and steps < _MAX_FIELD_DEPTH:
        steps += 1
        fd = current
        if (pair := _dict_id(fd)) is not None:
            if pair in seen:
                break
            seen.add(pair)
        raw_t = fd.get("T")
        if isinstance(raw_t, String) and raw_t.text:
            parts.append(raw_t.text)
        current = fd.get("Parent")
    parts.reverse()
    return ".".join(parts) if parts else None


def _read_action_fields_array(raw_fields: object) -> tuple[str, ...]:
    if not isinstance(raw_fields, Array):
        return ()
    if len(raw_fields) > _MAX_ACTION_ITEMS:
        code: LimitCode = "object_count"
        msg = f"form action /Fields exceeds the {_MAX_ACTION_ITEMS}-entry safety limit"
        raise LimitError(code, msg)
    names: list[str] = []
    for item in raw_fields:
        if isinstance(item, String) and item.text:
            names.append(item.text)
        elif _is_dict(item) and (qname := _qualified_field_name(item)) is not None:
            names.append(qname)
    return tuple(names)


def _read_submit_form_action(action: Dictionary) -> SubmitFormAction | None:
    from pyropdf._presentations import _read_file_spec  # noqa: PLC0415

    url = _read_file_spec(action.get("F"))
    if not url:
        return None
    raw_flags = action.get("Flags", 0)
    if not _is_non_bool_int(raw_flags):
        return None
    masked_flags = SubmitFormFlags(int(cast("int", raw_flags)) & _VALID_SUBMIT_FLAGS_MASK)
    raw_cs = action.get("CharSet")
    charset: SubmitFormCharset | None = None
    if isinstance(raw_cs, String):
        decoded = _decode_byte_or_text_string(raw_cs)
        if decoded in _SUBMIT_CHARSETS:
            charset = cast("SubmitFormCharset", decoded)
    return SubmitFormAction(
        url,
        fields=_read_action_fields_array(action.get("Fields")),
        flags=masked_flags,
        charset=charset,
    )


def _read_reset_form_action(action: Dictionary) -> ResetFormAction | None:
    raw_flags = action.get("Flags", 0)
    if not _is_non_bool_int(raw_flags):
        return None
    return ResetFormAction(
        fields=_read_action_fields_array(action.get("Fields")),
        exclude=bool(int(cast("int", raw_flags)) & 1),
    )


def _read_import_data_action(action: Dictionary) -> ImportDataAction | None:
    from pyropdf._presentations import _read_file_spec  # noqa: PLC0415

    file = _read_file_spec(action.get("F"))
    return ImportDataAction(file) if file else None


def _read_js_action_dict(action: Dictionary) -> JavaScriptAction | None:
    source = _javascript_source(action)
    return None if source is None else JavaScriptAction(source)


_STATELESS_ACTION_READERS: dict[str, Callable[[Dictionary], Target | None]] = {
    "JavaScript": _read_js_action_dict,
    "GoToE": _read_gotoe_action,
    "GoTo3DView": read_media_action,
    "RichMediaExecute": read_media_action,
    "Rendition": read_media_action,
    "Hide": _read_hide_action,
    "SetOCGState": _read_set_ocg_state_action,
    "SubmitForm": _read_submit_form_action,
    "ResetForm": _read_reset_form_action,
    "ImportData": _read_import_data_action,
}


def _read_custom_action_dict(
    document: Document,
    action: Dictionary,
    *,
    thread_index_map: Mapping[tuple[int, int], int] | None = None,
) -> Target | None:
    """Read any custom action dictionary into its high-level :data:`Target`, or ``None``."""
    from pyropdf._presentations import TransitionAction, _read_transition  # noqa: PLC0415
    from pyropdf._threads import _read_thread_action_dict  # noqa: PLC0415

    kind = action.get("S")
    if not isinstance(kind, Name):
        return None
    kind_name = str(kind)
    if kind_name == "Trans":
        trans = _read_transition(action.get("Trans"))
        return None if trans is None else TransitionAction(trans)
    if kind_name == "Thread":
        return _read_thread_action_dict(document, action, thread_index_map=thread_index_map)
    reader = _STATELESS_ACTION_READERS.get(kind_name)
    return None if reader is None else reader(action)


def _read_custom_holder_action(document: Document, holder: Dictionary) -> Target | None:
    """Return a custom :data:`Target` when ``holder['A']`` holds one, else ``None``."""
    raw = holder.get("A")
    if not _is_dict(raw):
        return None
    return _read_custom_action_dict(document, raw)


def _collect_action_xrefs(action_obj: object) -> list[int]:
    """Collect indirect object numbers owned directly by an action dictionary (excluding shared pages/annots/OCGs)."""
    xrefs: list[int] = []
    if (act_xref := _indirect_xref(action_obj)) is not None:
        xrefs.append(act_xref)
    if _is_dict(action_obj):
        act = action_obj
        for sub_key in ("JS", "Trans", "F", "T"):
            sub = act.get(sub_key)
            if sub_key == "T" and act.get("S") == "Hide":
                continue
            if (sub_xref := _indirect_xref(sub)) is not None:
                xrefs.append(sub_xref)
    return xrefs


def _read_open_action(document: Document) -> Target | None:
    from pyropdf._presentations import _ActionReadContext  # noqa: PLC0415

    def read(_operation: _core._OperationBudget) -> Target | None:
        raw = _catalog(document).get("OpenAction")
        if raw is None:
            return None
        ctx = _ActionReadContext(document)
        if isinstance(raw, Array):
            return ctx.explicit_destination(raw)
        return ctx.read_action(raw)

    return _core.read_operation(_unlocked(document), read)


def _destination_array_for_open_action(document: Document, dest: Destination) -> list[object]:
    from pyropdf._presentations import _pdf_real, _to_pdf  # noqa: PLC0415

    pages = document.pages
    if dest.page >= len(pages):
        msg = f"destination page {dest.page} is out of range (0..{len(pages) - 1})"
        raise ValueError(msg)
    target_page = pages[dest.page]
    if dest.point is None and dest.zoom is None:
        return [target_page.obj, Name("Fit")]
    if dest.point is not None:
        space = _core.page_space(document._core(), target_page.xref, target_page._generation)  # noqa: SLF001
        if space is None:
            raise target_page._stale()  # noqa: SLF001
        rotation, crop, unit = space
        px, py = _to_pdf(crop, rotation, unit, dest.point.x, dest.point.y)
        left: float | None = _pdf_real(px, "left")
        top: float | None = _pdf_real(py, "top")
    else:
        left, top = None, None
    checked_zoom = None if dest.zoom is None else _pdf_real(dest.zoom, "zoom")
    return [target_page.obj, Name("XYZ"), left, top, checked_zoom]


def _write_open_action(document: Document, target: Target | None) -> None:
    from pyropdf._targets import Destination  # noqa: PLC0415

    if target is None:
        _delete_open_action(document)
        return
    if isinstance(target, Destination):
        value: object = _destination_array_for_open_action(document, target)
        minimum = (1, 1)
    else:
        value, minimum = _target_action_dict(document, target)

    with document.transaction("open action"):
        catalog = _catalog(document)
        if isinstance(target, Destination):
            value = _destination_array_for_open_action(document, target)
        else:
            value, minimum = _target_action_dict(document, target)
        retired = _collect_action_xrefs(catalog.get("OpenAction"))
        _require_version(document, catalog, max((1, 1), minimum))
        catalog["OpenAction"] = value
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


def _delete_open_action(document: Document) -> None:
    catalog = _catalog(document)
    if "OpenAction" not in catalog:
        return
    with document.transaction("open action"):
        catalog = _catalog(document)
        if "OpenAction" not in catalog:
            return
        retired = _collect_action_xrefs(catalog.get("OpenAction"))
        del catalog["OpenAction"]
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


class _ScriptTreeEntry(NamedTuple):
    raw_key: bytes
    name: str
    script: str


def _js_tree_root(document: Document) -> Dictionary | None:
    names = _catalog(document).get("Names")
    if not _is_dict(names):
        return None
    root = names.get("JavaScript")
    return root if _is_dict(root) else None


def _has_js_tree(document: Document) -> bool:
    names = _catalog(document).get("Names")
    return _is_dict(names) and "JavaScript" in names


def _scan_js_leaf_pairs(
    raw_names: object,
    unique: dict[bytes, _ScriptTreeEntry],
    counts: list[int],
) -> None:
    """Scan ``[key, action, ...]`` pairs; ``counts`` holds ``[scanned_entries, total_script_bytes]``."""
    if not isinstance(raw_names, Array):
        return
    items = list(raw_names)
    for key_obj, val_obj in zip(items[::2], items[1::2], strict=False):
        if not isinstance(key_obj, String):
            continue
        counts[0] += 1
        if counts[0] > _MAX_TREE_ENTRIES:
            code: LimitCode = "object_count"
            msg = f"the JavaScript name tree exceeds the {_MAX_TREE_ENTRIES}-entry safety limit"
            raise LimitError(code, msg)
        raw_key = bytes(key_obj)
        if raw_key in unique:
            continue
        source = _javascript_source(val_obj)
        if source is not None:
            counts[1] += _script_size(source)
            if counts[1] > _MAX_SCRIPT_SIZE:
                _refuse_script("document scripts")
            unique[raw_key] = _ScriptTreeEntry(raw_key, key_obj.text, source)


def _walk_document_scripts(document: Document) -> list[_ScriptTreeEntry]:
    """Walk ``Catalog['Names']['JavaScript']`` iteratively up to 32 levels, 4,096 entries, and 1 MiB of UTF-8."""
    root = _js_tree_root(document)
    if root is None:
        return []
    seen: set[tuple[int, int]] = set()
    stack: list[tuple[object, int]] = [(root, 1)]
    unique: dict[bytes, _ScriptTreeEntry] = {}
    counts = [0, 0]
    while stack:
        node, depth = stack.pop()
        if not _is_dict(node):
            continue
        nd = node
        if (pair := _dict_id(nd)) is not None:
            if pair in seen:
                continue
            seen.add(pair)
        if depth > _MAX_TREE_DEPTH:
            code: LimitCode = "object_depth"
            msg = f"the JavaScript name tree exceeds the {_MAX_TREE_DEPTH}-level safety limit"
            raise LimitError(code, msg)
        _scan_js_leaf_pairs(nd.get("Names"), unique, counts)
        raw_kids = nd.get("Kids")
        if isinstance(raw_kids, Array):
            stack.extend((kid, depth + 1) for kid in reversed(list(raw_kids)))
    return [unique[k] for k in sorted(unique)]


def _collect_js_node_children(nd: Dictionary, xrefs: list[int], stack: list[object]) -> None:
    raw_kids = nd.get("Kids")
    if isinstance(raw_kids, Array):
        if (kids_xref := _indirect_xref(raw_kids)) is not None:
            xrefs.append(kids_xref)
        stack.extend(list(raw_kids))
    raw_names = nd.get("Names")
    if (names_xref := _indirect_xref(raw_names)) is not None:
        xrefs.append(names_xref)
    if isinstance(raw_names, Array):
        for val_obj in list(raw_names)[1::2]:
            xrefs.extend(_collect_action_xrefs(val_obj))


def _collect_js_tree_xrefs(root: object) -> list[int]:
    """Collect indirect object numbers of a JavaScript name tree and its action/stream leaves iteratively."""
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
        nd = node
        if (pair := _dict_id(nd)) is not None:
            if pair in seen:
                continue
            seen.add(pair)
            xrefs.append(pair[0])
        steps += 1
        _collect_js_node_children(nd, xrefs, stack)
    return xrefs


def _build_canonical_js_tree(document: Document, entries: list[_ScriptTreeEntry]) -> Dictionary:
    """Write sorted ``entries`` as canonical 64-pair ``/Names`` / ``/Limits`` / ``/Kids`` nodes."""
    action_objs: list[Dictionary] = []
    for entry in entries:
        act_xref = document.objects.add({"Type": Name("Action"), "S": Name("JavaScript"), "JS": entry.script})
        action_objs.append(document.objects[act_xref])

    if len(entries) <= _MAX_LEAF_PAIRS:
        flat: list[object] = []
        for entry, act_obj in zip(entries, action_objs, strict=True):
            flat.extend((entry.raw_key, act_obj))
        root_xref = document.objects.add({"Names": flat})
        return document.objects[root_xref]

    level_nodes: list[tuple[Dictionary, bytes, bytes]] = []
    for start in range(0, len(entries), _MAX_LEAF_PAIRS):
        chunk_entries = entries[start : start + _MAX_LEAF_PAIRS]
        chunk_actions = action_objs[start : start + _MAX_LEAF_PAIRS]
        low, high = chunk_entries[0].raw_key, chunk_entries[-1].raw_key
        flat_leaf: list[object] = []
        for entry, act_obj in zip(chunk_entries, chunk_actions, strict=True):
            flat_leaf.extend((entry.raw_key, act_obj))
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


def _check_js_entries_limits(entries: list[_ScriptTreeEntry]) -> None:
    total_script = 0
    for entry in entries:
        total_script += _script_size(entry.script)
        if total_script > _MAX_SCRIPT_SIZE:
            _refuse_script("document scripts")
    if len(entries) > _MAX_TREE_ENTRIES:
        code: LimitCode = "object_count"
        msg = f"the JavaScript name tree exceeds the {_MAX_TREE_ENTRIES}-entry safety limit"
        raise LimitError(code, msg)


def _write_js_tree_entries(document: Document, entries: list[_ScriptTreeEntry]) -> None:
    _check_js_entries_limits(entries)
    catalog = _catalog(document)
    raw_names = catalog.get("Names")
    names_dict = raw_names if _is_dict(raw_names) else None
    retired = _collect_js_tree_xrefs(names_dict.get("JavaScript") if names_dict is not None else None)

    if not entries:
        if names_dict is not None and "JavaScript" in names_dict:
            del names_dict["JavaScript"]
            if len(names_dict) == 0:
                if (names_xref := _indirect_xref(names_dict)) is not None:
                    retired.append(names_xref)
                del catalog["Names"]
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001
        return

    entries_sorted = sorted(entries, key=lambda item: item.raw_key)
    _require_version(document, catalog, (1, 3))
    new_root = _build_canonical_js_tree(document, entries_sorted)
    if names_dict is None:
        names_xref = document.objects.add({"JavaScript": new_root})
        catalog["Names"] = document.objects[names_xref]
    else:
        names_dict["JavaScript"] = new_root
    if retired:
        _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


def _validate_script_key(name: object, *, allow_empty: bool = False) -> str:
    if not isinstance(name, str):
        msg = f"script name must be a str: {type(name).__name__}"
        raise TypeError(msg)
    plain = str.__str__(name)
    if not allow_empty and not plain:
        msg = "script name must not be empty"
        raise ValueError(msg)
    _check_text_budget((plain,), "document script name")
    return plain


def _extract_mapping_pairs(other: object, kwargs: Mapping[str, object]) -> list[tuple[object, object]]:
    pairs: list[tuple[object, object]] = []
    if isinstance(other, Mapping):
        pairs.extend(other.items())
    elif isinstance(other, (str, bytes, bytearray, memoryview)) or not isinstance(other, Iterable):
        msg = f"expected a mapping or iterable of pairs: {type(other).__name__}"
        raise TypeError(msg)
    else:
        for item in other:
            if isinstance(item, (str, bytes)) or not isinstance(item, tuple) or len(item) != _PAIR_LEN:
                msg = f"iterable item must be a (key, value) pair: {item!r}"
                raise TypeError(msg)
            pairs.append(item)
    pairs.extend(kwargs.items())
    return pairs


def _prepare_script_updates(pairs: Iterable[tuple[object, object]]) -> dict[str, _ScriptTreeEntry]:
    prepared: dict[str, _ScriptTreeEntry] = {}
    key_texts: list[str] = []
    total_script = 0
    for raw_name, raw_script in pairs:
        name = _validate_script_key(raw_name)
        script = _validate_script(raw_script, "script")
        key_texts.append(name)
        prepared[name] = _ScriptTreeEntry(_encode_text_string(name), name, script)
    _check_text_budget(key_texts, "document script names")
    for entry in prepared.values():
        total_script += _script_size(entry.script)
        if total_script > _MAX_SCRIPT_SIZE:
            _refuse_script("document scripts")
    return prepared


class DocumentScripts(MutableMapping[str, str]):
    """The document's ``/Names /JavaScript`` name tree as a live mutable mapping (ISO 32000-2 §12.6.4.17).

    Keys are script names sorted in PDF name-tree byte order, and values are
    ECMAScript source strings. pyropdf never runs the scripts. Reads and writes
    are bounded to 32 tree levels, 4,096 entries, and 1 MiB of UTF-8 across the
    mapping (``LimitError.code == "script_size"``).
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap ``document``; use ``doc.scripts``."""
        self._document = document

    def _entries_map(self) -> dict[str, str]:
        self._document._ensure_open()  # noqa: SLF001
        return {entry.name: entry.script for entry in _walk_document_scripts(self._document)}

    def __getitem__(self, name: str) -> str:
        """Return the script named ``name``, or raise :class:`KeyError`."""
        if not isinstance(name, str):
            raise KeyError(name)
        checked = _validate_script_key(name, allow_empty=True)
        self._document._ensure_open()  # noqa: SLF001
        for entry in _walk_document_scripts(self._document):
            if entry.name == checked:
                return entry.script
        raise KeyError(name)

    def __iter__(self) -> Iterator[str]:
        """Iterate document script names in name-tree order."""
        self._document._ensure_open()  # noqa: SLF001
        return iter([entry.name for entry in _walk_document_scripts(self._document)])

    def __len__(self) -> int:
        """Return the number of document scripts."""
        self._document._ensure_open()  # noqa: SLF001
        return len(_walk_document_scripts(self._document))

    def __contains__(self, name: object) -> bool:
        """Return whether ``name`` is in ``/Names /JavaScript``."""
        if not isinstance(name, str):
            return False
        checked = _validate_script_key(name, allow_empty=True)
        self._document._ensure_open()  # noqa: SLF001
        return any(entry.name == checked for entry in _walk_document_scripts(self._document))

    def __setitem__(self, name: str, script: str) -> None:
        """Set the document script named ``name`` in one transaction."""
        self.update([(name, script)])

    def __delitem__(self, name: str) -> None:
        """Remove ``name`` from ``/Names /JavaScript`` in one transaction, or raise :class:`KeyError`."""
        if not isinstance(name, str):
            raise KeyError(name)
        checked = _validate_script_key(name, allow_empty=True)
        self._document._ensure_open()  # noqa: SLF001
        with self._document.transaction("document scripts"):
            current = _walk_document_scripts(self._document)
            kept = [entry for entry in current if entry.name != checked]
            if len(kept) == len(current):
                raise KeyError(name)
            _write_js_tree_entries(self._document, kept)

    def update(self, other: object = (), /, **values: str) -> None:
        """Insert or replace every named script of ``other`` and ``values`` in one transaction."""
        self._document._ensure_open()  # noqa: SLF001
        prepared = _prepare_script_updates(_extract_mapping_pairs(other, values))
        if not prepared:
            return
        with self._document.transaction("document scripts"):
            by_name = {entry.name: entry for entry in _walk_document_scripts(self._document)}
            by_name.update(prepared)
            _write_js_tree_entries(self._document, list(by_name.values()))

    def clear(self) -> None:
        """Remove all document scripts and free retired name-tree nodes in one transaction."""
        _delete_document_scripts(self._document)

    def __repr__(self) -> str:
        """Return ``DocumentScripts({...})``."""
        return f"DocumentScripts({self._entries_map()!r})"


def _replace_document_scripts(
    document: Document,
    scripts: Mapping[str, str] | Iterable[tuple[str, str]],
) -> None:
    prepared = _prepare_script_updates(_extract_mapping_pairs(scripts, {}))
    if not prepared and not _has_js_tree(document):
        return
    with document.transaction("document scripts"):
        _write_js_tree_entries(document, list(prepared.values()))


def _delete_document_scripts(document: Document) -> None:
    document._ensure_open()  # noqa: SLF001
    if not _has_js_tree(document):
        return
    with document.transaction("document scripts"):
        if not _has_js_tree(document):
            return
        _write_js_tree_entries(document, [])


class DocumentTrigger(enum.StrEnum):
    """Catalog additional-action events (``Catalog["AA"]``, ISO 32000-2 Table 200)."""

    CLOSE = "close"
    """Before closing the document (``/DC``)."""
    WILL_SAVE = "will_save"
    """Before saving the document (``/WS``)."""
    DID_SAVE = "did_save"
    """After saving the document (``/DS``)."""
    WILL_PRINT = "will_print"
    """Before printing the document (``/WP``)."""
    DID_PRINT = "did_print"
    """After printing the document (``/DP``)."""


_DOCUMENT_TRIGGER_KEYS: dict[DocumentTrigger, str] = {
    DocumentTrigger.CLOSE: "DC",
    DocumentTrigger.WILL_SAVE: "WS",
    DocumentTrigger.DID_SAVE: "DS",
    DocumentTrigger.WILL_PRINT: "WP",
    DocumentTrigger.DID_PRINT: "DP",
}


def _read_document_triggers(document: Document) -> dict[DocumentTrigger, str]:
    aa = _catalog(document).get("AA")
    if not _is_dict(aa):
        return {}
    aa_dict = aa
    items: dict[DocumentTrigger, str] = {}
    total = 0
    for trigger, pdf_key in _DOCUMENT_TRIGGER_KEYS.items():
        source = _javascript_source(aa_dict.get(pdf_key))
        if source is not None:
            total += _script_size(source)
            if total > _MAX_SCRIPT_SIZE:
                _refuse_script("document trigger scripts")
            items[trigger] = source
    return items


def _collect_aa_xrefs(aa_obj: object, keys: Iterable[str]) -> list[int]:
    xrefs: list[int] = []
    if (aa_xref := _indirect_xref(aa_obj)) is not None:
        xrefs.append(aa_xref)
    if _is_dict(aa_obj):
        aa_dict = aa_obj
        for key in keys:
            xrefs.extend(_collect_action_xrefs(aa_dict.get(key)))
    return xrefs


def _write_document_aa(document: Document, mapping: Mapping[DocumentTrigger, str]) -> None:
    total = 0
    for source in mapping.values():
        total += _script_size(source)
        if total > _MAX_SCRIPT_SIZE:
            _refuse_script("document trigger scripts")
    catalog = _catalog(document)
    current_aa = catalog.get("AA")
    retired = _collect_aa_xrefs(current_aa, _DOCUMENT_TRIGGER_KEYS.values())
    preserved = (
        {k: current_aa[k] for k in current_aa if k not in _DOCUMENT_TRIGGER_KEYS.values()}
        if _is_dict(current_aa)
        else {}
    )
    for trigger, pdf_key in _DOCUMENT_TRIGGER_KEYS.items():
        if trigger in mapping:
            preserved[pdf_key] = {"Type": Name("Action"), "S": Name("JavaScript"), "JS": mapping[trigger]}
    if preserved:
        _require_version(document, catalog, (1, 4))
        catalog["AA"] = preserved
    elif "AA" in catalog:
        del catalog["AA"]
    if retired:
        _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


class DocumentTriggers(MutableMapping[DocumentTrigger, str]):
    """Catalog additional-action scripts by :class:`DocumentTrigger` (``Catalog["AA"]``, Table 200).

    Values are ECMAScript source strings written as ``ActionECMAScript``
    dictionaries (PDF 1.4), capped individually and together at 1 MiB of UTF-8
    (``LimitError.code == "script_size"``).
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap ``document``; use ``doc.triggers``."""
        self._document = document

    def _trigger(self, trigger: object) -> DocumentTrigger:
        if not isinstance(trigger, str):
            msg = f"trigger must be a DocumentTrigger or str: {trigger!r}"
            raise TypeError(msg)
        return DocumentTrigger(trigger)

    def __getitem__(self, trigger: DocumentTrigger) -> str:
        """Return the script for ``trigger``, or raise :class:`KeyError`."""
        self._document._ensure_open()  # noqa: SLF001
        return _read_document_triggers(self._document)[self._trigger(trigger)]

    def __setitem__(self, trigger: DocumentTrigger, script: str) -> None:
        """Set the script for ``trigger`` in one transaction."""
        self.update([(trigger, script)])

    def __delitem__(self, trigger: DocumentTrigger) -> None:
        """Remove ``trigger`` from ``Catalog['AA']`` in one transaction, or raise :class:`KeyError`."""
        resolved = self._trigger(trigger)
        self._document._ensure_open()  # noqa: SLF001
        with self._document.transaction("document triggers"):
            items = _read_document_triggers(self._document)
            if items.pop(resolved, None) is None:
                raise KeyError(trigger)
            _write_document_aa(self._document, items)

    def __iter__(self) -> Iterator[DocumentTrigger]:
        """Iterate triggers that currently have a script."""
        self._document._ensure_open()  # noqa: SLF001
        return iter(list(_read_document_triggers(self._document)))

    def __len__(self) -> int:
        """Return the number of configured document triggers."""
        self._document._ensure_open()  # noqa: SLF001
        return len(_read_document_triggers(self._document))

    def __contains__(self, trigger: object) -> bool:
        """Return whether ``trigger`` has a script."""
        if not isinstance(trigger, str):
            return False
        try:
            resolved = DocumentTrigger(trigger)
        except ValueError:
            return False
        self._document._ensure_open()  # noqa: SLF001
        return resolved in _read_document_triggers(self._document)

    def update(self, other: object = (), /, **values: str) -> None:
        """Set every ``(trigger, script)`` pair in one transaction."""
        self._document._ensure_open()  # noqa: SLF001
        raw_pairs = _extract_mapping_pairs(other, values)
        staged: dict[DocumentTrigger, str] = {}
        for raw_trig, raw_script in raw_pairs:
            staged[self._trigger(raw_trig)] = _validate_script(raw_script, "script")
        if not staged:
            return
        with self._document.transaction("document triggers"):
            items = _read_document_triggers(self._document)
            items.update(staged)
            _write_document_aa(self._document, items)

    def clear(self) -> None:
        """Remove all document triggers in one transaction."""
        _delete_document_triggers(self._document)

    def __repr__(self) -> str:
        """Return ``DocumentTriggers({...})``."""
        self._document._ensure_open()  # noqa: SLF001
        return f"DocumentTriggers({_read_document_triggers(self._document)!r})"


def _replace_document_triggers(
    document: Document,
    triggers: Mapping[DocumentTrigger, str] | Iterable[tuple[DocumentTrigger, str]],
) -> None:
    raw_pairs = _extract_mapping_pairs(triggers, {})
    staged: dict[DocumentTrigger, str] = {}
    total = 0
    for raw_trig, raw_script in raw_pairs:
        if not isinstance(raw_trig, str):
            msg = f"trigger must be a DocumentTrigger or str: {raw_trig!r}"
            raise TypeError(msg)
        staged[DocumentTrigger(raw_trig)] = _validate_script(raw_script, "script")
    for source in staged.values():
        total += _script_size(source)
        if total > _MAX_SCRIPT_SIZE:
            _refuse_script("document trigger scripts")
    if not staged and "AA" not in _catalog(document):
        return
    with document.transaction("document triggers"):
        _write_document_aa(document, staged)


def _delete_document_triggers(document: Document) -> None:
    document._ensure_open()  # noqa: SLF001
    if "AA" not in _catalog(document):
        return
    with document.transaction("document triggers"):
        if "AA" not in _catalog(document):
            return
        _write_document_aa(document, {})


class PageTrigger(enum.StrEnum):
    """Page additional-action events (``Page["AA"]``, ISO 32000-2 Table 198)."""

    OPEN = "open"
    """When the page is opened (``/O``)."""
    CLOSE = "close"
    """When the page is closed (``/C``)."""


_PAGE_TRIGGER_KEYS: dict[PageTrigger, str] = {
    PageTrigger.OPEN: "O",
    PageTrigger.CLOSE: "C",
}


def _read_page_triggers_map(page: Page) -> dict[PageTrigger, Target]:
    from pyropdf._presentations import _ActionReadContext  # noqa: PLC0415

    _ = page.index
    aa = page.obj.get("AA")
    if not _is_dict(aa):
        return {}
    aa_dict = aa
    ctx = _ActionReadContext(page.document)
    items: dict[PageTrigger, Target] = {}
    for trigger, pdf_key in _PAGE_TRIGGER_KEYS.items():
        resolved = ctx.read_action(aa_dict.get(pdf_key))
        if resolved is not None:
            items[trigger] = resolved
    return items


class PageTriggers(MutableMapping[PageTrigger, "Target"]):
    """Page additional actions by :class:`PageTrigger` (``Page["AA"]``, ISO 32000-2 Table 198)."""

    __slots__ = ("_page",)

    def __init__(self, page: Page) -> None:
        """Wrap ``page``; use ``page.triggers``."""
        self._page = page

    def _trigger(self, trigger: object) -> PageTrigger:
        if not isinstance(trigger, str):
            msg = f"trigger must be a PageTrigger or str: {trigger!r}"
            raise TypeError(msg)
        return PageTrigger(trigger)

    def __getitem__(self, trigger: PageTrigger) -> Target:
        """Return the action for ``trigger``, or raise :class:`KeyError`."""
        return _read_page_triggers_map(self._page)[self._trigger(trigger)]

    def __setitem__(self, trigger: PageTrigger, target: Target) -> None:
        """Set the action for ``trigger`` in one transaction."""
        self.update([(trigger, target)])

    def __delitem__(self, trigger: PageTrigger) -> None:
        """Remove ``trigger`` from ``Page['AA']`` in one transaction, or raise :class:`KeyError`."""
        resolved = self._trigger(trigger)
        _ = self._page.index
        pdf_key = _PAGE_TRIGGER_KEYS[resolved]
        document = self._page.document
        with document.transaction("page triggers"):
            _ = self._page.index
            aa = self._page.obj.get("AA")
            if not _is_dict(aa) or pdf_key not in aa:
                raise KeyError(trigger)
            aa_dict = aa
            retired = _collect_action_xrefs(aa_dict.get(pdf_key))
            if (aa_xref := _indirect_xref(aa_dict)) is not None:
                retired.append(aa_xref)
            kept = {k: aa_dict[k] for k in aa_dict if k != pdf_key}
            if kept:
                self._page.obj["AA"] = kept
            else:
                del self._page.obj["AA"]
            if retired:
                _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001

    def __iter__(self) -> Iterator[PageTrigger]:
        """Iterate triggers that currently resolve to an action."""
        return iter(list(_read_page_triggers_map(self._page)))

    def __len__(self) -> int:
        """Return the number of page triggers."""
        return len(_read_page_triggers_map(self._page))

    def __contains__(self, trigger: object) -> bool:
        """Return whether ``trigger`` has a resolved action."""
        if not isinstance(trigger, str):
            return False
        try:
            resolved = PageTrigger(trigger)
        except ValueError:
            return False
        return resolved in _read_page_triggers_map(self._page)

    def update(self, other: object = (), /, **values: Target) -> None:
        """Set every ``(trigger, target)`` pair in one transaction."""
        _ = self._page.index
        raw_pairs = _extract_mapping_pairs(other, values)
        staged: list[tuple[PageTrigger, Target]] = [(self._trigger(k), cast("Target", v)) for k, v in raw_pairs]
        if not staged:
            return
        document = self._page.document
        for _, target_val in staged:
            _target_action_dict(document, target_val)
        with document.transaction("page triggers"):
            _ = self._page.index
            prepared: dict[str, dict[str, object]] = {}
            minimum = (1, 2)
            for trig, target_val in staged:
                act_dict, req = _target_action_dict(document, target_val)
                prepared[_PAGE_TRIGGER_KEYS[trig]] = act_dict
                minimum = max(minimum, req)
            current_aa = self._page.obj.get("AA")
            retired = _collect_aa_xrefs(current_aa, prepared)
            merged = {k: current_aa[k] for k in current_aa} if _is_dict(current_aa) else {}
            merged.update(prepared)
            _require_version(document, _catalog(document), minimum)
            self._page.obj["AA"] = merged
            if retired:
                _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001

    def clear(self) -> None:
        """Remove all page triggers in one transaction."""
        _delete_page_triggers(self._page)

    def __repr__(self) -> str:
        """Return ``PageTriggers({...})``, or ``<stale PageTriggers>``."""
        try:
            items = _read_page_triggers_map(self._page)
        except StalePageError:
            return "<stale PageTriggers>"
        return f"PageTriggers({items!r})"


def _replace_page_triggers(
    page: Page,
    triggers: Mapping[PageTrigger, Target] | Iterable[tuple[PageTrigger, Target]],
) -> None:
    _ = page.index
    raw_pairs = _extract_mapping_pairs(triggers, {})
    staged: dict[PageTrigger, Target] = {}
    for raw_trig, raw_target in raw_pairs:
        if not isinstance(raw_trig, str):
            msg = f"trigger must be a PageTrigger or str: {raw_trig!r}"
            raise TypeError(msg)
        staged[PageTrigger(raw_trig)] = cast("Target", raw_target)
    document = page.document
    for target_val in staged.values():
        _target_action_dict(document, target_val)
    if not staged and "AA" not in page.obj:
        return
    with document.transaction("page triggers"):
        _ = page.index
        current_aa = page.obj.get("AA")
        retired = _collect_aa_xrefs(current_aa, _PAGE_TRIGGER_KEYS.values())
        preserved = (
            {k: current_aa[k] for k in current_aa if k not in _PAGE_TRIGGER_KEYS.values()}
            if _is_dict(current_aa)
            else {}
        )
        minimum = (1, 2)
        for trig, target_val in staged.items():
            act_dict, req = _target_action_dict(document, target_val)
            preserved[_PAGE_TRIGGER_KEYS[trig]] = act_dict
            minimum = max(minimum, req)
        if preserved:
            _require_version(document, _catalog(document), minimum)
            page.obj["AA"] = preserved
        elif "AA" in page.obj:
            del page.obj["AA"]
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


def _delete_page_triggers(page: Page) -> None:
    _ = page.index
    if "AA" not in page.obj:
        return
    document = page.document
    with document.transaction("page triggers"):
        _ = page.index
        current_aa = page.obj.get("AA")
        if not _is_dict(current_aa):
            if "AA" in page.obj:
                del page.obj["AA"]
            return
        retired = _collect_aa_xrefs(current_aa, _PAGE_TRIGGER_KEYS.values())
        preserved = {k: current_aa[k] for k in current_aa if k not in _PAGE_TRIGGER_KEYS.values()}
        if preserved:
            page.obj["AA"] = preserved
        else:
            del page.obj["AA"]
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


class AnnotationTrigger(enum.StrEnum):
    """Widget and screen-annotation trigger events (ISO 32000-2 Table 197)."""

    ENTER = "enter"
    """Cursor enters the annotation's active area (``/E``)."""
    EXIT = "exit"
    """Cursor exits the annotation's active area (``/X``)."""
    DOWN = "down"
    """Mouse button pressed inside the active area (``/D``)."""
    UP = "up"
    """Mouse button released inside the active area (``/U``)."""
    FOCUS = "focus"
    """Widget annotation receives input focus (``/Fo``, widgets only)."""
    BLUR = "blur"
    """Widget annotation loses input focus (``/Bl``, widgets only)."""
    PAGE_OPEN = "page_open"
    """Page containing the annotation is opened (``/PO``, PDF 1.5)."""
    PAGE_CLOSE = "page_close"
    """Page containing the annotation is closed (``/PC``, PDF 1.5)."""
    PAGE_VISIBLE = "page_visible"
    """Page containing the annotation becomes visible (``/PV``, PDF 1.5)."""
    PAGE_INVISIBLE = "page_invisible"
    """Page containing the annotation is no longer visible (``/PI``, PDF 1.5)."""
    ACTIVATE = "activate"
    """Annotation activation action (``/A``, widgets; screen annotations expose ``annotation.action``)."""


_ANNOTATION_TRIGGER_KEYS: dict[AnnotationTrigger, str] = {
    AnnotationTrigger.ENTER: "E",
    AnnotationTrigger.EXIT: "X",
    AnnotationTrigger.DOWN: "D",
    AnnotationTrigger.UP: "U",
    AnnotationTrigger.FOCUS: "Fo",
    AnnotationTrigger.BLUR: "Bl",
    AnnotationTrigger.PAGE_OPEN: "PO",
    AnnotationTrigger.PAGE_CLOSE: "PC",
    AnnotationTrigger.PAGE_VISIBLE: "PV",
    AnnotationTrigger.PAGE_INVISIBLE: "PI",
    AnnotationTrigger.ACTIVATE: "A",
}
_PDF_15_ANNOTATION_TRIGGERS = frozenset(
    {
        AnnotationTrigger.PAGE_OPEN,
        AnnotationTrigger.PAGE_CLOSE,
        AnnotationTrigger.PAGE_VISIBLE,
        AnnotationTrigger.PAGE_INVISIBLE,
    }
)
_WIDGET_TRIGGERS = frozenset(AnnotationTrigger)
_SCREEN_TRIGGERS = frozenset(AnnotationTrigger) - frozenset(
    {AnnotationTrigger.FOCUS, AnnotationTrigger.BLUR, AnnotationTrigger.ACTIVATE}
)


def _apply_annot_trigger_writes(
    holder: Dictionary,
    allowed: frozenset[AnnotationTrigger],
    staged: Mapping[AnnotationTrigger, dict[str, object]],
    *,
    replace_all: bool,
) -> list[int]:
    aa_keys = {_ANNOTATION_TRIGGER_KEYS[t] for t in allowed if t is not AnnotationTrigger.ACTIVATE}
    current_aa = holder.get("AA")
    retired: list[int] = []

    if AnnotationTrigger.ACTIVATE in allowed and (replace_all or AnnotationTrigger.ACTIVATE in staged):
        retired.extend(_collect_action_xrefs(holder.get("A")))
        if AnnotationTrigger.ACTIVATE in staged:
            holder["A"] = staged[AnnotationTrigger.ACTIVATE]
        elif "A" in holder:
            del holder["A"]

    aa_staged = {_ANNOTATION_TRIGGER_KEYS[t]: d for t, d in staged.items() if t is not AnnotationTrigger.ACTIVATE}
    has_managed_aa = _is_dict(current_aa) and any(k in current_aa for k in aa_keys)
    if aa_staged or (replace_all and (has_managed_aa or ("AA" in holder and not _is_dict(current_aa)))):
        retired.extend(_collect_aa_xrefs(current_aa, aa_keys if replace_all else aa_staged))
        merged = (
            {k: current_aa[k] for k in current_aa if not (replace_all and k in aa_keys)} if _is_dict(current_aa) else {}
        )
        merged.update(aa_staged)
        if merged:
            holder["AA"] = merged
        elif "AA" in holder:
            del holder["AA"]
    return retired


class ActionTriggers(MutableMapping[AnnotationTrigger, "Target"]):
    """Additional actions of a widget or screen annotation by :class:`AnnotationTrigger` (Table 197).

    Widgets accept all :class:`AnnotationTrigger` members (mapping ``ACTIVATE``
    to ``/A`` and the rest to ``/AA`` while keeping field-level ``/K``, ``/F``,
    ``/V``, and ``/C`` entries intact). ``Screen`` annotations accept the
    ``/AA`` triggers of ``AddActionScreenAnnotation`` and expose ``/A`` through
    :attr:`Annotation.action`; non-``Screen`` annotations read empty and refuse
    mutations with :class:`ValueError`.
    """

    __slots__ = ("_allowed", "_base_version", "_document", "_holder", "_writable")

    def __init__(
        self,
        document: Document,
        holder: Callable[[], Dictionary],
        allowed: frozenset[AnnotationTrigger],
        *,
        base_version: tuple[int, int],
        writable: Callable[[], bool] | None = None,
    ) -> None:
        """Wrap an annotation or widget dictionary; use ``widget.triggers`` or ``annotation.triggers``."""
        self._document = document
        self._holder = holder
        self._allowed = allowed
        self._base_version = base_version
        self._writable = writable

    def _require_writable(self) -> None:
        if self._writable is not None and not self._writable():
            msg = "only Screen annotations support triggers"
            raise ValueError(msg)

    def _trigger(self, trigger: object) -> AnnotationTrigger:
        if not isinstance(trigger, str):
            msg = f"trigger must be an AnnotationTrigger or str: {trigger!r}"
            raise TypeError(msg)
        resolved = AnnotationTrigger(trigger)
        if resolved not in self._allowed:
            owner = "widget" if AnnotationTrigger.FOCUS in self._allowed else "Screen annotation"
            msg = f"{resolved.name} is not a supported trigger on this {owner}"
            raise ValueError(msg)
        return resolved

    def _items(self) -> dict[AnnotationTrigger, Target]:
        from pyropdf._presentations import _ActionReadContext  # noqa: PLC0415

        holder = self._holder()
        if self._writable is not None and not self._writable():
            return {}
        aa = holder.get("AA")
        aa_dict = aa if _is_dict(aa) else None
        ctx = _ActionReadContext(self._document)
        items: dict[AnnotationTrigger, Target] = {}
        for trigger in AnnotationTrigger:
            if trigger not in self._allowed:
                continue
            raw = (
                holder.get("A")
                if trigger is AnnotationTrigger.ACTIVATE
                else (aa_dict.get(_ANNOTATION_TRIGGER_KEYS[trigger]) if aa_dict is not None else None)
            )
            resolved = ctx.read_action(raw)
            if resolved is not None:
                items[trigger] = resolved
        return items

    def __getitem__(self, trigger: AnnotationTrigger) -> Target:
        """Return the action for ``trigger``, or raise :class:`KeyError`."""
        resolved = self._trigger(trigger)
        return self._items()[resolved]

    def __setitem__(self, trigger: AnnotationTrigger, target: Target) -> None:
        """Set the action for ``trigger`` in one transaction."""
        self.update([(trigger, target)])

    def __delitem__(self, trigger: AnnotationTrigger) -> None:
        """Remove ``trigger`` in one transaction, or raise :class:`KeyError`."""
        _ = self._holder()
        self._require_writable()
        resolved = self._trigger(trigger)
        pdf_key = _ANNOTATION_TRIGGER_KEYS[resolved]
        with self._document.transaction("annotation triggers"):
            holder = self._holder()
            self._require_writable()
            retired: list[int] = []
            if resolved is AnnotationTrigger.ACTIVATE:
                if "A" not in holder:
                    raise KeyError(trigger)
                retired.extend(_collect_action_xrefs(holder.get("A")))
                del holder["A"]
            else:
                aa = holder.get("AA")
                if not _is_dict(aa) or pdf_key not in aa:
                    raise KeyError(trigger)
                aa_dict = aa
                retired.extend(_collect_action_xrefs(aa_dict.get(pdf_key)))
                if (aa_xref := _indirect_xref(aa_dict)) is not None:
                    retired.append(aa_xref)
                kept = {k: aa_dict[k] for k in aa_dict if k != pdf_key}
                if kept:
                    holder["AA"] = kept
                else:
                    del holder["AA"]
            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    def __iter__(self) -> Iterator[AnnotationTrigger]:
        """Iterate triggers that currently resolve to an action."""
        return iter(list(self._items()))

    def __len__(self) -> int:
        """Return the number of triggers."""
        return len(self._items())

    def __contains__(self, trigger: object) -> bool:
        """Return whether ``trigger`` has a resolved action."""
        if not isinstance(trigger, str):
            return False
        try:
            resolved = AnnotationTrigger(trigger)
        except ValueError:
            return False
        if resolved not in self._allowed:
            return False
        return resolved in self._items()

    def update(self, other: object = (), /, **values: Target) -> None:
        """Set every ``(trigger, target)`` pair in one transaction."""
        _ = self._holder()
        self._require_writable()
        raw_pairs = _extract_mapping_pairs(other, values)
        staged_targets: list[tuple[AnnotationTrigger, Target]] = [
            (self._trigger(k), cast("Target", v)) for k, v in raw_pairs
        ]
        if not staged_targets:
            return
        for _, target_val in staged_targets:
            _target_action_dict(self._document, target_val)
        with self._document.transaction("annotation triggers"):
            holder = self._holder()
            self._require_writable()
            staged_dicts: dict[AnnotationTrigger, dict[str, object]] = {}
            minimum = self._base_version
            for trig, target_val in staged_targets:
                act_dict, req = _target_action_dict(self._document, target_val)
                staged_dicts[trig] = act_dict
                trig_ver = (1, 5) if trig in _PDF_15_ANNOTATION_TRIGGERS else self._base_version
                minimum = max(minimum, req, trig_ver)
            _require_version(self._document, _catalog(self._document), minimum)
            retired = _apply_annot_trigger_writes(
                holder,
                self._allowed,
                staged_dicts,
                replace_all=False,
            )
            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    def clear(self) -> None:
        """Remove all triggers managed by this mapping in one transaction."""
        _ = self._holder()
        self._require_writable()
        with self._document.transaction("annotation triggers"):
            holder = self._holder()
            self._require_writable()
            retired = _apply_annot_trigger_writes(
                holder,
                self._allowed,
                {},
                replace_all=True,
            )
            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    def _replace(
        self,
        triggers: Mapping[AnnotationTrigger, Target] | Iterable[tuple[AnnotationTrigger, Target]],
    ) -> None:
        _ = self._holder()
        self._require_writable()
        raw_pairs = _extract_mapping_pairs(triggers, {})
        staged_targets: list[tuple[AnnotationTrigger, Target]] = [
            (self._trigger(k), cast("Target", v)) for k, v in raw_pairs
        ]
        for _, target_val in staged_targets:
            _target_action_dict(self._document, target_val)
        with self._document.transaction("annotation triggers"):
            holder = self._holder()
            self._require_writable()
            staged_dicts: dict[AnnotationTrigger, dict[str, object]] = {}
            minimum = self._base_version
            for trig, target_val in staged_targets:
                act_dict, req = _target_action_dict(self._document, target_val)
                staged_dicts[trig] = act_dict
                trig_ver = (1, 5) if trig in _PDF_15_ANNOTATION_TRIGGERS else self._base_version
                minimum = max(minimum, req, trig_ver)
            if staged_dicts:
                _require_version(self._document, _catalog(self._document), minimum)
            retired = _apply_annot_trigger_writes(
                holder,
                self._allowed,
                staged_dicts,
                replace_all=True,
            )
            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    def __repr__(self) -> str:
        """Return ``ActionTriggers({...})``, or ``<stale ActionTriggers>``."""
        try:
            items = self._items()
        except (StaleObjectError, StalePageError):
            return "<stale ActionTriggers>"
        return f"ActionTriggers({items!r})"


def _read_screen_action(document: Document, holder: Dictionary) -> Target | None:
    from pyropdf._presentations import _ActionReadContext  # noqa: PLC0415

    if holder.get("Subtype") != "Screen":
        return None
    return _ActionReadContext(document).read_action(holder.get("A"))


def _write_screen_action(document: Document, get_holder: Callable[[], Dictionary], target: Target | None) -> None:
    holder = get_holder()
    if holder.get("Subtype") != "Screen":
        msg = "only Screen annotations support action"
        raise ValueError(msg)
    if target is None:
        _delete_screen_action(document, get_holder)
        return
    act_dict, req = _target_action_dict(document, target)
    with document.transaction("annotation action"):
        holder = get_holder()
        if holder.get("Subtype") != "Screen":
            msg = "only Screen annotations support action"
            raise ValueError(msg)
        act_dict, req = _target_action_dict(document, target)
        retired = _collect_action_xrefs(holder.get("A"))
        _require_version(document, _catalog(document), max((1, 5), req))
        holder["A"] = act_dict
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


def _delete_screen_action(document: Document, get_holder: Callable[[], Dictionary]) -> None:
    holder = get_holder()
    if holder.get("Subtype") != "Screen":
        msg = "only Screen annotations support action"
        raise ValueError(msg)
    if "A" not in holder:
        return
    with document.transaction("annotation action"):
        holder = get_holder()
        if holder.get("Subtype") != "Screen":
            msg = "only Screen annotations support action"
            raise ValueError(msg)
        if "A" not in holder:
            return
        retired = _collect_action_xrefs(holder.get("A"))
        del holder["A"]
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


for _public in (
    ActionTriggers,
    AnnotationTrigger,
    DocumentScripts,
    DocumentTrigger,
    DocumentTriggers,
    EmbeddedDestination,
    EmbeddedTarget,
    HideAction,
    ImportDataAction,
    JavaScriptAction,
    LayerStateChange,
    LayerStateTransition,
    PageTrigger,
    PageTriggers,
    ResetFormAction,
    SetLayerStateAction,
    SubmitFormAction,
    SubmitFormFlags,
):
    _public.__module__ = "pyropdf"

__all__ = [
    "ActionTriggers",
    "AnnotationTrigger",
    "DocumentScripts",
    "DocumentTrigger",
    "DocumentTriggers",
    "EmbeddedDestination",
    "EmbeddedTarget",
    "HideAction",
    "ImportDataAction",
    "JavaScriptAction",
    "LayerStateChange",
    "LayerStateTransition",
    "PageTrigger",
    "PageTriggers",
    "ResetFormAction",
    "SetLayerStateAction",
    "SubmitFormAction",
    "SubmitFormFlags",
]
