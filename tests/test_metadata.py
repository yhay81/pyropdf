"""Document metadata and catalog properties: Info, XMP, page mode/layout, language, and MarkInfo.

Ported from pylopdf's tests/test_document_properties.py (pylopdf
ef5bd814ca8b513cd79d9ca85ba0fcda8904ed29 and freeze commit ebb01d3c), with the
XMP packet now written in step with the Info dictionary and catalog properties
backed directly by the versioned store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_pages import build_raw_pdf
from test_preservation import snapshot as _snapshot

import pyropdf
from pyropdf import LimitError, MarkInfo, PageLayout, PageMode

ASSETS = Path(__file__).parent / "assets"
REAL_WORLD = ASSETS / "real_world"
PDFA = ASSETS / "pdfa"
PDFUA = ASSETS / "pdfua"
ENCRYPTED = ASSETS / "encrypted"
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


@pytest.mark.spec("document-info", "read")
def test_metadata_lists_only_the_entries_present() -> None:
    doc = pyropdf.open(_pdf("", "<< /Title (A title) /Trapped /False /Author () >>", trailer="/Info 3 0 R"))
    assert dict(doc.metadata) == {"title": "A title", "trapped": "False"}
    assert "author" not in doc.metadata
    assert dict(pyropdf.Document().metadata) == {}


@pytest.mark.spec("document-info", "create")
def test_assigning_an_entry_adds_an_indirect_info_dictionary() -> None:
    doc = pyropdf.Document()
    doc.metadata["title"] = "日本語のタイトル"
    info = doc.trailer["Info"]
    assert info.is_indirect
    assert info["Title"].text == "日本語のタイトル"
    reopened = pyropdf.open(doc.to_bytes())
    assert reopened.metadata["title"] == "日本語のタイトル"
    assert pyropdf.probe(doc.to_bytes()).metadata["title"] == "日本語のタイトル"


@pytest.mark.spec("document-info", "edit")
@pytest.mark.spec("text-strings", "edit")
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
@pytest.mark.spec("document-info", "interpret")
@pytest.mark.spec("text-strings", "interpret")
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
@pytest.mark.spec("dates", "read", "interpret")
def test_parse_pdf_date(text: str, moment: datetime | None) -> None:
    assert pyropdf.parse_pdf_date(text) == moment


@pytest.mark.spec("dates", "create")
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


@pytest.mark.spec("xmp-metadata", "read")
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


@pytest.mark.spec("xmp-metadata", "create")
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


@pytest.mark.spec("dates", "edit")
@pytest.mark.spec("xmp-metadata", "interpret", "edit")
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
@pytest.mark.spec("output-intents", "preserve")
@pytest.mark.spec("pdfa", "edit", "preserve")
@pytest.mark.spec("xmp-metadata", "preserve")
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


# --- page mode and page layout ------------------------------------------------


@pytest.mark.spec("document-catalog", "read", "interpret")
def test_page_mode_and_layout_default_to_their_specified_values() -> None:
    doc = pyropdf.Document()
    assert doc.page_mode is PageMode.USE_NONE
    assert doc.page_layout is PageLayout.SINGLE_PAGE
    # A value that is no name, or a name outside ISO 32000-2 Table 29, reads as unset.
    odd = pyropdf.open(_pdf("/PageMode (UseOutlines) /PageLayout 7"))
    assert odd.page_mode is PageMode.USE_NONE
    assert odd.page_layout is PageLayout.SINGLE_PAGE
    custom = pyropdf.open(_pdf("/PageMode /Custom /PageLayout 3 0 R", "/TwoColumnLeft"))
    assert custom.page_mode is PageMode.USE_NONE
    assert custom.page_layout is PageLayout.TWO_COLUMN_LEFT
    assert pyropdf.open(REAL_WORLD / "usrguide.pdf").page_mode is PageMode.USE_OUTLINES


@pytest.mark.parametrize("mode", [PageMode.FULL_SCREEN, PageMode.USE_OC, "UseAttachments", "UseNone"])
@pytest.mark.spec("document-catalog", "edit")
def test_assigning_and_deleting_page_mode_writes_and_removes_the_name(mode: PageMode | str) -> None:
    seed = pyropdf.Document()
    seed.pages.new((200, 200))
    doc = pyropdf.open(seed.to_bytes())
    before = _snapshot(doc)
    catalog_xref = doc.catalog.xref

    doc.page_mode = mode
    assert doc.catalog["PageMode"] == mode
    assert isinstance(doc.catalog["PageMode"], pyropdf.Name)
    assert not doc.check()

    reopened = pyropdf.open(doc.to_bytes())
    assert reopened.page_mode == mode
    assert not reopened.check()
    after = _snapshot(reopened)
    assert [xref for xref, value in before.items() if xref != catalog_xref and after.get(xref) != value] == []

    doc.history.undo()
    assert "PageMode" not in doc.catalog
    doc.history.redo()
    assert doc.page_mode == mode

    del doc.page_mode
    assert "PageMode" not in doc.catalog
    assert doc.page_mode is PageMode.USE_NONE
    assert not doc.check()
    del doc.page_mode  # Removing an absent entry changes nothing.
    doc.history.undo()
    assert doc.page_mode == mode


@pytest.mark.parametrize("layout", [PageLayout.ONE_COLUMN, "TwoPageRight", "TwoColumnLeft"])
def test_assigning_and_deleting_page_layout_writes_and_removes_the_name(layout: PageLayout | str) -> None:
    doc = pyropdf.open(_pdf())
    doc.page_layout = layout
    assert doc.catalog["PageLayout"] == layout
    assert isinstance(doc.catalog["PageLayout"], pyropdf.Name)
    assert not doc.check()
    assert pyropdf.open(doc.to_bytes(incremental=True)).page_layout == layout

    del doc.page_layout
    assert "PageLayout" not in doc.catalog
    assert doc.page_layout is PageLayout.SINGLE_PAGE
    del doc.page_layout
    assert pyropdf.open(doc.to_bytes()).page_layout is PageLayout.SINGLE_PAGE


@pytest.mark.parametrize("value", ["", "/", "Custom", "SinglePage", "useoc", "/UseOC", None, 1])
def test_page_mode_and_layout_refuse_other_values(value: object) -> None:
    doc = pyropdf.Document()
    with pytest.raises(ValueError, match="page_mode must be one of"):
        doc.page_mode = value  # ty: ignore[invalid-assignment]
    assert "PageMode" not in doc.catalog
    assert not doc.history.can_undo

    with pytest.raises(ValueError, match="page_layout must be one of"):
        doc.page_layout = "UseNone"
    assert "PageLayout" not in doc.catalog


# --- language tags ------------------------------------------------------------


@pytest.mark.spec("accessibility", "read", "interpret")
def test_language_reads_the_stored_tag_unchanged() -> None:
    assert pyropdf.Document().language is None
    assert pyropdf.open(_pdf("/Lang (en-GB)")).language == "en-GB"
    assert pyropdf.open(_pdf("/Lang 3 0 R", "(zh-HK)")).language == "zh-HK"
    utf16 = "<FEFF007A0068002D00540057>"  # "zh-TW" as a UTF-16BE text string
    assert pyropdf.open(_pdf(f"/Lang {utf16}")).language == "zh-TW"
    # A malformed stored value reads as stored; a value that is no string reads as absent.
    assert pyropdf.open(_pdf("/Lang (not a tag)")).language == "not a tag"
    assert pyropdf.open(_pdf("/Lang /en")).language is None


@pytest.mark.parametrize(
    "tag", ["zh-CN", "zh-TW", "zh-HK", "en-US", "pt-BR", "sr-Latn-RS", "ja", "x-private", "de-CH-1996"]
)
@pytest.mark.spec("accessibility", "create", "edit")
def test_assigning_language_keeps_the_complete_tag_and_round_trips(tag: str) -> None:
    doc = pyropdf.open(_pdf())
    doc.language = tag
    assert doc.language == tag
    lang = doc.catalog["Lang"]
    assert isinstance(lang, pyropdf.String)
    assert bytes(lang) == tag.encode()
    assert not doc.check()
    assert pyropdf.open(doc.to_bytes(incremental=True)).language == tag
    assert pyropdf.open(doc.to_bytes()).language == tag


def test_deleting_and_undoing_language() -> None:
    doc = pyropdf.open(_pdf("/Lang (en-US)"))
    doc.language = "a" + "-a" * 127  # 255 ASCII bytes
    assert doc.language == "a" + "-a" * 127
    doc.history.undo()
    assert doc.language == "en-US"

    del doc.language
    assert "Lang" not in doc.catalog
    assert doc.language is None
    assert not doc.check()
    del doc.language  # Removing an absent tag changes nothing.
    assert pyropdf.open(doc.to_bytes()).language is None
    doc.history.undo()
    assert doc.language == "en-US"


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "-",
        "en-",
        "en--US",
        "1en",
        "en_US",
        "en US",
        "toolongtag",
        "en-123456789",
        "ja\n",
        "日本語",
        "a" + "-a" * 128,
    ],
)
def test_language_refuses_malformed_tags_and_non_strings(tag: str) -> None:
    doc = pyropdf.open(_pdf("/Lang (ja)"))
    with pytest.raises(pyropdf.PdfError, match="BCP 47"):
        doc.language = tag
    assert doc.language == "ja"
    assert not doc.history.can_undo

    for wrong_type in (5, None, b"en"):
        with pytest.raises(TypeError, match="str"):
            doc.language = wrong_type  # ty: ignore[invalid-assignment]
    assert doc.language == "ja"


# --- MarkInfo -----------------------------------------------------------------


@pytest.mark.spec("tagged-pdf", "read", "interpret")
def test_mark_info_reads_the_three_flags() -> None:
    assert pyropdf.Document().mark_info is None
    assert pyropdf.open(_pdf("/MarkInfo true")).mark_info is None
    assert pyropdf.open(REAL_WORLD / "f1040.pdf").mark_info == MarkInfo(marked=True)
    indirect = pyropdf.open(_pdf("/MarkInfo 3 0 R", "<< /Suspects true /Marked 5 >>"))
    assert indirect.mark_info == MarkInfo(suspects=True)


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("tagged-pdf", "create", "edit", "preserve")
def test_assigning_and_deleting_mark_info_writes_all_three_flags(*, incremental: bool) -> None:
    doc = pyropdf.open(_pdf("/MarkInfo << /UserProperties true /Extra 1 >>"))
    flags = MarkInfo(marked=True, suspects=True)
    doc.mark_info = flags
    # Flags left false are written false, whatever the document held.
    assert doc.mark_info == flags
    assert sorted(doc.catalog["MarkInfo"]) == ["Marked", "Suspects", "UserProperties"]
    assert not doc.check()

    reopened = pyropdf.open(doc.to_bytes(incremental=incremental))
    assert reopened.mark_info == flags
    assert not reopened.check()

    # An existing indirect /MarkInfo keeps its object number and undoes in one step.
    indirect = pyropdf.open(_pdf("/MarkInfo 3 0 R", "<< /Marked false /Extra 2 >>"))
    before = _snapshot(indirect)
    indirect.mark_info = MarkInfo(marked=True, user_properties=True)
    assert indirect.catalog["MarkInfo"].xref == 3
    assert "Extra" not in indirect.catalog["MarkInfo"]
    assert not indirect.check()
    saved_indirect = pyropdf.open(indirect.to_bytes(incremental=incremental))
    after = _snapshot(saved_indirect)
    assert [xref for xref, value in before.items() if xref != 3 and after.get(xref) != value] == []

    indirect.history.undo()
    assert indirect.mark_info == MarkInfo()
    assert indirect.catalog["MarkInfo"]["Extra"] == 2
    indirect.history.redo()
    assert indirect.mark_info == MarkInfo(marked=True, user_properties=True)

    del indirect.mark_info
    assert indirect.mark_info is None
    assert not indirect.check()
    del indirect.mark_info


def test_mark_info_refuses_other_values_and_pdfua_checks_hold() -> None:
    doc = pyropdf.open(REAL_WORLD / "f1040.pdf")
    with pytest.raises(TypeError, match="MarkInfo"):
        doc.mark_info = {"Marked": True}  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError, match="bool"):
        MarkInfo(marked=1)  # ty: ignore[invalid-argument-type]

    ua = pyropdf.open(PDFUA / "pdfua-1.pdf")
    assert ua.language == "en-US"
    assert ua.mark_info == MarkInfo(marked=True)
    assert not ua.check()

    del ua.language
    assert "PDF/UA-1/Lang" in {defect.clause for defect in ua.check().defects}
    ua.language = "en-US"
    assert not ua.check()

    ua.mark_info = MarkInfo(marked=False)
    assert "PDF/UA-1/MarkInfo" in {defect.clause for defect in ua.check().defects}
    ua.mark_info = MarkInfo(marked=True)
    assert not ua.check()

    old = pyropdf.open(_pdf().replace(b"%PDF-1.7\n", b"%PDF-1.3\n", 1))
    old.language = "en"
    assert old.catalog["Version"] == pyropdf.Name("1.4")
    assert not old.check()
    old.mark_info = MarkInfo(marked=True)
    assert old.catalog["Version"] == pyropdf.Name("1.6")
    assert not old.check()


def test_catalog_properties_respect_closed_locked_and_page_handles() -> None:
    seed = pyropdf.Document()
    seed_page = seed.pages.new((200, 200))
    with seed_page.draw() as canvas:
        canvas.text("plain", (36, 72))
    doc = pyropdf.open(seed.to_bytes())
    page = doc.pages[0]

    doc.page_mode = PageMode.USE_THUMBS
    doc.page_layout = PageLayout.ONE_COLUMN
    doc.mark_info = MarkInfo(marked=True)
    doc.language = "en"
    doc.viewer_preferences = pyropdf.ViewerPreferences(display_title=True)
    assert page.text().strip() == "plain"
    assert not doc.check()

    locked = pyropdf.open(ENCRYPTED / "user-aes-256.pdf")
    for op in (
        lambda: locked.page_mode,
        lambda: locked.page_layout,
        lambda: locked.language,
        lambda: locked.mark_info,
    ):
        with pytest.raises(pyropdf.EncryptedDocumentError):
            op()

    doc.close()
    for closed_op in (
        lambda: doc.page_mode,
        lambda: doc.page_layout,
        lambda: doc.language,
        lambda: doc.mark_info,
    ):
        with pytest.raises(pyropdf.DocumentClosedError):
            closed_op()
