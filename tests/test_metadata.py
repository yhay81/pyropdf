"""Document metadata: the Info dictionary and the XMP packet, kept in step.

Ported from pylopdf's tests/test_document_properties.py (pylopdf
ef5bd814ca8b513cd79d9ca85ba0fcda8904ed29), with the XMP packet now written in
step with the Info dictionary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_pages import build_raw_pdf

import pyropdf
from pyropdf import LimitError

REAL_WORLD = Path(__file__).parent / "assets" / "real_world"
PDFA = Path(__file__).parent / "assets" / "pdfa"
XMP = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:pdf="http://ns.adobe.com/pdf/1.3/" pdf:Producer="Old producer">'
    '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">Old</rdf:li></rdf:Alt></dc:title>'
    "</rdf:Description></rdf:RDF></x:xmpmeta>\n"
    '<?xpacket end="w"?>'
)


def _pdf(catalog: str = "", *extra: str, trailer: str = "") -> bytes:
    """Build an empty document whose catalog holds ``catalog``; extra objects start at 3."""
    objects = {
        1: f"<< /Type /Catalog /Pages 2 0 R {catalog} >>",
        2: "<< /Type /Pages /Kids [] /Count 0 >>",
        **dict(enumerate(extra, start=3)),
    }
    data = build_raw_pdf(objects)
    if trailer:
        data = data.replace(b"/Root 1 0 R", b"/Root 1 0 R " + trailer.encode(), 1)
    return data


def _xmp_pdf(data: bytes, *, compressed: bool = False) -> bytes:
    """Build an empty document whose catalog names a metadata stream of ``data``."""
    doc = pyropdf.Document()
    entries = {"Type": pyropdf.Name("Metadata"), "Subtype": pyropdf.Name("XML")}
    number = doc.objects.add_stream(data, entries, filter="FlateDecode" if compressed else None)
    doc.catalog["Metadata"] = doc.objects[number]
    return doc.to_bytes()


# --- the Info dictionary ----------------------------------------------------


def test_metadata_lists_only_the_entries_present() -> None:
    doc = pyropdf.open(_pdf("", "<< /Title (A title) /Trapped /False /Author () >>", trailer="/Info 3 0 R"))
    assert dict(doc.metadata) == {"title": "A title", "trapped": "False"}
    assert "author" not in doc.metadata
    assert dict(pyropdf.Document().metadata) == {}


def test_assigning_an_entry_adds_an_indirect_info_dictionary() -> None:
    doc = pyropdf.Document()
    doc.metadata["title"] = "日本語のタイトル"
    info = doc.trailer["Info"]
    assert info.is_indirect
    assert info["Title"].text == "日本語のタイトル"
    reopened = pyropdf.open(doc.to_bytes())
    assert reopened.metadata["title"] == "日本語のタイトル"
    assert pyropdf.probe(doc.to_bytes()).metadata["title"] == "日本語のタイトル"


def test_update_is_one_undo_step_and_deleting_removes_the_entry() -> None:
    doc = pyropdf.Document()
    doc.metadata.update(title="T", author="A", trapped="True")
    assert doc.trailer["Info"]["Trapped"] == pyropdf.Name("True")
    del doc.metadata["author"]
    assert dict(doc.metadata) == {"title": "T", "trapped": "True"}
    doc.history.undo()
    assert dict(doc.metadata) == {"title": "T", "author": "A", "trapped": "True"}
    doc.history.undo()
    assert dict(doc.metadata) == {}


def test_assigning_a_mapping_replaces_every_entry() -> None:
    doc = pyropdf.open(_pdf("", "<< /Title (Old) /Author (Me) >>", trailer="/Info 3 0 R"))
    doc.metadata = {"subject": "New"}
    assert dict(doc.metadata) == {"subject": "New"}
    doc.metadata.clear()
    assert dict(doc.metadata) == {}


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("color", "red", KeyError),
        ("title", 1, TypeError),
        ("title", "", ValueError),
        ("trapped", "Maybe", ValueError),
    ],
)
def test_invalid_values_change_nothing(key: str, value: object, error: type[Exception]) -> None:
    doc = pyropdf.Document()
    with pytest.raises(error):
        doc.metadata.update({"author": "kept out", key: value})
    assert "Info" not in doc.trailer
    with pytest.raises(TypeError, match="mapping"):
        doc.metadata = [("title", "T")]  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    ("stored", "text"),
    [
        (b"one\ttwo\nthree\rfour", "one\ttwo\nthree\rfour"),
        (b"\xef\xbb\xbfcaf\xc3\xa9", "café"),
        (b"\xfe\xff\x00A\x00B\x34", "AB"),
    ],
)
def test_info_text_string_decoding(stored: bytes, text: str) -> None:
    doc = pyropdf.Document()
    doc.trailer["Info"] = doc.objects[doc.objects.add({"Title": pyropdf.String(stored)})]
    data = doc.to_bytes()
    assert pyropdf.open(data).metadata["title"] == text
    assert pyropdf.probe(data).metadata["title"] == text


# --- dates --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "moment"),
    [
        ("D:20240102030405Z", datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("D:20240102030405+09'00'", datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=9)))),
        ("D:20240102030405-05'30", datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(-timedelta(hours=5, minutes=30)))),
        ("D:2024", datetime(2024, 1, 1)),  # noqa: DTZ001 - a date without a zone is naive.
        ("D:20241302", None),
        ("2024", None),
    ],
)
def test_parse_pdf_date(text: str, moment: datetime | None) -> None:
    assert pyropdf.parse_pdf_date(text) == moment


def test_pdf_date_formats_time_zones_and_round_trips() -> None:
    tokyo = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=9)))
    assert pyropdf.pdf_date(tokyo) == "D:20240102030405+09'00'"
    assert pyropdf.pdf_date(datetime(2024, 1, 2, tzinfo=UTC)) == "D:20240102000000Z"
    assert pyropdf.pdf_date(datetime(2024, 1, 2)) == "D:20240102000000Z"  # noqa: DTZ001 - naive is UTC.
    assert pyropdf.parse_pdf_date(pyropdf.pdf_date(tokyo)) == tokyo


def test_assigning_a_datetime_stores_a_pdf_date() -> None:
    doc = pyropdf.Document()
    doc.metadata["modified"] = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert doc.metadata["modified"] == "D:20240102030405Z"
    with pytest.raises(TypeError):
        doc.metadata["title"] = datetime(2024, 1, 2, tzinfo=UTC)


# --- the XMP packet -----------------------------------------------------------


def test_xmp_is_none_without_a_metadata_stream() -> None:
    assert pyropdf.Document().xmp is None
    assert pyropdf.open(_pdf("/Metadata 3 0 R", "<< /Type /Metadata >>")).xmp is None


def test_xmp_decodes_filters_invalid_utf8_and_nul_as_pymupdf() -> None:
    xmp = '<x:xmpmeta xmlns:x="adobe:ns:meta/">日本語</x:xmpmeta>'
    assert pyropdf.open(_xmp_pdf(xmp.encode(), compressed=True)).xmp == xmp
    assert pyropdf.open(_xmp_pdf(b"<a>\xff</a>\x00")).xmp == "<a>�</a>"


@pytest.mark.parametrize(
    ("name", "length"),
    [("bill-hr815.pdf", 5440), ("f1040.pdf", 4090), ("pdf20-simple.pdf", 2351), ("wdl6812-manuscript.pdf", 2004)],
)
def test_real_world_xmp_matches_pymupdf_lengths(name: str, length: int) -> None:
    # pymupdf 1.28.2 returns text of the same lengths for these files (pylopdf).
    xmp = pyropdf.open(REAL_WORLD / name).xmp
    assert xmp is not None
    assert len(xmp) == length
    assert pyropdf.open(REAL_WORLD / "usrguide.pdf").xmp is None


def test_xmp_read_is_bounded() -> None:
    doc = pyropdf.open(_xmp_pdf(b"<x/>" + b" " * 2_000_000, compressed=True))
    with pytest.raises(LimitError) as caught:
        _ = doc.xmp
    assert caught.value.code == "stream_data_size"
    assert len(doc.catalog["Metadata"].read(max_size=None)) == 2_000_004


def test_assigning_xmp_adds_an_unfiltered_metadata_stream(tmp_path: Path) -> None:
    doc = pyropdf.Document()
    doc.xmp = "<x:xmpmeta>héllo</x:xmpmeta>"
    stream = doc.catalog["Metadata"]
    assert isinstance(stream, pyropdf.Stream)
    assert stream["Type"] == "Metadata"
    assert stream["Subtype"] == "XML"
    assert "Filter" not in stream
    path = tmp_path / "xmp.pdf"
    doc.save(path, compress=True)
    assert "<x:xmpmeta>héllo</x:xmpmeta>".encode() in path.read_bytes()
    assert pyropdf.open(path).xmp == "<x:xmpmeta>héllo</x:xmpmeta>"


def test_assigning_xmp_rewrites_an_existing_stream_in_place() -> None:
    doc = pyropdf.open(_xmp_pdf(b"<old/>", compressed=True))
    xref = doc.catalog["Metadata"].xref
    doc.xmp = "<new/>"
    stream = doc.catalog["Metadata"]
    assert stream.xref == xref
    assert "Filter" not in stream
    assert stream.read(decode=False) == b"<new/>"
    with pytest.raises(TypeError, match="str"):
        doc.xmp = b"<x/>"  # ty: ignore[invalid-assignment]


def test_deleting_xmp_removes_the_catalog_entry() -> None:
    doc = pyropdf.open(_xmp_pdf(b"<old/>"))
    del doc.xmp
    assert doc.xmp is None
    del doc.xmp  # Removing absent metadata changes nothing.
    assert pyropdf.open(doc.to_bytes()).xmp is None


# --- Info and XMP in step -----------------------------------------------------


def test_info_edits_write_the_matching_xmp_properties() -> None:
    doc = pyropdf.open(_xmp_pdf(XMP.encode()))
    doc.metadata.update(
        title="New title",
        author="An author",
        producer="pyropdf",
        created=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=9))),
    )
    xmp = doc.xmp
    assert xmp is not None
    assert xmp.startswith('<?xpacket begin="﻿"')
    assert xmp.endswith('<?xpacket end="w"?>')
    assert '<rdf:li xml:lang="x-default">New title</rdf:li>' in xmp
    assert "<rdf:Seq><rdf:li>An author</rdf:li></rdf:Seq>" in xmp
    assert "<pdf:Producer>pyropdf</pdf:Producer>" in xmp
    assert "Old producer" not in xmp
    assert "<xmp:CreateDate>2024-01-02T03:04:05+09:00</xmp:CreateDate>" in xmp
    del doc.metadata["title"]
    assert "dc:title" not in (doc.xmp or "")
    assert pyropdf.open(doc.to_bytes()).metadata["producer"] == "pyropdf"


def test_info_edits_leave_a_packet_that_is_not_xmp_unchanged() -> None:
    doc = pyropdf.open(_xmp_pdf(b"<old/>"))
    doc.metadata["title"] = "New"
    doc.metadata = {"author": "Me"}
    assert doc.xmp == "<old/>"


@pytest.mark.parametrize("name", sorted(path.name for path in PDFA.glob("*.pdf")))
def test_pdfa_documents_keep_their_claim_and_equivalent_metadata(name: str) -> None:
    doc = pyropdf.open(PDFA / name)
    claim = doc.pdfa_claim
    doc.metadata.update(title="Retitled", author="Someone")
    assert doc.pdfa_claim == claim
    xmp = doc.xmp or ""
    assert ">Retitled</rdf:li>" in xmp
    assert ">Someone</rdf:li>" in xmp
    assert not doc.changes(incremental=True).claims
    assert not [defect for defect in doc.check().defects if defect.kind == "profile"]
