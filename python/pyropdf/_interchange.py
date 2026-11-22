"""Document interchange: developer extensions, associated files, page-piece data, and document parts.

Implements high-level handles and specifications on one versioned object store
for ISO 32000-2 §7.12 (``/Extensions``), §14.5 (``/PieceInfo``), §14.12
(``/DPartRoot`` and ``/DPart``), §12.6.4.5 (``/GoToDp`` actions), and §14.13
(``/AF`` associated files). Every traversal is iterative and bounded with
cycle detection (DESIGN.md §3 F6), and every mutation runs in one atomic
transaction while keeping Arlington's structural invariants clean.
"""

from __future__ import annotations

import enum
import itertools
import math
import secrets
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast, overload

from pyropdf import _core
from pyropdf._errors import LimitError, PdfError, StaleObjectError
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._metadata import _require_pdf20, _require_version, pdf_date
from pyropdf._objects import (
    Array,
    Dictionary,
    Name,
    Real,
    Stream,
    String,
    _catalog,
    _encode_text_string,
    _Handle,
)
from pyropdf._profiles import pdfa_claim

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._page import Page

_DEFAULT_VERSION = (1, 7)
_DEFAULT_MAX_AF_SIZE = 64 * 1024 * 1024
_MAX_INPUT_TEXT_SIZE = 1024 * 1024
_MAX_URL_LENGTH = 4096
_MAX_REVISION_LENGTH = 1024
_MAX_NAME_LENGTH = 127
_MAX_DATE_LENGTH = 64
_MAX_EXTENSION_PREFIXES = 1024
_MAX_EXTENSIONS_PER_PREFIX = 1024
_MAX_AF_ENTRIES = 4096
_MAX_PIECE_ENTRIES = 1024
_MAX_PRIVATE_DEPTH = 32
_MAX_PRIVATE_ENTRIES = 4096
_MAX_DPART_DEPTH = 64
_MAX_DPART_NODES = 16_384
_MAX_NODE_NAMES = 1024
_MAX_NAVIGATION_STEPS = 4096
_TRAILER_ID_BYTES = 16

_CODE_EMBEDDED_INPUT = "embedded_file_input_size"
_CODE_EMBEDDED_SIZE = "embedded_file_size"
_CODE_OBJECT_COUNT = "object_count"

_PDF_DELIMITERS = frozenset("()<>[]{}/%")
_AF_PDF17_RELATIONSHIPS = frozenset({"Source", "Data", "Alternative", "Supplement", "Unspecified"})

#: Canonical ``(url, extension_revision)`` metadata for registered ISO/TS extensions (ISO 32000-2 §7.12).
ISO_EXTENSIONS: Mapping[int, tuple[str, str]] = MappingProxyType(
    {
        32001: ("https://www.iso.org/standard/45874.html", ":2022"),
        32002: ("https://www.iso.org/standard/45875.html", ":2022"),
        32003: ("https://www.iso.org/standard/45876.html", ":2023"),
        32004: ("https://www.iso.org/standard/45877.html", ":2024"),
        24064: ("https://www.iso.org/standard/77686.html", "1:2023"),
        32007: ("https://www.iso.org/standard/45880.html", "1:2024"),
    }
)


def _is_non_bool_int(value: object) -> bool:
    """Return whether ``value`` is an ``int`` and not a ``bool``."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_dict(value: object) -> bool:
    """Return whether ``value`` is a non-stream PDF ``Dictionary``."""
    return isinstance(value, Dictionary) and not isinstance(value, Stream)


def _indirect_xref(value: object) -> int | None:
    """Return the indirect object number of ``value``, or ``None``."""
    if isinstance(value, _Handle) and value.is_indirect:
        return value.xref
    return None


def _dict_pair(value: object) -> tuple[int, int] | None:
    """Return ``(xref, generation)`` for an indirect ``Dictionary``, else ``None``."""
    if _is_dict(value):
        d = cast("Dictionary", value)
        if d.is_indirect and d.xref is not None and d.generation is not None:
            return (d.xref, d.generation)
    return None


def _parse_version(text: str) -> tuple[int, int] | None:
    major, _, minor = text.partition(".")
    return (int(major), int(minor)) if major.isdigit() and minor.isdigit() else None


def _effective_version(document: Document, catalog: Dictionary) -> tuple[int, int]:
    """Return the effective PDF version from the file header and ``Catalog['Version']``."""
    header = _core.header_version(document._core())  # noqa: SLF001
    version = (_parse_version(header) if header is not None else None) or _DEFAULT_VERSION
    declared = catalog.get("Version")
    if isinstance(declared, Name):
        version = max(version, _parse_version(str(declared)) or version)
    return version


def _ensure_trailer_id(document: Document) -> None:
    """Ensure ``trailer['ID']`` holds two 16-byte strings as PDF 2.0 requires."""
    if "ID" not in document.trailer:
        file_id = secrets.token_bytes(_TRAILER_ID_BYTES)
        document.trailer["ID"] = [file_id, file_id]


def _require_interchange_version(document: Document, catalog: Dictionary, minimum: tuple[int, int]) -> None:
    """Promote ``catalog['Version']`` to ``minimum`` and refuse PDF 2.0 promotions that create defects."""
    if minimum >= (2, 0):
        _require_pdf20(document, catalog, "PDF 2.0 interchange")
        return
    _require_version(document, catalog, minimum)
    if _effective_version(document, catalog) >= (2, 0):
        _ensure_trailer_id(document)


def _has_catalog_extension(catalog: Dictionary, prefix: str) -> bool:
    """Return whether ``Catalog['Extensions']`` declares ``prefix``."""
    ext = catalog.get("Extensions")
    if not _is_dict(ext):
        return False
    entry = cast("Dictionary", ext).get(prefix)
    return _is_dict(entry) or (isinstance(entry, Array) and len(entry) > 0)


def _now_pdf_date() -> str:
    """Return the current UTC timestamp formatted as a PDF date string."""
    return pdf_date(datetime.now(UTC))


def _is_pdf_date(value: str) -> bool:
    return (
        len("D:") < str.__len__(value) <= _MAX_DATE_LENGTH
        and str.startswith(value, "D:")
        and str.isascii(value)
        and str.isprintable(value)
    )


def _validate_pdf_date(value: object, label: str, *, allow_datetime: bool = False) -> str:
    if allow_datetime and isinstance(value, datetime):
        return pdf_date(value)
    if not isinstance(value, str):
        expected = "a PDF date string or datetime" if allow_datetime else "a PDF date string"
        msg = f"{label} must be {expected}: {type(value).__name__}"
        raise TypeError(msg)
    plain = str.__str__(value)
    if not _is_pdf_date(plain):
        msg = f"{label} must be a PDF date string such as 'D:20260101120000Z': {value!r}"
        raise ValueError(msg)
    return plain


def _validate_pdf_name(value: object, label: str) -> str:
    """Validate an ASCII non-delimiter PDF name up to 127 bytes."""
    if not isinstance(value, str):
        msg = f"{label} must be a str: {type(value).__name__}"
        raise TypeError(msg)
    plain = str.__str__(value)
    if not 1 <= len(plain) <= _MAX_NAME_LENGTH:
        msg = f"{label} must be 1 to {_MAX_NAME_LENGTH} characters: {value!r}"
        raise ValueError(msg)
    if any(not ("!" <= ch <= "~") or ch in _PDF_DELIMITERS for ch in plain):
        msg = f"{label} must use printable non-delimiter ASCII characters: {value!r}"
        raise ValueError(msg)
    return plain


def _validate_media_type(value: object) -> str:
    """Validate an associated file MIME media type stored as a PDF ``Name``."""
    if not isinstance(value, str):
        msg = f"media_type must be a str: {type(value).__name__}"
        raise TypeError(msg)
    plain = str.__str__(value)
    if not 1 <= len(plain) <= _MAX_NAME_LENGTH or plain.startswith("/"):
        msg = f"media_type must be 1 to {_MAX_NAME_LENGTH} characters without a leading '/': {value!r}"
        raise ValueError(msg)
    if any(not ("!" <= ch <= "~") or ch in "()<>[]{}%" for ch in plain):
        msg = f"media_type must use printable ASCII MIME characters: {value!r}"
        raise ValueError(msg)
    return plain


def _validate_text_size(texts: Sequence[str], label: str) -> None:
    """Refuse texts whose combined UTF-8 byte length exceeds 1 MiB."""
    size = 0
    for text in texts:
        if str.__len__(text) > _MAX_INPUT_TEXT_SIZE - size:
            msg = f"the {label} exceeds the {_MAX_INPUT_TEXT_SIZE}-byte UTF-8 limit"
            raise LimitError(_CODE_EMBEDDED_INPUT, msg)
        size += len(str.encode(text, "utf-8", "surrogatepass"))
        if size > _MAX_INPUT_TEXT_SIZE:
            msg = f"the {label} exceeds the {_MAX_INPUT_TEXT_SIZE}-byte UTF-8 limit"
            raise LimitError(_CODE_EMBEDDED_INPUT, msg)


def _validate_bytes_payload(data: object, max_size: int | None) -> bytes:
    """Validate a bytes-like payload against ``max_size`` via ``memoryview``."""
    _validate_optional_positive_int("max_size", max_size)
    try:
        view = memoryview(data)  # ty: ignore[invalid-argument-type]
    except TypeError:
        msg = f"data must be a bytes-like object: {type(data).__name__}"
        raise TypeError(msg) from None
    with view:
        if max_size is not None and view.nbytes > max_size:
            msg = f"associated file input is {view.nbytes} bytes, exceeding the {max_size}-byte limit"
            raise LimitError(_CODE_EMBEDDED_SIZE, msg)
        return view.tobytes()


def _resolve_seq_index(index: object, count: int, label: str) -> int:
    if not _is_non_bool_int(index):
        msg = f"{label} must be an int, not {type(index).__name__}"
        raise TypeError(msg)
    assert isinstance(index, int)  # noqa: S101 - narrows type for ty.
    resolved = index + count if index < 0 else index
    if not 0 <= resolved < count:
        msg = f"{label} {index} is out of range for {count} items"
        raise IndexError(msg)
    return resolved


# ---------------------------------------------------------------------------
# 1. Developer and ISO Extensions (§7.12, developer-extensions)
# ---------------------------------------------------------------------------


def _validate_extension_url(url: str | None) -> str | None:
    if url is None:
        return None
    if not isinstance(url, str):
        msg = f"url must be a str or None: {type(url).__name__}"
        raise TypeError(msg)
    plain = str.__str__(url)
    if not plain or len(plain) > _MAX_URL_LENGTH or not plain.isascii() or not plain.isprintable():
        msg = f"url must be a non-empty printable ASCII string of at most {_MAX_URL_LENGTH} characters: {url!r}"
        raise ValueError(msg)
    return plain


def _validate_extension_revision(rev: str | None) -> str | None:
    if rev is None:
        return None
    if not isinstance(rev, str):
        msg = f"extension_revision must be a str or None: {type(rev).__name__}"
        raise TypeError(msg)
    plain = str.__str__(rev)
    if not plain or len(plain) > _MAX_REVISION_LENGTH:
        msg = f"extension_revision must be a non-empty string of at most {_MAX_REVISION_LENGTH} characters: {rev!r}"
        raise ValueError(msg)
    return plain


def _resolve_iso_extension_meta(
    base_version: str,
    level: int,
    url: str | None,
    rev: str | None,
) -> tuple[str, str]:
    if base_version != "2.0":
        msg = f"ISO_ extensions require base_version '2.0': {base_version!r}"
        raise ValueError(msg)
    canonical = ISO_EXTENSIONS.get(level)
    if canonical is None:
        msg = f"ISO_ extension_level must be one of {sorted(ISO_EXTENSIONS)}: {level}"
        raise ValueError(msg)
    canon_url, canon_rev = canonical
    resolved_url = canon_url if url is None else url
    if resolved_url != canon_url:
        msg = f"ISO_ {level} url must be {canon_url!r}, got {resolved_url!r}"
        raise ValueError(msg)
    resolved_rev = canon_rev if rev is None else rev
    if resolved_rev != canon_rev:
        msg = f"ISO_ {level} extension_revision must be {canon_rev!r}, got {resolved_rev!r}"
        raise ValueError(msg)
    return resolved_url, resolved_rev


@dataclass(frozen=True, slots=True)
class DeveloperExtension:
    """A developer or ISO extension entry in ``Catalog['Extensions']`` (ISO 32000-2 §7.12).

    ``prefix`` is the registered developer prefix (or ``"ISO_"`` for ISO
    technical specifications in :data:`ISO_EXTENSIONS`), ``base_version`` is
    ``"1.7"`` or ``"2.0"``, and ``extension_level`` identifies the extension
    number. ``url`` and ``extension_revision`` record PDF 2.0 extension
    metadata and are populated or checked automatically for ``"ISO_"`` entries.
    """

    prefix: str
    base_version: str
    extension_level: int
    url: str | None = None
    extension_revision: str | None = None

    def __post_init__(self) -> None:
        """Validate extension fields and apply canonical ``ISO_`` metadata."""
        checked_prefix = _validate_pdf_name(self.prefix, "prefix")
        if checked_prefix == "Type":
            msg = "prefix cannot be 'Type'"
            raise ValueError(msg)
        if not isinstance(self.base_version, str):
            msg = f"base_version must be '1.7' or '2.0': {type(self.base_version).__name__}"
            raise TypeError(msg)
        checked_base = str.__str__(self.base_version)
        if checked_base not in {"1.7", "2.0"}:
            msg = f"base_version must be '1.7' or '2.0': {self.base_version!r}"
            raise ValueError(msg)
        if not _is_non_bool_int(self.extension_level):
            msg = f"extension_level must be an int: {type(self.extension_level).__name__}"
            raise TypeError(msg)
        checked_level = int(self.extension_level)
        checked_url = _validate_extension_url(self.url)
        checked_rev = _validate_extension_revision(self.extension_revision)
        if checked_prefix == "ISO_":
            checked_url, checked_rev = _resolve_iso_extension_meta(
                checked_base,
                checked_level,
                checked_url,
                checked_rev,
            )

        object.__setattr__(self, "prefix", checked_prefix)
        object.__setattr__(self, "base_version", checked_base)
        object.__setattr__(self, "extension_level", checked_level)
        object.__setattr__(self, "url", checked_url)
        object.__setattr__(self, "extension_revision", checked_rev)


def _parse_dev_extension_dict(prefix: str, entry: Dictionary) -> DeveloperExtension | None:
    raw_base = entry.get("BaseVersion")
    raw_level = entry.get("ExtensionLevel")
    if not isinstance(raw_base, Name) or not _is_non_bool_int(raw_level):
        return None
    base_str = str(raw_base)
    level = int(cast("int", raw_level))
    raw_url = entry.get("URL")
    url = raw_url.text if isinstance(raw_url, String) else None
    raw_rev = entry.get("ExtensionRevision")
    rev = raw_rev.text if isinstance(raw_rev, String) else None
    try:
        return DeveloperExtension(
            prefix=prefix,
            base_version=base_str,
            extension_level=level,
            url=url,
            extension_revision=rev,
        )
    except ValueError:
        return None


def _parse_prefix_extensions(prefix: str, val: object) -> list[DeveloperExtension]:
    if _is_dict(val):
        item = _parse_dev_extension_dict(prefix, cast("Dictionary", val))
        return [] if item is None else [item]
    if isinstance(val, Array):
        if len(val) > _MAX_EXTENSIONS_PER_PREFIX:
            msg = f"Extensions[{prefix!r}] exceeds {_MAX_EXTENSIONS_PER_PREFIX} entries"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        return [
            item
            for elem in val
            if _is_dict(elem) and (item := _parse_dev_extension_dict(prefix, cast("Dictionary", elem))) is not None
        ]
    return []


def _read_extensions_map(document: Document) -> dict[str, tuple[DeveloperExtension, ...]]:
    raw = _catalog(document).get("Extensions")
    if not _is_dict(raw):
        return {}
    ext_dict = cast("Dictionary", raw)
    result: dict[str, tuple[DeveloperExtension, ...]] = {}
    count = 0
    for key in ext_dict:
        if key == "Type":
            continue
        count += 1
        if count > _MAX_EXTENSION_PREFIXES:
            msg = f"Catalog['Extensions'] exceeds {_MAX_EXTENSION_PREFIXES} prefixes"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        parsed = _parse_prefix_extensions(str(key), ext_dict.get(key))
        if parsed:
            result[str(key)] = tuple(parsed)
    return result


def _serialize_extension_entry(ext: DeveloperExtension) -> dict[str, object]:
    """Serialize one :class:`DeveloperExtension` as a strictly direct dictionary."""
    entry: dict[str, object] = {
        "Type": Name("DeveloperExtensions"),
        "BaseVersion": Name(ext.base_version),
        "ExtensionLevel": ext.extension_level,
    }
    if ext.url is not None:
        entry["URL"] = ext.url
    if ext.extension_revision is not None:
        entry["ExtensionRevision"] = ext.extension_revision
    return entry


def _existing_extension_raw_entries(old_ext: object) -> dict[tuple[str, DeveloperExtension], dict[str, object]]:
    """Index existing direct extension dictionaries by ``(prefix, parsed)`` to preserve foreign keys."""
    if not _is_dict(old_ext):
        return {}
    ext_dict = cast("Dictionary", old_ext)
    existing: dict[tuple[str, DeveloperExtension], dict[str, object]] = {}
    for key in ext_dict:
        prefix = str(key)
        if prefix == "Type":
            continue
        val = ext_dict.get(key)
        raw_dicts = [val] if _is_dict(val) else ([e for e in val if _is_dict(e)] if isinstance(val, Array) else [])
        for raw_d in raw_dicts:
            entry = cast("Dictionary", raw_d)
            parsed = _parse_dev_extension_dict(prefix, entry)
            if parsed is not None and (prefix, parsed) not in existing:
                existing[prefix, parsed] = {str(k): entry[k] for k in entry}
    return existing


def _write_extensions_grouped(
    document: Document,
    grouped: Mapping[str, Sequence[DeveloperExtension]],
    *,
    preserve_raw: bool = True,
) -> None:
    """Write ``grouped`` directly onto ``Catalog['Extensions']``, or remove it when empty."""
    catalog = _catalog(document)
    retired: list[int] = []
    old_ext = catalog.get("Extensions")
    if (old_xref := _indirect_xref(old_ext)) is not None:
        retired.append(old_xref)

    non_empty = {prefix: list(items) for prefix, items in grouped.items() if items}
    if not non_empty:
        if "Extensions" in catalog:
            del catalog["Extensions"]
        if retired:
            _core.object_free_unreferenced(document._core(), retired)  # noqa: SLF001
        return

    preserved = _existing_extension_raw_entries(old_ext) if preserve_raw else {}
    minimum = (1, 7)
    direct_map: dict[str, object] = {}
    for prefix, items in non_empty.items():
        if len(items) >= 2:  # noqa: PLR2004 - ArrayOfDevExtensions requires PDF 2.0.
            minimum = (2, 0)
            direct_map[prefix] = [preserved.get((prefix, item)) or _serialize_extension_entry(item) for item in items]
        else:
            only = items[0]
            if (
                only.prefix == "ISO_"
                or only.base_version == "2.0"
                or only.url is not None
                or only.extension_revision is not None
            ):
                minimum = (2, 0)
            direct_map[prefix] = preserved.get((prefix, only)) or _serialize_extension_entry(only)

    _require_interchange_version(document, catalog, minimum)
    catalog["Extensions"] = direct_map
    if retired:
        _core.object_free_unreferenced(document._core(), retired)  # noqa: SLF001


def _replace_extensions(document: Document, extensions: Iterable[DeveloperExtension]) -> None:
    if isinstance(extensions, (str, bytes)) or not isinstance(extensions, Iterable):
        msg = f"extensions must be an iterable of DeveloperExtension values: {type(extensions).__name__}"
        raise TypeError(msg)
    grouped: dict[str, list[DeveloperExtension]] = {}
    for total, item in enumerate(extensions, start=1):
        if not isinstance(item, DeveloperExtension):
            msg = f"extensions items must be DeveloperExtension values: {type(item).__name__}"
            raise TypeError(msg)
        if total > _MAX_EXTENSION_PREFIXES * _MAX_EXTENSIONS_PER_PREFIX:
            msg = "extensions exceeds the maximum entry count"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        bucket = grouped.setdefault(item.prefix, [])
        if item not in bucket:
            if len(bucket) >= _MAX_EXTENSIONS_PER_PREFIX:
                msg = f"Extensions[{item.prefix!r}] exceeds {_MAX_EXTENSIONS_PER_PREFIX} entries"
                raise LimitError(_CODE_OBJECT_COUNT, msg)
            bucket.append(item)
    if len(grouped) > _MAX_EXTENSION_PREFIXES:
        msg = f"Catalog['Extensions'] exceeds {_MAX_EXTENSION_PREFIXES} prefixes"
        raise LimitError(_CODE_OBJECT_COUNT, msg)
    with document.transaction("extensions"):
        _write_extensions_grouped(document, grouped, preserve_raw=False)


def _delete_extensions(document: Document) -> None:
    catalog = _catalog(document)
    if "Extensions" not in catalog:
        return
    with document.transaction("extensions"):
        _write_extensions_grouped(document, {})


class Extensions(Mapping[str, tuple[DeveloperExtension, ...]]):
    """Live container for ``Catalog['Extensions']`` keyed by developer prefix (ISO 32000-2 §7.12).

    Every dictionary and array written under ``Catalog['Extensions']`` is stored
    directly as Arlington's ``fn:MustBeDirect()`` requires. Indexing by prefix
    returns a tuple of :class:`DeveloperExtension` records, :meth:`all` returns
    every declared extension in order, and removing the last entry deletes
    ``Catalog['Extensions']``.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap ``document``; use :attr:`Document.extensions`."""
        self._document = document

    def _entries(self) -> dict[str, tuple[DeveloperExtension, ...]]:
        self._document._ensure_open()  # noqa: SLF001
        return _read_extensions_map(self._document)

    def __getitem__(self, prefix: str) -> tuple[DeveloperExtension, ...]:
        """Return the extensions registered under ``prefix``, or raise :class:`KeyError`."""
        if not isinstance(prefix, str):
            raise KeyError(prefix)
        return self._entries()[prefix]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the declared developer prefixes in dictionary order."""
        return iter(list(self._entries()))

    def __len__(self) -> int:
        """Return the number of declared developer prefixes."""
        return len(self._entries())

    def __contains__(self, item: object) -> bool:
        """Return whether ``item`` is a declared prefix string or :class:`DeveloperExtension`."""
        entries = self._entries()
        if isinstance(item, str):
            return item in entries
        if isinstance(item, DeveloperExtension):
            return item in entries.get(item.prefix, ())
        return False

    def __bool__(self) -> bool:
        """Return whether the document declares at least one developer extension."""
        return bool(self._entries())

    def all(self) -> tuple[DeveloperExtension, ...]:
        """Return all declared :class:`DeveloperExtension` entries across every prefix."""
        return tuple(ext for group in self._entries().values() for ext in group)

    def add(self, extension: DeveloperExtension) -> None:
        """Add ``extension`` under its prefix in one transaction."""
        if not isinstance(extension, DeveloperExtension):
            msg = f"extension must be a DeveloperExtension: {type(extension).__name__}"
            raise TypeError(msg)
        self._document._ensure_open()  # noqa: SLF001
        with self._document.transaction("add extension"):
            current = {k: list(v) for k, v in _read_extensions_map(self._document).items()}
            bucket = current.setdefault(extension.prefix, [])
            if extension in bucket:
                return
            if len(current) > _MAX_EXTENSION_PREFIXES:
                msg = f"Catalog['Extensions'] exceeds {_MAX_EXTENSION_PREFIXES} prefixes"
                raise LimitError(_CODE_OBJECT_COUNT, msg)
            if len(bucket) >= _MAX_EXTENSIONS_PER_PREFIX:
                msg = f"Extensions[{extension.prefix!r}] exceeds {_MAX_EXTENSIONS_PER_PREFIX} entries"
                raise LimitError(_CODE_OBJECT_COUNT, msg)
            bucket.append(extension)
            _write_extensions_grouped(self._document, current)

    def remove(self, extension: DeveloperExtension) -> None:
        """Remove ``extension`` in one transaction, or raise :class:`ValueError` when absent."""
        if not isinstance(extension, DeveloperExtension):
            msg = f"extension must be a DeveloperExtension: {type(extension).__name__}"
            raise TypeError(msg)
        self._document._ensure_open()  # noqa: SLF001
        with self._document.transaction("remove extension"):
            current = {k: list(v) for k, v in _read_extensions_map(self._document).items()}
            bucket = current.get(extension.prefix)
            if bucket is None or extension not in bucket:
                msg = f"extension is not present: {extension!r}"
                raise ValueError(msg)
            bucket.remove(extension)
            if not bucket:
                del current[extension.prefix]
            _write_extensions_grouped(self._document, current)

    def __delitem__(self, prefix: str) -> None:
        """Remove all extensions under ``prefix`` in one transaction, or raise :class:`KeyError`."""
        if not isinstance(prefix, str):
            raise KeyError(prefix)
        self._document._ensure_open()  # noqa: SLF001
        with self._document.transaction("delete extension prefix"):
            current = {k: list(v) for k, v in _read_extensions_map(self._document).items()}
            if prefix not in current:
                raise KeyError(prefix)
            del current[prefix]
            _write_extensions_grouped(self._document, current)

    def clear(self) -> None:
        """Remove ``Catalog['Extensions']`` in one transaction."""
        self._document._ensure_open()  # noqa: SLF001
        _delete_extensions(self._document)

    def __repr__(self) -> str:
        """Return ``Extensions([...])`` with all declared extensions."""
        return f"Extensions({list(self.all())!r})"


# ---------------------------------------------------------------------------
# 2. Associated Files (§14.13, associated-files)
# ---------------------------------------------------------------------------


class AssociatedFileRelationship(enum.StrEnum):
    """Semantic relationship of an associated file to its host object (``/AFRelationship``, Table 44)."""

    SOURCE = "Source"
    """Original source content from which the PDF content was derived."""
    DATA = "Data"
    """Dataset used to derive visual or tabular presentation in the PDF."""
    ALTERNATIVE = "Alternative"
    """Alternative representation of the PDF content."""
    SUPPLEMENT = "Supplement"
    """Supplemental information accompanying the host object."""
    ENCRYPTED_PAYLOAD = "EncryptedPayload"
    """Encrypted PDF payload wrapped by an unencrypted cover document (PDF 2.0)."""
    FORM_DATA = "FormData"
    """Form data associated with interactive form fields (PDF 2.0)."""
    SCHEMA = "Schema"
    """Schema definition governing metadata or structure elements (PDF 2.0)."""
    UNSPECIFIED = "Unspecified"
    """Relationship not known or not covered by another standard name."""


def _validate_af_relationship(value: object) -> str:
    if isinstance(value, AssociatedFileRelationship):
        return value.value
    return _validate_pdf_name(value, "relationship")


def _encode_ascii_filename(filename: str) -> bytes:
    """Encode ``filename`` into an ASCII byte string suitable for ``/F``."""
    encoded = filename.encode("ascii", "replace")
    return encoded or b"_"


def _embedded_stream_of(filespec: Dictionary) -> Stream | None:
    ef = filespec.get("EF")
    if not _is_dict(ef):
        return None
    ef_dict = cast("Dictionary", ef)
    for key in ("UF", "F"):
        stream = ef_dict.get(key)
        if isinstance(stream, Stream):
            return stream
    return None


def _af_object_xrefs(filespec: object) -> list[int]:
    """Collect indirect object numbers owned by an associated file specification."""
    if not _is_dict(filespec):
        return []
    spec = cast("Dictionary", filespec)
    xrefs: list[int] = []
    if spec.is_indirect and spec.xref is not None:
        xrefs.append(spec.xref)
    ef = spec.get("EF")
    if _is_dict(ef):
        ef_dict = cast("Dictionary", ef)
        if ef_dict.is_indirect and ef_dict.xref is not None:
            xrefs.append(ef_dict.xref)
        for key in ef_dict:
            stream = ef_dict.get(key)
            if isinstance(stream, Stream) and stream.xref is not None:
                xrefs.append(stream.xref)
                params_xref = _indirect_xref(stream.get("Params"))
                if params_xref is not None:
                    xrefs.append(params_xref)
    return xrefs


def _promote_for_af(
    document: Document,
    *,
    relationship: str,
    host_requires_2_0: bool,
) -> None:
    """Promote ``document`` to PDF 2.0 (or 1.7 when PDF/A-3 already covers the host and relationship)."""
    catalog = _catalog(document)
    if not host_requires_2_0 and relationship in _AF_PDF17_RELATIONSHIPS:
        claim = pdfa_claim(document)
        has_pdfa3 = (claim is not None and claim[0] == 3) or _has_catalog_extension(catalog, "ISO_19005_3")  # noqa: PLR2004
        if has_pdfa3:
            _require_interchange_version(document, catalog, (1, 7))
            return
    _require_interchange_version(document, catalog, (2, 0))


class AssociatedFile:
    """Live handle on an ``AFFileSpecification`` dictionary (ISO 32000-2 §14.13).

    Properties read the current version of the document, and assigning
    :attr:`filename`, :attr:`data`, :attr:`media_type`, :attr:`relationship`,
    :attr:`description`, :attr:`created`, or :attr:`modified` edits the file
    specification or embedded stream in one transaction. Once removed from its
    host, the handle raises :class:`StaleObjectError`, and works again if undo
    restores it.
    """

    __slots__ = ("_document", "_host_getter", "_host_requires_2_0", "_id", "_index", "_single_key")

    def __init__(  # noqa: PLR0913 - internal constructor binding host and object identity.
        self,
        document: Document,
        host_getter: Callable[[], Dictionary],
        item_id: tuple[int, int],
        index: int = 0,
        *,
        host_requires_2_0: bool = False,
        single_key: str | None = None,
    ) -> None:
        """Bind an associated file inside its host container."""
        self._document = document
        self._host_getter = host_getter
        self._id = item_id
        self._index = index
        self._host_requires_2_0 = host_requires_2_0
        self._single_key = single_key

    def _filespec(self) -> Dictionary:
        self._document._ensure_open()  # noqa: SLF001
        host = self._host_getter()
        if self._single_key is not None:
            single = host.get(self._single_key)
            if not _is_dict(single):
                msg = "associated file no longer exists on its host"
                raise StaleObjectError(msg)
            spec = cast("Dictionary", single)
            if self._id[0] and _dict_pair(spec) != self._id:
                msg = f"associated file {self._id[0]} {self._id[1]} R no longer exists on its host"
                raise StaleObjectError(msg)
            return spec

        af = host.get("AF")
        if not isinstance(af, Array):
            msg = "associated file no longer exists in host /AF"
            raise StaleObjectError(msg)
        if len(af) > _MAX_AF_ENTRIES:
            msg = f"/AF array exceeds {_MAX_AF_ENTRIES} entries"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        if self._id[0]:
            for pos, raw in enumerate(af):
                if _dict_pair(raw) == self._id:
                    self._index = pos
                    return cast("Dictionary", raw)
            msg = f"associated file {self._id[0]} {self._id[1]} R is no longer in host /AF"
            raise StaleObjectError(msg)
        if 0 <= self._index < len(af) and _is_dict(af[self._index]):
            return cast("Dictionary", af[self._index])
        msg = "direct associated file no longer exists in host /AF"
        raise StaleObjectError(msg)

    def _stream(self) -> Stream:
        stream = _embedded_stream_of(self._filespec())
        if stream is None:
            msg = "associated file has no embedded file stream (/EF)"
            raise PdfError(msg)
        return stream

    @property
    def xref(self) -> int:
        """Return the indirect object number of the file specification, or ``0`` when direct."""
        _ = self._filespec()
        return self._id[0]

    @property
    def obj(self) -> Dictionary:
        """Return the ``AFFileSpecification`` dictionary as a low-level handle."""
        return self._filespec()

    @property
    def filename(self) -> str:
        """Return or set the file name (``/UF``, falling back to ``/F``); never empty."""
        spec = self._filespec()
        for key in ("UF", "F"):
            val = spec.get(key)
            if isinstance(val, String) and val.text:
                return val.text
        return ""

    @filename.setter
    def filename(self, value: str) -> None:
        if not isinstance(value, str):
            msg = f"filename must be a str: {type(value).__name__}"
            raise TypeError(msg)
        plain = str.__str__(value)
        if not plain:
            msg = "filename must not be empty"
            raise ValueError(msg)
        _validate_text_size((plain,), "associated file filename")
        with self._document.transaction("associated file filename"):
            spec = self._filespec()
            spec["F"] = _encode_ascii_filename(plain)
            spec["UF"] = _encode_text_string(plain)

    @property
    def data(self) -> bytes:
        """Return or replace the decoded bytes of the embedded file stream (bounded at 64 MiB)."""
        stream = self._stream()
        try:
            return stream.read(max_size=_DEFAULT_MAX_AF_SIZE)
        except LimitError as error:
            if error.code == "stream_data_size":
                msg = f"associated file exceeds the {_DEFAULT_MAX_AF_SIZE}-byte read limit"
                raise LimitError(_CODE_EMBEDDED_SIZE, msg) from error
            raise

    @data.setter
    def data(self, value: bytes) -> None:
        payload = _validate_bytes_payload(value, _DEFAULT_MAX_AF_SIZE)
        with self._document.transaction("associated file data"):
            stream = self._stream()
            stream.write(payload)
            params = stream.get("Params")
            if not _is_dict(params):
                stream["Params"] = {"Size": len(payload), "ModDate": _now_pdf_date()}
            else:
                params_dict = cast("Dictionary", params)
                params_dict["Size"] = len(payload)
                params_dict["ModDate"] = _now_pdf_date()
                if "CheckSum" in params_dict:
                    del params_dict["CheckSum"]

    @property
    def media_type(self) -> str:
        """Return or set the MIME media type stored on the stream's ``/Subtype``."""
        subtype = self._stream().get("Subtype")
        return str(subtype) if isinstance(subtype, Name) else ""

    @media_type.setter
    def media_type(self, value: str) -> None:
        checked = _validate_media_type(value)
        with self._document.transaction("associated file media_type"):
            self._stream()["Subtype"] = Name(checked)

    @property
    def relationship(self) -> AssociatedFileRelationship | str:
        """Return or set ``/AFRelationship`` (:class:`AssociatedFileRelationship` or custom name)."""
        raw = self._filespec().get("AFRelationship")
        if not isinstance(raw, Name):
            return AssociatedFileRelationship.UNSPECIFIED
        name = str(raw)
        try:
            return AssociatedFileRelationship(name)
        except ValueError:
            return name

    @relationship.setter
    def relationship(self, value: AssociatedFileRelationship | str) -> None:
        checked = _validate_af_relationship(value)
        with self._document.transaction("associated file relationship"):
            spec = self._filespec()
            _promote_for_af(
                self._document,
                relationship=checked,
                host_requires_2_0=self._host_requires_2_0 or self._single_key is not None,
            )
            spec["AFRelationship"] = Name(checked)
            if checked == AssociatedFileRelationship.ENCRYPTED_PAYLOAD.value and not _is_dict(spec.get("EP")):
                spec["EP"] = {"Type": Name("EncryptedPayload"), "Subtype": Name("Adobe.PDFSecurity")}
            elif checked != AssociatedFileRelationship.ENCRYPTED_PAYLOAD.value and "EP" in spec:
                del spec["EP"]

    @property
    def description(self) -> str | None:
        """Return, set, or delete the optional file description (``/Desc``)."""
        raw = self._filespec().get("Desc")
        return raw.text if isinstance(raw, String) and raw.text else None

    @description.setter
    def description(self, value: str) -> None:
        if not isinstance(value, str):
            msg = f"description must be a str: {type(value).__name__}"
            raise TypeError(msg)
        plain = str.__str__(value)
        _validate_text_size((plain,), "associated file description")
        with self._document.transaction("associated file description"):
            self._filespec()["Desc"] = _encode_text_string(plain)

    @description.deleter
    def description(self) -> None:
        spec = self._filespec()
        if "Desc" not in spec:
            return
        with self._document.transaction("associated file description"):
            spec = self._filespec()
            if "Desc" in spec:
                del spec["Desc"]

    @property
    def created(self) -> str | None:
        """Return, set, or delete ``/Params /CreationDate`` as a PDF date string."""
        params = self._stream().get("Params")
        if not _is_dict(params):
            return None
        raw = cast("Dictionary", params).get("CreationDate")
        return raw.text if isinstance(raw, String) and raw.text else None

    @created.setter
    def created(self, value: str | datetime) -> None:
        checked = _validate_pdf_date(value, "created", allow_datetime=True)
        with self._document.transaction("associated file created"):
            stream = self._stream()
            params = stream.get("Params")
            if not _is_dict(params):
                stream["Params"] = {"ModDate": _now_pdf_date(), "CreationDate": checked}
            else:
                cast("Dictionary", params)["CreationDate"] = checked

    @created.deleter
    def created(self) -> None:
        stream = self._stream()
        params = stream.get("Params")
        if not _is_dict(params) or "CreationDate" not in cast("Dictionary", params):
            return
        with self._document.transaction("associated file created"):
            params = self._stream().get("Params")
            if _is_dict(params) and "CreationDate" in cast("Dictionary", params):
                del cast("Dictionary", params)["CreationDate"]

    @property
    def modified(self) -> str | None:
        """Return or set ``/Params /ModDate`` as a PDF date string."""
        params = self._stream().get("Params")
        if not _is_dict(params):
            return None
        raw = cast("Dictionary", params).get("ModDate")
        return raw.text if isinstance(raw, String) and raw.text else None

    @modified.setter
    def modified(self, value: str | datetime) -> None:
        checked = _validate_pdf_date(value, "modified", allow_datetime=True)
        with self._document.transaction("associated file modified"):
            stream = self._stream()
            params = stream.get("Params")
            if not _is_dict(params):
                stream["Params"] = {"ModDate": checked}
            else:
                cast("Dictionary", params)["ModDate"] = checked

    @property
    def size(self) -> int:
        """Return ``/Params /Size``, or ``-1`` when absent."""
        params = self._stream().get("Params")
        if not _is_dict(params):
            return -1
        raw = cast("Dictionary", params).get("Size")
        return int(cast("int", raw)) if _is_non_bool_int(raw) else -1

    def _key(self) -> tuple[int, int]:
        return self._id if self._id[0] else (0, self._index)

    def __eq__(self, other: object) -> bool:
        """Compare handles by document and file specification identity."""
        if not isinstance(other, AssociatedFile):
            return NotImplemented
        return self._document is other._document and self._key() == other._key()

    def __hash__(self) -> int:
        """Hash consistently with equality."""
        return hash((id(self._document), self._key()))

    def __repr__(self) -> str:
        """Return ``AssociatedFile(filename=..., relationship=...)``, or a stale note."""
        try:
            return f"AssociatedFile(filename={self.filename!r}, relationship={str(self.relationship)!r})"
        except StaleObjectError:
            return "<stale AssociatedFile>"


def _validate_af_text_fields(filename: object, description: object) -> tuple[str, str | None]:
    if not isinstance(filename, str):
        msg = f"filename must be a str: {type(filename).__name__}"
        raise TypeError(msg)
    plain_filename = str.__str__(filename)
    if not plain_filename:
        msg = "filename must not be empty"
        raise ValueError(msg)
    plain_desc: str | None = None
    if description is not None:
        if not isinstance(description, str):
            msg = f"description must be a str or None: {type(description).__name__}"
            raise TypeError(msg)
        plain_desc = str.__str__(description)
    texts = (plain_filename,) if plain_desc is None else (plain_filename, plain_desc)
    _validate_text_size(texts, "associated file filename and description")
    return plain_filename, plain_desc


def _create_af_filespec_object(  # noqa: PLR0913 - validated fields of one AFFileSpecification.
    document: Document,
    *,
    filename: str,
    payload: bytes,
    media_type: str,
    relationship: str,
    description: str | None,
    created: str | None,
    modified: str,
) -> tuple[Dictionary, tuple[int, int]]:
    params: dict[str, object] = {"Size": len(payload), "ModDate": modified}
    if created is not None:
        params["CreationDate"] = created
    stream_dict: dict[str, object] = {
        "Type": Name("EmbeddedFile"),
        "Subtype": Name(media_type),
        "Params": params,
    }
    objects = document.objects
    stream_ref = objects[objects.add_stream(payload, stream_dict)]

    filespec_dict: dict[str, object] = {
        "Type": Name("Filespec"),
        "F": _encode_ascii_filename(filename),
        "UF": _encode_text_string(filename),
        "EF": {"F": stream_ref, "UF": stream_ref},
        "AFRelationship": Name(relationship),
    }
    if description is not None:
        filespec_dict["Desc"] = _encode_text_string(description)
    if relationship == AssociatedFileRelationship.ENCRYPTED_PAYLOAD.value:
        filespec_dict["EP"] = {"Type": Name("EncryptedPayload"), "Subtype": Name("Adobe.PDFSecurity")}

    spec_xref = objects.add(filespec_dict)
    spec_obj = cast("Dictionary", objects[spec_xref])
    return spec_obj, (spec_xref, spec_obj.generation or 0)


class AssociatedFiles(Sequence[AssociatedFile]):
    """Live sequence of :class:`AssociatedFile` handles in a host's ``/AF`` array (ISO 32000-2 §14.13).

    Exposed on :attr:`Document.associated_files`, :attr:`Page.associated_files`,
    :attr:`StructureElement.associated_files`, :attr:`Annotation.associated_files`,
    and :attr:`DocumentPart.associated_files`. Deleting the last entry removes
    ``/AF`` from the host dictionary so ``ArrayOfAFFileSpecifications`` is never empty.
    """

    __slots__ = ("_document", "_host_getter", "_host_requires_2_0", "_prepare_host")

    def __init__(
        self,
        document: Document,
        host_getter: Callable[[], Dictionary],
        *,
        host_requires_2_0: bool = False,
        prepare_host: Callable[[], None] | None = None,
    ) -> None:
        """Wrap the ``/AF`` array of the dictionary returned by ``host_getter``."""
        self._document = document
        self._host_getter = host_getter
        self._host_requires_2_0 = host_requires_2_0
        self._prepare_host = prepare_host

    def _handles(self) -> list[AssociatedFile]:
        self._document._ensure_open()  # noqa: SLF001
        host = self._host_getter()
        af = host.get("AF")
        if not isinstance(af, Array):
            return []
        if len(af) > _MAX_AF_ENTRIES:
            msg = f"/AF array exceeds {_MAX_AF_ENTRIES} entries"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        handles: list[AssociatedFile] = []
        for pos, raw in enumerate(af):
            if _is_dict(raw):
                pair = _dict_pair(raw) or (0, 0)
                handles.append(
                    AssociatedFile(
                        self._document,
                        self._host_getter,
                        pair,
                        pos,
                        host_requires_2_0=self._host_requires_2_0,
                    )
                )
        return handles

    @overload
    def __getitem__(self, index: int) -> AssociatedFile: ...
    @overload
    def __getitem__(self, index: slice) -> list[AssociatedFile]: ...
    def __getitem__(self, index: int | slice) -> AssociatedFile | list[AssociatedFile]:
        """Return one :class:`AssociatedFile`, or a list for a slice."""
        handles = self._handles()
        if isinstance(index, slice):
            return handles[index]
        return handles[_resolve_seq_index(index, len(handles), "associated file index")]

    def __len__(self) -> int:
        """Return the number of associated files on this host."""
        return len(self._handles())

    def __iter__(self) -> Iterator[AssociatedFile]:
        """Iterate over the host's :class:`AssociatedFile` handles."""
        return iter(self._handles())

    def __bool__(self) -> bool:
        """Return whether the host has at least one associated file."""
        return bool(self._handles())

    def __delitem__(self, index: int | slice) -> None:
        """Delete one associated file or a slice, removing ``/AF`` when empty."""
        self._document._ensure_open()  # noqa: SLF001
        host = self._host_getter()
        af = host.get("AF")
        raw_items = [item for item in af if _is_dict(item)] if isinstance(af, Array) else []
        if isinstance(index, slice):
            removed_indices = set(range(len(raw_items))[index])
            if not removed_indices:
                return
        else:
            removed_indices = {_resolve_seq_index(index, len(raw_items), "associated file index")}

        with self._document.transaction("delete associated file"):
            if self._prepare_host is not None:
                self._prepare_host()
            host = self._host_getter()
            af = host.get("AF")
            current = [item for item in af if _is_dict(item)] if isinstance(af, Array) else []
            retired: list[int] = []
            if isinstance(af, Array) and af.is_indirect and af.xref is not None:
                retired.append(af.xref)
            kept: list[object] = []
            for pos, item in enumerate(current):
                if pos in removed_indices:
                    retired.extend(_af_object_xrefs(item))
                else:
                    kept.append(item)
            if kept:
                host["AF"] = kept
            elif "AF" in host:
                del host["AF"]
            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    def clear(self) -> None:
        """Remove all associated files from this host and delete ``/AF``."""
        self._document._ensure_open()  # noqa: SLF001
        host = self._host_getter()
        if "AF" not in host:
            return
        with self._document.transaction("clear associated files"):
            if self._prepare_host is not None:
                self._prepare_host()
            host = self._host_getter()
            af = host.get("AF")
            retired: list[int] = []
            if isinstance(af, Array):
                if af.is_indirect and af.xref is not None:
                    retired.append(af.xref)
                for item in af:
                    retired.extend(_af_object_xrefs(item))
            if "AF" in host:
                del host["AF"]
            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    def add(  # noqa: PLR0913 - keyword metadata options for creating an AFFileSpecification.
        self,
        filename: str,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        relationship: AssociatedFileRelationship | str = AssociatedFileRelationship.UNSPECIFIED,
        description: str | None = None,
        created: str | datetime | None = None,
        modified: str | datetime | None = None,
        max_size: int | None = _DEFAULT_MAX_AF_SIZE,
    ) -> AssociatedFile:
        """Embed ``data`` as an associated file on this host and return its handle."""
        self._document._ensure_open()  # noqa: SLF001
        plain_filename, plain_desc = _validate_af_text_fields(filename, description)
        checked_media = _validate_media_type(media_type)
        checked_rel = _validate_af_relationship(relationship)
        checked_created = None if created is None else _validate_pdf_date(created, "created", allow_datetime=True)
        checked_modified = (
            _now_pdf_date() if modified is None else _validate_pdf_date(modified, "modified", allow_datetime=True)
        )
        payload = _validate_bytes_payload(data, max_size)
        _ = self._host_getter()

        with self._document.transaction("add associated file"):
            if self._prepare_host is not None:
                self._prepare_host()
            host = self._host_getter()
            existing_af = host.get("AF")
            existing_items = [item for item in existing_af if _is_dict(item)] if isinstance(existing_af, Array) else []
            if len(existing_items) >= _MAX_AF_ENTRIES:
                msg = f"/AF array exceeds {_MAX_AF_ENTRIES} entries"
                raise LimitError(_CODE_OBJECT_COUNT, msg)

            host_needs_2_0 = self._host_requires_2_0 or host.get("Subtype") == "3D"
            _promote_for_af(self._document, relationship=checked_rel, host_requires_2_0=host_needs_2_0)
            spec_obj, spec_pair = _create_af_filespec_object(
                self._document,
                filename=plain_filename,
                payload=payload,
                media_type=checked_media,
                relationship=checked_rel,
                description=plain_desc,
                created=checked_created,
                modified=checked_modified,
            )
            if isinstance(existing_af, Array) and not existing_af.is_indirect:
                existing_af.append(spec_obj)
                pos = len(existing_af) - 1
            else:
                host["AF"] = [*existing_items, spec_obj]
                pos = len(existing_items)

        return AssociatedFile(
            self._document,
            self._host_getter,
            spec_pair,
            pos,
            host_requires_2_0=self._host_requires_2_0,
        )

    def __repr__(self) -> str:
        """Return ``AssociatedFiles([...])``."""
        return f"AssociatedFiles({self._handles()!r})"


# ---------------------------------------------------------------------------
# 3. Page-Piece Dictionaries (§14.5, page-piece)
# ---------------------------------------------------------------------------


def _append_into_container(target: dict[str, Any] | list[Any], key: str | None, value: object) -> None:
    if isinstance(target, dict):
        assert key is not None  # noqa: S101
        target[key] = value
    else:
        target.append(value)


def _validate_structured_scalar(
    document: Document,
    current: object,
    *,
    allow_streams: bool,
    label: str,
) -> bool:
    """Return ``True`` if ``current`` is a valid scalar or stream; ``False`` if container/unknown."""
    if current is None or isinstance(current, (bool, int, Name, String, bytes, Real)):
        return True
    if isinstance(current, Decimal):
        if not current.is_finite():
            msg = f"{label} contains a non-finite Decimal: {current!r}"
            raise ValueError(msg)
        return True
    if isinstance(current, float):
        if not math.isfinite(current):
            msg = f"{label} contains a non-finite float: {current!r}"
            raise ValueError(msg)
        return True
    if isinstance(current, str):
        _validate_text_size((str.__str__(current),), label)
        return True
    if isinstance(current, Stream):
        if not allow_streams:
            msg = f"{label} cannot contain streams"
            raise ValueError(msg)
        if current._document is not document:  # noqa: SLF001
            msg = f"{label} stream belongs to a different document"
            raise ValueError(msg)
        return True
    return False


def _validate_structured_value(  # noqa: C901, PLR0912 - bounded iterative validator for private/DPM trees.
    document: Document,
    root: object,
    *,
    allow_streams: bool,
    label: str,
) -> object:
    """Validate and clone a ``PieceInfo`` private value or ``DPM`` value iteratively."""
    stack: list[tuple[object, int]] = [(root, 0)]
    seen_handles: set[tuple[int, int]] = set()
    seen_python: set[int] = set()
    budget = _MAX_PRIVATE_ENTRIES

    while stack:
        current, depth = stack.pop()
        budget -= 1
        if budget < 0:
            msg = f"{label} exceeds the {_MAX_PRIVATE_ENTRIES}-entry safety limit"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        if depth > _MAX_PRIVATE_DEPTH:
            msg = f"{label} exceeds the {_MAX_PRIVATE_DEPTH}-level nesting limit"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        if _validate_structured_scalar(document, current, allow_streams=allow_streams, label=label):
            continue
        if isinstance(current, Dictionary):
            if current._document is not document:  # noqa: SLF001
                msg = f"{label} dictionary belongs to a different document"
                raise ValueError(msg)
            pair = _dict_pair(current)
            if pair is not None and pair in seen_handles:
                continue
            if pair is not None:
                seen_handles.add(pair)
            stack.extend((current.get(k), depth + 1) for k in current)
        elif isinstance(current, Array):
            if current._document is not document:  # noqa: SLF001
                msg = f"{label} array belongs to a different document"
                raise ValueError(msg)
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, (list, tuple)):
            if id(current) in seen_python:
                msg = f"{label} cannot contain cyclic containers"
                raise ValueError(msg)
            seen_python.add(id(current))
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, Mapping):
            if id(current) in seen_python:
                msg = f"{label} cannot contain cyclic containers"
                raise ValueError(msg)
            seen_python.add(id(current))
            for k, v in current.items():
                _validate_pdf_name(k, f"{label} key")
                stack.append((v, depth + 1))
        else:
            msg = f"unsupported value in {label}: {type(current).__name__}"
            raise TypeError(msg)

    return _clone_structured_value(root)


def _clone_structured_value(root: object) -> object:
    """Convert Python mappings/sequences into plain ``dict``/``list`` containers iteratively."""
    if not isinstance(root, (list, tuple, Mapping)) or isinstance(root, (Dictionary, Array, Stream)):
        return str.__str__(root) if isinstance(root, str) and not isinstance(root, Name) else root

    top_container: dict[str, object] | list[object] = {} if isinstance(root, Mapping) else []
    top_it: Iterator[tuple[str | None, object]] = (
        ((str.__str__(k), v) for k, v in root.items()) if isinstance(root, Mapping) else ((None, v) for v in root)
    )
    frames: list[tuple[dict[str, object] | list[object], Iterator[tuple[str | None, object]]]] = [
        (top_container, top_it)
    ]

    while frames:
        target, iterator = frames[-1]
        try:
            key, val = next(iterator)
        except StopIteration:
            frames.pop()
            continue

        if isinstance(val, Mapping) and not isinstance(val, (Dictionary, Stream)):
            child_dict: dict[str, object] = {}
            _append_into_container(target, key, child_dict)
            frames.append((child_dict, ((str.__str__(ck), cv) for ck, cv in val.items())))
        elif isinstance(val, (list, tuple)) and not isinstance(val, Array):
            child_list: list[object] = []
            _append_into_container(target, key, child_list)
            frames.append((child_list, ((None, cv) for cv in val)))
        else:
            scalar = str.__str__(val) if isinstance(val, str) and not isinstance(val, Name) else val
            _append_into_container(target, key, scalar)
    return top_container


def _open_pdf_container(
    container: Dictionary | Array,
    seen_xrefs: set[int],
) -> tuple[dict[str, Any] | list[Any], Iterator[tuple[str | None, object]]] | None:
    if container.is_indirect and container.xref is not None:
        if container.xref in seen_xrefs:
            return None
        seen_xrefs.add(container.xref)
    if isinstance(container, Dictionary):
        return {}, ((str(k), container.get(k)) for k in container)
    return [], ((None, item) for item in container)


def _materialize_pdf_value(root: object) -> Any:  # noqa: ANN401 - private PDF data can be any nested scalar/container.
    """Materialize a PDF dictionary/array tree iteratively, keeping ``Name``, ``Real``, and ``Stream``."""
    if root is None or isinstance(root, (bool, int, Real, Name, Stream)):
        return root
    if isinstance(root, String):
        return root.text
    if not isinstance(root, (Dictionary, Array)):
        return root

    seen_xrefs: set[int] = set()
    opened = _open_pdf_container(root, seen_xrefs)
    assert opened is not None  # noqa: S101
    top_out, top_iter = opened
    budget = _MAX_PRIVATE_ENTRIES
    frames: list[tuple[dict[str, Any] | list[Any], Iterator[tuple[str | None, object]], int]] = [(top_out, top_iter, 1)]

    while frames:
        target, iterator, depth = frames[-1]
        try:
            key, val = next(iterator)
        except StopIteration:
            frames.pop()
            continue
        budget -= 1
        if budget < 0:
            msg = f"application data exceeds the {_MAX_PRIVATE_ENTRIES}-entry safety limit"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        if depth > _MAX_PRIVATE_DEPTH:
            msg = f"application data exceeds the {_MAX_PRIVATE_DEPTH}-level nesting limit"
            raise LimitError(_CODE_OBJECT_COUNT, msg)

        if isinstance(val, (Dictionary, Array)) and not isinstance(val, Stream):
            sub_opened = _open_pdf_container(val, seen_xrefs)
            if sub_opened is not None:
                sub_out, sub_iter = sub_opened
                _append_into_container(target, key, sub_out)
                frames.append((sub_out, sub_iter, depth + 1))
        else:
            _append_into_container(target, key, val.text if isinstance(val, String) else val)
    return top_out


def _collect_value_xrefs(root: object) -> list[int]:
    """Collect indirect object numbers reachable inside ``root`` iteratively."""
    stack: list[object] = [root]
    seen: set[int] = set()
    budget = _MAX_PRIVATE_ENTRIES
    while stack and budget > 0:
        budget -= 1
        item = stack.pop()
        xref = _indirect_xref(item)
        if xref is not None:
            if xref in seen:
                continue
            seen.add(xref)
        if isinstance(item, Stream):
            continue
        if isinstance(item, Dictionary):
            stack.extend(item.get(k) for k in item)
        elif isinstance(item, Array):
            stack.extend(item)
    return sorted(seen)


def _ensure_trailer_info_moddate(document: Document, mod_date: str) -> None:
    """Ensure ``trailer['Info']`` exists (indirect) with ``ModDate`` for ``Catalog['PieceInfo']``."""
    info = document.trailer.get("Info")
    if _is_dict(info):
        info_dict = cast("Dictionary", info)
        if not info_dict.is_indirect:
            copied = {str(k): info_dict.get(k) for k in info_dict}
            copied["ModDate"] = mod_date
            xref = document.objects.add(copied)
            document.trailer["Info"] = document.objects[xref]
        elif "ModDate" not in info_dict:
            info_dict["ModDate"] = mod_date
    else:
        xref = document.objects.add({"ModDate": mod_date})
        document.trailer["Info"] = document.objects[xref]


@dataclass(frozen=True, slots=True)
class PieceData:
    """One application's entry in a ``/PieceInfo`` page-piece dictionary (ISO 32000-2 Table 351).

    ``last_modified`` is a PDF date string (``"D:..."``), and ``private`` holds
    the application's private data dictionary, array, stream, or primitive.
    """

    last_modified: str
    private: Any = None

    def __post_init__(self) -> None:
        """Validate ``last_modified`` as a PDF date string."""
        checked = _validate_pdf_date(self.last_modified, "last_modified")
        object.__setattr__(self, "last_modified", checked)


def _merge_piece_entry(
    existing: object,
    checked_name: str,
    data_entry: dict[str, object],
) -> tuple[dict[str, object], list[int]]:
    retired: list[int] = []
    current_map: dict[str, object] = {}
    if _is_dict(existing):
        ex_dict = cast("Dictionary", existing)
        if checked_name not in ex_dict and len(ex_dict) >= _MAX_PIECE_ENTRIES:
            msg = f"/PieceInfo exceeds {_MAX_PIECE_ENTRIES} entries"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        if checked_name in ex_dict:
            retired.extend(_collect_value_xrefs(ex_dict.get(checked_name)))
        for k in ex_dict:
            if k != checked_name:
                current_map[str(k)] = ex_dict.get(k)
        if ex_dict.is_indirect and ex_dict.xref is not None:
            retired.append(ex_dict.xref)
    current_map[checked_name] = data_entry
    return current_map, retired


class PieceInfo(MutableMapping[str, PieceData]):
    """Live ``MutableMapping`` over ``Catalog['PieceInfo']`` or ``PageObject['PieceInfo']`` (ISO 32000-2 §14.5).

    Writing promotes the document to PDF 1.4 on the catalog (ensuring trailer
    ``/Info`` carries ``/ModDate``) or PDF 1.3 on a page (updating
    ``PageObject['LastModified']``). Removing the last entry deletes
    ``/PieceInfo`` from the host dictionary.
    """

    __slots__ = ("_document", "_host_getter", "_is_page")

    def __init__(
        self,
        document: Document,
        host_getter: Callable[[], Dictionary],
        *,
        is_page: bool,
    ) -> None:
        """Wrap ``host_getter``'s ``/PieceInfo`` dictionary."""
        self._document = document
        self._host_getter = host_getter
        self._is_page = is_page

    def _raw_piece_dict(self) -> Dictionary | None:
        self._document._ensure_open()  # noqa: SLF001
        raw = self._host_getter().get("PieceInfo")
        return cast("Dictionary", raw) if _is_dict(raw) else None

    def _entries(self) -> dict[str, PieceData]:
        piece = self._raw_piece_dict()
        if piece is None:
            return {}
        result: dict[str, PieceData] = {}
        count = 0
        for key in piece:
            count += 1
            if count > _MAX_PIECE_ENTRIES:
                msg = f"/PieceInfo exceeds {_MAX_PIECE_ENTRIES} entries"
                raise LimitError(_CODE_OBJECT_COUNT, msg)
            entry = piece.get(key)
            if not _is_dict(entry):
                continue
            entry_dict = cast("Dictionary", entry)
            raw_mod = entry_dict.get("LastModified")
            mod_text = raw_mod.text if isinstance(raw_mod, String) else ""
            if not _is_pdf_date(mod_text):
                continue
            priv = _materialize_pdf_value(entry_dict.get("Private")) if "Private" in entry_dict else None
            result[str(key)] = PieceData(last_modified=mod_text, private=priv)
        return result

    def __getitem__(self, name: str) -> PieceData:
        """Return the :class:`PieceData` for application ``name``, or raise :class:`KeyError`."""
        if not isinstance(name, str):
            raise KeyError(name)
        return self._entries()[name]

    def __setitem__(self, name: str, value: PieceData) -> None:
        """Store ``value`` under ``name`` in one transaction."""
        checked_name = _validate_pdf_name(name, "application name")
        if not isinstance(value, PieceData):
            msg = f"PieceInfo value must be a PieceData: {type(value).__name__}"
            raise TypeError(msg)
        cloned_private = _validate_structured_value(
            self._document,
            value.private,
            allow_streams=True,
            label="PieceData.private",
        )
        _ = self._host_getter()

        with self._document.transaction("set piece_info"):
            host = self._host_getter()
            catalog = _catalog(self._document)
            _require_interchange_version(self._document, catalog, (1, 3) if self._is_page else (1, 4))
            data_entry: dict[str, object] = {"LastModified": value.last_modified}
            if cloned_private is not None:
                data_entry["Private"] = cloned_private
            current_map, retired = _merge_piece_entry(host.get("PieceInfo"), checked_name, data_entry)
            host["PieceInfo"] = current_map

            if self._is_page:
                host["LastModified"] = value.last_modified
            else:
                _ensure_trailer_info_moddate(self._document, value.last_modified)

            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    def set(
        self,
        name: str,
        private: object = None,
        *,
        last_modified: str | None = None,
    ) -> PieceData:
        """Create, store, and return a :class:`PieceData` under ``name`` (defaulting ``last_modified`` to UTC now)."""
        stamp = _now_pdf_date() if last_modified is None else _validate_pdf_date(last_modified, "last_modified")
        data = PieceData(last_modified=stamp, private=private)
        self[name] = data
        return self[name]

    def __delitem__(self, name: str) -> None:
        """Remove application ``name`` from ``/PieceInfo``, deleting ``/PieceInfo`` when empty."""
        if not isinstance(name, str):
            raise KeyError(name)
        self._document._ensure_open()  # noqa: SLF001
        host = self._host_getter()
        existing = host.get("PieceInfo")
        if not _is_dict(existing) or name not in cast("Dictionary", existing):
            raise KeyError(name)

        with self._document.transaction("delete piece_info"):
            host = self._host_getter()
            existing = host.get("PieceInfo")
            if not _is_dict(existing) or name not in cast("Dictionary", existing):
                raise KeyError(name)
            ex_dict = cast("Dictionary", existing)
            retired = _collect_value_xrefs(ex_dict.get(name))
            if ex_dict.is_indirect and ex_dict.xref is not None:
                retired.append(ex_dict.xref)
            kept = {str(k): ex_dict.get(k) for k in ex_dict if k != name}
            if kept:
                host["PieceInfo"] = kept
            else:
                del host["PieceInfo"]
            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    def clear(self) -> None:
        """Remove ``/PieceInfo`` and all of its entries in one transaction."""
        self._document._ensure_open()  # noqa: SLF001
        host = self._host_getter()
        if "PieceInfo" not in host:
            return
        with self._document.transaction("clear piece_info"):
            host = self._host_getter()
            if "PieceInfo" not in host:
                return
            retired = _collect_value_xrefs(host.get("PieceInfo"))
            del host["PieceInfo"]
            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    def __iter__(self) -> Iterator[str]:
        """Iterate over the application names stored in ``/PieceInfo``."""
        return iter(list(self._entries()))

    def __len__(self) -> int:
        """Return the number of valid application entries in ``/PieceInfo``."""
        return len(self._entries())

    def __bool__(self) -> bool:
        """Return whether ``/PieceInfo`` holds at least one entry."""
        return bool(self._entries())

    def __repr__(self) -> str:
        """Return ``PieceInfo({...})``."""
        return f"PieceInfo({self._entries()!r})"


def _collect_piece_input_pairs(
    mapping: Mapping[str, PieceData] | Iterable[tuple[str, PieceData]],
) -> Sequence[object]:
    if isinstance(mapping, Mapping):
        raw_pairs: Sequence[object] = list(itertools.islice(mapping.items(), _MAX_PIECE_ENTRIES + 1))
    elif not isinstance(mapping, (str, bytes)) and isinstance(mapping, Iterable):
        raw_pairs = list(itertools.islice(mapping, _MAX_PIECE_ENTRIES + 1))
    else:
        msg = f"piece_info must be a mapping or iterable of (name, PieceData) pairs: {type(mapping).__name__}"
        raise TypeError(msg)
    if len(raw_pairs) > _MAX_PIECE_ENTRIES:
        msg = f"/PieceInfo exceeds {_MAX_PIECE_ENTRIES} entries"
        raise LimitError(_CODE_OBJECT_COUNT, msg)
    return raw_pairs


def _replace_piece_info(
    document: Document,
    host_getter: Callable[[], Dictionary],
    mapping: Mapping[str, PieceData] | Iterable[tuple[str, PieceData]],
    *,
    is_page: bool,
) -> None:
    raw_pairs = _collect_piece_input_pairs(mapping)
    prepared: dict[str, dict[str, object]] = {}
    latest_date: str | None = None
    for pair in raw_pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:  # noqa: PLR2004
            msg = "piece_info items must be (name, PieceData) pairs"
            raise TypeError(msg)
        key, val = pair
        checked_key = _validate_pdf_name(key, "application name")
        if not isinstance(val, PieceData):
            msg = f"piece_info value must be a PieceData: {type(val).__name__}"
            raise TypeError(msg)
        cloned = _validate_structured_value(document, val.private, allow_streams=True, label="PieceData.private")
        entry: dict[str, object] = {"LastModified": val.last_modified}
        if cloned is not None:
            entry["Private"] = cloned
        prepared[checked_key] = entry
        latest_date = val.last_modified

    _ = host_getter()
    with document.transaction("piece_info"):
        host = host_getter()
        retired = _collect_value_xrefs(host.get("PieceInfo")) if "PieceInfo" in host else []
        if not prepared:
            if "PieceInfo" in host:
                del host["PieceInfo"]
        else:
            _require_interchange_version(document, _catalog(document), (1, 3) if is_page else (1, 4))
            host["PieceInfo"] = prepared
            assert latest_date is not None  # noqa: S101
            if is_page:
                host["LastModified"] = latest_date
            else:
                _ensure_trailer_info_moddate(document, latest_date)
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


def _delete_piece_info(document: Document, host_getter: Callable[[], Dictionary], *, is_page: bool) -> None:
    PieceInfo(document, host_getter, is_page=is_page).clear()


# ---------------------------------------------------------------------------
# 4. Document Parts and /GoToDp Actions (§14.12, §12.6.4.5)
# ---------------------------------------------------------------------------


def _dpart_root_dict(document: Document) -> Dictionary | None:
    raw = _catalog(document).get("DPartRoot")
    if _is_dict(raw):
        d = cast("Dictionary", raw)
        if d.is_indirect:
            return d
    return None


def _promote_for_dpart(document: Document) -> None:
    catalog = _catalog(document)
    if _has_catalog_extension(catalog, "PDF_VT2") and _effective_version(document, catalog) >= (1, 6):
        _require_interchange_version(document, catalog, (1, 6))
    else:
        _require_interchange_version(document, catalog, (2, 0))


def _resolve_page_handle(document: Document, page: Page | int, label: str = "page") -> Page:
    from pyropdf._page import Page  # noqa: PLC0415 - breaks import cycle with _page.py.

    if isinstance(page, Page):
        if page.document is not document:
            msg = f"{label} belongs to a different document"
            raise ValueError(msg)
        _ = page.index
        return page
    if _is_non_bool_int(page):
        return document.pages[int(page)]
    msg = f"{label} must be a Page or int: {type(page).__name__}"
    raise TypeError(msg)


def _validate_page_span(
    document: Document,
    start_page: Page | int,
    end_page: Page | int | None,
) -> tuple[Page, Page]:
    start = _resolve_page_handle(document, start_page, "start_page")
    end = start if end_page is None else _resolve_page_handle(document, end_page, "end_page")
    if start.index > end.index:
        msg = f"start_page index ({start.index}) must not exceed end_page index ({end.index})"
        raise ValueError(msg)
    return start, end


def _validate_dpm_input(document: Document, dpm: Mapping[str, Any] | None) -> dict[str, object] | None:
    if dpm is None:
        return None
    if not isinstance(dpm, Mapping):
        msg = f"dpm must be a mapping or None: {type(dpm).__name__}"
        raise TypeError(msg)
    cloned = _validate_structured_value(document, dpm, allow_streams=False, label="dpm")
    return cast("dict[str, object]", cloned)


def _validate_record_level(value: int | None) -> int | None:
    if value is None:
        return None
    if not _is_non_bool_int(value):
        msg = f"record_level must be a non-negative int or None: {type(value).__name__}"
        raise TypeError(msg)
    if value < 0:
        msg = f"record_level must be non-negative: {value}"
        raise ValueError(msg)
    return int(value)


def _validate_node_names(names: Sequence[str] | None) -> list[Name] | None:
    if names is None:
        return None
    if isinstance(names, (str, bytes)) or not isinstance(names, Iterable):
        msg = f"node_names must be a sequence of strings or None: {type(names).__name__}"
        raise TypeError(msg)
    items = list(itertools.islice(names, _MAX_NODE_NAMES + 1))
    if len(items) > _MAX_NODE_NAMES:
        msg = f"node_names exceeds {_MAX_NODE_NAMES} entries"
        raise LimitError(_CODE_OBJECT_COUNT, msg)
    return [Name(_validate_pdf_name(item, "node name")) for item in items]


def _dpart_child_pairs(node: Dictionary) -> list[tuple[int, int]]:
    """Extract child ``DPart`` ``(xref, generation)`` pairs from ``node['DParts']`` (flat or nested)."""
    dparts = node.get("DParts")
    if not isinstance(dparts, Array):
        return []
    if len(dparts) > _MAX_DPART_NODES:
        msg = f"/DParts array exceeds {_MAX_DPART_NODES} entries"
        raise LimitError(_CODE_OBJECT_COUNT, msg)
    pairs: list[tuple[int, int]] = []
    for raw in dparts:
        items = raw if isinstance(raw, Array) else (raw,)
        for elem in items:
            if len(pairs) >= _MAX_DPART_NODES:
                msg = f"/DParts array exceeds {_MAX_DPART_NODES} entries"
                raise LimitError(_CODE_OBJECT_COUNT, msg)
            if (cp := _dict_pair(elem)) is not None:
                pairs.append(cp)
    return pairs


def _encode_dparts_array(children: Sequence[Dictionary]) -> list[list[Dictionary]]:
    """Format child ``DPart`` references for ``DPart['DParts']`` (``ArrayOfDPartArrays``)."""
    return [list(children)]


def _walk_dpart_tree(document: Document) -> list[tuple[tuple[int, int], tuple[int, int] | None, int]]:
    """Return ``[(pair, parent_pair, depth), ...]`` in pre-order iteratively with cycle & size bounds."""
    root_dict = _dpart_root_dict(document)
    if root_dict is None:
        return []
    root_node = root_dict.get("DPartRootNode")
    root_pair = _dict_pair(root_node)
    if root_pair is None:
        return []

    order: list[tuple[tuple[int, int], tuple[int, int] | None, int]] = []
    seen: set[int] = set()
    stack: list[tuple[tuple[int, int], tuple[int, int] | None, int]] = [(root_pair, None, 1)]
    while stack:
        pair, parent_pair, depth = stack.pop()
        if pair[0] in seen:
            continue
        if depth > _MAX_DPART_DEPTH:
            msg = f"document part hierarchy exceeds {_MAX_DPART_DEPTH} levels"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        if len(order) >= _MAX_DPART_NODES:
            msg = f"document part hierarchy exceeds {_MAX_DPART_NODES} nodes"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        seen.add(pair[0])
        order.append((pair, parent_pair, depth))
        node = Dictionary(document, pair)
        children = [cp for cp in _dpart_child_pairs(node) if cp[0] not in seen]
        stack.extend((cpair, pair, depth + 1) for cpair in reversed(children))
    return order


def _scrub_gotodp_holder(holder: Dictionary, key: str, dead_dparts: set[int]) -> int | None:
    act = holder.get(key)
    if not _is_dict(act):
        return None
    act_dict = cast("Dictionary", act)
    if act_dict.get("S") != "GoToDp":
        return None
    dp_xref = _indirect_xref(act_dict.get("Dp"))
    if dp_xref is not None and dp_xref in dead_dparts:
        old_act = _indirect_xref(act_dict)
        del holder[key]
        return old_act
    return None


def _scrub_aa_dict(holder: Dictionary, dead_dparts: set[int], extra: list[int]) -> None:
    aa = holder.get("AA")
    if not _is_dict(aa):
        return
    aa_dict = cast("Dictionary", aa)
    removed_any = False
    for trig_key in list(aa_dict):
        retired = _scrub_gotodp_holder(aa_dict, str(trig_key), dead_dparts)
        if retired is not None:
            extra.append(retired)
            removed_any = True
    if removed_any and len(aa_dict) == 0:
        if aa_dict.is_indirect and aa_dict.xref is not None:
            extra.append(aa_dict.xref)
        del holder["AA"]


def _scrub_page_annots_dpart(page_dict: Dictionary, dead_dparts: set[int], extra: list[int]) -> None:
    annots = page_dict.get("Annots")
    if not isinstance(annots, Array):
        return
    for annot in annots:
        if _is_dict(annot):
            annot_dict = cast("Dictionary", annot)
            retired = _scrub_gotodp_holder(annot_dict, "A", dead_dparts)
            if retired is not None:
                extra.append(retired)
            _scrub_aa_dict(annot_dict, dead_dparts, extra)


def _scrub_page_dpart_refs(page_dict: Dictionary, dead_dparts: set[int], extra: list[int]) -> None:
    if (dp_xref := _indirect_xref(page_dict.get("DPart"))) is not None and dp_xref in dead_dparts:
        del page_dict["DPart"]
    _scrub_aa_dict(page_dict, dead_dparts, extra)
    _scrub_page_annots_dpart(page_dict, dead_dparts, extra)
    first = page_dict.get("PresSteps")
    if not _is_dict(first):
        return
    seen: set[int] = set()
    current = cast("Dictionary", first)
    steps = 0
    while steps < _MAX_NAVIGATION_STEPS:
        steps += 1
        xref = _indirect_xref(current)
        if xref is not None:
            if xref in seen:
                break
            seen.add(xref)
        for nav_key in ("NA", "PA"):
            retired = _scrub_gotodp_holder(current, nav_key, dead_dparts)
            if retired is not None:
                extra.append(retired)
        nxt = current.get("Next")
        if not _is_dict(nxt):
            break
        current = cast("Dictionary", nxt)


def _free_dpart_objects(
    document: Document,
    removed_dparts: set[int],
    extra_candidates: Sequence[int],
) -> None:
    """Free unreferenced ``DPart``/``DPartRoot``/``AF`` objects and scrub dangling ``/GoToDp`` actions."""
    candidates = sorted({*removed_dparts, *extra_candidates})
    if not candidates:
        return
    core = document._core()  # noqa: SLF001
    freed = set(_core.object_free_unreferenced(core, candidates))
    live_dparts = removed_dparts - freed
    if not live_dparts:
        return

    extra: list[int] = []
    catalog = _catalog(document)
    retired_open = _scrub_gotodp_holder(catalog, "OpenAction", live_dparts)
    if retired_open is not None:
        extra.append(retired_open)
    for p_pair in _core.page_objects(core):
        _scrub_page_dpart_refs(Dictionary(document, p_pair), live_dparts, extra)
    for item in document.outline.walk():
        retired_item = _scrub_gotodp_holder(item.obj, "A", live_dparts)
        if retired_item is not None:
            extra.append(retired_item)
    retry = sorted({*live_dparts, *extra})
    if retry:
        _core.object_free_unreferenced(core, retry)


def _dpart_extra_xrefs(node: Dictionary) -> list[int]:
    """Collect indirect object numbers owned by a ``DPart`` dictionary (``DPM``, ``DParts``, ``AF``)."""
    extra: list[int] = []
    if "DPM" in node:
        extra.extend(_collect_value_xrefs(node.get("DPM")))
    dparts = node.get("DParts")
    if isinstance(dparts, Array) and dparts.is_indirect and dparts.xref is not None:
        extra.append(dparts.xref)
    af = node.get("AF")
    if isinstance(af, Array):
        if af.is_indirect and af.xref is not None:
            extra.append(af.xref)
        for item in af:
            extra.extend(_af_object_xrefs(item))
    return extra


def _collect_dpart_subtree_pairs(
    root_pair: tuple[int, int],
    children_map: Mapping[tuple[int, int], Sequence[tuple[int, int]]],
) -> set[tuple[int, int]]:
    """Collect all ``(xref, generation)`` pairs in the subtree rooted at ``root_pair`` iteratively."""
    collected: set[tuple[int, int]] = set()
    stack: list[tuple[int, int]] = [root_pair]
    while stack:
        current = stack.pop()
        if current in collected:
            continue
        collected.add(current)
        stack.extend(children_map.get(current, ()))
    return collected


def _drop_dpart_root_if_needed(
    document: Document,
    removed_pairs: set[tuple[int, int]],
    extra: list[int],
) -> None:
    """Remove ``Catalog['DPartRoot']`` when its ``DPartRootNode`` has been removed."""
    root_dict = _dpart_root_dict(document)
    if root_dict is None:
        return
    root_node_pair = _dict_pair(root_dict.get("DPartRootNode"))
    if root_node_pair is not None and root_node_pair not in removed_pairs:
        return
    if root_dict.xref is not None:
        extra.append(root_dict.xref)
    if (ns_xref := _indirect_xref(root_dict.get("NodeNameList"))) is not None:
        extra.append(ns_xref)
    catalog = _catalog(document)
    if "DPartRoot" in catalog:
        del catalog["DPartRoot"]


def _resolve_initial_root_span(
    document: Document,
    start_page: Page | int | None,
    end_page: Page | int | None,
) -> tuple[Page, Page]:
    """Resolve the initial ``(start, end)`` page span for a newly created root ``DPart``."""
    if start_page is not None:
        return _validate_page_span(document, start_page, end_page)
    if len(document.pages) == 0:
        msg = "document has no pages to span in DPartRoot"
        raise ValueError(msg)
    if end_page is not None:
        return _validate_page_span(document, document.pages[0], end_page)
    return document.pages[0], document.pages[-1]


def _update_existing_dpart_root(  # noqa: PLR0913 - updates optional root DPart and DPartRoot fields together.
    document: Document,
    existing: DocumentPart,
    *,
    start: Page | None,
    end: Page | None,
    dpm: dict[str, object] | None,
    record_level: int | None,
    node_names: list[Name] | None,
) -> None:
    """Update optional fields on an existing ``DPartRoot`` and its ``DPartRootNode``."""
    root_dict = _dpart_root_dict(document)
    assert root_dict is not None  # noqa: S101
    if record_level is not None:
        root_dict["RecordLevel"] = record_level
    if node_names is not None:
        old_ns = _indirect_xref(root_dict.get("NodeNameList"))
        root_dict["NodeNameList"] = node_names
        if old_ns is not None:
            _core.object_free_unreferenced(document._core(), [old_ns])  # noqa: SLF001
    node = existing._dict()  # noqa: SLF001
    if dpm is not None:
        retired = _collect_value_xrefs(node.get("DPM")) if "DPM" in node else []
        node["DPM"] = dpm
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001
    if start is not None:
        if "DParts" in node:
            msg = "cannot set start_page on a branch DocumentPart with children"
            raise ValueError(msg)
        node["Start"] = start.obj
        if end is None or end == start:
            if "End" in node:
                del node["End"]
        else:
            node["End"] = end.obj


class DocumentPart:
    """Live handle on an indirect ``DPart`` node in ``Document.parts`` (ISO 32000-2 §14.12).

    Leaf parts span a contiguous range of pages (:attr:`start_page` through
    :attr:`end_page`); branch parts group child :class:`DocumentPart` handles in
    ``/DParts``. Metadata lives in :attr:`dpm` (never ``/Metadata``), and files
    in :attr:`associated_files`. A deleted handle raises :class:`StaleObjectError`
    and recovers on undo.
    """

    __slots__ = ("_document", "_id")

    def __init__(self, document: Document, xref: int, generation: int = 0) -> None:
        """Bind indirect ``DPart`` object ``(xref, generation)``."""
        self._document = document
        self._id = (xref, generation)

    def _dict(self) -> Dictionary:
        core = self._document._core()  # noqa: SLF001
        if _dpart_root_dict(self._document) is None:
            msg = f"document part {self._id[0]} {self._id[1]} R is no longer in DPartRoot"
            raise StaleObjectError(msg)
        found = _core.object_lookup(core, self._id[0])
        if found is None or found[0] != self._id[1] or not isinstance(found[1], tuple) or found[1][0] != "d":
            msg = f"document part {self._id[0]} {self._id[1]} R does not exist in this version"
            raise StaleObjectError(msg)
        d = Dictionary(self._document, self._id)
        if _dict_pair(d.get("Parent")) is None:
            msg = f"object {self._id[0]} {self._id[1]} R is not a valid DPart"
            raise StaleObjectError(msg)
        return d

    def _tree_level(self) -> int:
        order = _walk_dpart_tree(self._document)
        for pair, _, depth in order:
            if pair == self._id:
                return depth
        msg = f"document part {self._id[0]} {self._id[1]} R is not reachable from DPartRoot"
        raise StaleObjectError(msg)

    @property
    def document(self) -> Document:
        """Return the owning :class:`Document`."""
        return self._document

    @property
    def xref(self) -> int:
        """Return the indirect object number of this ``DPart`` dictionary."""
        _ = self._dict()
        return self._id[0]

    @property
    def obj(self) -> Dictionary:
        """Return the ``DPart`` dictionary in the low-level object model."""
        return self._dict()

    @property
    def parent(self) -> DocumentPart | None:
        """Return the parent :class:`DocumentPart`, or ``None`` for the root ``DPart``."""
        d = self._dict()
        root_dict = _dpart_root_dict(self._document)
        parent_pair = _dict_pair(d.get("Parent"))
        if parent_pair is None or (root_dict is not None and _dict_pair(root_dict) == parent_pair):
            return None
        return DocumentPart(self._document, *parent_pair)

    @property
    def children(self) -> tuple[DocumentPart, ...]:
        """Return the direct child :class:`DocumentPart` handles in ``/DParts`` order."""
        d = self._dict()
        return tuple(DocumentPart(self._document, *pair) for pair in _dpart_child_pairs(d))

    def _page_from_ref(self, value: object) -> Page | None:
        from pyropdf._page import Page  # noqa: PLC0415

        pair = _dict_pair(value)
        if pair is None:
            return None
        if _core.page_index(self._document._core(), pair[0], pair[1]) is None:  # noqa: SLF001
            return None
        return Page(self._document, *pair)

    @property
    def start_page(self) -> Page | None:
        """Return or set the starting :class:`Page` of a leaf part, or ``None`` on a branch."""
        d = self._dict()
        if "DParts" in d:
            return None
        return self._page_from_ref(d.get("Start"))

    @start_page.setter
    def start_page(self, value: Page | int) -> None:
        d = self._dict()
        if "DParts" in d:
            msg = "cannot set start_page on a branch DocumentPart with children"
            raise ValueError(msg)
        new_start = _resolve_page_handle(self._document, value, "start_page")
        current_end = self.end_page
        if current_end is not None and new_start.index > current_end.index:
            msg = f"start_page index ({new_start.index}) must not exceed end_page index ({current_end.index})"
            raise ValueError(msg)
        with self._document.transaction("dpart start_page"):
            d = self._dict()
            d["Start"] = new_start.obj
            if current_end is not None and new_start == current_end and "End" in d:
                del d["End"]

    @property
    def end_page(self) -> Page | None:
        """Return or set the ending :class:`Page` of a leaf part (defaults to :attr:`start_page`), or ``None``."""
        d = self._dict()
        if "DParts" in d:
            return None
        end = self._page_from_ref(d.get("End"))
        return end if end is not None else self._page_from_ref(d.get("Start"))

    @end_page.setter
    def end_page(self, value: Page | int | None) -> None:
        d = self._dict()
        if "DParts" in d:
            msg = "cannot set end_page on a branch DocumentPart with children"
            raise ValueError(msg)
        current_start = self.start_page
        if current_start is None:
            msg = "leaf DocumentPart has no valid start_page"
            raise PdfError(msg)
        if value is None:
            with self._document.transaction("dpart end_page"):
                d = self._dict()
                if "End" in d:
                    del d["End"]
            return
        new_end = _resolve_page_handle(self._document, value, "end_page")
        if current_start.index > new_end.index:
            msg = f"end_page index ({new_end.index}) must not precede start_page index ({current_start.index})"
            raise ValueError(msg)
        with self._document.transaction("dpart end_page"):
            d = self._dict()
            if new_end == current_start:
                if "End" in d:
                    del d["End"]
            else:
                d["End"] = new_end.obj

    @property
    def pages(self) -> tuple[Page, ...]:
        """Return all :class:`Page` handles spanned by this leaf or branch subtree in document order."""
        from pyropdf._page import Page  # noqa: PLC0415

        _ = self._dict()
        page_objs = _core.page_objects(self._document._core())  # noqa: SLF001
        index_by_pair = {pair: idx for idx, pair in enumerate(page_objs)}

        selected_indices: set[int] = set()
        seen_nodes: set[int] = set()
        stack: list[tuple[tuple[int, int], int]] = [(self._id, 1)]
        visited_count = 0
        while stack:
            pair, depth = stack.pop()
            if pair[0] in seen_nodes:
                continue
            if depth > _MAX_DPART_DEPTH:
                msg = f"document part hierarchy exceeds {_MAX_DPART_DEPTH} levels"
                raise LimitError(_CODE_OBJECT_COUNT, msg)
            visited_count += 1
            if visited_count > _MAX_DPART_NODES:
                msg = f"document part hierarchy exceeds {_MAX_DPART_NODES} nodes"
                raise LimitError(_CODE_OBJECT_COUNT, msg)
            seen_nodes.add(pair[0])
            node = Dictionary(self._document, pair)
            children = _dpart_child_pairs(node)
            if children:
                stack.extend((cp, depth + 1) for cp in reversed(children))
            else:
                s_pair = _dict_pair(node.get("Start"))
                e_pair = _dict_pair(node.get("End")) or s_pair
                s_idx = index_by_pair.get(s_pair) if s_pair is not None else None
                e_idx = index_by_pair.get(e_pair) if e_pair is not None else s_idx
                if s_idx is not None and e_idx is not None and s_idx <= e_idx:
                    selected_indices.update(range(s_idx, e_idx + 1))

        return tuple(Page(self._document, *page_objs[idx]) for idx in sorted(selected_indices))

    @property
    def dpm(self) -> dict[str, Any] | None:
        """Return, replace, or delete the document part metadata dictionary (``/DPM``)."""
        raw = self._dict().get("DPM")
        if not _is_dict(raw):
            return None
        return cast("dict[str, Any]", _materialize_pdf_value(raw))

    @dpm.setter
    def dpm(self, value: Mapping[str, Any] | None) -> None:
        checked = _validate_dpm_input(self._document, value)
        _ = self._dict()
        with self._document.transaction("dpart dpm"):
            d = self._dict()
            retired = _collect_value_xrefs(d.get("DPM")) if "DPM" in d else []
            if checked is None:
                if "DPM" in d:
                    del d["DPM"]
            else:
                d["DPM"] = checked
            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    @dpm.deleter
    def dpm(self) -> None:
        d = self._dict()
        if "DPM" not in d:
            return
        with self._document.transaction("delete dpart dpm"):
            d = self._dict()
            if "DPM" not in d:
                return
            retired = _collect_value_xrefs(d.get("DPM"))
            del d["DPM"]
            if retired:
                _core.object_free_unreferenced(self._document._core(), sorted(set(retired)))  # noqa: SLF001

    @property
    def associated_files(self) -> AssociatedFiles:
        """Return the ``/AF`` associated files of this document part (PDF 2.0)."""
        _ = self._dict()
        return AssociatedFiles(self._document, self._dict, host_requires_2_0=True)

    def add_child(
        self,
        start_page: Page | int,
        end_page: Page | int | None = None,
        *,
        dpm: Mapping[str, Any] | None = None,
    ) -> DocumentPart:
        """Add and return an indirect leaf :class:`DocumentPart` child under this node in one transaction.

        If this node was previously a leaf placeholder, ``/Start`` and ``/End``
        are cleared when ``/DParts`` receives the new child so Arlington's
        branch/leaf invariant holds throughout.
        """
        level = self._tree_level()
        if level + 1 > _MAX_DPART_DEPTH:
            msg = f"document part hierarchy exceeds {_MAX_DPART_DEPTH} levels"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        order = _walk_dpart_tree(self._document)
        if len(order) >= _MAX_DPART_NODES:
            msg = f"document part hierarchy exceeds {_MAX_DPART_NODES} nodes"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        start, end = _validate_page_span(self._document, start_page, end_page)
        checked_dpm = _validate_dpm_input(self._document, dpm)

        with self._document.transaction("add dpart child"):
            _promote_for_dpart(self._document)
            parent_dict = self._dict()
            child_entries: dict[str, object] = {
                "Type": Name("DPart"),
                "Parent": parent_dict,
                "Start": start.obj,
            }
            if end != start:
                child_entries["End"] = end.obj
            if checked_dpm is not None:
                child_entries["DPM"] = checked_dpm
            objects = self._document.objects
            child_xref = objects.add(child_entries)
            child_obj = cast("Dictionary", objects[child_xref])

            existing_pairs = _dpart_child_pairs(parent_dict)
            updated_children = [*[Dictionary(self._document, cp) for cp in existing_pairs], child_obj]
            parent_dict["DParts"] = _encode_dparts_array(updated_children)
            if "Start" in parent_dict:
                del parent_dict["Start"]
            if "End" in parent_dict:
                del parent_dict["End"]

        return DocumentPart(self._document, child_xref, child_obj.generation or 0)

    def delete(self) -> None:
        """Remove this part and its subtree, pruning empty ancestor branches and ``DPartRoot``."""
        _ = self._dict()
        order = _walk_dpart_tree(self._document)
        children_map: dict[tuple[int, int], list[tuple[int, int]]] = {}
        parent_map: dict[tuple[int, int], tuple[int, int] | None] = {}
        for pair, p_pair, _ in order:
            parent_map[pair] = p_pair
            if p_pair is not None:
                children_map.setdefault(p_pair, []).append(pair)
        if self._id not in parent_map:
            msg = f"document part {self._id[0]} {self._id[1]} R is not in DPartRoot"
            raise StaleObjectError(msg)

        removed_pairs = _collect_dpart_subtree_pairs(self._id, children_map)
        with self._document.transaction("delete dpart"):
            anc = parent_map.get(self._id)
            while anc is not None:
                anc_dict = Dictionary(self._document, anc)
                kept = [
                    Dictionary(self._document, cp) for cp in _dpart_child_pairs(anc_dict) if cp not in removed_pairs
                ]
                if kept:
                    anc_dict["DParts"] = _encode_dparts_array(kept)
                    break
                removed_pairs.add(anc)
                anc = parent_map.get(anc)

            extra = [xr for r_pair in removed_pairs for xr in _dpart_extra_xrefs(Dictionary(self._document, r_pair))]
            _drop_dpart_root_if_needed(self._document, removed_pairs, extra)
            _free_dpart_objects(self._document, {p[0] for p in removed_pairs}, extra)

    def __eq__(self, other: object) -> bool:
        """Compare handles by document and ``(xref, generation)``."""
        if not isinstance(other, DocumentPart):
            return NotImplemented
        return self._document is other._document and self._id == other._id

    def __hash__(self) -> int:
        """Hash consistently with equality."""
        return hash((id(self._document), self._id))

    def __repr__(self) -> str:
        """Return ``DocumentPart(xref=...)``, or a stale marker."""
        try:
            _ = self._dict()
        except StaleObjectError:
            return f"<stale DocumentPart xref={self._id[0]}>"
        return f"DocumentPart(xref={self._id[0]})"


class DocumentParts(Sequence[DocumentPart]):
    """Live view over ``Catalog['DPartRoot']`` and its top-level :class:`DocumentPart` nodes (ISO 32000-2 §14.12)."""

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap ``document``; use :attr:`Document.parts`."""
        self._document = document

    @property
    def root(self) -> DocumentPart | None:
        """Return the root :class:`DocumentPart` (``DPartRootNode``), or ``None`` when absent."""
        self._document._ensure_open()  # noqa: SLF001
        root_dict = _dpart_root_dict(self._document)
        if root_dict is None:
            return None
        pair = _dict_pair(root_dict.get("DPartRootNode"))
        return None if pair is None else DocumentPart(self._document, *pair)

    def _top_parts(self) -> list[DocumentPart]:
        root_part = self.root
        if root_part is None:
            return []
        kids = root_part.children
        return list(kids) if kids else [root_part]

    @overload
    def __getitem__(self, index: int) -> DocumentPart: ...
    @overload
    def __getitem__(self, index: slice) -> list[DocumentPart]: ...
    def __getitem__(self, index: int | slice) -> DocumentPart | list[DocumentPart]:
        """Return a top-level :class:`DocumentPart`, or a list for a slice."""
        parts = self._top_parts()
        if isinstance(index, slice):
            return parts[index]
        return parts[_resolve_seq_index(index, len(parts), "document part index")]

    def __len__(self) -> int:
        """Return the number of top-level document parts (``0`` when ``DPartRoot`` is absent)."""
        return len(self._top_parts())

    def __iter__(self) -> Iterator[DocumentPart]:
        """Iterate over the top-level :class:`DocumentPart` handles."""
        return iter(self._top_parts())

    def __bool__(self) -> bool:
        """Return whether ``Catalog['DPartRoot']`` is present with a valid root node."""
        return self.root is not None

    @property
    def record_level(self) -> int | None:
        """Return, set, or delete ``DPartRoot['RecordLevel']``."""
        self._document._ensure_open()  # noqa: SLF001
        root_dict = _dpart_root_dict(self._document)
        if root_dict is None:
            return None
        raw = root_dict.get("RecordLevel")
        return int(cast("int", raw)) if _is_non_bool_int(raw) and int(cast("int", raw)) >= 0 else None

    @record_level.setter
    def record_level(self, value: int | None) -> None:
        self._document._ensure_open()  # noqa: SLF001
        checked = _validate_record_level(value)
        root_dict = _dpart_root_dict(self._document)
        if root_dict is None:
            if checked is None:
                return
            msg = "Document has no DPartRoot; call doc.parts.ensure_root() or doc.parts.add_part() first"
            raise PdfError(msg)
        with self._document.transaction("dpart record_level"):
            root_dict = _dpart_root_dict(self._document)
            assert root_dict is not None  # noqa: S101
            if checked is None:
                if "RecordLevel" in root_dict:
                    del root_dict["RecordLevel"]
            else:
                root_dict["RecordLevel"] = checked

    @record_level.deleter
    def record_level(self) -> None:
        self.record_level = None

    @property
    def node_names(self) -> tuple[str, ...] | None:
        """Return, set, or delete ``DPartRoot['NodeNameList']``."""
        self._document._ensure_open()  # noqa: SLF001
        root_dict = _dpart_root_dict(self._document)
        if root_dict is None:
            return None
        raw = root_dict.get("NodeNameList")
        if not isinstance(raw, Array):
            return None
        if len(raw) > _MAX_NODE_NAMES:
            msg = f"NodeNameList exceeds {_MAX_NODE_NAMES} entries"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        return tuple(str(item) for item in raw if isinstance(item, Name))

    @node_names.setter
    def node_names(self, value: Sequence[str] | None) -> None:
        self._document._ensure_open()  # noqa: SLF001
        checked = _validate_node_names(value)
        root_dict = _dpart_root_dict(self._document)
        if root_dict is None:
            if checked is None:
                return
            msg = "Document has no DPartRoot; call doc.parts.ensure_root() or doc.parts.add_part() first"
            raise PdfError(msg)
        with self._document.transaction("dpart node_names"):
            root_dict = _dpart_root_dict(self._document)
            assert root_dict is not None  # noqa: S101
            old_xref = _indirect_xref(root_dict.get("NodeNameList"))
            if checked is None:
                if "NodeNameList" in root_dict:
                    del root_dict["NodeNameList"]
            else:
                root_dict["NodeNameList"] = checked
            if old_xref is not None:
                _core.object_free_unreferenced(self._document._core(), [old_xref])  # noqa: SLF001

    @node_names.deleter
    def node_names(self) -> None:
        self.node_names = None

    def ensure_root(
        self,
        *,
        start_page: Page | int | None = None,
        end_page: Page | int | None = None,
        dpm: Mapping[str, Any] | None = None,
        record_level: int | None = None,
        node_names: Sequence[str] | None = None,
    ) -> DocumentPart:
        """Return the root :class:`DocumentPart`, creating ``DPartRoot`` when absent."""
        self._document._ensure_open()  # noqa: SLF001
        checked_level = _validate_record_level(record_level)
        checked_names = _validate_node_names(node_names)
        checked_dpm = _validate_dpm_input(self._document, dpm)

        existing = self.root
        if existing is not None:
            if checked_level is None and checked_names is None and checked_dpm is None and start_page is None:
                return existing
            start, end = (
                _validate_page_span(self._document, start_page, end_page) if start_page is not None else (None, None)
            )
            with self._document.transaction("ensure dpart root"):
                _update_existing_dpart_root(
                    self._document,
                    existing,
                    start=start,
                    end=end,
                    dpm=checked_dpm,
                    record_level=checked_level,
                    node_names=checked_names,
                )
            return existing

        start, end = _resolve_initial_root_span(self._document, start_page, end_page)
        with self._document.transaction("create dpart root"):
            _promote_for_dpart(self._document)
            objects = self._document.objects
            dpart_root_xref = objects.add({"Type": Name("DPartRoot")})
            dpart_root_obj = cast("Dictionary", objects[dpart_root_xref])

            root_entries: dict[str, object] = {
                "Type": Name("DPart"),
                "Parent": dpart_root_obj,
                "Start": start.obj,
            }
            if end != start:
                root_entries["End"] = end.obj
            if checked_dpm is not None:
                root_entries["DPM"] = checked_dpm
            root_node_xref = objects.add(root_entries)
            root_node_obj = cast("Dictionary", objects[root_node_xref])

            dpart_root_dict: dict[str, object] = {"Type": Name("DPartRoot"), "DPartRootNode": root_node_obj}
            if checked_level is not None:
                dpart_root_dict["RecordLevel"] = checked_level
            if checked_names is not None:
                dpart_root_dict["NodeNameList"] = checked_names
            objects[dpart_root_xref] = dpart_root_dict
            _catalog(self._document)["DPartRoot"] = dpart_root_obj

        return DocumentPart(self._document, root_node_xref, root_node_obj.generation or 0)

    def add_part(
        self,
        start_page: Page | int,
        end_page: Page | int | None = None,
        *,
        dpm: Mapping[str, Any] | None = None,
        record_level: int | None = None,
        node_names: Sequence[str] | None = None,
    ) -> DocumentPart:
        """Create and return a leaf :class:`DocumentPart` under ``DPartRoot`` in one transaction."""
        self._document._ensure_open()  # noqa: SLF001
        start, end = _validate_page_span(self._document, start_page, end_page)
        checked_dpm = _validate_dpm_input(self._document, dpm)
        checked_level = _validate_record_level(record_level)
        checked_names = _validate_node_names(node_names)

        with self._document.transaction("add document part"):
            _promote_for_dpart(self._document)
            existing_root = self.root
            if existing_root is not None:
                root_dict = _dpart_root_dict(self._document)
                assert root_dict is not None  # noqa: S101
                if checked_level is not None:
                    root_dict["RecordLevel"] = checked_level
                if checked_names is not None:
                    root_dict["NodeNameList"] = checked_names
                return existing_root.add_child(start, end, dpm=checked_dpm)

            objects = self._document.objects
            dpart_root_xref = objects.add({"Type": Name("DPartRoot")})
            dpart_root_obj = cast("Dictionary", objects[dpart_root_xref])
            root_node_xref = objects.add({"Type": Name("DPart"), "Parent": dpart_root_obj})
            root_node_obj = cast("Dictionary", objects[root_node_xref])

            leaf_entries: dict[str, object] = {
                "Type": Name("DPart"),
                "Parent": root_node_obj,
                "Start": start.obj,
            }
            if end != start:
                leaf_entries["End"] = end.obj
            if checked_dpm is not None:
                leaf_entries["DPM"] = checked_dpm
            leaf_xref = objects.add(leaf_entries)
            leaf_obj = cast("Dictionary", objects[leaf_xref])

            objects[root_node_xref] = {
                "Type": Name("DPart"),
                "Parent": dpart_root_obj,
                "DParts": _encode_dparts_array([leaf_obj]),
            }
            dpart_root_dict: dict[str, object] = {
                "Type": Name("DPartRoot"),
                "DPartRootNode": root_node_obj,
            }
            if checked_level is not None:
                dpart_root_dict["RecordLevel"] = checked_level
            if checked_names is not None:
                dpart_root_dict["NodeNameList"] = checked_names
            objects[dpart_root_xref] = dpart_root_dict
            _catalog(self._document)["DPartRoot"] = dpart_root_obj

        return DocumentPart(self._document, leaf_xref, leaf_obj.generation or 0)

    def clear(self) -> None:
        """Remove ``Catalog['DPartRoot']`` and free all unreferenced ``DPart`` objects."""
        self._document._ensure_open()  # noqa: SLF001
        catalog = _catalog(self._document)
        if "DPartRoot" not in catalog:
            return
        order = _walk_dpart_tree(self._document)
        root_dict = _dpart_root_dict(self._document)
        with self._document.transaction("clear document parts"):
            catalog = _catalog(self._document)
            if "DPartRoot" not in catalog:
                return
            extra: list[int] = []
            if root_dict is not None and root_dict.xref is not None:
                extra.append(root_dict.xref)
                ns_xref = _indirect_xref(root_dict.get("NodeNameList"))
                if ns_xref is not None:
                    extra.append(ns_xref)
            elif (raw_xref := _indirect_xref(catalog.get("DPartRoot"))) is not None:
                extra.append(raw_xref)
            removed_xrefs = {pair[0] for pair, _, _ in order}
            for pair, _, _ in order:
                extra.extend(_dpart_extra_xrefs(Dictionary(self._document, pair)))
            del catalog["DPartRoot"]
            _free_dpart_objects(self._document, removed_xrefs, extra)

    def __repr__(self) -> str:
        """Return ``DocumentParts([...])``."""
        return f"DocumentParts({self._top_parts()!r})"


def _has_dparts(document: Document) -> bool:
    """Return whether ``Catalog['DPartRoot']`` is present on an unlocked document."""
    if document.needs_password:
        return False
    return "DPartRoot" in _catalog(document)


def _prune_leaf_dpart_span(
    document: Document,
    node: Dictionary,
    old_pages: Sequence[tuple[int, int]],
    old_index: Mapping[tuple[int, int], int],
    removed_pages: set[tuple[int, int]],
) -> bool:
    """Update leaf ``node``'s ``[Start..End]`` span; return ``True`` when at least one page survives."""
    s_pair = _dict_pair(node.get("Start"))
    e_pair = _dict_pair(node.get("End")) or s_pair
    s_idx = old_index.get(s_pair) if s_pair is not None else None
    e_idx = old_index.get(e_pair) if e_pair is not None else s_idx
    if s_idx is None or e_idx is None or s_idx > e_idx:
        surviving = [s_pair] if s_pair is not None and s_pair in old_index and s_pair not in removed_pages else []
    else:
        surviving = [p for p in old_pages[s_idx : e_idx + 1] if p not in removed_pages]

    if not surviving:
        return False
    new_start, new_end = surviving[0], surviving[-1]
    if s_pair != new_start:
        node["Start"] = Dictionary(document, new_start)
    if new_start == new_end:
        if "End" in node:
            del node["End"]
    elif _dict_pair(node.get("End")) != new_end:
        node["End"] = Dictionary(document, new_end)
    return True


def _prune_dparts_for_removed_pages(  # noqa: C901, PLR0912 - iterative post-order pruning across page spans.
    document: Document,
    old_pages: Sequence[tuple[int, int]],
    removed_pages: set[tuple[int, int]],
) -> None:
    """Update or prune ``DPart`` leaf spans and empty branches before ``_core.set_pages`` commits."""
    if not removed_pages or not _has_dparts(document):
        return
    order = _walk_dpart_tree(document)
    if not order:
        catalog = _catalog(document)
        if "DPartRoot" in catalog:
            old_xref = _indirect_xref(catalog.get("DPartRoot"))
            del catalog["DPartRoot"]
            if old_xref is not None:
                _core.object_free_unreferenced(document._core(), [old_xref])  # noqa: SLF001
        return

    old_index = {pair: idx for idx, pair in enumerate(old_pages)}
    children_map: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for pair, p_pair, _ in order:
        if p_pair is not None:
            children_map.setdefault(p_pair, []).append(pair)

    removed_dparts: set[tuple[int, int]] = set()
    extra: list[int] = []

    for pair, _, _ in reversed(order):
        node = Dictionary(document, pair)
        if pair in children_map or isinstance(node.get("DParts"), Array):
            kept_children = [Dictionary(document, cp) for cp in children_map.get(pair, ()) if cp not in removed_dparts]
            if not kept_children:
                removed_dparts.add(pair)
                extra.extend(_dpart_extra_xrefs(node))
            elif len(kept_children) != len(children_map.get(pair, ())):
                node["DParts"] = _encode_dparts_array(kept_children)
        elif not _prune_leaf_dpart_span(document, node, old_pages, old_index, removed_pages):
            removed_dparts.add(pair)
            extra.extend(_dpart_extra_xrefs(node))

    for rp in removed_pages:
        p_dict = Dictionary(document, rp)
        if "DPart" in p_dict:
            del p_dict["DPart"]

    if not removed_dparts:
        return

    _drop_dpart_root_if_needed(document, removed_dparts, extra)
    _free_dpart_objects(document, {p[0] for p in removed_dparts}, extra)


@dataclass(frozen=True, slots=True)
class DocumentPartAction:
    """An action that jumps to a :class:`DocumentPart` (``/S /GoToDp``, ISO 32000-2 §12.6.4.5, PDF 2.0)."""

    part: DocumentPart

    def __post_init__(self) -> None:
        """Validate ``part``."""
        if not isinstance(self.part, DocumentPart):
            msg = f"part must be a DocumentPart: {type(self.part).__name__}"
            raise TypeError(msg)


def _gotodp_action_dict(
    document: Document,
    action: DocumentPartAction,
) -> tuple[dict[str, object], tuple[int, int]]:
    """Serialize ``action`` into an ``ActionGoToDp`` dictionary and its PDF 2.0 version requirement."""
    if action.part.document is not document:
        msg = "DocumentPartAction part belongs to a different document"
        raise ValueError(msg)
    dpart_dict = action.part._dict()  # noqa: SLF001
    return (
        {
            "Type": Name("Action"),
            "S": Name("GoToDp"),
            "Dp": dpart_dict,
        },
        (2, 0),
    )


def _read_gotodp_action_dict(document: Document, action: Dictionary) -> DocumentPartAction | None:
    """Read an ``ActionGoToDp`` dictionary into a :class:`DocumentPartAction`, or ``None``."""
    pair = _dict_pair(action.get("Dp"))
    if pair is None:
        return None
    part = DocumentPart(document, *pair)
    try:
        _ = part._dict()  # noqa: SLF001
    except StaleObjectError:
        return None
    return DocumentPartAction(part)


for _public in (
    AssociatedFile,
    AssociatedFileRelationship,
    AssociatedFiles,
    DeveloperExtension,
    DocumentPart,
    DocumentPartAction,
    DocumentParts,
    Extensions,
    PieceData,
    PieceInfo,
):
    _public.__module__ = "pyropdf"

__all__ = [
    "ISO_EXTENSIONS",
    "AssociatedFile",
    "AssociatedFileRelationship",
    "AssociatedFiles",
    "DeveloperExtension",
    "DocumentPart",
    "DocumentPartAction",
    "DocumentParts",
    "Extensions",
    "PieceData",
    "PieceInfo",
]
