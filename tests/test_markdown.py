"""Markdown from ``Page.markdown``, ``Document.markdown``, and ``Table.markdown``.

Ported from pylopdf 036be111 ``tests/test_markdown.py`` and the Markdown
tests of ``tests/test_tables.py``. The differences:

- pylopdf adds text with ``page.apply_ocr``, which pyropdf does not have
  (#102); these tests show the same strings with a font whose ToUnicode CMap
  maps one code to each whole string.
- pylopdf's ``page.document.markdown()`` is ``document.markdown()``.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
from test_content import build_raw_pdf
from test_tables import _bordered_table_pdf, _sparse_row_grid_pdf, build_visual_rtl_pdf

import pyropdf

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
