"""``Page.fonts`` and ``Document.font_program``."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from test_pages import build_raw_pdf

import pyropdf
from pyropdf import FontInfo, FontProgram, LimitError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "assets" / "real_world"
#: PyMuPDF's ``get_fonts(full=True)`` of every corpus page, from ``evidence/pymupdf-comparison/fonts.py``.
ORACLE = ROOT / "evidence" / "pymupdf-comparison" / "results" / "page-fonts.json"

FORM_ONLY = {
    1: "<< /Type /Catalog /Pages 2 0 R >>",
    2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 6 0 R"
    " /Resources << /XObject << /Fm0 4 0 R >> >> >>",
    4: "<< /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Length 25"
    " /Resources << /Font << /F1 5 0 R >> /XObject << /Fm1 4 0 R >> >> >>\n"
    "stream\nBT /F1 12 Tf (A) Tj ET\nendstream",
    5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    6: "<< /Length 8 >>\nstream\n/Fm0 Do\nendstream",
}


def _as_pymupdf(font: FontInfo) -> list[list[object]]:
    """Return ``font`` as the rows that PyMuPDF's ``get_fonts(full=True)`` lists for it."""
    return [
        [font.xref or 0, font.program or "n/a", font.subtype, font.base_font, name, font.encoding, form]
        for name in font.names
        for form in font.forms or (0,)
    ]


@pytest.mark.parametrize("name", sorted(json.loads(ORACLE.read_text(encoding="utf-8"))["fonts"]))
def test_fonts_of_the_corpus_match_pymupdf(name: str) -> None:
    expected = json.loads(ORACLE.read_text(encoding="utf-8"))["fonts"][name]
    with pyropdf.open(CORPUS / name) as doc:
        got = [sorted(row for font in page.fonts() for row in _as_pymupdf(font)) for page in doc.pages]
    assert got == expected


def _sfnt_tables(data: bytes) -> dict[bytes, tuple[int, int]]:
    """Return the table directory of an sfnt program, checking that each table lies in ``data``."""
    version, count = struct.unpack_from(">IH", data)
    assert version in {0x00010000, 0x74727565}
    tables = {}
    for index in range(count):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * index)
        assert offset + length <= len(data)
        tables[tag] = (offset, length)
    return tables


@pytest.mark.parametrize(
    ("name", "page", "subtype"), [("nics-background-checks-2015-11.pdf", 0, "TrueType"), ("mhlw-doc.pdf", 0, "Type0")]
)
def test_extracted_truetype_programs_parse(name: str, page: int, subtype: str) -> None:
    with pyropdf.open(CORPUS / name) as doc:
        fonts = [font for font in doc.pages[page].fonts() if font.program == "ttf" and font.subtype == subtype]
        assert fonts
        for font in fonts:
            program = doc.font_program(font)
            assert isinstance(program, FontProgram)
            assert program.type == "ttf"
            assert {b"head", b"maxp", b"loca", b"glyf"} <= _sfnt_tables(program.data).keys()
            assert font.xref is not None
            assert doc.font_program(font.xref) == program


def test_extracted_cff_and_type1_programs() -> None:
    with pyropdf.open(CORPUS / "bill-hr815.pdf") as doc:
        font = doc.pages[0].fonts()[0]
        program = doc.font_program(font)
        assert font.program == "cff"
        assert font.subset
        assert program is not None
        assert program.type == "cff"
        assert program.data[0] == 1  # CFF major version
    with pyropdf.open(CORPUS / "usrguide.pdf") as doc:
        font = doc.pages[0].fonts()[0]
        program = doc.font_program(font)
        assert font.program == "pfa"
        assert program is not None
        assert program.type == "pfa"
        assert program.data.startswith(b"%!")


def test_unembedded_type3_and_direct_fonts_have_no_program() -> None:
    with pyropdf.open(CORPUS / "pdfium-type3.pdf") as doc:
        (font,) = doc.pages[0].fonts()
        assert (font.subtype, font.base_font, font.program) == ("Type3", "", None)
        assert doc.font_program(font) is None
    with pyropdf.open(CORPUS / "patent-us223898.pdf") as doc:
        (font,) = doc.pages[0].fonts()
        assert font.xref is None
        assert (font.base_font, font.subset) == ("Courier", False)
        assert doc.font_program(font) is None


def test_font_used_only_inside_a_form_xobject_is_listed() -> None:
    doc = pyropdf.open(build_raw_pdf(FORM_ONLY))
    (font,) = doc.pages[0].fonts()
    assert (font.xref, font.names, font.forms) == (5, ("F1",), (4,))
    assert (font.base_font, font.subtype, font.encoding, font.program) == (
        "Helvetica",
        "Type1",
        "WinAnsiEncoding",
        None,
    )


def test_font_named_by_the_page_and_a_form_is_listed_once() -> None:
    objects = dict(FORM_ONLY)
    objects[3] = objects[3].replace("/Resources <<", "/Resources << /Font << /F9 5 0 R >>")
    doc = pyropdf.open(build_raw_pdf(objects))
    (font,) = doc.pages[0].fonts()
    assert (font.names, font.forms) == (("F9", "F1"), (4,))


def test_inherited_resources() -> None:
    objects = dict(FORM_ONLY)
    objects[2] = "<< /Type /Pages /Kids [3 0 R] /Count 1 /Resources << /Font << /F2 5 0 R >> >> >>"
    objects[3] = "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"
    doc = pyropdf.open(build_raw_pdf(objects))
    assert [(font.xref, font.names) for font in doc.pages[0].fonts()] == [(5, ("F2",))]


def test_font_program_rejects_a_value_that_is_not_a_font() -> None:
    doc = pyropdf.open(build_raw_pdf(FORM_ONLY))
    with pytest.raises(ValueError, match="not a font dictionary"):
        doc.font_program(6)


def test_forms_beyond_the_bound_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    objects = dict(FORM_ONLY)
    objects[4] = objects[4].replace("/Fm1 4 0 R", "/Fm1 7 0 R")
    objects[7] = objects[4].replace("/Fm1 7 0 R", "/Fm1 8 0 R")
    objects[8] = objects[4].replace("/Fm1 7 0 R", "")
    monkeypatch.setattr("pyropdf._fonts._MAX_PAGE_FORMS", 2)
    doc = pyropdf.open(build_raw_pdf(objects))
    with pytest.raises(LimitError) as raised:
        doc.pages[0].fonts()
    assert raised.value.code == "page_fonts_forms"


def test_entries_beyond_the_bound_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two forms share one /Font dictionary; each visit spends its entries.
    objects = dict(FORM_ONLY)
    objects[3] = objects[3].replace("/XObject << /Fm0 4 0 R >>", "/XObject << /Fm0 4 0 R /Fm1 7 0 R >>")
    objects[4] = objects[4].replace("/Font << /F1 5 0 R >> /XObject << /Fm1 4 0 R >>", "/Font 8 0 R")
    objects[7] = objects[4]
    objects[8] = "<< /F1 5 0 R /F2 5 0 R /F3 5 0 R >>"
    doc = pyropdf.open(build_raw_pdf(objects))
    assert doc.pages[0].fonts()[0].names == ("F1", "F2", "F3") * 2
    monkeypatch.setattr("pyropdf._fonts._MAX_PAGE_ENTRIES", 7)
    with pytest.raises(LimitError) as raised:
        doc.pages[0].fonts()
    assert raised.value.code == "page_fonts_entries"


def _tree(parents: int, *, resources: bool) -> pyropdf.Document:
    """Return one page under a chain of ``parents`` page tree nodes, the root holding the fonts if ``resources``."""
    objects = {1: "<< /Type /Catalog /Pages 2 0 R >>", 5000: FORM_ONLY[5]}
    for level in range(parents):
        parent = f" /Parent {level + 1} 0 R" if level else ""
        fonts = " /Resources << /Font << /F1 5000 0 R >> >>" if level == 0 and resources else ""
        objects[2 + level] = f"<< /Type /Pages /Kids [{3 + level} 0 R] /Count 1{parent}{fonts} >>"
    objects[2 + parents] = f"<< /Type /Page /Parent {1 + parents} 0 R /MediaBox [0 0 100 100] >>"
    return pyropdf.open(build_raw_pdf(objects))


def test_resources_inherited_through_64_parents() -> None:
    assert _tree(63, resources=False).pages[0].fonts() == []
    assert _tree(64, resources=False).pages[0].fonts() == []
    assert [font.xref for font in _tree(64, resources=True).pages[0].fonts()] == [5000]


def test_a_page_tree_deeper_than_the_bound_raises() -> None:
    with pytest.raises(LimitError) as raised:
        _tree(65, resources=True).pages[0].fonts()
    assert raised.value.code == "page_fonts_tree"


def test_font_program_rejects_a_font_info_of_another_document() -> None:
    first = pyropdf.open(CORPUS / "bill-hr815.pdf")
    second = pyropdf.open(CORPUS / "bill-hr815.pdf")
    font = first.pages[0].fonts()[0]
    with pytest.raises(ValueError, match="another document"):
        second.font_program(font)


def test_a_direct_font_in_resources_that_the_page_and_a_form_share_is_listed_once() -> None:
    objects = dict(FORM_ONLY)
    objects[3] = objects[3].replace("/Resources << /XObject << /Fm0 4 0 R >> >>", "/Resources 7 0 R")
    objects[4] = objects[4].replace(
        "/Resources << /Font << /F1 5 0 R >> /XObject << /Fm1 4 0 R >> >>", "/Resources 7 0 R"
    )
    objects[7] = f"<< /Font << /F1 {FORM_ONLY[5]} >> /XObject << /Fm0 4 0 R >> >>"
    doc = pyropdf.open(build_raw_pdf(objects))
    (font,) = doc.pages[0].fonts()
    assert (font.xref, font.names, font.forms) == (None, ("F1", "F1"), (4,))
