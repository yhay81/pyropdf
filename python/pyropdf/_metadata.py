"""Document metadata and catalog properties: Info, XMP, page mode/layout, language, MarkInfo, and viewer preferences."""

from __future__ import annotations

import enum
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

from pyropdf import _core
from pyropdf._errors import PdfError
from pyropdf._objects import Array, Dictionary, Name, Stream, String

if TYPE_CHECKING:
    from pyropdf._document import Document

#: The XMP packet that :attr:`Document.xmp` reads is capped at 1 MiB; read a
#: larger one through ``doc.catalog["Metadata"].read(max_size=...)``.
_MAX_XMP_SIZE = 1024 * 1024

#: The Info dictionary entries that :attr:`Document.metadata` holds, by their
#: names in pyropdf, and their PDF keys.
METADATA_KEYS: dict[str, str] = {
    "title": "Title",
    "author": "Author",
    "subject": "Subject",
    "keywords": "Keywords",
    "creator": "Creator",
    "producer": "Producer",
    "created": "CreationDate",
    "modified": "ModDate",
    "trapped": "Trapped",
}
#: The values that ``trapped`` takes (ISO 32000-2 Table 349).
TRAPPED_VALUES = ("True", "False", "Unknown")

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XML = "http://www.w3.org/XML/1998/namespace"
_NAMESPACES = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "pdf": "http://ns.adobe.com/pdf/1.3/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
}
#: The XMP property that each entry corresponds to (ISO 32000-2 14.3.2 and
#: ISO 19005-1 6.7.3), and its form: an alternative, a sequence, or text.
_XMP_PROPERTIES: dict[str, tuple[str, str, str]] = {
    "title": ("dc", "title", "Alt"),
    "author": ("dc", "creator", "Seq"),
    "subject": ("dc", "description", "Alt"),
    "keywords": ("pdf", "Keywords", ""),
    "creator": ("xmp", "CreatorTool", ""),
    "producer": ("pdf", "Producer", ""),
    "created": ("xmp", "CreateDate", ""),
    "modified": ("xmp", "ModifyDate", ""),
    "trapped": ("pdf", "Trapped", ""),
}
_PDF_DATE = re.compile(
    rb"D:(?P<year>\d{4})(?P<month>\d{2})?(?P<day>\d{2})?(?P<hour>\d{2})?(?P<minute>\d{2})?(?P<second>\d{2})?"
    rb"(?:(?P<zone>[Zz+-])(?:(?P<zh>\d{2})'?(?P<zm>\d{2})?'?)?)?"
)
_PACKET = re.compile(
    r"(?s)\A(?P<head>.*?)(?P<body><(?:\w+:)?xmpmeta\b.*</(?:\w+:)?xmpmeta>|<rdf:RDF\b.*</rdf:RDF>)(?P<tail>.*)\Z"
)


def pdf_date(moment: datetime) -> str:
    """Return ``moment`` as a PDF date string, such as ``"D:20240101120000Z"``; a naive moment is UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    offset = moment.utcoffset() or timedelta()
    if not offset:
        return moment.strftime("D:%Y%m%d%H%M%SZ")
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes > 0 else "-"
    hours, minutes = divmod(abs(minutes), 60)
    return moment.strftime("D:%Y%m%d%H%M%S") + f"{sign}{hours:02d}'{minutes:02d}'"


def parse_pdf_date(value: str) -> datetime | None:
    """Return the moment of a PDF date string, or ``None`` when it is not one.

    Missing fields take their earliest values, and a date without a time zone
    is returned naive, as ISO 32000-2 section 7.9.4 leaves its zone unknown.
    """
    match = _PDF_DATE.match(value.encode("latin-1", "replace"))
    if match is None:
        return None
    field = {key: int(found) for key, found in match.groupdict().items() if found and key != "zone"}
    zone = match["zone"]
    tz: timezone | None = None
    if zone in {b"Z", b"z"}:
        tz = UTC
    elif zone is not None:
        offset = timedelta(hours=field.get("zh", 0), minutes=field.get("zm", 0))
        tz = timezone(offset if zone == b"+" else -offset)
    try:
        return datetime(
            field["year"],
            field.get("month", 1),
            field.get("day", 1),
            field.get("hour", 0),
            field.get("minute", 0),
            field.get("second", 0),
            tzinfo=tz,
        )
    except ValueError:
        return None


def _metadata_value(key: str, value: object) -> str:
    """Validate one value for ``key``: a non-empty string or, for a date, a :class:`datetime`."""
    if key not in METADATA_KEYS:
        msg = f"unknown metadata key {key!r} (valid: {list(METADATA_KEYS)})"
        raise KeyError(msg)
    if key in {"created", "modified"} and isinstance(value, datetime):
        return pdf_date(value)
    if not isinstance(value, str):
        msg = f"metadata values are strings: {key}={value!r}"
        raise TypeError(msg)
    if not value:
        msg = f"metadata values are not empty; delete {key!r} to remove it"
        raise ValueError(msg)
    if key == "trapped" and value not in TRAPPED_VALUES:
        msg = f"trapped is 'True', 'False', or 'Unknown': {value!r}"
        raise ValueError(msg)
    return value


def _info(document: Document) -> Dictionary | None:
    info = document.trailer.get("Info")
    return info if isinstance(info, Dictionary) else None


def _entries(document: Document) -> dict[str, str]:
    info = _info(document)
    if info is None:
        return {}
    entries: dict[str, str] = {}
    for name, key in METADATA_KEYS.items():
        value = info.get(key)
        if isinstance(value, String):
            text = value.text
        elif isinstance(value, Name):
            text = str(value)
        else:
            continue
        if text:
            entries[name] = text
    return entries


def _xmp_stream(document: Document) -> Stream | None:
    stream = document.catalog.get("Metadata")
    return stream if isinstance(stream, Stream) else None


def read_xmp(document: Document) -> str | None:
    """Return the catalog's XMP packet as text, or ``None`` without a metadata stream."""
    stream = _xmp_stream(document)
    if stream is None:
        return None
    return stream.read(max_size=_MAX_XMP_SIZE).decode("utf-8", "replace").replace("\x00", "")


def write_xmp(document: Document, packet: str) -> None:
    """Store ``packet`` in the catalog's metadata stream, unfiltered, adding the stream when absent."""
    data = packet.encode("utf-8")
    stream = _xmp_stream(document)
    with document.transaction("xmp"):
        if stream is not None:
            stream.write(data, filter=None)
            return
        objects = document.objects
        added = objects[objects.add_stream(data, {"Type": Name("Metadata"), "Subtype": Name("XML")}, filter=None)]
        document.catalog["Metadata"] = added


def _xmp_text(key: str, value: str) -> str:
    """Return an Info value as its XMP property's text: dates in ISO 8601."""
    if key in {"created", "modified"} and (moment := parse_pdf_date(value)) is not None:
        return moment.isoformat()
    return value


def _register(packet: str) -> None:
    """Keep the packet's own namespace prefixes when it is written again."""
    for prefix, uri in re.findall(r'xmlns:([\w.-]+)\s*=\s*["\']([^"\']*)["\']', packet):
        if not prefix.lower().startswith(("ns", "xml")):
            ET.register_namespace(prefix, uri)


def _set_property(descriptions: list[ET.Element], key: str, text: str | None) -> None:
    """Set the XMP property of ``key`` to ``text``, or remove it for ``None``."""
    prefix, local, form = _XMP_PROPERTIES[key]
    tag = f"{{{_NAMESPACES[prefix]}}}{local}"
    for description in descriptions:
        description.attrib.pop(tag, None)
        for element in description.findall(tag):
            description.remove(element)
    if text is None:
        return
    element = ET.SubElement(descriptions[0], tag)
    if form:
        container = ET.SubElement(element, f"{{{_RDF}}}{form}")
        item = ET.SubElement(container, f"{{{_RDF}}}li")
        if form == "Alt":
            item.set(f"{{{_XML}}}lang", "x-default")
        item.text = text
    else:
        element.text = text


def _updated_xmp(packet: str, values: Mapping[str, str]) -> str | None:
    """Return ``packet`` with the properties of ``values`` updated, ``""`` removing one.

    A packet that does not parse as XMP is left alone and ``None`` returned.
    """
    match = _PACKET.match(packet)
    if match is None:
        return None
    _register(packet)
    ET.register_namespace("rdf", _RDF)
    for prefix, uri in _NAMESPACES.items():
        ET.register_namespace(prefix, uri)
    try:
        root = ET.fromstring(match["body"])  # noqa: S314 - bounded by _MAX_XMP_SIZE; expat caps entity expansion.
    except ET.ParseError:
        return None
    rdf = root if root.tag == f"{{{_RDF}}}RDF" else root.find(f"{{{_RDF}}}RDF")
    if rdf is None:
        return None
    descriptions = rdf.findall(f"{{{_RDF}}}Description")
    if not descriptions:
        descriptions = [ET.SubElement(rdf, f"{{{_RDF}}}Description", {f"{{{_RDF}}}about": ""})]
    for key, value in values.items():
        _set_property(descriptions, key, _xmp_text(key, value) if value else None)
    return match["head"] + ET.tostring(root, encoding="unicode") + match["tail"]


def write_entries(document: Document, values: Mapping[str, str], *, replace: bool = False) -> None:
    """Apply validated values in one transaction, where ``""`` removes an entry.

    With ``replace``, every entry not in ``values`` is removed too. An existing
    XMP packet gets the same values, so that the two stay equivalent.
    """
    if replace:
        values = {key: values.get(key, "") for key in METADATA_KEYS}
    with document.transaction("metadata"):
        info = _info(document)
        if info is None:
            if not any(values.values()):
                return
            objects = document.objects
            info = objects[objects.add({})]
            document.trailer["Info"] = info
        for key, value in values.items():
            pdf_key = METADATA_KEYS[key]
            if not value:
                if pdf_key in info:
                    del info[pdf_key]
            else:
                info[pdf_key] = Name(value) if key == "trapped" else value
        packet = read_xmp(document)
        if packet is not None and (updated := _updated_xmp(packet, values)) is not None:
            write_xmp(document, updated)


class Metadata(MutableMapping[str, str]):
    """The document's Info dictionary as a live mapping, as ``doc.metadata`` returns it.

    The keys are ``title``, ``author``, ``subject``, ``keywords``,
    ``creator``, ``producer``, ``created``, ``modified``, and ``trapped``;
    only the entries present are listed. Values are strings as stored, the
    dates as PDF date strings such as ``"D:20240101120000Z"``; assign a
    :class:`datetime` to ``created`` or ``modified`` to store one. Each
    assignment or deletion is one transaction, and :meth:`update` applies all
    of its values in one. When the document has an XMP packet, its matching
    properties are written in the same transaction, so that the two stay
    equivalent as PDF/A requires.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap a document; use ``Document.metadata``."""
        self._document = document

    def _entries(self) -> dict[str, str]:
        self._document._ensure_open()  # noqa: SLF001 - the document's own view.
        return _entries(self._document)

    def __getitem__(self, key: str) -> str:
        """Return the entry ``key``; an absent or unknown key raises :class:`KeyError`."""
        return self._entries()[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the keys present, in the order of ``METADATA_KEYS``."""
        return iter(self._entries())

    def __len__(self) -> int:
        """Return the number of entries present."""
        return len(self._entries())

    def __setitem__(self, key: str, value: str | datetime) -> None:
        """Store ``value`` under ``key``."""
        self.update({key: value})

    def __delitem__(self, key: str) -> None:
        """Remove ``key``; an absent key raises :class:`KeyError`."""
        if key not in self._entries():
            raise KeyError(key)
        write_entries(self._document, {key: ""})

    def update(self, other: object = (), /, **values: str | datetime) -> None:
        """Store every value of ``other`` and ``values`` in one transaction, or none of them.

        ``other`` is a mapping or an iterable of key and value pairs, as for
        :meth:`dict.update`.
        """
        merged = dict(cast("Mapping[str, object] | Iterable[tuple[str, object]]", other))
        merged.update(values)
        checked = {key: _metadata_value(key, value) for key, value in merged.items()}
        self._document._ensure_open()  # noqa: SLF001 - the document's own view.
        write_entries(self._document, checked)

    def clear(self) -> None:
        """Remove every entry, and the matching XMP properties."""
        self._document._ensure_open()  # noqa: SLF001 - the document's own view.
        write_entries(self._document, {}, replace=True)

    def __repr__(self) -> str:
        """Return ``Metadata({...})`` with the entries present."""
        return f"Metadata({self._entries()!r})"


class PageMode(enum.StrEnum):
    """How a viewer opens the document (``/PageMode``, ISO 32000-2 Table 29)."""

    USE_NONE = "UseNone"
    USE_OUTLINES = "UseOutlines"
    USE_THUMBS = "UseThumbs"
    FULL_SCREEN = "FullScreen"
    USE_OC = "UseOC"
    USE_ATTACHMENTS = "UseAttachments"


class PageLayout(enum.StrEnum):
    """How a viewer lays pages out (``/PageLayout``, ISO 32000-2 Table 29)."""

    SINGLE_PAGE = "SinglePage"
    ONE_COLUMN = "OneColumn"
    TWO_COLUMN_LEFT = "TwoColumnLeft"
    TWO_COLUMN_RIGHT = "TwoColumnRight"
    TWO_PAGE_LEFT = "TwoPageLeft"
    TWO_PAGE_RIGHT = "TwoPageRight"


@dataclass(frozen=True, slots=True, kw_only=True)
class MarkInfo:
    """The catalog's ``/MarkInfo`` flags of a tagged document (ISO 32000-2 Table 356).

    ``marked`` says whether the document conforms to the Tagged PDF
    conventions, ``user_properties`` whether structure elements carry user
    properties, and ``suspects`` whether the tags may be unreliable.
    """

    marked: bool = False
    """Whether the document conforms to the tagged PDF conventions."""
    user_properties: bool = False
    """Whether structure elements carry user properties."""
    suspects: bool = False
    """Whether the tags may be unreliable."""

    def __post_init__(self) -> None:
        """Refuse a flag that is not a bool."""
        for name in ("marked", "user_properties", "suspects"):
            if not isinstance(getattr(self, name), bool):
                msg = f"{name} must be a bool: {getattr(self, name)!r}"
                raise TypeError(msg)


class ReadingDirection(enum.StrEnum):
    """The reading order of the text, which lays out side-by-side pages (``/Direction``)."""

    L2R = "L2R"
    """Left to right."""
    R2L = "R2L"
    """Right to left, and vertical writing."""


class PageBoundary(enum.StrEnum):
    """A page box that viewers and printers show or clip to (``/ViewArea`` and its kin)."""

    MEDIA = "MediaBox"
    CROP = "CropBox"
    BLEED = "BleedBox"
    TRIM = "TrimBox"
    ART = "ArtBox"


class PrintScaling(enum.StrEnum):
    """Whether a print dialog scales pages by default (``/PrintScaling``)."""

    NONE = "None"
    """No scaling."""
    APP_DEFAULT = "AppDefault"
    """The viewer's own default."""


class Duplex(enum.StrEnum):
    """How a print dialog prints both sides by default (``/Duplex``)."""

    SIMPLEX = "Simplex"
    FLIP_SHORT_EDGE = "DuplexFlipShortEdge"
    FLIP_LONG_EDGE = "DuplexFlipLongEdge"


#: The most page ranges and enforced names that a ``ViewerPreferences`` holds.
_MAX_PRINT_PAGE_RANGES = 4_096
_MAX_ENFORCED = 64
_MAX_NAME_SIZE = 127
_MAX_LANGUAGE_TAG_BYTES = 256
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*", re.ASCII)
_DEFAULT_VERSION = (1, 7)
_CATALOG_VERSION_MIN = (1, 4)

#: The page modes that ``/NonFullScreenPageMode`` takes (ISO 32000-2 Table 147).
_NON_FULL_SCREEN_MODES = frozenset({PageMode.USE_NONE, PageMode.USE_OUTLINES, PageMode.USE_THUMBS, PageMode.USE_OC})

_BOOLEAN_PREFERENCES: tuple[tuple[str, str, tuple[int, int]], ...] = (
    ("hide_toolbar", "HideToolbar", (1, 2)),
    ("hide_menubar", "HideMenubar", (1, 2)),
    ("hide_window_ui", "HideWindowUI", (1, 2)),
    ("fit_window", "FitWindow", (1, 2)),
    ("center_window", "CenterWindow", (1, 2)),
    ("display_title", "DisplayDocTitle", (1, 4)),
    ("pick_tray_by_pdf_size", "PickTrayByPDFSize", (1, 7)),
)
_NAME_PREFERENCES: tuple[tuple[str, str, type[enum.StrEnum], tuple[int, int]], ...] = (
    ("non_full_screen_page_mode", "NonFullScreenPageMode", PageMode, (1, 2)),
    ("direction", "Direction", ReadingDirection, (1, 3)),
    ("view_area", "ViewArea", PageBoundary, (1, 4)),
    ("view_clip", "ViewClip", PageBoundary, (1, 4)),
    ("print_area", "PrintArea", PageBoundary, (1, 4)),
    ("print_clip", "PrintClip", PageBoundary, (1, 4)),
    ("print_scaling", "PrintScaling", PrintScaling, (1, 6)),
    ("duplex", "Duplex", Duplex, (1, 7)),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewerPreferences:
    """The catalog's ``/ViewerPreferences`` (ISO 32000-2 Table 147).

    Each field is ``None`` when the dictionary leaves the entry out, so the
    viewer uses its default; reading and assigning a record round-trips what
    pyropdf understands. Page ranges are zero-based and inclusive.
    """

    hide_toolbar: bool | None = None
    """Whether to hide the viewer's toolbars."""
    hide_menubar: bool | None = None
    """Whether to hide the viewer's menu bar."""
    hide_window_ui: bool | None = None
    """Whether to hide scroll bars and navigation controls."""
    fit_window: bool | None = None
    """Whether to resize the window to the first page."""
    center_window: bool | None = None
    """Whether to center the window on the screen."""
    display_title: bool | None = None
    """Whether the title bar shows the document's title rather than its file name."""
    non_full_screen_page_mode: PageMode | None = None
    """The page mode on leaving full-screen mode: ``USE_NONE``, ``USE_OUTLINES``, ``USE_THUMBS``, or ``USE_OC``."""
    direction: ReadingDirection | None = None
    """The reading order of the text."""
    view_area: PageBoundary | None = None
    """The box that a viewer shows (deprecated in PDF 2.0)."""
    view_clip: PageBoundary | None = None
    """The box that a viewer clips to (deprecated in PDF 2.0)."""
    print_area: PageBoundary | None = None
    """The box that printing shows (deprecated in PDF 2.0)."""
    print_clip: PageBoundary | None = None
    """The box that printing clips to (deprecated in PDF 2.0)."""
    print_scaling: PrintScaling | None = None
    """The print dialog's default scaling."""
    duplex: Duplex | None = None
    """The print dialog's default duplex mode."""
    pick_tray_by_pdf_size: bool | None = None
    """Whether the paper tray follows the page size."""
    print_page_ranges: tuple[tuple[int, int], ...] | None = None
    """The page ranges that the print dialog selects, as zero-based ``(first, last)`` pairs."""
    copies: int | None = None
    """The number of copies that the print dialog selects."""
    enforce: tuple[str, ...] | None = None
    """The preferences that a viewer must not let the user change, such as ``("PrintScaling",)``."""

    def __post_init__(self) -> None:
        """Validate every field and normalize enums and tuples."""
        for field_name, _, _ in _BOOLEAN_PREFERENCES:
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                msg = f"{field_name} must be a bool or None: {value!r}"
                raise TypeError(msg)
        for field_name, _, kind, _ in _NAME_PREFERENCES:
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, kind(value))
        if self.non_full_screen_page_mode is not None and self.non_full_screen_page_mode not in _NON_FULL_SCREEN_MODES:
            msg = f"non_full_screen_page_mode cannot be {self.non_full_screen_page_mode!r}"
            raise ValueError(msg)
        if self.print_page_ranges is not None:
            object.__setattr__(self, "print_page_ranges", _page_ranges(self.print_page_ranges))
        copies = self.copies
        if copies is not None and (isinstance(copies, bool) or not isinstance(copies, int) or copies < 1):
            msg = f"copies must be a positive int or None: {copies!r}"
            raise ValueError(msg)
        if self.enforce is not None:
            object.__setattr__(self, "enforce", _enforced_names(self.enforce))


def _page_ranges(ranges: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Validate print page ranges: zero-based ``(first, last)`` pairs."""
    if isinstance(ranges, (str, bytes)):
        msg = "print_page_ranges takes an iterable of (first, last) pairs"
        raise TypeError(msg)
    pairs: list[tuple[int, int]] = []
    for item in ranges:
        if len(pairs) >= _MAX_PRINT_PAGE_RANGES:
            msg = f"print_page_ranges holds at most {_MAX_PRINT_PAGE_RANGES} ranges"
            raise ValueError(msg)
        if isinstance(item, (str, bytes)):
            msg = f"a print page range is a pair of zero-based pages (first, last), first <= last: {item!r}"
            raise TypeError(msg)
        pair = tuple(item)
        if (
            len(pair) != 2  # noqa: PLR2004 - a range is a pair.
            or not all(isinstance(page, int) and not isinstance(page, bool) for page in pair)
            or not 0 <= pair[0] <= pair[1]
        ):
            msg = f"a print page range is a pair of zero-based pages (first, last), first <= last: {pair!r}"
            raise ValueError(msg)
        pairs.append((pair[0], pair[1]))
    return tuple(pairs)


def _is_preference_name(name: object) -> bool:
    return isinstance(name, str) and 0 < len(name) <= _MAX_NAME_SIZE and name.isascii() and name.isidentifier()


def _enforced_names(names: Iterable[str]) -> tuple[str, ...]:
    """Validate the names of enforced preferences."""
    if isinstance(names, (str, bytes)):
        msg = "enforce takes a tuple of names, such as ('PrintScaling',)"
        raise TypeError(msg)
    enforced: list[str] = []
    for name in names:
        if len(enforced) >= _MAX_ENFORCED or not _is_preference_name(name):
            msg = f"enforce takes up to {_MAX_ENFORCED} preference names, such as 'PrintScaling': {name!r}"
            raise ValueError(msg)
        enforced.append(name)
    return tuple(enforced)


def _read_enforced(enforce: object) -> tuple[str, ...] | None:
    """Read ``/Enforce`` as names, or ``None`` when it is malformed."""
    if not isinstance(enforce, Array) or len(enforce) > _MAX_ENFORCED:
        return None
    if not all(isinstance(name, Name) for name in enforce):
        return None
    names = tuple(str(name) for name in enforce)
    return names if all(_is_preference_name(name) for name in names) else None


def _read_page_ranges(ranges: object) -> tuple[tuple[int, int], ...] | None:
    """Read ``/PrintPageRange`` as zero-based pairs, or ``None`` when it is malformed."""
    if not isinstance(ranges, Array) or len(ranges) > 2 * _MAX_PRINT_PAGE_RANGES or len(ranges) % 2:
        return None
    pages = list(ranges)
    if not all(isinstance(page, int) and not isinstance(page, bool) for page in pages):
        return None
    pairs = tuple((first - 1, last - 1) for first, last in zip(pages[::2], pages[1::2], strict=True))
    return pairs if all(0 <= first <= last for first, last in pairs) else None


def _viewer_preferences(entries: Mapping[str, object]) -> ViewerPreferences:
    """Read a ``/ViewerPreferences`` dictionary, leaving out entries of the wrong type or value."""
    fields: dict[str, object] = {}
    for field_name, key, _ in _BOOLEAN_PREFERENCES:
        value = entries.get(key)
        if isinstance(value, bool):
            fields[field_name] = value
    for field_name, key, kind, _ in _NAME_PREFERENCES:
        value = entries.get(key)
        if isinstance(value, Name):
            try:
                fields[field_name] = kind(str(value))
            except ValueError:
                continue
    if fields.get("non_full_screen_page_mode") not in {None, *_NON_FULL_SCREEN_MODES}:
        del fields["non_full_screen_page_mode"]
    ranges = _read_page_ranges(entries.get("PrintPageRange"))
    if ranges is not None:
        fields["print_page_ranges"] = ranges
    copies = entries.get("NumCopies")
    if isinstance(copies, int) and not isinstance(copies, bool) and copies >= 1:
        fields["copies"] = copies
    enforce = _read_enforced(entries.get("Enforce"))
    if enforce is not None:
        fields["enforce"] = enforce
    return ViewerPreferences(**fields)  # ty: ignore[invalid-argument-type]


def _viewer_preference_dict(preferences: ViewerPreferences) -> tuple[dict[str, object], tuple[int, int]]:
    """Return the PDF dictionary entries and minimum PDF version of ``preferences``."""
    entries: dict[str, object] = {}
    minimum = (1, 2)
    for field_name, key, since in _BOOLEAN_PREFERENCES:
        value = getattr(preferences, field_name)
        if value is not None:
            entries[key] = value
            minimum = max(minimum, since)
    for field_name, key, _, since in _NAME_PREFERENCES:
        value = getattr(preferences, field_name)
        if value is not None:
            entries[key] = Name(value.value)
            minimum = max(minimum, since)
    if preferences.print_page_ranges is not None:
        entries["PrintPageRange"] = [
            page for first, last in preferences.print_page_ranges for page in (first + 1, last + 1)
        ]
        minimum = max(minimum, (1, 7))
    if preferences.copies is not None:
        entries["NumCopies"] = preferences.copies
        minimum = max(minimum, (1, 7))
    if preferences.enforce is not None:
        entries["Enforce"] = [Name(name) for name in preferences.enforce]
        minimum = max(minimum, (1, 7))
    return entries, minimum


def _parse_version(text: str) -> tuple[int, int] | None:
    major, _, minor = text.partition(".")
    return (int(major), int(minor)) if major.isdigit() and minor.isdigit() else None


def _require_version(document: Document, catalog: Dictionary, minimum: tuple[int, int]) -> None:
    """Raise ``catalog['Version']`` when ``document`` declares an older PDF version than ``minimum``."""
    header = _core.header_version(document._core())  # noqa: SLF001
    version = (_parse_version(header) if header is not None else None) or _DEFAULT_VERSION
    declared = catalog.get("Version")
    if isinstance(declared, Name):
        version = max(version, _parse_version(str(declared)) or version)
    if version < minimum:
        target = max(minimum, _CATALOG_VERSION_MIN)
        catalog["Version"] = Name(f"{target[0]}.{target[1]}")


def _catalog_enum(kind: type[PageMode | PageLayout], value: object, name: str) -> str:
    """Return the catalog name of a page mode or layout, given as a member or its exact value."""
    if isinstance(value, str):
        try:
            return str(kind(value))
        except ValueError:
            pass
    msg = f"{name} must be one of {[member.value for member in kind]}: {value!r}"
    raise ValueError(msg)


def _validate_language_tag(tag: object) -> str:
    """Validate a BCP 47 language tag of at most 256 ASCII bytes."""
    if not isinstance(tag, str):
        msg = f"language must be a str: {type(tag).__name__}"
        raise TypeError(msg)
    if not tag.isascii() or len(tag) > _MAX_LANGUAGE_TAG_BYTES or _LANGUAGE_TAG.fullmatch(tag) is None:
        msg = f"not a well-formed BCP 47 language tag: {tag!r}"
        raise PdfError(msg)
    return tag


def read_page_mode(document: Document) -> PageMode:
    """Return the catalog's ``/PageMode``, defaulting to :attr:`PageMode.USE_NONE`."""
    value = document.catalog.get("PageMode")
    try:
        return PageMode(str(value)) if isinstance(value, Name) else PageMode.USE_NONE
    except ValueError:
        return PageMode.USE_NONE


def write_page_mode(document: Document, mode: PageMode | str) -> None:
    """Write ``mode`` to the catalog's ``/PageMode`` in one transaction."""
    name = _catalog_enum(PageMode, mode, "page_mode")
    catalog = document.catalog
    with document.transaction("page mode"):
        catalog["PageMode"] = Name(name)


def delete_page_mode(document: Document) -> None:
    """Remove ``/PageMode`` from the catalog when present."""
    catalog = document.catalog
    if "PageMode" in catalog:
        with document.transaction("page mode"):
            del catalog["PageMode"]


def read_page_layout(document: Document) -> PageLayout:
    """Return the catalog's ``/PageLayout``, defaulting to :attr:`PageLayout.SINGLE_PAGE`."""
    value = document.catalog.get("PageLayout")
    try:
        return PageLayout(str(value)) if isinstance(value, Name) else PageLayout.SINGLE_PAGE
    except ValueError:
        return PageLayout.SINGLE_PAGE


def write_page_layout(document: Document, layout: PageLayout | str) -> None:
    """Write ``layout`` to the catalog's ``/PageLayout`` in one transaction."""
    name = _catalog_enum(PageLayout, layout, "page_layout")
    catalog = document.catalog
    with document.transaction("page layout"):
        catalog["PageLayout"] = Name(name)


def delete_page_layout(document: Document) -> None:
    """Remove ``/PageLayout`` from the catalog when present."""
    catalog = document.catalog
    if "PageLayout" in catalog:
        with document.transaction("page layout"):
            del catalog["PageLayout"]


def read_language(document: Document) -> str | None:
    """Return the catalog's ``/Lang`` text string as stored, or ``None``."""
    value = document.catalog.get("Lang")
    return value.text if isinstance(value, String) else None


def write_language(document: Document, tag: str) -> None:
    """Write validated BCP 47 ``tag`` to the catalog's ``/Lang`` in one transaction."""
    checked = _validate_language_tag(tag)
    catalog = document.catalog
    with document.transaction("language"):
        _require_version(document, catalog, (1, 4))
        catalog["Lang"] = checked


def delete_language(document: Document) -> None:
    """Remove ``/Lang`` from the catalog when present."""
    catalog = document.catalog
    if "Lang" in catalog:
        with document.transaction("language"):
            del catalog["Lang"]


def read_mark_info(document: Document) -> MarkInfo | None:
    """Return the catalog's ``/MarkInfo`` flags, or ``None`` without a ``/MarkInfo`` dictionary."""
    mark_info = document.catalog.get("MarkInfo")
    if not isinstance(mark_info, Dictionary):
        return None
    return MarkInfo(
        marked=mark_info.get("Marked") is True,
        user_properties=mark_info.get("UserProperties") is True,
        suspects=mark_info.get("Suspects") is True,
    )


def write_mark_info(document: Document, flags: MarkInfo) -> None:
    """Write ``flags`` to the catalog's ``/MarkInfo`` in one transaction."""
    if not isinstance(flags, MarkInfo):
        msg = f"mark_info must be a MarkInfo: {type(flags).__name__}"
        raise TypeError(msg)
    catalog = document.catalog
    entries = {
        "Marked": flags.marked,
        "UserProperties": flags.user_properties,
        "Suspects": flags.suspects,
    }
    existing = catalog.get("MarkInfo")
    with document.transaction("mark info"):
        _require_version(document, catalog, (1, 6))
        if isinstance(existing, Dictionary) and existing.is_indirect and existing.xref is not None:
            document.objects[existing.xref] = entries
        else:
            catalog["MarkInfo"] = entries


def delete_mark_info(document: Document) -> None:
    """Remove ``/MarkInfo`` from the catalog when present."""
    catalog = document.catalog
    if "MarkInfo" in catalog:
        with document.transaction("mark info"):
            del catalog["MarkInfo"]


def read_viewer_preferences(document: Document) -> ViewerPreferences | None:
    """Return the catalog's ``/ViewerPreferences``, or ``None`` without the dictionary."""
    preferences = document.catalog.get("ViewerPreferences")
    return _viewer_preferences(preferences) if isinstance(preferences, Dictionary) else None


def write_viewer_preferences(document: Document, preferences: ViewerPreferences) -> None:
    """Write ``preferences`` to the catalog's ``/ViewerPreferences`` in one transaction."""
    if not isinstance(preferences, ViewerPreferences):
        msg = f"viewer_preferences must be a ViewerPreferences: {type(preferences).__name__}"
        raise TypeError(msg)
    catalog = document.catalog
    entries, minimum = _viewer_preference_dict(preferences)
    existing = catalog.get("ViewerPreferences")
    with document.transaction("viewer preferences"):
        _require_version(document, catalog, minimum)
        if isinstance(existing, Dictionary) and existing.is_indirect and existing.xref is not None:
            document.objects[existing.xref] = entries
        else:
            catalog["ViewerPreferences"] = entries


def delete_viewer_preferences(document: Document) -> None:
    """Remove ``/ViewerPreferences`` from the catalog when present."""
    catalog = document.catalog
    if "ViewerPreferences" in catalog:
        with document.transaction("viewer preferences"):
            del catalog["ViewerPreferences"]


for _public in (
    Metadata,
    PageMode,
    PageLayout,
    MarkInfo,
    ReadingDirection,
    PageBoundary,
    PrintScaling,
    Duplex,
    ViewerPreferences,
):
    _public.__module__ = "pyropdf"

__all__ = [
    "METADATA_KEYS",
    "Duplex",
    "MarkInfo",
    "Metadata",
    "PageBoundary",
    "PageLayout",
    "PageMode",
    "PrintScaling",
    "ReadingDirection",
    "ViewerPreferences",
    "parse_pdf_date",
    "pdf_date",
]
