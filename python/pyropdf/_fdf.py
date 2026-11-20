"""FDF and XFDF form data exchange: ``FdfData``, ``parse_fdf``, and ``parse_xfdf``."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pyropdf import _core
from pyropdf._canvas import _cache
from pyropdf._core import _Document
from pyropdf._errors import LimitError, MalformedPdfError, PdfError
from pyropdf._fonts import add_glyphs, embedded_font
from pyropdf._form import (
    _MAX_FIELD_CHOICES,
    _MAX_FIELD_DEPTH,
    _MAX_FIELD_INPUT_SIZE,
    Field,
    FieldFlags,
    FieldType,
    Form,
    _field_text,
)
from pyropdf._limits import DocumentLimits, _validate_optional_positive_int
from pyropdf._objects import Array, Dictionary, Name, Stream, String, _unlocked

if TYPE_CHECKING:
    from pyropdf._document import Document

#: Bytes searched from the start of an FDF input for its ``%FDF-x.y`` header.
_HEADER_WINDOW = 1024
#: Maximum hierarchy depth of FDF ``/Fields`` and XFDF ``<field>`` elements.
_MAX_FDF_DEPTH = 32
#: Maximum XML element depth in XFDF (``<xfdf>`` + ``<fields>`` + 32 ``<field>`` levels + ``<value>``).
_MAX_XFDF_ELEMENT_DEPTH = _MAX_FDF_DEPTH + 3
#: Maximum field dictionaries or entries in one FDF or XFDF document.
_MAX_FDF_FIELDS = 4096
#: Maximum ``/Fields`` and ``/Kids`` array entries visited in one FDF walk.
_MAX_FDF_EDGES = 8192
#: Default maximum byte length of an XFDF input (16 MiB).
_DEFAULT_MAX_XFDF_SIZE = 16 * 1024 * 1024
#: Chunk size fed into ``XMLPullParser``.
_XFDF_CHUNK_SIZE = 65536
#: Maximum byte length of an ``/ID`` or ``<ids/>`` hex pair.
_MAX_ID_BYTES = 4096

_XFDF_NS = "http://ns.adobe.com/xfdf/"
_XFDF_NS_PREFIX = f"{{{_XFDF_NS}}}"
_DOCTYPE_OR_ENTITY = re.compile(rb"<!\s*(?:doctype|entity)", re.IGNORECASE)
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")

_EXPORTABLE_TYPES = frozenset(
    {
        FieldType.TEXT,
        FieldType.CHECKBOX,
        FieldType.RADIO,
        FieldType.COMBOBOX,
        FieldType.LISTBOX,
    }
)


def _validate_field_entry(key: object, value: object) -> tuple[str, str | tuple[str, ...] | None]:
    """Validate one ``(name, value)`` pair passed to :class:`FdfData`."""
    if not isinstance(key, str):
        msg = f"field names must be str: {key!r}"
        raise TypeError(msg)
    if not key:
        msg = "field names must be non-empty"
        raise ValueError(msg)
    if value is None or isinstance(value, str):
        return key, value
    if isinstance(value, tuple):
        for item in value:
            if not isinstance(item, str):
                msg = f"tuple field values must contain str items: {item!r}"
                raise TypeError(msg)
        return key, tuple(value)
    msg = f"field value must be str, tuple[str, ...], or None: {value!r}"
    raise TypeError(msg)


def _validate_id_pair(raw_id: object) -> tuple[bytes, bytes] | None:
    """Validate and normalize the optional ``(original, modified)`` identifier pair."""
    if raw_id is None:
        return None
    if not isinstance(raw_id, tuple) or len(raw_id) != 2:  # noqa: PLR2004
        msg = f"id must be a 2-tuple of bytes or None: {raw_id!r}"
        raise ValueError(msg)
    first, second = raw_id
    if not isinstance(first, (bytes, bytearray, memoryview)) or not isinstance(second, (bytes, bytearray, memoryview)):
        msg = f"id elements must be bytes-like: {raw_id!r}"
        raise TypeError(msg)
    return (bytes(first), bytes(second))


@dataclass(frozen=True, slots=True, kw_only=True)
class FdfData:
    """Form field values and target-document metadata read by :func:`parse_fdf` or :func:`parse_xfdf`."""

    #: Full field names mapped to text, a tuple of selected choice values, or ``None`` when unset.
    fields: Mapping[str, str | tuple[str, ...] | None]
    #: The target PDF file specification (``/F`` or ``<f href="..."/>``), or ``None``.
    file: str | None = None
    #: The target document's two-part identifier (``/ID`` or ``<ids/>``), or ``None``.
    id: tuple[bytes, bytes] | None = None

    def __post_init__(self) -> None:
        """Validate and freeze the field mapping and document identifier."""
        if not isinstance(self.fields, Mapping):
            msg = f"fields must be a Mapping: {self.fields!r}"
            raise TypeError(msg)
        validated = dict(_validate_field_entry(k, v) for k, v in self.fields.items())
        if self.file is not None and not isinstance(self.file, str):
            msg = f"file must be a str or None: {self.file!r}"
            raise TypeError(msg)
        object.__setattr__(self, "id", _validate_id_pair(self.id))
        object.__setattr__(self, "fields", MappingProxyType(validated))


def _utf8_len(text: str) -> int:
    """Return the UTF-8 byte length of ``text``."""
    return len(text.encode("utf-8", "surrogatepass"))


class _TextBudget:
    """Cumulative 1 MiB UTF-8 text budget across one FDF or XFDF read."""

    __slots__ = ("_used",)

    def __init__(self) -> None:
        self._used = 0

    def charge(self, text: str) -> str:
        """Charge ``text``'s UTF-8 bytes and return ``text``."""
        self._used += _utf8_len(text)
        if self._used > _MAX_FIELD_INPUT_SIZE:
            code = "form_field_input_size"
            msg = f"form data text exceeds the {_MAX_FIELD_INPUT_SIZE}-byte UTF-8 safety limit"
            raise LimitError(code, msg)
        return text


def _read_bounded_file(path: str, max_bytes: int | None, limit_code: str, label: str) -> bytes:
    """Read ``path`` into memory, refusing files larger than ``max_bytes`` before reading all bytes."""
    try:
        with Path(path).open("rb") as handle:
            if max_bytes is not None:
                size = os.fstat(handle.fileno()).st_size
                if size > max_bytes:
                    msg = f"{label} size {size} exceeds the {max_bytes}-byte safety limit"
                    raise LimitError(limit_code, msg)
                data = handle.read(max_bytes + 1)
                if len(data) > max_bytes:
                    msg = f"{label} exceeds the {max_bytes}-byte safety limit"
                    raise LimitError(limit_code, msg)
                return data
            return handle.read()
    except FileNotFoundError:
        raise
    except OSError as exc:
        msg = f"failed to read {path}: {exc}"
        raise PdfError(msg) from exc


def _patch_fdf_header(raw: bytes | bytearray | memoryview) -> bytes:
    """Verify ``%FDF-x.y`` within the first 1,024 bytes and replace ``%FDF-`` with ``%PDF-``."""
    view = memoryview(raw)
    head = bytes(view[: _HEADER_WINDOW + 8])
    offset = head.find(b"%FDF-", 0, _HEADER_WINDOW + 5)
    if offset < 0 or offset >= _HEADER_WINDOW or len(head) < offset + 8:
        msg = "missing %FDF-x.y header within the first 1024 bytes"
        raise MalformedPdfError(msg)
    major, dot, minor = head[offset + 5], head[offset + 6], head[offset + 7]
    ascii_zero, ascii_nine, ascii_dot = 48, 57, 46
    if not (ascii_zero <= major <= ascii_nine and dot == ascii_dot and ascii_zero <= minor <= ascii_nine):
        msg = "invalid %FDF-x.y header version"
        raise MalformedPdfError(msg)
    if len(head) > offset + 8 and ascii_zero <= head[offset + 8] <= ascii_nine:
        msg = "invalid %FDF-x.y header version"
        raise MalformedPdfError(msg)
    return bytes(view[:offset]) + b"%PDF-" + bytes(view[offset + 5 :])


def _open_fdf_document(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview,
    limits: DocumentLimits | None,
) -> Document:
    """Open an FDF source as a temporary ``Document`` backed by ``pyropdf-store``."""
    from pyropdf._document import Document, _open_source, _resolve_limits  # noqa: PLC0415

    resolved = _resolve_limits(limits)
    path, buffer = _open_source(source)
    if path is not None:
        raw: bytes | bytearray | memoryview = _read_bounded_file(path, resolved.max_file_size, "file_size", "FDF file")
    else:
        assert buffer is not None  # noqa: S101 - _open_source returns either path or buffer.
        nbytes = memoryview(buffer).nbytes
        if resolved.max_file_size is not None and nbytes > resolved.max_file_size:
            code = "file_size"
            msg = f"FDF input size {nbytes} exceeds the {resolved.max_file_size}-byte safety limit"
            raise LimitError(code, msg)
        raw = buffer
    patched = _patch_fdf_header(raw)
    document = Document.__new__(Document)
    document._setup(lambda lim: _Document.open_buffer(patched, lim, None), resolved)  # noqa: SLF001
    return document


def _read_fdf_file_spec(fdf_dict: Dictionary, budget: _TextBudget) -> str | None:
    """Extract the optional ``/F`` target file name from an ``/FDF`` dictionary."""
    spec = fdf_dict.get("F")
    if spec is None:
        return None
    if isinstance(spec, String):
        return budget.charge(spec.text)
    if isinstance(spec, Dictionary):
        for key in ("UF", "F", "Unix", "DOS", "Mac"):
            entry = spec.get(key)
            if isinstance(entry, String):
                return budget.charge(entry.text)
        return None
    msg = "FDF /F entry must be a string or file specification dictionary"
    raise MalformedPdfError(msg)


def _read_pdf_id_pair(holder: Dictionary) -> tuple[bytes, bytes] | None:
    """Extract a two-element byte-string ``/ID`` array when present and valid."""
    raw_id = holder.get("ID")
    if raw_id is None or not isinstance(raw_id, Array) or len(raw_id) != 2:  # noqa: PLR2004
        return None
    first, second = raw_id[0], raw_id[1]
    if not isinstance(first, String) or not isinstance(second, String):
        return None
    if len(first) + len(second) > _MAX_ID_BYTES:
        code = "form_field_input_size"
        msg = f"FDF /ID exceeds the {_MAX_ID_BYTES}-byte safety limit"
        raise LimitError(code, msg)
    return (bytes(first), bytes(second))


def _read_fdf_stream_value(stream: Stream, budget: _TextBudget) -> str:
    """Decode a stream ``/V`` entry under ``_MAX_FIELD_INPUT_SIZE``."""
    try:
        raw = stream.read(max_size=_MAX_FIELD_INPUT_SIZE * 2 + 4)
    except LimitError as exc:
        if exc.code == "stream_data_size":
            code = "form_field_input_size"
            msg = f"FDF stream field value exceeds the {_MAX_FIELD_INPUT_SIZE}-byte safety limit"
            raise LimitError(code, msg) from exc
        raise
    return budget.charge(String(raw).text)


def _read_fdf_array_value(arr: Array, budget: _TextBudget) -> tuple[str, ...]:
    """Decode an array ``/V`` entry into a tuple of strings."""
    if len(arr) > _MAX_FDF_FIELDS:
        msg = f"FDF array field value exceeds the {_MAX_FDF_FIELDS}-item safety limit"
        raise PdfError(msg)
    items: list[str] = []
    for item in arr:
        if item is None:
            continue
        if isinstance(item, String):
            items.append(budget.charge(item.text))
        elif isinstance(item, Name):
            items.append(budget.charge(str(item)))
        else:
            msg = "FDF array /V contains an element that is neither a string nor a name"
            raise MalformedPdfError(msg)
    return tuple(items)


def _read_fdf_value(node: Dictionary, budget: _TextBudget) -> str | tuple[str, ...] | None:
    """Decode ``/V`` from a terminal FDF field dictionary."""
    if "V" not in node:
        return None
    val = node.get("V")
    if val is None:
        return None
    if isinstance(val, String):
        return budget.charge(val.text)
    if isinstance(val, Name):
        return budget.charge(str(val))
    if isinstance(val, Stream):
        return _read_fdf_stream_value(val, budget)
    if isinstance(val, Array):
        return _read_fdf_array_value(val, budget)
    msg = f"unsupported FDF field /V value: {type(val).__name__}"
    raise MalformedPdfError(msg)


def _fdf_next_ancestors(
    entry: Dictionary,
    ancestors: frozenset[tuple[int, int]],
) -> frozenset[tuple[int, int]]:
    """Record ``entry`` in ``ancestors`` when indirect, refusing cycles."""
    if not entry.is_indirect:
        return ancestors
    xref = entry.xref
    gen = entry.generation
    assert xref is not None  # noqa: S101 - indirect handles carry xref.
    assert gen is not None  # noqa: S101 - indirect handles carry generation.
    ident = (xref, gen)
    if ident in ancestors:
        msg = "cycle detected in FDF /Fields hierarchy"
        raise MalformedPdfError(msg)
    return ancestors | {ident}


def _fdf_full_name(entry: Dictionary, prefix: str, budget: _TextBudget) -> str:
    """Join ``prefix`` and ``entry``'s optional ``/T`` partial name."""
    raw_t = entry.get("T")
    if raw_t is None:
        part = ""
    elif isinstance(raw_t, String):
        part = budget.charge(raw_t.text)
    else:
        msg = "FDF field /T entry is not a string"
        raise MalformedPdfError(msg)
    if prefix and part:
        return f"{prefix}.{part}"
    return part or prefix


def _fdf_subfield_kids(entry: Dictionary, edge_count: int) -> tuple[list[Dictionary], int]:
    """Return child subfield dictionaries when ``entry`` is non-terminal, or ``[]`` for a leaf."""
    kids = entry.get("Kids")
    if kids is None:
        return [], edge_count
    if not isinstance(kids, Array):
        msg = "FDF field /Kids entry is not an array"
        raise MalformedPdfError(msg)
    edge_count += len(kids)
    if edge_count > _MAX_FDF_EDGES:
        msg = f"FDF field tree exceeds the {_MAX_FDF_EDGES}-edge safety limit"
        raise PdfError(msg)
    child_dicts: list[Dictionary] = []
    for kid in kids:
        if kid is None:
            continue
        if not isinstance(kid, Dictionary):
            msg = "FDF /Kids entry is not a dictionary"
            raise MalformedPdfError(msg)
        child_dicts.append(kid)
    if child_dicts and any("T" in child or "Kids" in child for child in child_dicts):
        return child_dicts, edge_count
    return [], edge_count


def _initial_fdf_stack(
    fdf_dict: Dictionary,
) -> tuple[list[tuple[object, str, int, frozenset[tuple[int, int]]]], int]:
    """Read ``/Fields`` from ``fdf_dict`` and build the initial reversed traversal stack."""
    raw_fields = fdf_dict.get("Fields")
    if raw_fields is None:
        return [], 0
    if not isinstance(raw_fields, Array):
        msg = "FDF /Fields entry is not an array"
        raise MalformedPdfError(msg)
    if len(raw_fields) > _MAX_FDF_EDGES:
        msg = f"FDF field tree exceeds the {_MAX_FDF_EDGES}-edge safety limit"
        raise PdfError(msg)
    stack: list[tuple[object, str, int, frozenset[tuple[int, int]]]] = [
        (raw_fields[idx], "", 1, frozenset()) for idx in range(len(raw_fields) - 1, -1, -1)
    ]
    return stack, len(raw_fields)


def _walk_fdf_fields(fdf_dict: Dictionary, budget: _TextBudget) -> dict[str, str | tuple[str, ...] | None]:
    """Walk ``/FDF /Fields`` iteratively without recursion, enforcing depth, node, cycle, and text caps."""
    stack, edge_count = _initial_fdf_stack(fdf_dict)
    node_count = 0
    fields_out: dict[str, str | tuple[str, ...] | None] = {}

    while stack:
        entry, prefix, depth, ancestors = stack.pop()
        if entry is None:
            continue
        if not isinstance(entry, Dictionary):
            msg = "FDF field entry is not a dictionary"
            raise MalformedPdfError(msg)

        next_ancestors = _fdf_next_ancestors(entry, ancestors)
        node_count += 1
        if node_count > _MAX_FDF_FIELDS:
            msg = f"FDF field tree exceeds the {_MAX_FDF_FIELDS}-node safety limit"
            raise PdfError(msg)

        full_name = _fdf_full_name(entry, prefix, budget)
        subfields, edge_count = _fdf_subfield_kids(entry, edge_count)
        if subfields:
            if depth + 1 > _MAX_FDF_DEPTH:
                code = "form_field_parent_depth"
                msg = f"FDF field hierarchy exceeds the {_MAX_FDF_DEPTH}-level depth limit"
                raise LimitError(code, msg)
            stack.extend((child, full_name, depth + 1, next_ancestors) for child in reversed(subfields))
            continue

        if not full_name:
            msg = "terminal FDF field is missing a non-empty /T name"
            raise MalformedPdfError(msg)
        budget.charge(full_name)
        if full_name not in fields_out and len(fields_out) >= _MAX_FDF_FIELDS:
            msg = f"FDF exceeds the {_MAX_FDF_FIELDS}-field safety limit"
            raise PdfError(msg)
        fields_out[full_name] = _read_fdf_value(entry, budget)

    return fields_out


def parse_fdf(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview,
    /,
    *,
    limits: DocumentLimits | None = None,
) -> FdfData:
    """Parse an FDF file or byte buffer into :class:`FdfData` (ISO 32000-2 section 12.7.8).

    ``source`` is a file path or a bytes-like buffer. Both cross-reference-indexed
    and cross-reference-less FDF files are read through ``pyropdf-store`` and
    ``hayro-syntax`` under ``limits``, capping the field hierarchy at 32 levels,
    4,096 fields, and 1 MiB of UTF-8 text. Invalid headers, missing ``/Root /FDF``
    dictionaries, and cyclic ``/Kids`` graphs raise :class:`MalformedPdfError`.
    """
    with _open_fdf_document(source, limits) as doc:
        root = doc.trailer.get("Root")
        if not isinstance(root, Dictionary):
            msg = "FDF trailer is missing a /Root dictionary"
            raise MalformedPdfError(msg)
        fdf_dict = root.get("FDF")
        if not isinstance(fdf_dict, Dictionary):
            msg = "FDF catalog is missing an /FDF dictionary"
            raise MalformedPdfError(msg)
        budget = _TextBudget()
        file_spec = _read_fdf_file_spec(fdf_dict, budget)
        doc_id = _read_pdf_id_pair(fdf_dict) or _read_pdf_id_pair(doc.trailer)
        fields = _walk_fdf_fields(fdf_dict, budget)
        return FdfData(fields=fields, file=file_spec, id=doc_id)


def _first_non_ws_char(text: str) -> str:
    """Return the first character after any UTF-8 BOM or XML whitespace, or ``''``."""
    for ch in text:
        if ch not in "\ufeff \t\r\n":
            return ch
    return ""


def _encode_xfdf_xml_str(source: str, max_size: int | None) -> bytes:
    """Encode an inline XML ``str`` to UTF-8 bytes under ``max_size``."""
    if max_size is not None and len(source) > max_size:
        code = "form_field_input_size"
        msg = f"XFDF input length {len(source)} exceeds max_size {max_size}"
        raise LimitError(code, msg)
    try:
        encoded = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        msg = f"XFDF string is not valid UTF-8: {exc}"
        raise MalformedPdfError(msg) from exc
    if max_size is not None and len(encoded) > max_size:
        code = "form_field_input_size"
        msg = f"XFDF input size {len(encoded)} exceeds max_size {max_size}"
        raise LimitError(code, msg)
    return encoded


def _read_xfdf_bytes(source: object, max_size: int | None) -> bytes:
    """Resolve ``source`` (path, XML string, or bytes-like buffer) into raw XML bytes bounded by ``max_size``."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        nbytes = memoryview(source).nbytes
        if max_size is not None and nbytes > max_size:
            code = "form_field_input_size"
            msg = f"XFDF input size {nbytes} exceeds max_size {max_size}"
            raise LimitError(code, msg)
        return bytes(source)
    if isinstance(source, os.PathLike):
        path = os.fspath(source)
        if not isinstance(path, str):
            msg = f"source path must be str or os.PathLike[str]: {type(source).__name__}"
            raise TypeError(msg)
        return _read_bounded_file(path, max_size, "form_field_input_size", "XFDF file")
    if isinstance(source, str):
        first = _first_non_ws_char(source)
        if not first:
            msg = "XFDF input is empty"
            raise MalformedPdfError(msg)
        if first == "<":
            return _encode_xfdf_xml_str(source, max_size)
        return _read_bounded_file(source, max_size, "form_field_input_size", "XFDF file")
    if hasattr(source, "read"):
        msg = f"source must be a path, XML str, or bytes; read the file object first: {type(source).__name__}"
        raise TypeError(msg)
    msg = (
        "source must be a file path (str or os.PathLike[str]), an XML str, or bytes, bytearray, or memoryview: "
        f"{type(source).__name__}"
    )
    raise TypeError(msg)


def _strip_xfdf_tag(tag: str, *, is_root: bool = False) -> str | None:
    """Return the local tag name for unqualified or XFDF-namespaced elements, or ``None`` for foreign namespaces."""
    if tag.startswith("{"):
        if tag.startswith(_XFDF_NS_PREFIX):
            return tag[len(_XFDF_NS_PREFIX) :]
        if is_root:
            msg = f"unexpected root XML namespace on {tag}"
            raise MalformedPdfError(msg)
        return None
    return tag


def _decode_xfdf_ids(attrib: Mapping[str, str]) -> tuple[bytes, bytes] | None:
    """Decode ``<ids original="..." modified="..."/>`` hex attributes when both are valid byte strings."""
    orig = attrib.get("original")
    mod = attrib.get("modified")
    if orig is None or mod is None:
        return None
    if len(orig) + len(mod) > _MAX_ID_BYTES * 2:
        code = "form_field_input_size"
        msg = f"XFDF <ids> attributes exceed the {_MAX_ID_BYTES}-byte safety limit"
        raise LimitError(code, msg)
    if not orig or not mod or len(orig) % 2 != 0 or len(mod) % 2 != 0:
        return None
    if _HEX_RE.fullmatch(orig) is None or _HEX_RE.fullmatch(mod) is None:
        return None
    return (bytes.fromhex(orig), bytes.fromhex(mod))


@dataclass(slots=True)
class _XfdfFieldFrame:
    """One active ``<field>`` element on the iterative pull-parser stack."""

    full_name: str
    depth: int
    values: list[str] = field(default_factory=list)
    has_child_field: bool = False


class _XfdfPullState:
    """Iterative pull-parser state machine for ISO 19444-1 XFDF streams."""

    __slots__ = (
        "budget",
        "depth",
        "doc_id",
        "field_stack",
        "fields_out",
        "file_spec",
        "foreign_inside_fields",
        "in_fields_depth",
        "node_count",
        "saw_root",
        "value_depth",
    )

    def __init__(self) -> None:
        self.budget = _TextBudget()
        self.fields_out: dict[str, str | tuple[str, ...] | None] = {}
        self.file_spec: str | None = None
        self.doc_id: tuple[bytes, bytes] | None = None
        self.saw_root = False
        self.depth = 0
        self.in_fields_depth: int | None = None
        self.foreign_inside_fields: int | None = None
        self.value_depth: int | None = None
        self.node_count = 0
        self.field_stack: list[_XfdfFieldFrame] = []

    def _on_top_child(self, local: str | None, elem: ET.Element) -> None:
        if local == "f":
            href = elem.attrib.get("href")
            if isinstance(href, str):
                self.file_spec = self.budget.charge(href)
        elif local == "ids":
            parsed_id = _decode_xfdf_ids(elem.attrib)
            if parsed_id is not None:
                self.doc_id = parsed_id
        elif local == "fields" and self.in_fields_depth is None:
            self.in_fields_depth = self.depth

    def _push_field(self, elem: ET.Element) -> None:
        if len(self.field_stack) >= _MAX_FDF_DEPTH:
            code = "form_field_parent_depth"
            msg = f"XFDF <field> hierarchy exceeds the {_MAX_FDF_DEPTH}-level depth limit"
            raise LimitError(code, msg)
        part = elem.attrib.get("name")
        if not part:
            msg = "XFDF <field> element is missing a non-empty name attribute"
            raise MalformedPdfError(msg)
        self.budget.charge(part)
        self.node_count += 1
        if self.node_count > _MAX_FDF_FIELDS:
            msg = f"XFDF field tree exceeds the {_MAX_FDF_FIELDS}-field safety limit"
            raise PdfError(msg)
        if self.field_stack:
            self.field_stack[-1].has_child_field = True
            full_name = f"{self.field_stack[-1].full_name}.{part}"
        else:
            full_name = part
        self.field_stack.append(_XfdfFieldFrame(full_name=full_name, depth=self.depth))

    def on_start(self, elem: ET.Element) -> None:
        """Handle a ``start`` event."""
        self.depth += 1
        if self.depth > _MAX_XFDF_ELEMENT_DEPTH:
            code = "form_field_parent_depth"
            msg = f"XFDF element nesting exceeds the {_MAX_FDF_DEPTH}-level depth limit"
            raise LimitError(code, msg)
        local = _strip_xfdf_tag(elem.tag, is_root=(self.depth == 1))
        if self.depth == 1:
            if local != "xfdf":
                msg = f"expected <xfdf> root element, got <{elem.tag}>"
                raise MalformedPdfError(msg)
            self.saw_root = True
            return
        if self.depth == 2:  # noqa: PLR2004
            self._on_top_child(local, elem)
            return
        if self.in_fields_depth is None or self.foreign_inside_fields is not None or self.value_depth is not None:
            return
        expected_parent = self.field_stack[-1].depth if self.field_stack else self.in_fields_depth
        if self.depth == expected_parent + 1 and local == "field":
            self._push_field(elem)
        elif self.field_stack and self.depth == self.field_stack[-1].depth + 1 and local == "value":
            self.value_depth = self.depth
        else:
            self.foreign_inside_fields = self.depth

    def _finish_field(self, frame: _XfdfFieldFrame) -> None:
        if frame.has_child_field:
            return
        self.budget.charge(frame.full_name)
        if frame.full_name not in self.fields_out and len(self.fields_out) >= _MAX_FDF_FIELDS:
            msg = f"XFDF exceeds the {_MAX_FDF_FIELDS}-field safety limit"
            raise PdfError(msg)
        if not frame.values:
            self.fields_out[frame.full_name] = None
        elif len(frame.values) == 1:
            self.fields_out[frame.full_name] = frame.values[0]
        else:
            self.fields_out[frame.full_name] = tuple(frame.values)

    def on_end(self, elem: ET.Element) -> None:
        """Handle an ``end`` event."""
        local = _strip_xfdf_tag(elem.tag)
        if self.value_depth == self.depth:
            text = self.budget.charge(elem.text or "")
            frame = self.field_stack[-1]
            if len(frame.values) >= _MAX_FDF_FIELDS:
                msg = f"XFDF <field> exceeds the {_MAX_FDF_FIELDS}-value safety limit"
                raise PdfError(msg)
            frame.values.append(text)
            self.value_depth = None
            elem.clear()
        elif self.foreign_inside_fields == self.depth:
            self.foreign_inside_fields = None
            elem.clear()
        elif self.field_stack and self.field_stack[-1].depth == self.depth and local == "field":
            self._finish_field(self.field_stack.pop())
            elem.clear()
        elif self.in_fields_depth == self.depth:
            self.in_fields_depth = None
            elem.clear()
        elif self.value_depth is None:
            elem.clear()
        self.depth -= 1


def _drain_pull_events(parser: ET.XMLPullParser, state: _XfdfPullState) -> None:
    """Dispatch ``start`` and ``end`` element events from ``parser`` to ``state``."""
    for item in parser.read_events():
        if len(item) != 2:  # noqa: PLR2004
            continue
        event, elem = item
        if isinstance(elem, ET.Element):
            if event == "start":
                state.on_start(elem)
            else:
                state.on_end(elem)


def parse_xfdf(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview,
    /,
    *,
    max_size: int | None = _DEFAULT_MAX_XFDF_SIZE,
) -> FdfData:
    """Parse an XFDF file, XML string, or byte buffer into :class:`FdfData` (ISO 19444-1).

    A ``str`` whose first non-whitespace character is ``"<"`` is parsed as XML
    text; any other non-empty ``str`` or :class:`os.PathLike` is read as a file
    path. ``max_size`` bounds the input at 16 MiB by default with
    :class:`LimitError` code ``form_field_input_size`` (``None`` lifts the raw
    byte bound while keeping the 1 MiB decoded field-text cap, 32-level depth
    limit, and 4,096-field cap). ``<!DOCTYPE`` and ``<!ENTITY`` declarations
    are rejected with :class:`MalformedPdfError`.
    """
    _validate_optional_positive_int("max_size", max_size)
    raw = _read_xfdf_bytes(source, max_size)
    if not raw:
        msg = "XFDF input is empty"
        raise MalformedPdfError(msg)
    probe_bytes = raw if b"\x00" not in raw else raw.replace(b"\x00", b"")
    if _DOCTYPE_OR_ENTITY.search(probe_bytes) is not None:
        msg = "DOCTYPE and ENTITY declarations are forbidden in XFDF input"
        raise MalformedPdfError(msg)

    parser = ET.XMLPullParser(("start", "end"))
    state = _XfdfPullState()
    try:
        for start in range(0, len(raw), _XFDF_CHUNK_SIZE):
            parser.feed(raw[start : start + _XFDF_CHUNK_SIZE])
            _drain_pull_events(parser, state)
        parser.close()
        _drain_pull_events(parser, state)
    except ET.ParseError as exc:
        msg = f"malformed XFDF XML: {exc}"
        raise MalformedPdfError(msg) from exc

    if not state.saw_root:
        msg = "XFDF input contains no root <xfdf> element"
        raise MalformedPdfError(msg)
    return FdfData(fields=state.fields_out, file=state.file_spec, id=state.doc_id)


def _pdf_text_value(text: str) -> str | bytes:
    """Encode ``text`` so ASCII control characters use UTF-16BE rather than lossy PDFDocEncoding."""
    if text.isascii() and not any(ord(ch) < 32 or ord(ch) == 127 for ch in text):  # noqa: PLR2004
        return text
    return b"\xfe\xff" + text.encode("utf-16-be", "surrogatepass")


def _inherited_raw_v(field_handle: Field) -> Any:  # noqa: ANN401 - PDF object values span several types.
    """Return the field's own or inherited ``/V`` PDF object, or ``None`` when omitted."""
    node: Any = field_handle.obj
    for _ in range(_MAX_FIELD_DEPTH):
        if not isinstance(node, Dictionary):
            return None
        if "V" in node:
            val = node.get("V")
            if val is not None:
                return val
        node = node.get("Parent")
    return None


@dataclass(slots=True)
class _ExportEntry:
    """One terminal field's normalized value prepared for FDF or XFDF export."""

    parts: tuple[str, ...]
    is_button: bool
    value: str | tuple[str, ...] | None


def _resolve_selected_names(form: Form, fields: Iterable[str] | None) -> list[str]:
    """Validate ``fields`` and return sorted field names present in ``form``."""
    if fields is None:
        return list(form)
    if isinstance(fields, (str, bytes, bytearray, memoryview)) or not isinstance(fields, Iterable):
        msg = f"fields must be an iterable of field names or None: {fields!r}"
        raise TypeError(msg)
    requested: set[str] = set()
    for item in fields:
        if not isinstance(item, str):
            msg = f"field names in fields must be str: {item!r}"
            raise TypeError(msg)
        if item not in form:
            raise KeyError(item)
        requested.add(item)
    return sorted(requested)


def _normalize_export_value(
    field_handle: Field,
    *,
    is_button: bool,
) -> str | tuple[str, ...] | None:
    """Read and normalize one field's PDF ``/V`` for FDF/XFDF serialization."""
    raw_v = _inherited_raw_v(field_handle)
    if raw_v is None:
        return None
    if isinstance(raw_v, Name):
        return str(raw_v)
    if isinstance(raw_v, String):
        return raw_v.text
    if not is_button and isinstance(raw_v, Array):
        return tuple(
            item.text if isinstance(item, String) else str(item) for item in raw_v if isinstance(item, (String, Name))
        )
    return field_handle.value


def _select_export_entries(
    form: Form,
    fields: Iterable[str] | None,
    *,
    include_empty: bool,
) -> list[_ExportEntry]:
    """Validate export options and collect exportable terminal fields in sorted name order."""
    if not isinstance(include_empty, bool):
        msg = f"include_empty must be a bool: {include_empty!r}"
        raise TypeError(msg)
    selected_names = _resolve_selected_names(form, fields)

    entries: list[_ExportEntry] = []
    for name in selected_names:
        field_handle = form[name]
        kind = field_handle.type
        if kind not in _EXPORTABLE_TYPES:
            continue
        parts = tuple(name.split("."))
        if len(parts) > _MAX_FDF_DEPTH:
            code = "form_field_parent_depth"
            msg = f"field {name!r} exceeds the {_MAX_FDF_DEPTH}-level FDF/XFDF hierarchy limit"
            raise LimitError(code, msg)

        is_button = kind in {FieldType.CHECKBOX, FieldType.RADIO}
        value = _normalize_export_value(field_handle, is_button=is_button)
        if not include_empty and (value is None or value in ("", ())):
            continue
        entries.append(_ExportEntry(parts=parts, is_button=is_button, value=value))
    return entries


@dataclass(slots=True)
class _TreeNode:
    """Prefix-tree node used to build hierarchical FDF and XFDF field structures iteratively."""

    name: str
    children: dict[str, _TreeNode] = field(default_factory=dict)
    leaf: _ExportEntry | None = None


def _build_prefix_tree(entries: list[_ExportEntry]) -> list[_TreeNode]:
    """Assemble sorted ``_ExportEntry`` records into a deterministic prefix forest."""
    roots: dict[str, _TreeNode] = {}
    for entry in entries:
        first = entry.parts[0]
        node = roots.get(first)
        if node is None:
            node = _TreeNode(name=first)
            roots[first] = node
        for part in entry.parts[1:]:
            child = node.children.get(part)
            if child is None:
                child = _TreeNode(name=part)
                node.children[part] = child
            node = child
        node.leaf = entry
    return list(roots.values())


def _fdf_leaf_dict(node: _TreeNode) -> dict[str, object]:
    """Convert a leaf ``_TreeNode`` into a direct FDF field dictionary."""
    entry = node.leaf
    assert entry is not None  # noqa: S101 - every leaf node in the prefix tree came from an _ExportEntry.
    out: dict[str, object] = {"T": _pdf_text_value(node.name)}
    if entry.value is None:
        return out
    if entry.is_button:
        assert isinstance(entry.value, str)  # noqa: S101 - button export values are always str.
        out["V"] = Name(entry.value)
    elif isinstance(entry.value, tuple):
        out["V"] = [_pdf_text_value(item) for item in entry.value]
    else:
        out["V"] = _pdf_text_value(entry.value)
    return out


def _export_fdf(
    form: Form,
    *,
    fields: Iterable[str] | None = None,
    include_empty: bool = True,
    file: str | None = None,
) -> bytes:
    """Serialize ``form``'s fields as ``%FDF-1.2`` bytes with exact cross-reference offsets."""
    from pyropdf._document import Document  # noqa: PLC0415

    _unlocked(form._document)  # noqa: SLF001
    if file is not None:
        _field_text(file, "file")
    entries = _select_export_entries(form, fields, include_empty=include_empty)
    doc_id = _read_pdf_id_pair(form._document.trailer)  # noqa: SLF001
    roots = _build_prefix_tree(entries)

    with Document() as temp:
        with temp.transaction():
            post_order: list[_TreeNode] = []
            visit_stack = list(reversed(roots))
            while visit_stack:
                current = visit_stack.pop()
                post_order.append(current)
                visit_stack.extend(reversed(list(current.children.values())))

            built: dict[int, object] = {}
            for current in reversed(post_order):
                if current.children:
                    kids = [built[id(child)] for child in current.children.values()]
                    xref = temp.objects.add({"T": _pdf_text_value(current.name), "Kids": kids})
                    built[id(current)] = temp.objects[xref]
                else:
                    built[id(current)] = _fdf_leaf_dict(current)

            fdf_dict: dict[str, object] = {"Fields": [built[id(root)] for root in roots]}
            if file is not None:
                fdf_dict["F"] = _pdf_text_value(file)
            if doc_id is not None:
                fdf_dict["ID"] = [doc_id[0], doc_id[1]]
            root_xref = temp.objects.add({"FDF": fdf_dict})
            temp.trailer["Root"] = temp.objects[root_xref]

        raw = temp.to_bytes(prune=True, renumber=True, keep_id=True, cross_reference="table")
    assert raw.startswith(b"%PDF-1.7")  # noqa: S101 - empty Document saves always write %PDF-1.7 (8 bytes).
    return b"%FDF-1.2" + raw[8:]


def _check_xml_chars(text: str, label: str) -> None:
    """Refuse characters that XML 1.0 cannot represent."""
    for ch in text:
        code = ord(ch)
        valid = (
            code in {0x09, 0x0A, 0x0D}
            or (0x20 <= code <= 0xD7FF)  # noqa: PLR2004
            or (0xE000 <= code <= 0xFFFD)  # noqa: PLR2004
            or (0x10000 <= code <= 0x10FFFF)  # noqa: PLR2004
        )
        if not valid:
            msg = f"XFDF XML 1.0 cannot represent character U+{code:04X} in {label}"
            raise PdfError(msg)


def _escape_xml_text(text: str, label: str) -> str:
    r"""Escape character data for ``<value>...</value>``, preserving ``\r`` via ``&#xD;``."""
    _check_xml_chars(text, label)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#xD;")


def _escape_xml_attr(text: str, label: str) -> str:
    """Escape an XML attribute value, preserving whitespace characters against normalization."""
    _check_xml_chars(text, label)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\t", "&#x9;")
        .replace("\n", "&#xA;")
        .replace("\r", "&#xD;")
    )


def _append_xfdf_leaf(lines: list[str], node: _TreeNode, escaped_name: str, indent_level: int) -> None:
    """Append the XML lines of one terminal ``<field>`` element."""
    pad = "  " * indent_level
    entry = node.leaf
    assert entry is not None  # noqa: S101
    if entry.value is None or entry.value == ():
        lines.append(f'{pad}<field name="{escaped_name}"/>')
    elif isinstance(entry.value, tuple):
        lines.append(f'{pad}<field name="{escaped_name}">')
        val_pad = "  " * (indent_level + 1)
        lines.extend(f"{val_pad}<value>{_escape_xml_text(item, 'field value')}</value>" for item in entry.value)
        lines.append(f"{pad}</field>")
    else:
        escaped_val = _escape_xml_text(entry.value, "field value")
        lines.append(f'{pad}<field name="{escaped_name}"><value>{escaped_val}</value></field>')


def _export_xfdf(
    form: Form,
    *,
    fields: Iterable[str] | None = None,
    include_empty: bool = True,
    file: str | None = None,
) -> str:
    """Serialize ``form``'s fields as an ISO 19444-1 XFDF XML string."""
    _unlocked(form._document)  # noqa: SLF001
    if file is not None:
        _field_text(file, "file")
    entries = _select_export_entries(form, fields, include_empty=include_empty)
    doc_id = _read_pdf_id_pair(form._document.trailer)  # noqa: SLF001
    roots = _build_prefix_tree(entries)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<xfdf xmlns="{_XFDF_NS}" xml:space="preserve">',
    ]
    if file is not None:
        lines.append(f'  <f href="{_escape_xml_attr(file, "file")}"/>')
    if doc_id is not None:
        lines.append(f'  <ids original="{doc_id[0].hex().upper()}" modified="{doc_id[1].hex().upper()}"/>')

    if not roots:
        lines.append("  <fields/>")
    else:
        lines.append("  <fields>")
        stack: list[tuple[bool, _TreeNode, int]] = [(True, root, 2) for root in reversed(roots)]
        while stack:
            entering, node, indent_level = stack.pop()
            pad = "  " * indent_level
            if not entering:
                lines.append(f"{pad}</field>")
            elif node.children:
                lines.append(f'{pad}<field name="{_escape_xml_attr(node.name, "field name")}">')
                stack.append((False, node, indent_level))
                stack.extend((True, child, indent_level + 1) for child in reversed(list(node.children.values())))
            else:
                _append_xfdf_leaf(lines, node, _escape_xml_attr(node.name, "field name"), indent_level)
        lines.append("  </fields>")

    lines.append("</xfdf>\n")
    return "\n".join(lines)


def _redraw_text_or_choice_field(document: Document, field_handle: Field) -> None:
    """Redraw ``field_handle``'s widgets from its current ``/V`` using Standard 14 or ``Form.font``."""
    field_handle._edit()  # noqa: SLF001
    font = document._form_font  # noqa: SLF001
    if font is None:
        return
    core = field_handle._core()  # noqa: SLF001
    face = font._font  # noqa: SLF001
    drawn = _core.form_field_draw_face(core, field_handle._id, face)  # noqa: SLF001
    xref, mapped = 0, {}
    if drawn:
        xref = embedded_font(document, font, _cache(document).fonts)
        mapped = add_glyphs(document, xref, font, drawn)
    _core.form_field_draw_face(core, field_handle._id, face, (xref, mapped))  # noqa: SLF001


def _clear_field_value(document: Document, field_handle: Field) -> None:
    """Remove ``/V`` (and ``/I`` on choice fields) and reset widget appearances."""
    kind = field_handle.type
    if kind in {FieldType.CHECKBOX, FieldType.RADIO}:
        field_handle.value = False
        obj = field_handle.obj
        if "V" in obj:
            del obj["V"]
        return
    obj = field_handle.obj
    if "V" in obj:
        del obj["V"]
    if "I" in obj:
        del obj["I"]
    _redraw_text_or_choice_field(document, field_handle)


def _apply_multi_select(document: Document, field_handle: Field, values: tuple[str, ...]) -> None:
    """Set array ``/V`` and ascending ``/I`` on a ``MULTI_SELECT`` listbox and redraw its widgets."""
    if len(values) > _MAX_FIELD_CHOICES:
        msg = f"a choice field accepts at most {_MAX_FIELD_CHOICES} selected values"
        raise PdfError(msg)
    total_bytes = sum(_utf8_len(item) for item in values) + max(0, len(values) - 1) * 2
    if total_bytes > _MAX_FIELD_INPUT_SIZE:
        code = "form_field_input_size"
        msg = f"choice field values exceed the {_MAX_FIELD_INPUT_SIZE}-byte UTF-8 limit"
        raise LimitError(code, msg)

    selected = set(values)
    indices = [idx for idx, choice in enumerate(field_handle.choices) if choice.value in selected]
    obj = field_handle.obj
    obj["V"] = [_pdf_text_value(item) for item in values]
    if indices:
        obj["I"] = indices
    elif "I" in obj:
        del obj["I"]
    _redraw_text_or_choice_field(document, field_handle)


def _apply_field_value(
    document: Document,
    field_handle: Field,
    raw_value: str | tuple[str, ...] | None,
) -> None:
    """Apply one parsed FDF/XFDF value to ``field_handle`` inside the caller's transaction."""
    if raw_value is None:
        _clear_field_value(document, field_handle)
        return

    kind = field_handle.type
    if isinstance(raw_value, tuple):
        if not raw_value:
            _clear_field_value(document, field_handle)
            return
        if kind is FieldType.LISTBOX and FieldFlags.MULTI_SELECT in field_handle.flags:
            _apply_multi_select(document, field_handle, raw_value)
            return
        if len(raw_value) == 1:
            raw_value = raw_value[0]
        else:
            msg = f"field {field_handle.name!r} ({kind.value}) does not accept multiple values"
            raise PdfError(msg)

    if kind in {FieldType.CHECKBOX, FieldType.RADIO}:
        if raw_value in ("", "Off"):
            field_handle.value = False
        else:
            field_handle.value = raw_value
        return

    if kind in {FieldType.COMBOBOX, FieldType.LISTBOX}:
        obj = field_handle.obj
        if "I" in obj:
            del obj["I"]
    field_handle.value = raw_value


def _apply_form_data(form: Form, data: FdfData, step_name: str) -> list[str]:
    """Apply matching fillable fields of ``data`` to ``form`` in one atomic undoable operation."""
    document = form._document  # noqa: SLF001
    core = _unlocked(document)
    targets: list[tuple[str, Field, str | tuple[str, ...] | None]] = []
    for name in form:
        if name not in data.fields:
            continue
        field_handle = form[name]
        if field_handle.type not in _EXPORTABLE_TYPES:
            continue
        targets.append((name, field_handle, data.fields[name]))

    if not targets:
        return []

    operation = _core.begin_operation(core, step_name)
    try:
        for _, field_handle, raw_value in targets:
            _apply_field_value(document, field_handle, raw_value)
    except BaseException:
        _core.abort_canvas(core, operation)
        raise
    _core.end_transaction(core, True)  # noqa: FBT003 - the core's commit flag.
    return [name for name, _, _ in targets]


for _public in (FdfData, parse_fdf, parse_xfdf):
    _public.__module__ = "pyropdf"

__all__ = ["FdfData", "parse_fdf", "parse_xfdf"]
