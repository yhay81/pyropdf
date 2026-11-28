"""Markdown from ``Page.markdown``, ``Document.markdown``, and ``Table.markdown``.

Ported from pylopdf 036be111 ``tests/test_markdown.py`` and the Markdown
tests of ``tests/test_tables.py``. The differences:

- pylopdf adds text with ``page.apply_ocr``; these tests show the same
  strings with a font whose ToUnicode CMap maps one code to each whole
  string, so that Markdown does not depend on the OCR layer.
- pylopdf's ``page.document.markdown()`` is ``document.markdown()``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from test_content import build_raw_pdf
from test_tables import _bordered_table_pdf, _sparse_row_grid_pdf, build_visual_rtl_pdf

import pyropdf

_PDFUA_ASSETS = Path(__file__).parent / "assets" / "pdfua"

if TYPE_CHECKING:
    from collections.abc import Iterator


def _table_pdf(
    *,
    bordered: bool = True,
    table_font_size: int = 12,
    include_cells: bool = True,
) -> bytes:
    """Build a three-by-two table between ordinary page text."""
    rules = (
        "q 0 G 1 w\n"
        "40 250 m 300 250 l\n"
        "40 210 m 300 210 l\n"
        "40 170 m 300 170 l\n"
        "40 130 m 300 130 l\n"
        "40 130 m 40 250 l\n"
        "170 130 m 170 250 l\n"
        "300 130 m 300 250 l\n"
        "S Q\n"
        if bordered
        else ""
    )
    values = [
        (50, 225, "Name"),
        (180, 225, "Value"),
        (50, 185, "Alpha"),
        (180, 185, "42"),
        (50, 145, "Beta"),
        (180, 145, "7"),
    ]
    cells = (
        "\n".join(f"BT /F1 {table_font_size} Tf {x} {y} Td ({text}) Tj ET" for x, y, text in values)
        if include_cells
        else ""
    )
    stream = (
        "BT /F1 18 Tf 40 310 Td (Section) Tj ET\n"
        "BT /F1 12 Tf 40 280 Td (Body text) Tj ET\n"
        f"{rules}{cells}\n"
        "BT /F1 12 Tf 40 60 Td (After table) Tj ET"
    )
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 340 340] "
                "/Resources << /Font << /F1 3 0 R >> >> >>"
            ),
            3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
            5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        }
    )


def _layout_pdf(pages: list[list[tuple]]) -> bytes:
    """Build pages from ``(size, baseline_y, text[, font])`` tuples.

    Fonts are F1 = Helvetica (default), F2 = Helvetica-Bold, and
    F3 = Helvetica-Oblique.
    """
    n = len(pages)
    kids = " ".join(f"{10 + 2 * i} 0 R" for i in range(n))
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Kids [{kids}] /Count {n} /MediaBox [0 0 612 792]"
            " /Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> >>"
        ),
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>",
    }
    for i, items in enumerate(pages):
        ops = ""
        for item in items:
            size, y, text = item[0], item[1], item[2]
            font = item[3] if len(item) > 3 else "F1"
            escaped = text.replace(chr(92), chr(92) * 2).replace("(", chr(92) + "(").replace(")", chr(92) + ")")
            ops += f"BT /{font} {size} Tf 72 {y} Td ({escaped}) Tj ET\n"
        objects[10 + 2 * i] = f"<< /Type /Page /Parent 2 0 R /Contents {11 + 2 * i} 0 R >>"
        objects[11 + 2 * i] = f"<< /Length {len(ops)} >>\nstream\n{ops}endstream"
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{objects[num]}\nendobj\n".encode("latin-1")
    xref_pos = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for num in range(1, size):
        if num in offsets:
            out += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
        else:
            out += b"0000000000 65535 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode("ascii")
    return bytes(out)


def _unicode_pages_pdf(streams: list[str], mappings: dict[str, str]) -> bytes:
    """Build a page per stream whose Courier glyphs map to Unicode while keeping their advances."""
    entries = "\n".join(
        f"<{code.encode().hex()}> <{text.encode('utf-16-be').hex()}>" for code, text in mappings.items()
    )
    cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CMapType 2 def\n1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        f"{len(mappings)} beginbfchar\n{entries}\nendbfchar\nendcmap\nend\nend"
    )
    kids = " ".join(f"{10 + 2 * index} 0 R" for index in range(len(streams)))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} /MediaBox [0 0 300 200]"
        " /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /ToUnicode 7 0 R >>",
        4: "<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Oblique /ToUnicode 7 0 R >>",
        7: f"<< /Length {len(cmap)} >>\nstream\n{cmap}\nendstream",
    }
    for index, stream in enumerate(streams):
        objects[10 + 2 * index] = f"<< /Type /Page /Parent 2 0 R /Contents {11 + 2 * index} 0 R >>"
        objects[11 + 2 * index] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    return build_raw_pdf(objects)


def _unicode_font_spans_pdf(stream: str, mappings: dict[str, str]) -> bytes:
    """Map Courier glyphs to Unicode while keeping specified advance geometry."""
    return _unicode_pages_pdf([stream], mappings)


def _wrapped_lines_pdf(first: str, second: str) -> bytes:
    """Build two lines of one paragraph, each one glyph that maps to a whole string."""
    return _unicode_font_spans_pdf("BT /F1 12 Tf 50 150 Td (A) Tj 0 -14 Td (B) Tj ET", {"A": first, "B": second})


def test_heading_detected_by_size() -> None:
    doc = pyropdf.open(
        _layout_pdf(
            [
                [
                    (24, 720, "Big Title"),
                    (12, 660, "Body line one"),
                    (12, 646, "body line two"),
                ]
            ]
        )
    )
    md = doc.markdown()
    assert md.startswith("# Big Title")
    # Join two body lines with a space inside one paragraph.
    assert "Body line one body line two" in md


def test_two_heading_levels() -> None:
    doc = pyropdf.open(
        _layout_pdf(
            [
                [
                    (28, 720, "Title"),
                    (18, 660, "Section"),
                    (12, 600, "Body text here"),
                    (12, 586, "and more body"),
                ]
            ]
        )
    )
    md = doc.markdown()
    assert "# Title" in md
    assert "## Section" in md
    assert "### " not in md


def test_uniform_size_has_no_headings() -> None:
    doc = pyropdf.open(_layout_pdf([[(12, 720, "Only body"), (12, 706, "same size")]]))
    assert "#" not in doc.markdown()


def test_bullets_and_numbers_normalize() -> None:
    doc = pyropdf.open(
        _layout_pdf(
            [
                [
                    (12, 720, "Intro paragraph"),
                    (12, 680, "- first item"),
                    (12, 666, "- second item"),
                    (12, 626, "1) numbered"),
                ]
            ]
        )
    )
    md = doc.markdown()
    assert "- first item\n- second item" in md  # Adjacent items form one list.
    assert "1. numbered" in md


def test_dict_spans_have_font_and_flags_keys() -> None:
    # Embedded fonts exercise real bold/italic detection in test_interop.py.
    # Hayro exposes no metadata for Standard 14 Type1 substitutes, so the
    # current contract is flags=0 and an empty font name.
    doc = pyropdf.open(_layout_pdf([[(12, 720, "Standard font words", "F2")]]))
    span = doc.pages[0].layout().to_dict()["blocks"][0]["lines"][0]["spans"][0]
    assert span["flags"] == 0
    assert span["font"] == ""


def test_cjk_lines_join_without_space() -> None:
    doc = pyropdf.open(_wrapped_lines_pdf("日本語の折り返しは", "空白なしで繋がる"))
    md = doc.markdown()
    assert "日本語の折り返しは空白なしで繋がる" in md


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("ภาษาไทยไม่มี", "ช่องว่าง"),
        ("ພາສາລາວບໍ່ມີ", "ຊ່ອງຫວ່າງ"),
        ("ភាសាខ្មែរមិនមាន", "ចន្លោះ"),
        ("မြန်မာစာတွင်", "နေရာလွတ်"),
    ],
)
def test_no_space_scripts_join_wrapped_lines_without_ascii_gap(first: str, second: str) -> None:
    doc = pyropdf.open(_wrapped_lines_pdf(first, second))

    markdown = doc.markdown()

    assert first + second in markdown
    assert first + " " + second not in markdown


def test_latin_lines_join_with_space() -> None:
    doc = pyropdf.open(_wrapped_lines_pdf("Latin lines", "join spaced"))
    assert "Latin lines join spaced" in doc.markdown()


def test_word_gap_between_font_spans_becomes_a_space() -> None:
    # A TJ adjustment of -300 leaves a 0.3 em gap between the spans and no space glyph.
    stream = (
        "BT /F1 12 Tf 72 720 Td [(Regular) -300] TJ /F2 12 Tf (Bold) Tj ET\n"
        "BT /F2 12 Tf 72 600 Td (Bold) Tj /F1 12 Tf (er) Tj ET"
    )
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: (
                    "<< /Type /Pages /Kids [5 0 R] /Count 1 /MediaBox [0 0 612 792] "
                    "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>"
                ),
                3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
                5: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R >>",
                6: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            }
        )
    )
    spans = doc.pages[0].layout().blocks[0].lines[0].spans
    assert [span.text for span in spans] == ["Regular", "Bold"]
    # The font change inside "Bolder" has no gap, so it stays one word.
    assert doc.markdown() == "Regular Bold\n\nBolder"


@pytest.mark.parametrize(("last_x", "expected"), [(52, "a\u0301b"), (58, "a\u0301 b")])
def test_combining_mark_span_keeps_its_base_for_following_word_gap(last_x: int, expected: str) -> None:
    stream = (
        f"BT /F1 20 Tf 40 140 Td (a) Tj /F2 10 Tf 1 0 0 1 40 140 Tm (A) Tj /F1 20 Tf 1 0 0 1 {last_x} 140 Tm (b) Tj ET"
    )
    doc = pyropdf.open(_unicode_font_spans_pdf(stream, {"A": "\u0301"}))
    assert doc.pages[0].text() == expected + "\n"
    assert doc.markdown() == expected


@pytest.mark.parametrize(("first", "second", "separator"), [("日", "本", " "), ("Ａ", "Ｂ", " "), ("ก", "ข", "")])
@pytest.mark.parametrize("font", ["F1", "F2"])
def test_font_span_word_gaps_follow_extractor_script_rules(first: str, second: str, separator: str, font: str) -> None:
    stream = f"BT /F1 20 Tf 40 140 Td [(A) -300] TJ /{font} 20 Tf (B) Tj ET"
    doc = pyropdf.open(_unicode_font_spans_pdf(stream, {"A": first, "B": second}))
    expected = first + separator + second
    assert doc.pages[0].text() == expected + "\n"
    assert doc.markdown() == expected


def test_page_to_markdown_and_page_selection() -> None:
    doc = pyropdf.open(
        _layout_pdf(
            [
                [(12, 720, "First page")],
                [(12, 720, "Second page")],
            ]
        )
    )
    assert doc.pages[1].markdown() == "Second page"
    assert doc.markdown(pages=[1, 0]) == "Second page\n\nFirst page"
    full = doc.markdown()
    assert "First page" in full
    assert "Second page" in full


def test_markdown_bounds_utf8_output_without_partial_result() -> None:
    doc = pyropdf.open(
        _unicode_pages_pdf(
            ["BT /F1 12 Tf 20 170 Td (A) Tj ET", "BT /F1 12 Tf 20 170 Td (B) Tj ET"],
            {"A": "First page", "B": "日本語の二ページ目"},
        )
    )
    expected = doc.markdown(max_size=None)
    exact_size = len(expected.encode())
    expected_page = doc.pages[1].markdown(max_size=None)

    with pytest.raises(pyropdf.LimitError) as caught:
        doc.markdown(max_size=exact_size - 1)
    assert caught.value.code == "markdown_output_size"
    assert doc.markdown(max_size=exact_size) == expected
    assert doc.pages[1].markdown(max_size=len(expected_page.encode())) == expected_page


def test_markdown_bounds_page_entries_before_final_join(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = pyropdf.open(_layout_pdf([[(12, 720, "Page body exceeds budget")]]))

    def fail_final_join(*_args: object, **_kwargs: object) -> str:
        raise AssertionError

    monkeypatch.setattr("pyropdf._markdown._join_entries", fail_final_join)
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.markdown(max_size=4)
    assert caught.value.code == "markdown_output_size"


def test_markdown_table_and_prose_share_exact_page_budget() -> None:
    doc = pyropdf.open(_table_pdf())
    expected = doc.markdown(max_size=None)
    exact_size = len(expected.encode())

    assert doc.markdown(max_size=exact_size) == expected
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.markdown(max_size=exact_size - 1)
    assert caught.value.code == "markdown_output_size"


def test_markdown_headings_lists_and_paragraphs_share_exact_budget() -> None:
    doc = pyropdf.open(
        _layout_pdf(
            [
                [
                    (24, 720, "Title"),
                    (12, 680, "Intro paragraph"),
                    (12, 640, "- first item"),
                    (12, 626, "- second item"),
                ]
            ]
        )
    )
    expected = doc.markdown(max_size=None)
    exact_size = len(expected.encode())

    assert doc.markdown(max_size=exact_size) == expected
    with pytest.raises(pyropdf.LimitError):
        doc.markdown(max_size=exact_size - 1)


def test_markdown_bounds_page_iterable_before_interpretation() -> None:
    doc = pyropdf.open(_layout_pdf([[(12, 720, "Page")]]))
    consumed = 0

    def pages() -> Iterator[int]:
        nonlocal consumed
        for _ in range(5000):
            consumed += 1
            yield 0

    with pytest.raises(pyropdf.LimitError, match="4096 entries") as caught:
        doc.markdown(pages=pages())
    assert caught.value.code == "page_entry_count"
    assert consumed == 4097


@pytest.mark.parametrize("max_size", [True, 1.5, "1024"])
def test_markdown_rejects_non_integer_size_limits(max_size: object) -> None:
    doc = pyropdf.open(_layout_pdf([[(12, 720, "Page")]]))
    with pytest.raises(TypeError, match="max_size"):
        doc.markdown(max_size=max_size)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("max_size", [0, -1])
def test_markdown_rejects_non_positive_size_limits(max_size: int) -> None:
    doc = pyropdf.open(_layout_pdf([[(12, 720, "Page")]]))
    with pytest.raises(ValueError, match="max_size"):
        doc.markdown(max_size=max_size)


def test_empty_document() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    assert doc.markdown() == ""


def test_bordered_table_is_inserted_without_duplicate_cell_text() -> None:
    doc = pyropdf.open(_table_pdf())

    markdown = doc.markdown()

    table = "| Name | Value |\n| --- | --- |\n| Alpha | 42 |\n| Beta | 7 |"
    assert table in markdown
    assert markdown.index("# Section") < markdown.index(table) < markdown.index("After table")
    assert markdown.count("Name") == 1
    assert markdown.count("Alpha") == 1


def test_table_conversion_can_be_disabled() -> None:
    doc = pyropdf.open(_table_pdf())

    markdown = doc.pages[0].markdown(table_strategy=None)

    assert "| --- |" not in markdown
    assert "Name" in markdown
    assert "Alpha" in markdown


def test_borderless_table_conversion_is_explicit() -> None:
    doc = pyropdf.open(_table_pdf(bordered=False))

    default = doc.markdown()
    with_text_tables = doc.markdown(table_strategy="text")

    assert "| --- |" not in default
    assert "| Name | Value |\n| --- | --- |\n| Alpha | 42 |\n| Beta | 7 |" in with_text_tables
    assert with_text_tables.count("Name") == 1


def test_text_strategy_prefers_overlapping_bordered_table() -> None:
    doc = pyropdf.open(_table_pdf())

    markdown = doc.markdown(table_strategy="text")

    assert markdown.count("| --- | --- |") == 1
    assert markdown.count("Alpha") == 1


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_table_markdown_preserves_page_reading_order(rotation: int) -> None:
    doc = pyropdf.open(_table_pdf())
    doc.pages[0].rotation = rotation

    markdown = doc.pages[0].markdown()

    assert markdown.index("# Section") < markdown.index("| Name | Value |") < markdown.index("After table")
    assert markdown.count("Alpha") == 1


def test_table_text_does_not_influence_heading_size_inference() -> None:
    doc = pyropdf.open(_table_pdf(table_font_size=24))

    markdown = doc.markdown()

    assert markdown.startswith("# Section")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_empty_bordered_table_uses_geometric_reading_order(rotation: int) -> None:
    doc = pyropdf.open(_table_pdf(include_cells=False))
    doc.pages[0].rotation = rotation

    markdown = doc.markdown()

    table = "|  |  |\n| --- | --- |\n|  |  |\n|  |  |"
    assert markdown.index("Body text") < markdown.index(table) < markdown.index("After table")


@pytest.mark.parametrize("table_strategy", ["guess", 1, False])
def test_markdown_rejects_unknown_table_strategy(table_strategy: object) -> None:
    doc = pyropdf.open(_table_pdf())
    with pytest.raises(ValueError, match="table_strategy"):
        doc.markdown(table_strategy=table_strategy)  # ty: ignore[invalid-argument-type]


def test_bordered_table_markdown() -> None:
    page = pyropdf.open(_bordered_table_pdf()).pages[0]

    assert page.tables()[0].markdown() == "| Name | Value |\n| --- | --- |\n| Alpha | 42 |"


@pytest.mark.parametrize(
    ("hebrew_visual", "expected"),
    [
        ("0001000200030004000500010003000200060007", "שְלום עולם"),
        ("0001000200030004000500310032003300050001000300020006", "שלום 123 עולם"),
    ],
)
def test_bordered_table_markdown_restores_rtl_cell_text(hebrew_visual: str, expected: str) -> None:
    stream = (
        "q 0 G 1 w\n"
        "40 260 m 300 260 l\n"
        "40 220 m 300 220 l\n"
        "40 180 m 300 180 l\n"
        "40 180 m 40 260 l\n"
        "170 180 m 170 260 l\n"
        "300 180 m 300 260 l\n"
        "S Q\n"
        "BT /F2 12 Tf 50 235 Td (Name) Tj ET\n"
        "BT /F2 12 Tf 180 235 Td (Value) Tj ET\n"
        f"BT /F1 8 Tf 50 195 Td <{hebrew_visual}> Tj ET\n"
        "BT /F2 12 Tf 180 195 Td (42) Tj ET"
    )
    document = pyropdf.open(build_visual_rtl_pdf(stream))

    assert expected in document.pages[0].tables()[0].markdown()
    assert expected in document.markdown()


def test_table_markdown_preflights_escaped_utf8_output() -> None:
    source = pyropdf.open(_bordered_table_pdf()).pages[0].tables()[0]
    table = dataclasses.replace(source, rows=(("\\|\n日本", "Value"), ("Alpha", "42")))
    expected = table.markdown(max_size=None)
    exact_size = len(expected.encode())

    assert table.markdown(max_size=exact_size) == expected
    with pytest.raises(pyropdf.LimitError) as caught:
        table.markdown(max_size=exact_size - 1)
    assert caught.value.code == "markdown_output_size"


@pytest.mark.parametrize("max_size", [0, -1, True, 1.5])
def test_table_markdown_validates_output_limit(max_size: object) -> None:
    table = pyropdf.open(_bordered_table_pdf()).pages[0].tables()[0]

    with pytest.raises((TypeError, ValueError), match="max_size"):
        table.markdown(max_size=max_size)  # ty: ignore[invalid-argument-type]


def test_document_preflights_table_before_page_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    document = pyropdf.open(_bordered_table_pdf())

    def fail_page_render(*_args: object, **_kwargs: object) -> str:
        raise AssertionError

    monkeypatch.setattr("pyropdf._markdown.page_to_markdown", fail_page_render)
    with pytest.raises(pyropdf.LimitError) as caught:
        document.markdown(max_size=16)
    assert caught.value.code == "markdown_output_size"


def test_rectangular_merged_cell_markdown() -> None:
    pdf = _bordered_table_pdf().replace(b"170 180 m 170 260 l", b"170 180 m 170 220 l")
    table = pyropdf.open(pdf).pages[0].tables()[0]

    assert table.markdown() == ("| Name Value | Name Value |\n| --- | --- |\n| Alpha | 42 |")
    assert table.markdown(fill_empty=False) == ("| Name Value |  |\n| --- | --- |\n| Alpha | 42 |")
    filled = table.markdown(max_size=None)
    assert table.markdown(max_size=len(filled.encode())) == filled
    with pytest.raises(pyropdf.LimitError, match="Markdown output exceeds"):
        table.markdown(max_size=len(filled.encode()) - 1)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_sparse_row_grid_markdown_keeps_logical_orientation(rotation: int) -> None:
    document = pyropdf.open(_sparse_row_grid_pdf())
    document.pages[0].rotation = rotation

    markdown = document.markdown()

    assert "| Name | Qty | Total |" in markdown
    assert "| Item1 | 1 | 10 |" in markdown
    assert "| Item5 | 5 | 50 |" in markdown
    assert markdown.count("Item3") == 1


def _stream_obj(data: str, extra: str = "") -> str:
    return f"<< /Length {len(data)} {extra} >>\nstream\n{data}\nendstream"


def _rich_tagged_pdf(*, marked: bool = True, suspects: bool = False) -> bytes:
    """Build a uniform-12pt tagged PDF covering headings, lists, tables, quotes, code, figures, and artifacts."""
    stream = (
        "BT /F1 12 Tf "
        "40 760 Td /Artifact BMC (Running Header) Tj EMC "
        "0 -20 Td /P << /MCID 1 >> BDC (Second logically) Tj EMC "
        "0 -20 Td /H1 << /MCID 0 >> BDC (Main Title) Tj EMC "
        "0 -20 Td /H << /MCID 2 >> BDC (Nested Heading) Tj EMC "
        "0 -20 Td /P << /MCID 3 >> BDC (Intro ) Tj EMC "
        "50 0 Td /Strong << /MCID 4 >> BDC (bold) Tj EMC "
        "40 0 Td /Em << /MCID 5 >> BDC (italic) Tj EMC "
        "50 0 Td /Code << /MCID 6 >> BDC (x=1) Tj EMC "
        "-140 -20 Td /Lbl << /MCID 7 >> BDC (*) Tj EMC "
        "12 0 Td /LBody << /MCID 8 >> BDC (First item) Tj EMC "
        "-12 -20 Td /LBody << /MCID 9 >> BDC (Nested item) Tj EMC "
        "0 -20 Td /LBody << /MCID 10 >> BDC (Ordered one) Tj EMC "
        "0 -20 Td /BlockQuote << /MCID 11 >> BDC (Quoted line) Tj EMC "
        "0 -20 Td /Code << /MCID 12 >> BDC (print(1)) Tj EMC "
        "0 -20 Td /TH << /MCID 13 >> BDC (Metric) Tj EMC "
        "100 0 Td /TD << /MCID 14 >> BDC (100) Tj EMC "
        "-100 -20 Td /Span /Abbr BDC (API) Tj EMC "
        "0 -20 Td /P << /MCID 16 >> BDC (SkipMeArtifactElem) Tj EMC "
        "ET\n"
        "/Figure << /MCID 17 >> BDC 0 0 1 rg 40 200 80 40 re f EMC\n"
        "BT /F1 12 Tf 40 40 Td /Artifact BMC (Page 1 of 1) Tj EMC ET"
    )
    mark_info = f"<< /Marked {'true' if marked else 'false'} /Suspects {'true' if suspects else 'false'} >>"
    return build_raw_pdf(
        {
            1: (f"<< /Type /Catalog /Pages 2 0 R /Lang (en-US) /MarkInfo {mark_info} /StructTreeRoot 10 0 R >>"),
            2: (
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> "
                "/Properties << /Abbr << /MCID 15 /E (Application Programming Interface) >> >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: _stream_obj(stream),
            10: "<< /Type /StructTreeRoot /K 11 0 R >>",
            11: (
                "<< /Type /StructElem /S /Document /P 10 0 R /Pg 3 0 R "
                "/K [12 0 R 13 0 R 14 0 R 16 0 R 20 0 R 24 0 R 26 0 R 27 0 R 28 0 R 33 0 R 34 0 R 35 0 R] >>"
            ),
            # Reading order places H1 (MCID 0) BEFORE P (MCID 1) even though MCID 1 is visually above MCID 0.
            12: "<< /Type /StructElem /S /H1 /P 11 0 R /Pg 3 0 R /K 0 >>",
            13: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K 1 >>",
            14: "<< /Type /StructElem /S /Sect /P 11 0 R /Pg 3 0 R /K [15 0 R] >>",
            15: "<< /Type /StructElem /S /H /P 14 0 R /Pg 3 0 R /K 2 >>",
            16: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K [3 17 0 R 18 0 R 19 0 R] >>",
            17: "<< /Type /StructElem /S /Strong /P 16 0 R /Pg 3 0 R /K 4 >>",
            18: "<< /Type /StructElem /S /Em /P 16 0 R /Pg 3 0 R /K 5 >>",
            19: "<< /Type /StructElem /S /Code /P 16 0 R /Pg 3 0 R /K 6 >>",
            20: "<< /Type /StructElem /S /L /P 11 0 R /Pg 3 0 R /K [21 0 R] >>",
            21: "<< /Type /StructElem /S /LI /P 20 0 R /Pg 3 0 R /K [22 0 R 23 0 R] >>",
            22: "<< /Type /StructElem /S /Lbl /P 21 0 R /Pg 3 0 R /K 7 >>",
            23: "<< /Type /StructElem /S /LBody /P 21 0 R /Pg 3 0 R /K [8 36 0 R] >>",
            36: "<< /Type /StructElem /S /L /P 23 0 R /Pg 3 0 R /K [37 0 R] >>",
            37: "<< /Type /StructElem /S /LI /P 36 0 R /Pg 3 0 R /K [38 0 R] >>",
            38: "<< /Type /StructElem /S /LBody /P 37 0 R /Pg 3 0 R /K 9 >>",
            24: (
                "<< /Type /StructElem /S /L /P 11 0 R /Pg 3 0 R "
                "/A << /O /List /ListNumbering /Decimal >> /K [25 0 R] >>"
            ),
            25: "<< /Type /StructElem /S /LI /P 24 0 R /Pg 3 0 R /K [39 0 R] >>",
            39: "<< /Type /StructElem /S /LBody /P 25 0 R /Pg 3 0 R /K 10 >>",
            26: "<< /Type /StructElem /S /BlockQuote /P 11 0 R /Pg 3 0 R /Lang (fr) /K 11 >>",
            27: "<< /Type /StructElem /S /Code /P 11 0 R /Pg 3 0 R /K 12 >>",
            28: "<< /Type /StructElem /S /Table /P 11 0 R /Pg 3 0 R /K [29 0 R 31 0 R] >>",
            29: "<< /Type /StructElem /S /THead /P 28 0 R /Pg 3 0 R /K [30 0 R] >>",
            30: "<< /Type /StructElem /S /TR /P 29 0 R /Pg 3 0 R /K [40 0 R] >>",
            40: "<< /Type /StructElem /S /TH /P 30 0 R /Pg 3 0 R /A << /O /Table /ColSpan 2 >> /K 13 >>",
            31: "<< /Type /StructElem /S /TBody /P 28 0 R /Pg 3 0 R /K [32 0 R] >>",
            32: "<< /Type /StructElem /S /TR /P 31 0 R /Pg 3 0 R /K [41 0 R 42 0 R] >>",
            41: "<< /Type /StructElem /S /TD /P 32 0 R /Pg 3 0 R /K 14 >>",
            42: "<< /Type /StructElem /S /TD /P 32 0 R /Pg 3 0 R /ActualText (OK) /K [] >>",
            33: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K 15 >>",
            34: "<< /Type /StructElem /S /Artifact /P 11 0 R /Pg 3 0 R /K 16 >>",
            35: "<< /Type /StructElem /S /Figure /P 11 0 R /Pg 3 0 R /Alt (Blue bar chart) /K 17 >>",
        }
    )


@pytest.mark.parametrize(
    ("filename", "expected_alt"),
    [
        ("pdfua-1.pdf", "A blue rectangle crossed by a red line"),
        ("pdfua-2.pdf", "Water level rose at each of three readings; the final level was three times the first."),
        (
            "pdfua-2-pdfa-4.pdf",
            "Water level rose at each of three readings; the final level was three times the first.",
        ),
    ],
)
@pytest.mark.spec("tagged-pdf", "interpret")
def test_markdown_reads_pdfua_figure_alt_from_structure_tree(filename: str, expected_alt: str) -> None:
    with pyropdf.open(_PDFUA_ASSETS / filename) as doc:
        expected = f"![{expected_alt}]()"
        assert doc.markdown() == expected
        assert doc.pages[0].markdown() == expected
        assert doc.markdown(use_structure="never") == ""


@pytest.mark.spec("tagged-pdf", "interpret")
def test_markdown_uses_structure_tree_roles_reading_order_and_artifact_exclusion() -> None:
    with pyropdf.open(_rich_tagged_pdf()) as doc:
        md = doc.markdown()
        assert md.startswith("# Main Title\n\nSecond logically\n\n## Nested Heading")
        assert "**bold**" in md
        assert "*italic*" in md
        assert "`x=1`" in md
        assert "- First item\n  - Nested item\n1. Ordered one" in md
        assert "> Quoted line" in md
        assert "```\nprint(1)\n```" in md
        assert "| Metric | Metric |\n| --- | --- |\n| 100 | OK |" in md
        assert "![Blue bar chart]()" in md
        assert "API" in md
        assert "Application Programming Interface" not in md
        assert "Running Header" not in md
        assert "Page 1 of 1" not in md
        assert "SkipMeArtifactElem" not in md

        expanded = doc.markdown(expand_abbreviations=True)
        assert "Application Programming Interface" in expanded

        # Geometric mode ignores structure roles and artifacts.
        geometric = doc.markdown(use_structure="never")
        assert "Running Header" in geometric
        assert "# Main Title" not in geometric
        assert geometric.index("Second logically") < geometric.index("Main Title")

        # table_strategy=None emits structure table cells without Markdown table syntax.
        no_tables = doc.markdown(table_strategy=None)
        assert "| --- |" not in no_tables
        assert "Metric" in no_tables


@pytest.mark.spec("tagged-pdf", "interpret")
def test_markdown_resolves_role_map_and_pdf2_namespaces() -> None:
    stream = (
        "BT /F1 12 Tf "
        "40 740 Td /CustomArt << /MCID 0 >> BDC (HeaderToDrop) Tj EMC "
        "0 -20 Td /MyTitle << /MCID 1 >> BDC (Chapter One) Tj EMC "
        "0 -20 Td /NsHeading << /MCID 2 >> BDC (Subsection) Tj EMC "
        "0 -20 Td /NsNote << /MCID 3 >> BDC (Important callout) Tj EMC "
        "0 -20 Td /NsBody << /MCID 4 >> BDC (Body content) Tj EMC ET"
    )
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Version /2.0 /Pages 2 0 R /MarkInfo << /Marked true >> /StructTreeRoot 10 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 4 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: _stream_obj(stream),
            10: (
                "<< /Type /StructTreeRoot /K 11 0 R "
                "/RoleMap << /MyTitle /ChapterHead /ChapterHead /H1 /CustomArt /Artifact >> "
                "/Namespaces [20 0 R 21 0 R] >>"
            ),
            11: (
                "<< /Type /StructElem /S /Document /NS 20 0 R /P 10 0 R /Pg 3 0 R "
                "/K [12 0 R 13 0 R 14 0 R 15 0 R 16 0 R] >>"
            ),
            12: "<< /Type /StructElem /S /CustomArt /P 11 0 R /Pg 3 0 R /K 0 >>",
            13: "<< /Type /StructElem /S /MyTitle /P 11 0 R /Pg 3 0 R /K 1 >>",
            14: "<< /Type /StructElem /S /NsHeading /NS 21 0 R /P 11 0 R /Pg 3 0 R /K 2 >>",
            15: "<< /Type /StructElem /S /NsNote /NS 21 0 R /P 11 0 R /Pg 3 0 R /K 3 >>",
            16: "<< /Type /StructElem /S /NsBody /NS 21 0 R /P 11 0 R /Pg 3 0 R /K 4 >>",
            20: "<< /Type /Namespace /NS (http://iso.org/pdf2/ssn) >>",
            21: (
                "<< /Type /Namespace /NS (https://example.com/custom-ns) "
                "/RoleMapNS << /NsHeading [/H2 20 0 R] /NsNote [/BlockQuote 20 0 R] /NsBody /P >> >>"
            ),
        }
    )
    with pyropdf.open(pdf) as doc:
        md = doc.markdown()
        assert md == "# Chapter One\n\n## Subsection\n\n> Important callout\n\nBody content"
        assert "HeaderToDrop" not in md


def test_markdown_trustworthiness_and_fallback_controls() -> None:
    # Marked=false falls back in auto mode, but always mode uses structure.
    with pyropdf.open(_rich_tagged_pdf(marked=False)) as doc:
        assert "# Main Title" not in doc.markdown(use_structure="auto")
        assert doc.markdown(use_structure="always").startswith("# Main Title")

    # Suspects=true falls back in auto mode, but always mode uses structure.
    with pyropdf.open(_rich_tagged_pdf(marked=True, suspects=True)) as doc:
        assert "# Main Title" not in doc.markdown(use_structure="auto")
        assert doc.markdown(use_structure="always").startswith("# Main Title")

    # Partially tagged page (<70% non-artifact text coverage) falls back to geometric without losing text.
    partial_stream = (
        "BT /F1 12 Tf 40 740 Td /H1 << /MCID 0 >> BDC (Short) Tj EMC "
        "0 -20 Td (A much longer untagged body paragraph that must never be dropped silently.) Tj ET"
    )
    partial_pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /MarkInfo << /Marked true >> /StructTreeRoot 10 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 4 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: _stream_obj(partial_stream),
            10: "<< /Type /StructTreeRoot /K 11 0 R >>",
            11: "<< /Type /StructElem /S /Document /P 10 0 R /Pg 3 0 R /K [12 0 R] >>",
            12: "<< /Type /StructElem /S /H1 /P 11 0 R /Pg 3 0 R /K 0 >>",
        }
    )
    with pyropdf.open(partial_pdf) as doc:
        auto_md = doc.markdown()
        assert "must never be dropped silently" in auto_md
        assert "# Short" not in auto_md
        assert doc.markdown(use_structure="always") == "# Short"

    # Broken/cyclic structure tree falls back cleanly in auto mode and raises in always mode.
    with pyropdf.open(_rich_tagged_pdf()) as doc:
        doc.structure[0].children[0].obj["K"] = doc.structure[0].obj
        fallback_md = doc.markdown(use_structure="auto")
        assert "Main Title" in fallback_md
        assert "# Main Title" not in fallback_md
        with pytest.raises(pyropdf.MalformedPdfError, match="cycle"):
            doc.markdown(use_structure="always")

    # Mixed 2-page PDF: page 0 is tagged and trustworthy, page 1 is untagged and falls back cleanly.
    mixed_pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /MarkInfo << /Marked true >> /StructTreeRoot 10 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 4 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: _stream_obj("BT /F1 12 Tf 40 740 Td /H1 << /MCID 0 >> BDC (Tagged Page) Tj EMC ET"),
            6: "<< /Type /Page /Parent 2 0 R /Contents 7 0 R >>",
            7: _stream_obj("BT /F1 12 Tf 40 740 Td (Untagged second page body) Tj ET"),
            10: "<< /Type /StructTreeRoot /K 11 0 R >>",
            11: "<< /Type /StructElem /S /Document /P 10 0 R /Pg 3 0 R /K [12 0 R] >>",
            12: "<< /Type /StructElem /S /H1 /P 11 0 R /Pg 3 0 R /K 0 >>",
        }
    )
    with pyropdf.open(mixed_pdf) as doc:
        assert doc.markdown() == "# Tagged Page\n\nUntagged second page body"


def test_markdown_canvas_form_stream_coverage_and_replacement() -> None:
    # Unresolved Form XObject tags (#989) without actual_text fall back to geometric without losing text.
    with pyropdf.Document() as doc:
        page = doc.pages.new((360, 200))
        with page.draw() as canvas:
            with canvas.tag(pyropdf.TagRole.HEADING_1):
                canvas.text("Unreplaced Heading", (36, 40), size=18)
            with canvas.tag(pyropdf.TagRole.PARAGRAPH):
                canvas.text("Body paragraph text", (36, 70), size=12)
        auto_md = doc.markdown()
        assert "Unreplaced Heading" in auto_md
        assert "Body paragraph text" in auto_md

    # Tagged Form XObject with explicit actual_text/alt/expanded uses structure and skips Artifact.
    with pyropdf.Document() as doc:
        page = doc.pages.new((360, 200))
        with page.draw() as canvas:
            with canvas.tag(pyropdf.TagRole.HEADING_1, actual_text="Summary"):
                canvas.text("Summary", (36, 40), size=12)
            with canvas.tag(pyropdf.TagRole.PARAGRAPH, actual_text="PDF", expanded="Portable Document Format"):
                canvas.text("PDF", (36, 70), size=12)
            with canvas.tag(pyropdf.TagRole.FIGURE, alt="Revenue chart"):
                canvas.rect((36, 90, 140, 140), fill=pyropdf.Color(0.2, 0.4, 0.8))
            with canvas.tag(pyropdf.TagRole.ARTIFACT):
                canvas.text("Page 1", (36, 180), size=9)
        assert doc.markdown() == "# Summary\n\nPDF\n\n![Revenue chart]()"
        assert doc.markdown(expand_abbreviations=True) == "# Summary\n\nPortable Document Format\n\n![Revenue chart]()"


def test_markdown_structure_options_validation_and_max_size() -> None:
    with pyropdf.open(_rich_tagged_pdf()) as doc:
        for bad_mode in ("true", True, None, 1):
            with pytest.raises(ValueError, match="use_structure"):
                doc.markdown(use_structure=bad_mode)  # ty: ignore[invalid-argument-type]
        for bad_expand in ("yes", 1, None):
            with pytest.raises(TypeError, match="expand_abbreviations"):
                doc.markdown(expand_abbreviations=bad_expand)  # ty: ignore[invalid-argument-type]

        expected = doc.markdown(max_size=None)
        exact = len(expected.encode())
        assert doc.markdown(max_size=exact) == expected
        with pytest.raises(pyropdf.LimitError) as caught:
            doc.markdown(max_size=exact - 1)
        assert caught.value.code == "markdown_output_size"


def _positioned_pdf(
    pages: list[str],
    *,
    media_box: str = "0 0 400 400",
    extra_objects: dict[int, bytes | str] | None = None,
    resources: str = "<< /Font << /F1 3 0 R >> >>",
) -> bytes:
    """Build a multi-page PDF from raw content streams with Helvetica on ``/F1``."""
    count = len(pages)
    kids = " ".join(f"{10 + 2 * i} 0 R" for i in range(count))
    objects: dict[int, bytes | str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {count} /MediaBox [{media_box}] /Resources {resources} >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    if extra_objects:
        objects.update(extra_objects)
    for i, stream in enumerate(pages):
        page_num = 10 + 2 * i
        stream_num = page_num + 1
        objects[page_num] = f"<< /Type /Page /Parent 2 0 R /Contents {stream_num} 0 R >>"
        objects[stream_num] = _stream_obj(stream)
    return build_raw_pdf(objects)


def _sample_jpeg_bytes() -> bytes:
    """Encode a tiny valid RGB JPEG through pyropdf's own Pixmap encoder."""
    pam = b"P7\nWIDTH 4\nHEIGHT 4\nDEPTH 3\nMAXVAL 255\nTUPLTYPE RGB\nENDHDR\n" + bytes([200, 60, 40] * 16)
    return pyropdf.Pixmap.from_bytes(pam).to_bytes("jpeg", quality=90)


def _dual_image_pdf() -> bytes:
    """Build a page with text, an RGB image (PNG export), and a DCTDecode image (JPEG export)."""
    rgb_pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])
    jpeg_bytes = _sample_jpeg_bytes()
    stream = (
        "BT /F1 16 Tf 40 360 Td (Visual Report) Tj ET\n"
        "BT /F1 11 Tf 40 320 Td (Intro paragraph.) Tj ET\n"
        "q 60 0 0 40 40 240 cm /Im1 Do Q\n"
        "BT /F1 11 Tf 40 200 Td (Middle paragraph.) Tj ET\n"
        "q 60 0 0 40 40 120 cm /Im2 Do Q\n"
        "BT /F1 11 Tf 40 80 Td (Closing paragraph.) Tj ET\n"
    )
    img1 = (
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length "
        + str(len(rgb_pixels)).encode()
        + b" >>\nstream\n"
        + rgb_pixels
        + b"\nendstream"
    )
    img2 = (
        b"<< /Type /XObject /Subtype /Image /Width 4 /Height 4 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
        + str(len(jpeg_bytes)).encode()
        + b" >>\nstream\n"
        + jpeg_bytes
        + b"\nendstream"
    )
    return _positioned_pdf(
        [stream],
        extra_objects={4: img1, 5: img2},
        resources="<< /Font << /F1 3 0 R >> /XObject << /Im1 4 0 R /Im2 5 0 R >> >>",
    )


def test_markdown_margins_clip_headers_footers_sidebars_and_tables(tmp_path: Path) -> None:
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])
    img_obj = (
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length "
        + str(len(pixels)).encode()
        + b" >>\nstream\n"
        + pixels
        + b"\nendstream"
    )
    # MediaBox 0 0 400 400 -> display y = 400 - pdf_y.
    # Header at y~20 (pdf y=380), footer at y~380 (pdf y=20), left sidebar at x=10, right sidebar at x=340.
    # Header logo at display y=10..30 (pdf y=370..390), body image at display y=280..310 (pdf y=90..120).
    stream = (
        "BT /F1 10 Tf 140 380 Td (Top Header) Tj ET\n"
        "q 20 0 0 20 100 370 cm /Im1 Do Q\n"
        "BT /F1 10 Tf 10 200 Td (LeftNav) Tj ET\n"
        "BT /F1 10 Tf 345 200 Td (RightNav) Tj ET\n"
        "BT /F1 16 Tf 100 320 Td (Body Title) Tj ET\n"
        "BT /F1 10 Tf 100 290 Td (Main body sentence.) Tj ET\n"
        "0.5 w 100 250 m 280 250 l S 100 230 m 280 230 l S 100 210 m 280 210 l S\n"
        "100 250 m 100 210 l S 190 250 m 190 210 l S 280 250 m 280 210 l S\n"
        "BT /F1 10 Tf 108 236 Td (Key) Tj 90 0 Td (Val) Tj ET\n"
        "BT /F1 10 Tf 108 216 Td (A) Tj 90 0 Td (1) Tj ET\n"
        "q 30 0 0 30 100 90 cm /Im1 Do Q\n"
        "BT /F1 10 Tf 140 20 Td (Bottom Footer) Tj ET\n"
    )
    pdf = _positioned_pdf(
        [stream],
        extra_objects={4: img_obj},
        resources="<< /Font << /F1 3 0 R >> /XObject << /Im1 4 0 R >> >>",
    )
    with pyropdf.open(pdf) as doc:
        unclipped = doc.markdown()
        assert "Top Header" in unclipped
        assert "Bottom Footer" in unclipped
        assert "LeftNav" in unclipped
        assert "RightNav" in unclipped

        # Zero margins behave like unclipped.
        assert doc.markdown(margins=0) == unclipped
        assert doc.markdown(margins=(0, 0)) == unclipped
        assert doc.markdown(margins=(0, 0, 0, 0)) == unclipped

        # 4-tuple margins exclude top header, bottom footer, left/right sidebars, and header logo,
        # while keeping the body heading, paragraph, table, and body image (paint index 1).
        out_dir = tmp_path / "clipped_imgs"
        clipped = doc.markdown(margins=(65, 45, 65, 45), images=out_dir)
        assert "Top Header" not in clipped
        assert "Bottom Footer" not in clipped
        assert "LeftNav" not in clipped
        assert "RightNav" not in clipped
        assert "# Body Title" in clipped
        assert "Main body sentence." in clipped
        assert "| Key | Val |" in clipped
        assert "![](page-0-image-1.png)" in clipped
        assert "page-0-image-0" not in clipped
        assert sorted(p.name for p in out_dir.iterdir()) == ["page-0-image-1.png"]

        # 2-tuple (horizontal, vertical) and scalar margins match Page.markdown().
        assert doc.markdown(margins=(65, 45)) == doc.pages[0].markdown(margins=(65, 45))
        scalar = doc.markdown(margins=65)
        assert "Body Title" in scalar
        assert "Top Header" not in scalar
        assert "LeftNav" not in scalar

        # Margins larger than page dimensions return empty Markdown without error.
        empty_dir = tmp_path / "empty_imgs"
        assert doc.markdown(margins=250, images=empty_dir) == ""
        assert not empty_dir.exists()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_markdown_margins_work_in_display_coordinates_on_rotated_pages(rotation: int) -> None:
    # MediaBox 0 0 400 300 with one token near each of the 4 physical edges and one in the center.
    stream = (
        "BT /F1 12 Tf 180 280 Td (NorthEdge) Tj ET\n"
        "BT /F1 12 Tf 180 15 Td (SouthEdge) Tj ET\n"
        "BT /F1 12 Tf 10 150 Td (WestEdge) Tj ET\n"
        "BT /F1 12 Tf 320 150 Td (EastEdge) Tj ET\n"
        "BT /F1 12 Tf 170 150 Td (CenterBody) Tj ET\n"
    )
    pdf = _positioned_pdf([stream], media_box="0 0 400 300")
    with pyropdf.open(pdf) as doc:
        page = doc.pages[0]
        page.rotation = rotation
        words = {w.text: w.bbox for w in page.words()}
        _, width, height = page._geometry()  # noqa: SLF001

        # Uniform margin excludes all four edges in display coordinates while keeping CenterBody.
        md = doc.markdown(margins=55)
        assert "CenterBody" in md
        for edge in ("NorthEdge", "SouthEdge", "WestEdge", "EastEdge"):
            assert edge not in md

        # Asymmetric top-only margin excludes ONLY the token closest to the display top (y=0).
        top_token = min(words, key=lambda name: (words[name].y0 + words[name].y1) * 0.5)
        bottom_token = max(words, key=lambda name: (words[name].y0 + words[name].y1) * 0.5)
        top_clipped = doc.markdown(margins=(0, 55, 0, 0))
        assert top_token not in top_clipped
        assert bottom_token in top_clipped
        assert "CenterBody" in top_clipped
        assert width > 0
        assert height > 0


def test_markdown_margins_filter_tagged_structure_blocks() -> None:
    # Tagged PDF where header, sidebar, and footer are tagged as P (not Artifact).
    stream = (
        "BT /F1 10 Tf 120 380 Td /P << /MCID 0 >> BDC (Header Banner) Tj EMC ET\n"
        "BT /F1 10 Tf 10 200 Td /P << /MCID 1 >> BDC (SideNote) Tj EMC ET\n"
        "BT /F1 16 Tf 100 300 Td /H1 << /MCID 2 >> BDC (Structured Title) Tj EMC ET\n"
        "BT /F1 12 Tf 100 260 Td /P << /MCID 3 >> BDC (Structured body text.) Tj EMC ET\n"
        "BT /F1 10 Tf 120 20 Td /P << /MCID 4 >> BDC (Footer Line) Tj EMC ET\n"
    )
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /MarkInfo << /Marked true >> /StructTreeRoot 10 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 400 400] "
                "/Resources << /Font << /F1 4 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: _stream_obj(stream),
            10: "<< /Type /StructTreeRoot /K 11 0 R >>",
            11: "<< /Type /StructElem /S /Document /P 10 0 R /Pg 3 0 R /K [12 0 R 13 0 R 14 0 R 15 0 R 16 0 R] >>",
            12: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K 0 >>",
            13: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K 1 >>",
            14: "<< /Type /StructElem /S /H1 /P 11 0 R /Pg 3 0 R /K 2 >>",
            15: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K 3 >>",
            16: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K 4 >>",
        }
    )
    with pyropdf.open(pdf) as doc:
        assert "Header Banner" in doc.markdown()
        clipped = doc.markdown(margins=(65, 45, 65, 45))
        assert clipped == "# Structured Title\n\nStructured body text."


def test_markdown_ignore_headers_footers_suppresses_repeated_bands_and_page_numbers() -> None:
    pages = [
        # Page 0: unique cover title near top, shared middle body line, sequential footer.
        (
            "BT /F1 18 Tf 40 375 Td (Quarterly Engineering Report) Tj ET\n"
            "BT /F1 11 Tf 40 220 Td (Executive overview.) Tj ET\n"
            "BT /F1 11 Tf 40 190 Td (Repeated body statement.) Tj ET\n"
            "BT /F1 9 Tf 150 20 Td (Page 1 of 4) Tj ET\n"
        ),
        (
            "BT /F1 9 Tf 40 380 Td (ACME Confidential Draft) Tj ET\n"
            "BT /F1 11 Tf 40 220 Td (Second page findings.) Tj ET\n"
            "BT /F1 11 Tf 40 190 Td (Repeated body statement.) Tj ET\n"
            "BT /F1 9 Tf 150 20 Td (Page 2 of 4) Tj ET\n"
        ),
        (
            "BT /F1 9 Tf 40 380 Td (ACME Confidential Draft) Tj ET\n"
            "BT /F1 11 Tf 40 220 Td (Third page metrics.) Tj ET\n"
            "BT /F1 11 Tf 40 190 Td (Repeated body statement.) Tj ET\n"
            "BT /F1 9 Tf 150 20 Td (Page 3 of 4) Tj ET\n"
        ),
        (
            "BT /F1 9 Tf 40 380 Td (ACME Confidential Draft) Tj ET\n"
            "BT /F1 11 Tf 40 220 Td (Fourth page outlook.) Tj ET\n"
            "BT /F1 11 Tf 40 190 Td (Repeated body statement.) Tj ET\n"
            "BT /F1 9 Tf 150 20 Td (Page 4 of 4) Tj ET\n"
        ),
    ]
    with pyropdf.open(_positioned_pdf(pages)) as doc:
        default_md = doc.markdown()
        assert "ACME Confidential Draft" in default_md
        assert "Page 1 of 4" in default_md

        clean_md = doc.markdown(ignore_headers_footers=True)
        assert "ACME Confidential Draft" not in clean_md
        for n in range(1, 5):
            assert f"Page {n} of 4" not in clean_md
        # Unique first-page title and all body lines (including repeated middle-of-page lines) stay.
        assert "# Quarterly Engineering Report" in clean_md
        assert clean_md.count("Repeated body statement.") == 4
        assert "Executive overview." in clean_md
        assert "Fourth page outlook." in clean_md

        # Single-page selection does not suppress anything.
        single = doc.markdown([1], ignore_headers_footers=True)
        assert "ACME Confidential Draft" in single
        assert "Page 2 of 4" in single


def test_markdown_ignore_headers_footers_handles_roman_slash_dash_and_recto_verso() -> None:
    romans = ("i", "ii", "iii", "iv")
    pages = [
        (
            f"BT /F1 9 Tf 40 380 Td ({'Even Header' if i % 2 == 0 else 'Odd Header'} - {i + 1} / 4) Tj ET\n"
            + ("BT /F1 11 Tf 40 360 Td (I write this unique intro.) Tj ET\n" if i == 0 else "")
            + f"BT /F1 11 Tf 40 200 Td (Body paragraph {i + 1}.) Tj ET\n"
            + f"BT /F1 9 Tf 160 35 Td (Preface - {romans[i]}  Page {romans[i]} of iv) Tj ET\n"
            + f"BT /F1 9 Tf 190 15 Td ({romans[i]}) Tj ET\n"
        )
        for i in range(4)
    ]
    with pyropdf.open(_positioned_pdf(pages)) as doc:
        cleaned = doc.markdown(ignore_headers_footers=True)
        assert "Even Header" not in cleaned
        assert "Odd Header" not in cleaned
        assert "Preface -" not in cleaned
        assert "of iv" not in cleaned
        assert cleaned.splitlines()[-1] == "Body paragraph 4."
        assert "I write this unique intro." in cleaned
        for i in range(1, 5):
            assert f"Body paragraph {i}." in cleaned


def test_markdown_ignore_headers_footers_works_on_tagged_documents() -> None:
    # 3-page tagged PDF with running headers/footers tagged as P instead of Artifact.
    objects: dict[int, bytes | str] = {
        1: "<< /Type /Catalog /Pages 2 0 R /MarkInfo << /Marked true >> /StructTreeRoot 10 0 R >>",
        2: (
            "<< /Type /Pages /Kids [3 0 R 6 0 R 8 0 R] /Count 3 /MediaBox [0 0 400 400] "
            "/Resources << /Font << /F1 4 0 R >> >> >>"
        ),
        3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
        4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: _stream_obj(
            "BT /F1 16 Tf 40 375 Td /H1 << /MCID 0 >> BDC (Unique Cover Title) Tj EMC ET\n"
            "BT /F1 11 Tf 40 200 Td /P << /MCID 1 >> BDC (Tagged page one.) Tj EMC ET\n"
            "BT /F1 9 Tf 160 20 Td /P << /MCID 2 >> BDC (- 1 -) Tj EMC ET\n"
        ),
        6: "<< /Type /Page /Parent 2 0 R /Contents 7 0 R /StructParents 1 >>",
        7: _stream_obj(
            "BT /F1 9 Tf 40 380 Td /P << /MCID 0 >> BDC (Running Doc Header) Tj EMC ET\n"
            "BT /F1 11 Tf 40 200 Td /P << /MCID 1 >> BDC (Tagged page two.) Tj EMC ET\n"
            "BT /F1 9 Tf 160 20 Td /P << /MCID 2 >> BDC (- 2 -) Tj EMC ET\n"
        ),
        8: "<< /Type /Page /Parent 2 0 R /Contents 9 0 R /StructParents 2 >>",
        9: _stream_obj(
            "BT /F1 9 Tf 40 380 Td /P << /MCID 0 >> BDC (Running Doc Header) Tj EMC ET\n"
            "BT /F1 11 Tf 40 200 Td /P << /MCID 1 >> BDC (Tagged page three.) Tj EMC ET\n"
            "BT /F1 9 Tf 160 20 Td /P << /MCID 2 >> BDC (- 3 -) Tj EMC ET\n"
        ),
        10: "<< /Type /StructTreeRoot /K 11 0 R >>",
        11: (
            "<< /Type /StructElem /S /Document /P 10 0 R "
            "/K [12 0 R 13 0 R 14 0 R 15 0 R 16 0 R 17 0 R 18 0 R 19 0 R 20 0 R] >>"
        ),
        12: "<< /Type /StructElem /S /H1 /P 11 0 R /Pg 3 0 R /K 0 >>",
        13: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K 1 >>",
        14: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K 2 >>",
        15: "<< /Type /StructElem /S /P /P 11 0 R /Pg 6 0 R /K 0 >>",
        16: "<< /Type /StructElem /S /P /P 11 0 R /Pg 6 0 R /K 1 >>",
        17: "<< /Type /StructElem /S /P /P 11 0 R /Pg 6 0 R /K 2 >>",
        18: "<< /Type /StructElem /S /P /P 11 0 R /Pg 8 0 R /K 0 >>",
        19: "<< /Type /StructElem /S /P /P 11 0 R /Pg 8 0 R /K 1 >>",
        20: "<< /Type /StructElem /S /P /P 11 0 R /Pg 8 0 R /K 2 >>",
    }
    with pyropdf.open(build_raw_pdf(objects)) as doc:
        md = doc.markdown(ignore_headers_footers=True)
        assert md == "# Unique Cover Title\n\nTagged page one.\n\nTagged page two.\n\nTagged page three."


def test_markdown_image_export_default_directory_and_callback(tmp_path: Path) -> None:
    pdf = _dual_image_pdf()
    with pyropdf.open(pdf) as doc:
        # Default images=None performs no disk writes and emits no image links.
        plain = doc.markdown()
        assert "![" not in plain
        assert list(tmp_path.iterdir()) == []

        # Directory export writes PNG for uncompressed RGB and JPEG passthrough for DCTDecode.
        out_dir = tmp_path / "exported"
        md = doc.markdown(images=out_dir)
        assert md == (
            "# Visual Report\n\n"
            "Intro paragraph.\n\n"
            "![](page-0-image-0.png)\n\n"
            "Middle paragraph.\n\n"
            "![](page-0-image-1.jpg)\n\n"
            "Closing paragraph."
        )
        assert sorted(p.name for p in out_dir.iterdir()) == ["page-0-image-0.png", "page-0-image-1.jpg"]
        assert (out_dir / "page-0-image-0.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert (out_dir / "page-0-image-1.jpg").read_bytes().startswith(b"\xff\xd8")

        # Page.markdown() supports directory export identically.
        page_dir = tmp_path / "page_only"
        assert doc.pages[0].markdown(images=page_dir) == md
        assert sorted(p.name for p in page_dir.iterdir()) == ["page-0-image-0.png", "page-0-image-1.jpg"]

        # Callback export inspects ExportedImage records without writing anything to disk.
        seen: list[pyropdf.ExportedImage] = []

        def handle_image(img: pyropdf.ExportedImage) -> str | None:
            seen.append(img)
            if img.index == 0:
                return f"assets/{img.filename}"
            return None

        cb_md = doc.markdown(images=handle_image)
        assert [(img.page, img.index, img.filename, img.media_type, img.width, img.height) for img in seen] == [
            (0, 0, "page-0-image-0.png", "image/png", 2, 2),
            (0, 1, "page-0-image-1.jpg", "image/jpeg", 4, 4),
        ]
        assert seen[0].data.startswith(b"\x89PNG\r\n\x1a\n")
        assert seen[1].data.startswith(b"\xff\xd8")
        assert "![](assets/page-0-image-0.png)" in cb_md
        assert "page-0-image-1" not in cb_md

        # Callback destination containing spaces/parentheses is wrapped safely in angle brackets.
        spaced = doc.markdown(images=lambda img: f"my images/fig ({img.index}).png")
        assert "![](<my images/fig (0).png>)" in spaced


def test_markdown_image_export_atomicity_and_limits(tmp_path: Path) -> None:
    pdf = _dual_image_pdf()
    with pyropdf.open(pdf) as doc:
        # Atomic failure: exceeding max_size writes zero files and does not create the directory.
        fail_dir = tmp_path / "should_not_exist"
        with pytest.raises(pyropdf.LimitError) as caught:
            doc.markdown(images=fail_dir, max_size=20)
        assert caught.value.code == "markdown_output_size"
        assert not fail_dir.exists()

    # Tight DocumentLimits.max_memory_size bounds image export bytes atomically.
    # Build a 64x64 non-uniform RGB image whose encoded PNG exceeds 4 KiB while the PDF opens easily.
    noisy = bytes(((x * 37 + y * 91 + c * 53) & 0xFF) for y in range(64) for x in range(64) for c in range(3))
    noisy_img = (
        b"<< /Type /XObject /Subtype /Image /Width 64 /Height 64 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length "
        + str(len(noisy)).encode()
        + b" >>\nstream\n"
        + noisy
        + b"\nendstream"
    )
    noisy_pdf = _positioned_pdf(
        ["BT /F1 11 Tf 40 300 Td (Big image) Tj ET\nq 64 0 0 64 40 180 cm /Im1 Do Q\n"],
        extra_objects={4: noisy_img},
        resources="<< /Font << /F1 3 0 R >> /XObject << /Im1 4 0 R >> >>",
    )
    mem_dir = tmp_path / "mem_fail"
    with pyropdf.open(noisy_pdf, limits=pyropdf.DocumentLimits(max_memory_size=4096)) as doc:
        with pytest.raises(pyropdf.LimitError) as caught:
            doc.markdown(images=mem_dir)
        assert caught.value.code == "memory_size"
        assert not mem_dir.exists()

    # Directory target on a text-only PDF does not create an empty directory.
    text_only_dir = tmp_path / "unused_dir"
    with pyropdf.open(_positioned_pdf(["BT /F1 12 Tf 40 300 Td (No images here.) Tj ET"])) as doc:
        assert doc.markdown(images=text_only_dir) == "No images here."
        assert not text_only_dir.exists()


def test_markdown_image_export_preserves_tagged_figure_alt_text_and_skips_artifacts(tmp_path: Path) -> None:
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])
    img_obj = (
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length "
        + str(len(pixels)).encode()
        + b" >>\nstream\n"
        + pixels
        + b"\nendstream"
    )
    stream = (
        "/Artifact BMC q 20 0 0 20 40 360 cm /Im1 Do Q EMC\n"
        "BT /F1 16 Tf 40 320 Td /H1 << /MCID 0 >> BDC (Tagged Gallery) Tj EMC ET\n"
        "/Figure << /MCID 1 >> BDC q 80 0 0 60 40 200 cm /Im1 Do Q EMC\n"
    )
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /MarkInfo << /Marked true >> /StructTreeRoot 10 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 400 400] "
                "/Resources << /Font << /F1 4 0 R >> /XObject << /Im1 6 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: _stream_obj(stream),
            6: img_obj,
            10: "<< /Type /StructTreeRoot /K 11 0 R >>",
            11: "<< /Type /StructElem /S /Document /P 10 0 R /Pg 3 0 R /K [12 0 R 13 0 R] >>",
            12: "<< /Type /StructElem /S /H1 /P 11 0 R /Pg 3 0 R /K 0 >>",
            13: "<< /Type /StructElem /S /Figure /P 11 0 R /Pg 3 0 R /Alt (Revenue chart) /K 1 >>",
        }
    )
    with pyropdf.open(pdf) as doc:
        out_dir = tmp_path / "tagged_imgs"
        md = doc.markdown(images=out_dir)
        # Artifact image at paint index 0 is skipped; Figure image at paint index 1 is exported with alt text.
        assert md == "# Tagged Gallery\n\n![Revenue chart](page-0-image-1.png)"
        assert sorted(p.name for p in out_dir.iterdir()) == ["page-0-image-1.png"]

        captured: list[pyropdf.ExportedImage] = []
        cb_md = doc.markdown(images=lambda img: (captured.append(img), f"cdn/{img.filename}")[1])
        assert cb_md == "# Tagged Gallery\n\n![Revenue chart](cdn/page-0-image-1.png)"
        assert len(captured) == 1
        assert captured[0].alt_text == "Revenue chart"


def test_markdown_max_graphics_bounds_vector_heavy_pages() -> None:
    lines = "\n".join(f"10 {20 + i} m 390 {20 + i} l S" for i in range(120))
    stream = f"BT /F1 12 Tf 40 350 Td (CAD Plan) Tj ET\n0.5 w\n{lines}\n"
    pdf = _positioned_pdf([stream])
    with pyropdf.open(pdf) as doc:
        with pytest.raises(pyropdf.LimitError) as caught:
            doc.markdown(max_graphics=50)
        assert caught.value.code == "content_element_count"
        with pytest.raises(pyropdf.LimitError) as caught_page:
            doc.pages[0].markdown(max_graphics=50)
        assert caught_page.value.code == "content_element_count"
        with pytest.raises(pyropdf.LimitError) as caught_no_tables:
            doc.markdown(table_strategy=None, max_graphics=50)
        assert caught_no_tables.value.code == "content_element_count"

        assert doc.markdown(max_graphics=5000) == "CAD Plan"
        assert doc.markdown(max_graphics=None) == "CAD Plan"

    token = pyropdf.CancellationToken()
    with pyropdf.open(pdf, limits=pyropdf.DocumentLimits(cancellation=token)) as doc:
        token.cancel()
        with pytest.raises(pyropdf.LimitError) as cancelled:
            doc.markdown()
        assert cancelled.value.code == "cancelled"


def test_markdown_retrieval_control_option_validation(tmp_path: Path) -> None:
    existing_file = tmp_path / "not_a_dir.txt"
    existing_file.write_text("x", encoding="utf-8")

    with pyropdf.open(_dual_image_pdf()) as doc:
        for bad_margin_type in (True, "10", [10, 20], (True, 10)):
            with pytest.raises(TypeError, match="margins"):
                doc.markdown(margins=bad_margin_type)  # ty: ignore[invalid-argument-type]
        for bad_margin_val in (-1, float("nan"), float("inf"), (), (10,), (1, 2, 3), (1, -2, 3, 4)):
            with pytest.raises(ValueError, match="margins"):
                doc.markdown(margins=bad_margin_val)  # ty: ignore[invalid-argument-type]
        for bad_hf in ("yes", 1, None):
            with pytest.raises(TypeError, match="ignore_headers_footers"):
                doc.markdown(ignore_headers_footers=bad_hf)  # ty: ignore[invalid-argument-type]
        for bad_images_type in (123, b"/tmp"):
            with pytest.raises(TypeError, match="images"):
                doc.markdown(images=bad_images_type)  # ty: ignore[invalid-argument-type]
        for bad_images_val in ("", existing_file):
            with pytest.raises(ValueError, match="images"):
                doc.markdown(images=bad_images_val)
        with pytest.raises(TypeError, match="images callback"):
            doc.markdown(images=lambda _img: 123)  # ty: ignore[invalid-argument-type]
        with pytest.raises(ValueError, match="single-line"):
            doc.markdown(images=lambda _img: "bad\nlink.png")
        for bad_gfx_type in (True, 1.5, "10"):
            with pytest.raises(TypeError, match="max_graphics"):
                doc.markdown(max_graphics=bad_gfx_type)  # ty: ignore[invalid-argument-type]
        for bad_gfx_val in (0, -5):
            with pytest.raises(ValueError, match="max_graphics"):
                doc.markdown(max_graphics=bad_gfx_val)
        for bad_ruby_type in (True, 123, None):
            with pytest.raises(TypeError, match="ruby"):
                doc.markdown(ruby=bad_ruby_type)  # ty: ignore[invalid-argument-type]
        with pytest.raises(ValueError, match="ruby"):
            doc.markdown(ruby="inline")  # ty: ignore[invalid-argument-type]


def _japanese_md_pdf(
    stream: str,
    *,
    extra_objects: dict[int, str | bytes] | None = None,
    catalog_extra: str = "",
    page_extra: str = "",
) -> bytes:
    objects: dict[int, str | bytes] = {
        1: f"<< /Type /Catalog /Pages 2 0 R {catalog_extra}>>",
        2: (
            "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 500 600] "
            "/Resources << /Font << /F1 5 0 R /FH 6 0 R /FV 7 0 R >> >> >>"
        ),
        3: f"<< /Type /Page /Parent 2 0 R /Contents 4 0 R {page_extra}>>",
        4: _stream_obj(stream),
        5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        6: "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Mincho /Encoding /90ms-RKSJ-H /DescendantFonts [8 0 R] >>",
        7: "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Mincho /Encoding /90ms-RKSJ-V /DescendantFonts [8 0 R] >>",
        8: (
            "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MS-Mincho "
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >> "
            "/FontDescriptor 9 0 R /DW 1000 >>"
        ),
        9: (
            "<< /Type /FontDescriptor /FontName /MS-Mincho /Flags 6 /FontBBox [0 -137 1000 859] "
            "/ItalicAngle 0 /Ascent 859 /Descent -140 /CapHeight 769 /StemV 78 >>"
        ),
    }
    if extra_objects:
        objects.update(extra_objects)
    return build_raw_pdf(objects)


def _sjis(text: str) -> str:
    return "".join(f"\\{byte:03o}" for byte in text.encode("cp932"))


def test_markdown_mixed_orientation_dan_gumi_and_embedded_vertical_columns() -> None:
    stream = "\n".join(
        [
            "BT /F1 20 Tf 40 550 Td (Chapter Title) Tj ET",
            f"BT /FV 12 Tf 320 490 Td ({_sjis('上段左側')}) Tj ET",
            f"BT /FV 12 Tf 340 490 Td ({_sjis('上段右側')}) Tj ET",
            "BT /F1 16 Tf 40 370 Td (Subheading) Tj ET",
            f"BT /FV 12 Tf 320 310 Td ({_sjis('下段左側')}) Tj ET",
            f"BT /FV 12 Tf 340 310 Td ({_sjis('下段右側')}) Tj ET",
            "BT /F1 12 Tf 40 80 Td (Footer note.) Tj ET",
        ]
    )
    with pyropdf.open(_japanese_md_pdf(stream)) as doc:
        md = doc.pages[0].markdown()
        assert md == "# Chapter Title\n\n上段右側上段左側\n\n## Subheading\n\n下段右側下段左側\n\nFooter note."
        assert doc.markdown() == md

    font_path = (
        Path(__file__).resolve().parent.parent / "crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf"
    )
    font = pyropdf.Font.from_file(font_path)
    with pyropdf.Document() as doc:
        page = doc.pages.new((400, 360))
        with page.draw() as canvas:
            canvas.text("Title Heading", (40, 40), size=20)
            canvas.textbox((120, 80, 300, 145), "日本語かなのテキスト。", font=font, size=12, direction="vertical")
            canvas.text("Footer Note", (40, 310), size=12)
        assert page.markdown() == "# Title Heading\n\n日本語かなのテキスト。\n\nFooter Note"


def test_markdown_geometric_japanese_ruby_preserves_body_size_and_spacing() -> None:
    stream = "\n".join(
        [
            "BT /F1 20 Tf 60 520 Td (Travel Guide) Tj ET",
            f"BT /FH 12 Tf 60 460 Td ({_sjis('東京から大阪への旅を楽しみましょう。')}) Tj ET",
            f"BT /FH 6 Tf 58 473 Td ({_sjis('とうきょう')}) Tj ET",
            f"BT /FH 6 Tf 106 473 Td ({_sjis('おおさか')}) Tj ET",
        ]
    )
    with pyropdf.open(_japanese_md_pdf(stream)) as doc:
        page = doc.pages[0]
        assert page.markdown() == "# Travel Guide\n\n東京から大阪への旅を楽しみましょう。"
        assert (
            page.markdown(ruby="parenthesize")
            == "# Travel Guide\n\n東京(とうきょう)から大阪(おおさか)への旅を楽しみましょう。"
        )
        assert (
            doc.markdown(ruby="parenthesize")
            == "# Travel Guide\n\n東京(とうきょう)から大阪(おおさか)への旅を楽しみましょう。"
        )
        kept = page.markdown(ruby="keep")
        assert "とうきょう" in kept
        assert "東京から大阪への旅を楽しみましょう。" in kept

    # Embedded horizontal ruby: no spurious space after parenthesized ruby span.
    font_path = (
        Path(__file__).resolve().parent.parent / "crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf"
    )
    font = pyropdf.Font.from_file(font_path)
    with pyropdf.Document() as doc:
        page = doc.pages.new((400, 300))
        with page.draw() as canvas:
            canvas.text("日本語テキスト", (60, 100), font=font, size=14)
            canvas.text("かな", (64, 86), font=font, size=6)
        assert page.markdown() == "日本語テキスト"
        assert page.markdown(ruby="parenthesize") == "日本(かな)語テキスト"


def test_markdown_structure_ruby_honors_rb_rt_and_rp_tags() -> None:
    # Tagged PDF containing two /Ruby annotations inside one /P:
    #   1. /RB (東京) + /RT (とうきょう) without /RP
    #   2. /RB (大阪) + /RP (（) + /RT (おおさか) + /RP (）)
    stream = "\n".join(
        [
            f"BT /FH 12 Tf 60 460 Td /RB << /MCID 0 >> BDC ({_sjis('東京')}) Tj EMC ET",
            f"BT /FH 6 Tf 58 473 Td /RT << /MCID 1 >> BDC ({_sjis('とうきょう')}) Tj EMC ET",
            f"BT /FH 12 Tf 84 460 Td /Span << /MCID 2 >> BDC ({_sjis('から')}) Tj EMC ET",
            f"BT /FH 12 Tf 108 460 Td /RB << /MCID 3 >> BDC ({_sjis('大阪')}) Tj EMC ET",
            f"BT /FH 6 Tf 104 473 Td /RP << /MCID 4 >> BDC ({_sjis('（')}) Tj EMC ET",
            f"BT /FH 6 Tf 110 473 Td /RT << /MCID 5 >> BDC ({_sjis('おおさか')}) Tj EMC ET",
            f"BT /FH 6 Tf 134 473 Td /RP << /MCID 6 >> BDC ({_sjis('）')}) Tj EMC ET",
            f"BT /FH 12 Tf 132 460 Td /Span << /MCID 7 >> BDC ({_sjis('へ')}) Tj EMC ET",
        ]
    )
    extra: dict[int, str | bytes] = {
        10: "<< /Type /StructTreeRoot /K 11 0 R >>",
        11: "<< /Type /StructElem /S /Document /P 10 0 R /Pg 3 0 R /K 12 0 R >>",
        12: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K [13 0 R 16 0 R 17 0 R 22 0 R] >>",
        13: "<< /Type /StructElem /S /Ruby /P 12 0 R /Pg 3 0 R /K [14 0 R 15 0 R] >>",
        14: "<< /Type /StructElem /S /RB /P 13 0 R /Pg 3 0 R /K 0 >>",
        15: "<< /Type /StructElem /S /RT /P 13 0 R /Pg 3 0 R /K 1 >>",
        16: "<< /Type /StructElem /S /Span /P 12 0 R /Pg 3 0 R /K 2 >>",
        17: "<< /Type /StructElem /S /Ruby /P 12 0 R /Pg 3 0 R /K [18 0 R 19 0 R 20 0 R 21 0 R] >>",
        18: "<< /Type /StructElem /S /RB /P 17 0 R /Pg 3 0 R /K 3 >>",
        19: "<< /Type /StructElem /S /RP /P 17 0 R /Pg 3 0 R /K 4 >>",
        20: "<< /Type /StructElem /S /RT /P 17 0 R /Pg 3 0 R /K 5 >>",
        21: "<< /Type /StructElem /S /RP /P 17 0 R /Pg 3 0 R /K 6 >>",
        22: "<< /Type /StructElem /S /Span /P 12 0 R /Pg 3 0 R /K 7 >>",
    }
    pdf = _japanese_md_pdf(
        stream,
        extra_objects=extra,
        catalog_extra="/MarkInfo << /Marked true >> /StructTreeRoot 10 0 R ",
        page_extra="/StructParents 0 ",
    )
    with pyropdf.open(pdf) as doc:
        for mode in ("auto", "always"):
            assert doc.markdown(use_structure=mode) == "東京から大阪へ"
            assert doc.markdown(use_structure=mode, ruby="parenthesize") == "東京(とうきょう)から大阪（おおさか）へ"
            assert doc.markdown(use_structure=mode, ruby="keep") == "東京とうきょうから大阪（おおさか）へ"


def test_markdown_undescribed_figure_with_text_falls_back_instead_of_dropping_text() -> None:
    stream = (
        "BT /F1 12 Tf 40 340 Td /P << /MCID 0 >> BDC (Intro) Tj EMC ET\n"
        "BT /F1 12 Tf 40 280 Td /Figure << /MCID 1 >> BDC (Important chart label text that must not vanish) Tj EMC ET\n"
    )
    objects: dict[int, bytes | str] = {
        1: "<< /Type /Catalog /Pages 2 0 R /MarkInfo << /Marked true >> /StructTreeRoot 10 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 400 400] /Resources << /Font << /F1 4 0 R >> >> >>",
        3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
        4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: _stream_obj(stream),
        10: "<< /Type /StructTreeRoot /K 11 0 R >>",
        11: "<< /Type /StructElem /S /Document /P 10 0 R /Pg 3 0 R /K [12 0 R 13 0 R] >>",
        12: "<< /Type /StructElem /S /P /P 11 0 R /Pg 3 0 R /K 0 >>",
        13: "<< /Type /StructElem /S /Figure /P 11 0 R /Pg 3 0 R /K 1 >>",
    }
    with pyropdf.open(build_raw_pdf(objects)) as doc:
        # Undescribed Figure with substantial painted text fails trustworthiness and falls back to geometric text.
        auto_md = doc.markdown(use_structure="auto")
        assert "Intro" in auto_md
        assert "Important chart label text that must not vanish" in auto_md
        # Explicit /Alt replaces the painted text and passes trustworthiness.
        doc.structure[0].children[1].alt = "Sales chart"
        assert doc.markdown(use_structure="auto") == "Intro\n\n![Sales chart]()"


def test_markdown_nested_blockquote_multipage_table_selection_and_multiline_heading() -> None:
    objects: dict[int, bytes | str] = {
        1: "<< /Type /Catalog /Pages 2 0 R /MarkInfo << /Marked true >> /StructTreeRoot 10 0 R >>",
        2: (
            "<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 /MediaBox [0 0 400 400] "
            "/Resources << /Font << /F1 4 0 R >> >> >>"
        ),
        3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
        4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: _stream_obj(
            "BT /F1 16 Tf 40 360 Td /H1 << /MCID 0 >> BDC (Heading) Tj EMC ET\n"
            "BT /F1 11 Tf 40 320 Td /BlockQuote << /MCID 1 >> BDC (Opening quote.) Tj EMC ET\n"
            "BT /F1 11 Tf 40 290 Td /P << /MCID 2 >> BDC (Nested quote.) Tj EMC ET\n"
            "BT /F1 11 Tf 40 240 Td /TH << /MCID 3 >> BDC (Name) Tj EMC "
            "100 0 Td /TH << /MCID 4 >> BDC (Qty) Tj EMC ET\n"
        ),
        6: "<< /Type /Page /Parent 2 0 R /Contents 7 0 R /StructParents 1 >>",
        7: _stream_obj(
            "BT /F1 11 Tf 40 340 Td /TD << /MCID 0 >> BDC (Bolt) Tj EMC 100 0 Td /TD << /MCID 1 >> BDC (12) Tj EMC ET\n"
        ),
        10: "<< /Type /StructTreeRoot /K 11 0 R >>",
        11: "<< /Type /StructElem /S /Document /P 10 0 R /Pg 3 0 R /K [12 0 R 13 0 R 16 0 R] >>",
        12: "<< /Type /StructElem /S /H1 /P 11 0 R /Pg 3 0 R /ActualText (Chapter 1\\nOverview) /K 0 >>",
        13: "<< /Type /StructElem /S /BlockQuote /P 11 0 R /Pg 3 0 R /K [1 14 0 R] >>",
        14: "<< /Type /StructElem /S /Div /P 13 0 R /Pg 3 0 R /K [15 0 R] >>",
        15: "<< /Type /StructElem /S /P /P 14 0 R /Pg 3 0 R /K 2 >>",
        16: "<< /Type /StructElem /S /Table /P 11 0 R /Pg 3 0 R /K [17 0 R 20 0 R] >>",
        17: "<< /Type /StructElem /S /TR /P 16 0 R /Pg 3 0 R /K [18 0 R 19 0 R] >>",
        18: "<< /Type /StructElem /S /TH /P 17 0 R /Pg 3 0 R /K 3 >>",
        19: "<< /Type /StructElem /S /TH /P 17 0 R /Pg 3 0 R /K 4 >>",
        20: "<< /Type /StructElem /S /TR /P 16 0 R /Pg 6 0 R /K [21 0 R 22 0 R] >>",
        21: "<< /Type /StructElem /S /TD /P 20 0 R /Pg 6 0 R /K 0 >>",
        22: "<< /Type /StructElem /S /TD /P 20 0 R /Pg 6 0 R /K 1 >>",
    }
    with pyropdf.open(build_raw_pdf(objects)) as doc:
        full_md = doc.markdown()
        assert full_md == (
            "# Chapter 1 Overview\n\n"
            "> Opening quote.\n\n"
            "> Nested quote.\n\n"
            "| Name | Qty |\n"
            "| --- | --- |\n"
            "| Bolt | 12 |"
        )
        # Selecting only page 1 keeps the page-1 table row instead of dropping the table.
        page1_md = doc.markdown([1])
        assert "| Bolt | 12 |" in page1_md


def test_markdown_subtree_mcid_scan_enforces_global_depth_limit() -> None:
    objects: dict[int, bytes | str] = {
        1: "<< /Type /Catalog /Pages 2 0 R /MarkInfo << /Marked true >> /StructTreeRoot 10 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 400 400] /Resources << /Font << /F1 4 0 R >> >> >>",
        3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
        4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: _stream_obj("BT /F1 12 Tf 40 340 Td /Artifact << /MCID 0 >> BDC (Header) Tj EMC ET"),
        10: "<< /Type /StructTreeRoot /K 11 0 R >>",
    }
    # 240 nested Divs -> Artifact -> 25 nested Divs exceeds _MAX_STRUCTURE_DEPTH (256) globally.
    for i in range(11, 251):
        parent = 10 if i == 11 else i - 1
        objects[i] = f"<< /Type /StructElem /S /Div /P {parent} 0 R /Pg 3 0 R /K {i + 1} 0 R >>"
    objects[251] = "<< /Type /StructElem /S /Artifact /P 250 0 R /Pg 3 0 R /K 252 0 R >>"
    for i in range(252, 276):
        objects[i] = f"<< /Type /StructElem /S /Div /P {i - 1} 0 R /Pg 3 0 R /K {i + 1} 0 R >>"
    objects[276] = "<< /Type /StructElem /S /Span /P 275 0 R /Pg 3 0 R /K 0 >>"

    with pyropdf.open(build_raw_pdf(objects)) as doc:
        with pytest.raises(pyropdf.LimitError) as caught:
            doc.markdown(use_structure="always")
        assert caught.value.code == "structure_tree_count"


def test_markdown_image_ordered_after_table_on_table_owned_line() -> None:
    from pyropdf import _markdown  # noqa: PLC0415

    layout = {
        "width": 400.0,
        "height": 400.0,
        "blocks": [
            {
                "bbox": (40.0, 115.0, 240.0, 130.0),
                "lines": [
                    {
                        "bbox": (40.0, 115.0, 240.0, 130.0),
                        "dir": (1.0, 0.0),
                        "wmode": 0,
                        "spans": [{"bbox": (40.0, 115.0, 240.0, 130.0), "size": 10.0, "flags": 0, "text": "Cell"}],
                    }
                ],
            }
        ],
    }
    tables = [((40.0, 100.0, 240.0, 140.0), "| A |\n| --- |\n| B |")]
    images = [
        ((40.0, 80.0, 80.0, 95.0), "![](above.png)"),
        ((40.0, 110.0, 80.0, 114.0), "![](below.png)"),
    ]
    md = _markdown.page_to_markdown(layout, {}, tables, None, images=images)
    assert md == "![](above.png)\n\n| A |\n| --- |\n| B |\n\n![](below.png)"


def test_markdown_ignore_headers_footers_nfkc_fullwidth_and_false_positive_guards() -> None:
    from pyropdf import _markdown  # noqa: PLC0415

    # NFKC normalizes fullwidth spaces, dashes, digits, and Unicode Roman numerals.
    assert _markdown._normalize_band_text("－ １ －") == "- # -"  # noqa: SLF001
    assert _markdown._normalize_band_text("Ｐａｇｅ　２　ｏｆ　１０") == "page # of #"  # noqa: SLF001
    assert _markdown._normalize_band_text("Ⅰ") == "#"  # noqa: SLF001
    assert _markdown._normalize_band_text("Ⅳ") == "#"  # noqa: SLF001
    assert _markdown._normalize_band_text("ii.") == "#."  # noqa: SLF001
    # Words like "Mix" (1009) or "Di" (501) are not normalized as small Roman page numbers.
    assert _markdown._normalize_band_text("Topic - Mix") == "topic - mix"  # noqa: SLF001
    assert _markdown._normalize_band_text("Topic - Di") == "topic - di"  # noqa: SLF001
    # Blocks taller than the 15% edge band are excluded from running header/footer matching.
    assert _markdown._band_for_bbox((0.0, 10.0, 300.0, 90.0), 0.0, 400.0) is None  # noqa: SLF001
    assert _markdown._band_for_bbox((0.0, 10.0, 300.0, 25.0), 0.0, 400.0) == "top"  # noqa: SLF001

    # End-to-end check: unique titles ending with Mix/Di/Cl in the top band are preserved;
    # dotted Roman footers (i., ii., iii.) are suppressed.
    pages = [
        (
            f"BT /F1 10 Tf 40 380 Td (Topic - {label}) Tj ET\n"
            f"BT /F1 11 Tf 40 200 Td (Body {idx}.) Tj ET\n"
            f"BT /F1 9 Tf 180 20 Td ({roman}.) Tj ET\n"
        )
        for idx, (label, roman) in enumerate((("Mix", "i"), ("Di", "ii"), ("Cl", "iii")), start=1)
    ]
    with pyropdf.open(_positioned_pdf(pages)) as doc:
        cleaned = doc.markdown(ignore_headers_footers=True)
        for label in ("Topic - Mix", "Topic - Di", "Topic - Cl"):
            assert label in cleaned
        assert "ii." not in cleaned


def test_markdown_image_export_budget_code_consistent_for_png_and_jpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyropdf import _markdown  # noqa: PLC0415

    # Both PNG and JPEG exceeding the cumulative export image budget raise export_output_size.
    monkeypatch.setattr(_markdown, "_DEFAULT_MAX_IMAGE_EXPORT_SIZE", 32)
    with pyropdf.open(_dual_image_pdf()) as doc:
        png_fail = tmp_path / "png_fail"
        with pytest.raises(pyropdf.LimitError) as png_err:
            doc.markdown(images=png_fail)
        assert png_err.value.code == "export_output_size"
        assert not png_fail.exists()

    jpeg_only = _positioned_pdf(
        ["BT /F1 11 Tf 40 300 Td (Photo) Tj ET\nq 40 0 0 40 40 180 cm /Im1 Do Q\n"],
        extra_objects={
            4: (
                b"<< /Type /XObject /Subtype /Image /Width 4 /Height 4 "
                b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
                + str(len(_sample_jpeg_bytes())).encode()
                + b" >>\nstream\n"
                + _sample_jpeg_bytes()
                + b"\nendstream"
            )
        },
        resources="<< /Font << /F1 3 0 R >> /XObject << /Im1 4 0 R >> >>",
    )
    with pyropdf.open(jpeg_only) as doc:
        jpg_fail = tmp_path / "jpg_fail"
        with pytest.raises(pyropdf.LimitError) as jpg_err:
            doc.markdown(images=jpg_fail)
        assert jpg_err.value.code == "export_output_size"
        assert not jpg_fail.exists()
