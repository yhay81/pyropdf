"""Document metadata: the Info dictionary and the XMP packet, kept in step."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

from pyropdf._objects import Dictionary, Name, Stream, String

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


Metadata.__module__ = "pyropdf"
