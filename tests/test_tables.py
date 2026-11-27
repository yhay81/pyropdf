"""Tables from vector rules and aligned text with ``Page.tables``.

Ported from pylopdf 036be111 ``tests/test_tables.py``. The differences:

- The Markdown tests and assertions are in ``tests/test_markdown.py``.
- pylopdf inserts a page from another document; these tests add a blank
  page with ``pages.new``.
- ``Page.tables`` also takes the ``max_glyphs`` and ``max_size`` budgets of
  the other text views, and a deleted page refuses it.
"""

from __future__ import annotations

import sys
import types

import pytest
from test_content import build_raw_pdf
from test_text import build_text_pdf

import pyropdf

_RESOURCES = "/Resources << /Font << /F1 3 0 R >> >>"


def _table_pdf(stream: str) -> bytes:
    """Build a 340 by 300 page that paints ``stream`` with Helvetica as /F1."""
    return build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 340 300] {_RESOURCES} >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
        5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    })  # fmt: skip


_CELLS = (
    "BT /F1 12 Tf 50 235 Td (Name) Tj ET\n"
    "BT /F1 12 Tf 180 235 Td (Value) Tj ET\n"
    "BT /F1 12 Tf 50 195 Td (Alpha) Tj ET\n"
    "BT /F1 12 Tf 180 195 Td (42) Tj ET"
)


def _bordered_table_pdf() -> bytes:
    """Build a two-by-two table from stroked rules and positioned text."""
    return _table_pdf(
        "q 0 G 1 w\n"
        "40 260 m 300 260 l\n"
        "40 220 m 300 220 l\n"
        "40 180 m 300 180 l\n"
        "40 180 m 40 260 l\n"
        "170 180 m 170 260 l\n"
        "300 180 m 300 260 l\n"
        f"S Q\n{_CELLS}"
    )


def _filled_rule_table_pdf() -> bytes:
    """Build the same grid from thin filled rectangles."""
    return _table_pdf(
        "q 0 g\n"
        "40 179 260 2 re f\n"
        "40 219 260 2 re f\n"
        "40 259 260 2 re f\n"
        "39 180 2 80 re f\n"
        "169 180 2 80 re f\n"
        "299 180 2 80 re f\n"
        f"Q\n{_CELLS}"
    )


def _borderless_table_pdf(*, rows: int = 3) -> bytes:
    """Build aligned, independently positioned text cells without borders."""
    values = [("Name", "Value"), ("Alpha", "42"), ("Beta", "7")][:rows]
    return _table_pdf(
        "\n".join(
            f"BT /F1 12 Tf 40 {240 - row * 30} Td ({left}) Tj ET\nBT /F1 12 Tf 180 {240 - row * 30} Td ({right}) Tj ET"
            for row, (left, right) in enumerate(values)
        )
    )


def _sparse_row_grid_pdf() -> bytes:
    """Build a grid whose data rows omit every internal horizontal rule."""
    rules = (
        "q 0 G 1 w\n"
        "40 260 m 300 260 l\n"
        "40 230 m 300 230 l\n"
        "40 130 m 300 130 l\n"
        "40 130 m 40 260 l\n"
        "140 130 m 140 260 l\n"
        "240 130 m 240 260 l\n"
        "300 130 m 300 260 l\n"
        "S Q"
    )
    cells = [("Name", "Qty", "Total")] + [(f"Item{row}", str(row), str(row * 10)) for row in range(1, 6)]
    text = "\n".join(
        " ".join(
            f"BT /F1 12 Tf {x} {baseline} Td ({value}) Tj ET" for x, value in zip((50, 150, 250), values, strict=True)
        )
        for baseline, values in zip((240, 215, 196, 177, 158, 139), cells, strict=True)
    )
    return _table_pdf(f"{rules}\n{text}")


def _multiline_merged_header_pdf() -> bytes:
    """Build a merged header whose dense lines occupy different columns."""
    return _table_pdf(
        "q 0 G 1 w\n"
        "40 260 m 300 260 l\n"
        "40 200 m 300 200 l\n"
        "40 160 m 300 160 l\n"
        "40 120 m 300 120 l\n"
        "40 120 m 40 260 l\n"
        "300 120 m 300 260 l\n"
        "140 120 m 140 200 l\n"
        "240 120 m 240 200 l\n"
        "S Q\n"
        "BT /F1 10 Tf 50 248 Td (First) Tj ET\n"
        "BT /F1 10 Tf 150 248 Td (summary) Tj ET\n"
        "BT /F1 10 Tf 50 228 Td (Second) Tj ET\n"
        "BT /F1 10 Tf 250 228 Td (summary) Tj ET\n"
        "BT /F1 10 Tf 150 208 Td (Third) Tj ET\n"
        "BT /F1 10 Tf 250 208 Td (summary) Tj ET\n"
        "BT /F1 12 Tf 50 175 Td (Alpha) Tj ET\n"
        "BT /F1 12 Tf 150 175 Td (1) Tj ET\n"
        "BT /F1 12 Tf 250 175 Td (10) Tj ET\n"
        "BT /F1 12 Tf 50 135 Td (Beta) Tj ET\n"
        "BT /F1 12 Tf 150 135 Td (2) Tj ET\n"
        "BT /F1 12 Tf 250 135 Td (20) Tj ET"
    )


_HEBREW_CMAP = """/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
13 beginbfchar
<0001> <05DD>
<0002> <05DC>
<0003> <05D5>
<0004> <05E2>
<0005> <0020>
<0006> <05E9>
<0007> <05B0>
<0031> <0031>
<0032> <0032>
<0033> <0033>
<0044> <0044>
<0046> <0046>
<0050> <0050>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""


def build_visual_rtl_pdf(stream: str) -> bytes:
    """Build Hebrew in visual order with an Identity-H ToUnicode CMap as /F1, and Helvetica as /F2."""
    return build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 340 300]"
        " /Resources << /Font << /F1 3 0 R /F2 9 0 R >> >> >>",
        3: "<< /Type /Font /Subtype /Type0 /BaseFont /Arial /Encoding /Identity-H"
        " /DescendantFonts [6 0 R] /ToUnicode 8 0 R >>",
        4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
        5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        6: "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Arial"
        " /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
        " /FontDescriptor 7 0 R /DW 1000 /CIDToGIDMap /Identity >>",
        7: "<< /Type /FontDescriptor /FontName /Arial /Flags 4 /FontBBox [0 -200 1000 900]"
        " /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>",
        8: f"<< /Length {len(_HEBREW_CMAP)} >>\nstream\n{_HEBREW_CMAP}\nendstream",
        9: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    })  # fmt: skip


def test_bordered_table() -> None:
    page = pyropdf.open(_bordered_table_pdf()).pages[0]

    tables = page.tables()

    assert isinstance(tables, list)
    assert len(tables) == 1
    table = tables[0]
    assert isinstance(table, pyropdf.Table)
    assert table.bbox == pytest.approx(pyropdf.Rect(40, 40, 300, 120))
    assert table.rows == (("Name", "Value"), ("Alpha", "42"))
    assert table.cells == (
        (pytest.approx(pyropdf.Rect(40, 40, 170, 80)), pytest.approx(pyropdf.Rect(170, 40, 300, 80))),
        (pytest.approx(pyropdf.Rect(40, 80, 170, 120)), pytest.approx(pyropdf.Rect(170, 80, 300, 120))),
    )
    assert {type(cell) for row in table.cells for cell in row} == {pyropdf.Rect}
    assert table.diagnostics == pyropdf.TableDiagnostics(
        strategy="lines",
        confidence=1.0,
        alignment_error_em=None,
        minimum_gutter_em=None,
        row_gap_variation_em=None,
    )
    assert page.tables() == tables
    assert len({table, page.tables()[0]}) == 1


@pytest.mark.parametrize(
    ("hebrew_visual", "expected"),
    [
        ("0001000200030004000500010003000200060007", "שְלום עולם"),
        ("0001000200030004000500310032003300050001000300020006", "שלום 123 עולם"),
    ],
)
def test_bordered_table_restores_rtl_cell_text(hebrew_visual: str, expected: str) -> None:
    """Keep table materialization consistent with logical page extraction."""
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
    page = pyropdf.open(build_visual_rtl_pdf(stream)).pages[0]

    assert page.tables()[0].rows == (("Name", "Value"), (expected, "42"))


@pytest.mark.parametrize(
    ("hebrew_visual", "expected"),
    [
        ("0001000200030004000500010003000200060007", "שְלום עולם"),
        ("0001000200030004000500500044004600050001000300020006", "שלום PDF עולם"),
    ],
)
def test_borderless_table_restores_rtl_cell_text(hebrew_visual: str, expected: str) -> None:
    """Apply the same logical ordering to accepted text-table segments."""
    rows = "\n".join(
        (
            f"BT /F1 10 Tf 40 {240 - row * 30} Td <{hebrew_visual}> Tj ET\n"
            f"BT /F2 12 Tf 200 {240 - row * 30} Td ({row + 1}) Tj ET"
        )
        for row in range(3)
    )
    page = pyropdf.open(build_visual_rtl_pdf(rows)).pages[0]

    assert page.tables(strategy="text")[0].rows == ((expected, "1"), (expected, "2"), (expected, "3"))


def test_tables_clip_is_conservative_and_uses_display_coordinates() -> None:
    """Return complete tables inside a region without synthesizing partial grids."""
    document = pyropdf.open(_bordered_table_pdf())
    page = document.pages[0]
    page.rotation = 90

    table_bbox = pyropdf.Rect(180, 40, 260, 300)
    tables = page.tables(clip=table_bbox)

    assert len(tables) == 1
    assert tables[0].bbox == pytest.approx(table_bbox)
    assert page.tables(clip=(180, 40, 240, 300)) == []
    assert page.tables(clip=(0, 0, 100, 100)) == []


@pytest.mark.parametrize("clip", [(0, 0, 0, 10), (0, 0, float("inf"), 10), (0, 0, 10)])
def test_tables_rejects_invalid_clip(clip: tuple[float, ...]) -> None:
    page = pyropdf.open(_bordered_table_pdf()).pages[0]
    with pytest.raises(ValueError, match="clip"):
        page.tables(clip=clip)


def test_tables_returns_empty_for_plain_text() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 24 Tf 72 720 Td (Not a table) Tj ET")).pages[0]
    assert page.tables() == []


def test_table_cache_is_invalidated_after_page_insertion() -> None:
    document = pyropdf.open(_bordered_table_pdf())
    assert len(document.pages[0].tables()) == 1

    document.pages.new(index=0)

    assert document.pages[0].tables() == []
    assert len(document.pages[1].tables()) == 1


def test_rectangular_merged_cell_is_reconstructed() -> None:
    """Represent a missing internal divider as a merged header cell."""
    pdf = _bordered_table_pdf().replace(b"170 180 m 170 260 l", b"170 180 m 170 220 l")
    table = pyropdf.open(pdf).pages[0].tables()[0]

    assert table.cells[0][0] == pytest.approx(pyropdf.Rect(40, 40, 300, 80))
    assert table.cells[0][1] is None
    assert table.rows == (("Name Value", None), ("Alpha", "42"))


def test_broken_outer_grid_is_not_reported_as_a_table() -> None:
    """Reject a missing exterior border instead of inventing a table."""
    pdf = _bordered_table_pdf().replace(b"40 180 m 40 260 l", b"40 180 m 40 220 l")
    assert pyropdf.open(pdf).pages[0].tables() == []


def test_row_spanning_cell_is_reconstructed() -> None:
    """Represent a missing internal horizontal divider as a row span."""
    pdf = _bordered_table_pdf().replace(b"40 220 m 300 220 l", b"80 220 m 300 220 l")
    table = pyropdf.open(pdf).pages[0].tables()[0]

    assert table.cells[0][0] == pytest.approx(pyropdf.Rect(40, 40, 170, 120))
    assert table.cells[1][0] is None
    assert table.rows == (("Name\nAlpha", "Value"), (None, "42"))


def test_table_with_filled_rectangle_rules() -> None:
    """Recognize generators that paint narrow rectangles instead of strokes."""
    table = pyropdf.open(_filled_rule_table_pdf()).pages[0].tables()[0]

    assert table.bbox == pytest.approx(pyropdf.Rect(40, 40, 300, 120))
    assert table.rows == (("Name", "Value"), ("Alpha", "42"))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_sparse_row_grid_infers_repeated_records(rotation: int) -> None:
    """Refine coarse row spans in every page rotation."""
    document = pyropdf.open(_sparse_row_grid_pdf())
    document.pages[0].rotation = rotation

    table = document.pages[0].tables()[0]

    physical_shape = (6, 3) if rotation in (0, 180) else (3, 6)
    assert (len(table.rows), len(table.rows[0])) == physical_shape
    assert table.diagnostics.confidence == 0.95
    cells = {cell for row in table.rows for cell in row}
    assert {"Name", "Qty", "Total", "Item1", "Item5", "50"} <= cells
    assert sum(cell == "Item3" for row in table.rows for cell in row) == 1


def test_sparse_row_inference_preserves_multiline_merged_header() -> None:
    """Reject dense multiline text when adjacent slot signatures disagree."""
    table = pyropdf.open(_multiline_merged_header_pdf()).pages[0].tables()[0]

    assert (len(table.rows), len(table.rows[0])) == (3, 3)
    assert table.diagnostics.confidence == 1.0
    assert table.rows == (
        ("First summary\nSecond summary\nThird summary", None, None),
        ("Alpha", "1", "10"),
        ("Beta", "2", "20"),
    )


def test_compact_filled_decorations_are_not_table_rules() -> None:
    """Do not turn ordinary filled boxes into a grid."""
    stream = "20 20 30 30 re f\n80 80 40 40 re f"
    pdf = build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
        3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
        4: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    })  # fmt: skip
    assert pyropdf.open(pdf).pages[0].tables() == []


def test_opt_in_text_strategy_finds_borderless_table() -> None:
    """Require explicit text strategy for sustained aligned rows."""
    page = pyropdf.open(_borderless_table_pdf()).pages[0]

    assert page.tables() == []
    table = page.tables(strategy="text")[0]
    assert (len(table.rows), len(table.rows[0])) == (3, 2)
    assert table.diagnostics.strategy == "text"
    assert table.diagnostics.confidence == pytest.approx(0.9333333333333333)
    assert table.diagnostics.alignment_error_em == pytest.approx(0.0)
    assert table.diagnostics.minimum_gutter_em is not None
    assert table.diagnostics.minimum_gutter_em > 2.0
    assert table.diagnostics.row_gap_variation_em == pytest.approx(0.0)
    assert table.rows == (("Name", "Value"), ("Alpha", "42"), ("Beta", "7"))

    assert page.tables(strategy="text", clip=table.bbox) == [table]


def test_text_strategy_requires_three_rows() -> None:
    """Reject short aligned pairs that are likely ordinary page layout."""
    page = pyropdf.open(_borderless_table_pdf(rows=2)).pages[0]
    assert page.tables(strategy="text") == []


def test_text_table_confidence_tracks_alignment_evidence() -> None:
    """Lower the heuristic score when one accepted row aligns less precisely."""
    perfect_pdf = _borderless_table_pdf()
    shifted_pdf = perfect_pdf.replace(b"40 180 Td (Beta)", b"50 180 Td (Beta)")

    perfect = pyropdf.open(perfect_pdf).pages[0].tables(strategy="text")[0]
    shifted = pyropdf.open(shifted_pdf).pages[0].tables(strategy="text")[0]

    assert shifted.diagnostics.alignment_error_em is not None
    assert perfect.diagnostics.alignment_error_em is not None
    assert shifted.diagnostics.alignment_error_em > perfect.diagnostics.alignment_error_em
    assert shifted.diagnostics.confidence < perfect.diagnostics.confidence


def test_tables_rejects_unknown_strategy() -> None:
    page = pyropdf.open(_borderless_table_pdf()).pages[0]
    with pytest.raises(ValueError, match="strategy"):
        page.tables(strategy="guess")  # ty: ignore[invalid-argument-type]


def test_tables_refuse_an_oversized_grid_instead_of_omitting_it() -> None:
    # 65 by 65 slots exceeds the 4,096-cell bound, with only 132 rules.
    rules = [f"0 {i * 4} m 260 {i * 4} l {i * 4} 0 m {i * 4} 260 l" for i in range(66)]
    page = pyropdf.open(_table_pdf("\n".join([*rules, "S"]))).pages[0]

    for _ in range(2):
        with pytest.raises(pyropdf.LimitError) as caught:
            page.tables()
        assert caught.value.code == "table_cell_count"


def test_tables_and_styles_refuse_excess_rules_instead_of_truncating() -> None:
    page = pyropdf.open(_table_pdf("0 20 m 200 20 l S\n" * 4097)).pages[0]

    with pytest.raises(pyropdf.LimitError) as caught:
        page.tables()
    assert caught.value.code == "text_rule_count"
    with pytest.raises(pyropdf.LimitError) as caught:
        page.layout(styles=True)
    assert caught.value.code == "text_rule_count"
    assert page.tables(strategy="text") == []


def test_text_tables_refuse_excess_columns_instead_of_omitting_rows() -> None:
    stream = "\n".join(f"BT /F1 1 Tf {column * 10} 240 Td (x) Tj ET" for column in range(33))
    page = pyropdf.open(_table_pdf(stream)).pages[0]
    with pytest.raises(pyropdf.LimitError) as caught:
        page.tables(strategy="text")
    assert caught.value.code == "table_column_count"


def _double_rule_hybrid_table_pdf(*, gap: float = 2.0, prose_body: bool = False) -> bytes:
    """Build a bordered 2x3 header separated by ``gap`` pt from an outer-bordered body box."""
    body_top = 220.0 - gap
    rules = (
        "q 0 G 1 w\n"
        # Header box (y = 220..260 in PDF coords -> 40..80 in display coords)
        "40 260 m 300 260 l\n"
        "40 240 m 300 240 l\n"
        "40 220 m 300 220 l\n"
        "40 220 m 40 260 l\n"
        "100 220 m 100 260 l\n"
        "235 220 m 235 260 l\n"
        "300 220 m 300 260 l\n"
        # Outer-bordered body box (no internal vertical rules)
        f"40 {body_top:.2f} m 300 {body_top:.2f} l\n"
        "40 80 m 300 80 l\n"
        f"40 80 m 40 {body_top:.2f} l\n"
        f"300 80 m 300 {body_top:.2f} l\n"
        "S Q"
    )
    header = (
        "BT /F1 8 Tf 48 247 Td (ID) Tj ET\n"
        "BT /F1 8 Tf 108 247 Td (Description) Tj ET\n"
        "BT /F1 8 Tf 245 247 Td (Amount) Tj ET\n"
        "BT /F1 8 Tf 48 227 Td (Code) Tj ET\n"
        "BT /F1 8 Tf 108 227 Td (Details) Tj ET\n"
        "BT /F1 8 Tf 245 227 Td (USD) Tj ET"
    )
    if prose_body:
        body = (
            "BT /F1 8 Tf 48 202 Td (This summary box explains the totals shown above.) Tj ET\n"
            "BT /F1 8 Tf 48 190 Td (It contains ordinary narrative prose across the full width.) Tj ET\n"
            "BT /F1 8 Tf 48 178 Td (It must not be attached as tabular data rows.) Tj ET"
        )
    else:
        body = (
            # Record 1 with 2 wrapped continuation lines (line 2 overflows past the
            # x=235 header divider into whitespace while staying left of 120.50 at x=268).
            "BT /F1 7 Tf 48 204 Td (V001) Tj ET\n"
            "BT /F1 7 Tf 105 204 Td (TRAVEL EXPENSE) Tj ET\n"
            "BT /F1 7 Tf 268 204 Td (120.50) Tj ET\n"
            "BT /F1 7 Tf 105 194 Td (AIRFARE KANSAS CITY TO WASHINGTON AND) Tj ET\n"
            "BT /F1 7 Tf 105 184 Td (RETURN) Tj ET\n"
            # Record 2 with 1 wrapped continuation line
            "BT /F1 7 Tf 48 172 Td (V002) Tj ET\n"
            "BT /F1 7 Tf 105 172 Td (RAIL EXPENSE) Tj ET\n"
            "BT /F1 7 Tf 268 172 Td (85.00) Tj ET\n"
            "BT /F1 7 Tf 105 162 Td (TRAIN FARE TO NEW YORK) Tj ET\n"
            # Record 3 single-line row
            "BT /F1 7 Tf 48 150 Td (V003) Tj ET\n"
            "BT /F1 7 Tf 105 150 Td (LODGING) Tj ET\n"
            "BT /F1 7 Tf 268 150 Td (210.00) Tj ET"
        )
    return _table_pdf(f"{rules}\n{header}\n{body}")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("gap", [2.0, 0.0])
def test_double_rule_and_connected_borderless_body_with_wrapped_rows(rotation: int, gap: float) -> None:
    """Attach borderless multi-line body rows beneath a bordered header in every rotation."""
    document = pyropdf.open(_double_rule_hybrid_table_pdf(gap=gap))
    document.pages[0].rotation = rotation

    tables = document.pages[0].tables()

    assert len(tables) == 1
    table = tables[0]
    physical_shape = (5, 3) if rotation in (0, 180) else (3, 5)
    assert (len(table.rows), len(table.rows[0])) == physical_shape
    assert table.diagnostics.confidence == 0.95
    if rotation == 0:
        assert table.rows[2:] == (
            ("V001", "TRAVEL EXPENSE\nAIRFARE KANSAS CITY TO WASHINGTON AND\nRETURN", "120.50"),
            ("V002", "RAIL EXPENSE\nTRAIN FARE TO NEW YORK", "85.00"),
            ("V003", "LODGING", "210.00"),
        )
    else:
        cells = {cell for row in table.rows for cell in row if cell}
        assert {
            "V001",
            "TRAVEL EXPENSE\nAIRFARE KANSAS CITY TO WASHINGTON AND\nRETURN",
            "120.50",
            "V002",
            "RAIL EXPENSE\nTRAIN FARE TO NEW YORK",
            "85.00",
            "V003",
            "LODGING",
            "210.00",
        } <= cells


def test_double_rule_prose_box_is_not_merged_into_header() -> None:
    """Keep a prose box below a bordered header out of the detected table."""
    table = pyropdf.open(_double_rule_hybrid_table_pdf(gap=2.0, prose_body=True)).pages[0].tables()[0]

    assert (len(table.rows), len(table.rows[0])) == (2, 3)
    assert table.diagnostics.confidence == 1.0


def test_double_rule_table_enforces_text_size_and_cell_count_budgets() -> None:
    """Refuse when a double-rule body exceeds max_size or table_cell_count."""
    page = pyropdf.open(_double_rule_hybrid_table_pdf(gap=2.0)).pages[0]
    # Allow glyph collection (page text fits) while refusing the materialized
    # header + borderless body cell strings.
    page_text_bytes = len(page.text().encode())
    with pytest.raises(pyropdf.LimitError) as caught:
        page.tables(max_size=page_text_bytes + 40)
    assert caught.value.code == "text_size"

    # A 10-column bordered header above a 420-row borderless body yields
    # (1 + 420) * 10 = 4,210 cells (> 4,096 default table_cell_count) with only
    # 17 vector rules.
    header_rules = [
        "q 0 G 1 w",
        "20 500 m 320 500 l",
        "20 480 m 320 480 l",
        *[f"{20 + col * 30} 480 m {20 + col * 30} 500 l" for col in range(11)],
        # Outer-bordered body box 2 pt below header.
        "20 478 m 320 478 l",
        "20 20 m 320 20 l",
        "20 20 m 20 478 l",
        "320 20 m 320 478 l",
        "S Q",
    ]
    body_rows = [
        f"BT /F1 0.8 Tf 25 {475 - row * 1.0:.1f} Td (A{row}) Tj ET\n"
        f"BT /F1 0.8 Tf 295 {475 - row * 1.0:.1f} Td (B{row}) Tj ET"
        for row in range(420)
    ]
    oversized_stream = "\n".join([*header_rules, *body_rows])
    oversized_pdf = build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 340 520] {_RESOURCES} >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
        5: f"<< /Length {len(oversized_stream)} >>\nstream\n{oversized_stream}\nendstream",
    })  # fmt: skip
    oversized_page = pyropdf.open(oversized_pdf).pages[0]
    with pytest.raises(pyropdf.LimitError) as caught:
        oversized_page.tables()
    assert caught.value.code == "table_cell_count"


def test_senate_expenditures_double_rule_table_all_rotations() -> None:
    """Extract all 34 rows and 7 columns cleanly across every page rotation."""
    base_page = pyropdf.open("tests/assets/real_world/senate-expenditures.pdf").pages[0]
    [base] = base_page.tables()

    assert (len(base.rows), len(base.rows[0])) == (34, 7)
    assert base.diagnostics.confidence == 0.95
    rows_by_doc = {row[0]: row for row in base.rows if row[0]}
    # Three-line wrapped description whose second line overflows past the header
    # rule at x=647.46 must stay intact without leaking words into AMOUNT ($).
    assert rows_by_doc["DHAW20190004"] == (
        "DHAW20190004",
        "04/03/2019",
        "CITIBANK - TRAVEL CBA CARD",
        "03/21/2019",
        "03/24/2019",
        (
            "STAFF TRANSPORTATION\n"
            "AIRFARE FOR K FORD 3/21 WASHINGTON DC TO SAINT LOUIS, KANSAS CITY; 3/24 SAINT LOUIS TO\n"
            "WASHINGTON DC"
        ),
        "903.90",
    )
    for rotation in (0, 90, 180, 270):
        doc = pyropdf.open("tests/assets/real_world/senate-expenditures.pdf")
        doc.pages[0].rotation = rotation
        [rotated] = doc.pages[0].tables()
        expected_shape = (34, 7) if rotation in (90, 270) else (7, 34)
        assert (len(rotated.rows), len(rotated.rows[0])) == expected_shape
        cells = {cell for row in rotated.rows for cell in row if cell}
        assert "BAIN, J MATTHEW" in cells
        assert "DHAW20190004" in cells
        assert "903.90" in cells


def test_tables_read_the_current_version_within_the_text_budgets() -> None:
    document = pyropdf.open(_borderless_table_pdf())
    page = document.pages[0]
    assert page.tables() == []

    page.obj["Contents"].write(_bordered_table_pdf().split(b"stream\n")[1].split(b"\nendstream")[0])

    assert len(page.tables()) == 1
    with pytest.raises(pyropdf.LimitError) as caught:
        page.tables(max_glyphs=5)
    assert caught.value.code == "text_glyph_count"
    del document.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.tables()


def test_explicit_line_guides_extract_borderless_and_rotated_tables() -> None:
    """Extract borderless tables using display-space explicit line guides across rotations."""
    doc = pyropdf.open(_borderless_table_pdf())
    page = doc.pages[0]
    assert page.tables() == []

    # Full explicit grid (display y = 300 - pdf_y; rows at pdf_y 240, 210, 180 -> display y 60, 90, 120).
    [full] = page.tables(horizontal_lines=[45, 75, 105, 135], vertical_lines=[35, 150, 260])
    assert full.rows == (("Name", "Value"), ("Alpha", "42"), ("Beta", "7"))
    assert full.bbox == pytest.approx(pyropdf.Rect(35, 45, 260, 135))
    assert full.header_rows == 1
    assert full.headers == ("Name", "Value")

    # Vertical guides alone derive row bounds from aligned text lines.
    [vert_only] = page.tables(vertical_lines=[35, 150, 260])
    assert vert_only.rows == (("Name", "Value"), ("Alpha", "42"), ("Beta", "7"))

    # Horizontal guides alone derive column bounds from separated text segments.
    [horiz_only] = page.tables(horizontal_lines=[45, 75, 105, 135])
    assert horiz_only.rows == (("Name", "Value"), ("Alpha", "42"), ("Beta", "7"))

    # Rotated 90 deg CW: display_x = pdf_y, display_y = pdf_x (page size 300 x 340).
    page.rotation = 90
    [rotated] = page.tables(horizontal_lines=[35, 150, 260], vertical_lines=[165, 195, 225, 255])
    assert (len(rotated.rows), len(rotated.rows[0])) == (2, 3)
    assert rotated.rows == (("Beta", "Alpha", "Name"), ("7", "42", "Value"))


def test_explicit_guide_augments_partially_ruled_grid() -> None:
    """Add a missing internal horizontal rule to an otherwise ruled table."""
    # Remove the middle horizontal rule at pdf y=220 (display y=80).
    pdf = _bordered_table_pdf().replace(b"40 220 m 300 220 l\n", b"")
    page = pyropdf.open(pdf).pages[0]
    assert page.tables() == []

    [table] = page.tables(horizontal_lines=[80])
    assert table.rows == (("Name", "Value"), ("Alpha", "42"))


def test_tolerance_options_tune_near_miss_rules_and_tight_gutters() -> None:
    """Tune join_tolerance, snap_tolerance, min_gutter, and min_words options."""
    # 1. Near-miss rule corners: horizontal rules end 2.5 pt short of vertical edges (42.5..297.5 vs 40..300).
    near_miss_pdf = _table_pdf(
        "q 0 G 1 w\n"
        "42.5 260 m 297.5 260 l\n"
        "42.5 220 m 297.5 220 l\n"
        "42.5 180 m 297.5 180 l\n"
        "40 180 m 40 260 l\n"
        "170 180 m 170 260 l\n"
        "300 180 m 300 260 l\n"
        f"S Q\n{_CELLS}"
    )
    near_miss_page = pyropdf.open(near_miss_pdf).pages[0]
    assert near_miss_page.tables() == []
    [joined] = near_miss_page.tables(join_tolerance=3.0)
    assert joined.rows == (("Name", "Value"), ("Alpha", "42"))

    # 2. Misaligned collinear rule segments shifted by 2.2 pt in y.
    misaligned_pdf = _table_pdf(
        "q 0 G 1 w\n"
        "40 260 m 170 260 l 170 262.2 m 300 262.2 l\n"
        "40 220 m 300 220 l\n"
        "40 180 m 300 180 l\n"
        "40 180 m 40 262.2 l\n"
        "170 180 m 170 262.2 l\n"
        "300 180 m 300 262.2 l\n"
        f"S Q\n{_CELLS}"
    )
    misaligned_page = pyropdf.open(misaligned_pdf).pages[0]
    assert misaligned_page.tables() == []
    [snapped] = misaligned_page.tables(snap_tolerance=3.0)
    assert snapped.rows == (("Name", "Value"), ("Alpha", "42"))

    # 3. Tight column gutter (~1.1 em at 12pt font: left ends near x=56, right starts at x=70 -> 14pt = 1.17 em).
    tight_stream = "\n".join(
        f"BT /F1 12 Tf 40 {240 - row * 20} Td ({left}) Tj ET\nBT /F1 12 Tf 70 {240 - row * 20} Td ({right}) Tj ET"
        for row, (left, right) in enumerate([("ID", "Val"), ("A1", "10"), ("B2", "20")])
    )
    tight_page = pyropdf.open(_table_pdf(tight_stream)).pages[0]
    assert tight_page.tables(strategy="text") == []
    [tight] = tight_page.tables(strategy="text", min_gutter=0.9)
    assert tight.rows == (("ID", "Val"), ("A1", "10"), ("B2", "20"))

    # 4. min_words_vertical and min_words_horizontal on borderless tables.
    two_row_page = pyropdf.open(_borderless_table_pdf(rows=2)).pages[0]
    assert two_row_page.tables(strategy="text") == []
    [two_row] = two_row_page.tables(strategy="text", min_words_vertical=2)
    assert two_row.rows == (("Name", "Value"), ("Alpha", "42"))

    three_row_page = pyropdf.open(_borderless_table_pdf(rows=3)).pages[0]
    assert three_row_page.tables(strategy="text", min_words_horizontal=3) == []


@pytest.mark.parametrize(
    ("kwargs", "error", "code"),
    [
        ({"horizontal_lines": [float("nan")]}, ValueError, None),
        ({"vertical_lines": [float("inf")]}, ValueError, None),
        ({"horizontal_lines": "10,20"}, TypeError, None),
        ({"vertical_lines": [True, False]}, TypeError, None),
        ({"horizontal_lines": list(range(4097))}, pyropdf.LimitError, "text_rule_count"),
        (
            {"horizontal_lines": list(range(2049)), "vertical_lines": list(range(2048))},
            pyropdf.LimitError,
            "text_rule_count",
        ),
        ({"snap_tolerance": -1.0}, ValueError, None),
        ({"snap_tolerance": float("inf")}, ValueError, None),
        ({"snap_tolerance": 1001.0}, ValueError, None),
        ({"snap_tolerance": True}, TypeError, None),
        ({"join_tolerance": -0.5}, ValueError, None),
        ({"join_tolerance": float("nan")}, ValueError, None),
        ({"min_gutter": 0.0}, ValueError, None),
        ({"min_gutter": -1.0}, ValueError, None),
        ({"min_gutter": 200.0}, ValueError, None),
        ({"min_words_vertical": 0}, ValueError, None),
        ({"min_words_vertical": True}, TypeError, None),
        ({"min_words_vertical": 4097}, pyropdf.LimitError, "table_cell_count"),
        ({"min_words_horizontal": 0}, ValueError, None),
        ({"min_words_horizontal": 33}, pyropdf.LimitError, "table_column_count"),
    ],
)
def test_tables_validates_guide_and_tolerance_bounds(
    kwargs: dict[str, object], error: type[Exception], code: str | None
) -> None:
    page = pyropdf.open(_bordered_table_pdf()).pages[0]
    with pytest.raises(error) as caught:
        page.tables(**kwargs)  # ty: ignore[invalid-argument-type]
    if code is not None:
        assert isinstance(caught.value, pyropdf.LimitError)
        assert caught.value.code == code


def test_header_detection_distinguishes_single_multirow_bold_and_headerless() -> None:
    """Detect single-row, multi-row merged, bold all-text, and headerless tables."""
    # 1. Standard single-row header via text-to-numeric transition.
    [single] = pyropdf.open(_bordered_table_pdf()).pages[0].tables()
    assert single.header_rows == 1
    assert single.headers == ("Name", "Value")

    # 2. Headerless table (numeric values in row 0 matching body rows, uniform regular font).
    headerless_pdf = _table_pdf(
        "q 0 G 1 w\n"
        "40 260 m 300 260 l 40 230 m 300 230 l 40 200 m 300 200 l 40 170 m 300 170 l\n"
        "40 170 m 40 260 l 170 170 m 170 260 l 300 170 m 300 260 l S Q\n"
        "BT /F1 12 Tf 50 242 Td (Alpha) Tj ET BT /F1 12 Tf 180 242 Td (10) Tj ET\n"
        "BT /F1 12 Tf 50 212 Td (Beta) Tj ET BT /F1 12 Tf 180 212 Td (20) Tj ET\n"
        "BT /F1 12 Tf 50 182 Td (Gamma) Tj ET BT /F1 12 Tf 180 182 Td (30) Tj ET"
    )
    [headerless] = pyropdf.open(headerless_pdf).pages[0].tables()
    assert headerless.header_rows == 0
    assert headerless.headers == ()

    # 3. Bold all-text header (no numeric columns, bold font distinguishes row 0).
    bold_stream = (
        "q 0 G 1 w\n"
        "40 260 m 300 260 l 40 230 m 300 230 l 40 200 m 300 200 l 40 170 m 300 170 l\n"
        "40 170 m 40 260 l 170 170 m 170 260 l 300 170 m 300 260 l S Q\n"
        "BT /F2 12 Tf 50 242 Td (Name) Tj ET BT /F2 12 Tf 180 242 Td (City) Tj ET\n"
        "BT /F1 12 Tf 50 212 Td (Alice) Tj ET BT /F1 12 Tf 180 212 Td (Tokyo) Tj ET\n"
        "BT /F1 12 Tf 50 182 Td (Bob) Tj ET BT /F1 12 Tf 180 182 Td (Osaka) Tj ET"
    )
    bold_pdf = build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 340 300]"
        " /Resources << /Font << /F1 3 0 R /F2 6 0 R >> >> >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
        5: f"<< /Length {len(bold_stream)} >>\nstream\n{bold_stream}\nendstream",
        6: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    })  # fmt: skip
    [bold_table] = pyropdf.open(bold_pdf).pages[0].tables()
    assert bold_table.header_rows == 1
    assert bold_table.headers == ("Name", "City")

    # 4. Multi-row merged group header:
    # Row 0: "Metric" (rowspan 2) | "2025" (colspan 2)
    # Row 1: (covered)            | "Rev" | "Cost"
    # Row 2: "Sales"              | "100" | "60"
    # Row 3: "Profit"             | "40"  | "20"
    multi_pdf = _table_pdf(
        "q 0 G 1 w\n"
        "40 260 m 300 260 l\n"
        "140 230 m 300 230 l\n"
        "40 200 m 300 200 l\n"
        "40 170 m 300 170 l\n"
        "40 140 m 300 140 l\n"
        "40 140 m 40 260 l\n"
        "140 140 m 140 260 l\n"
        "220 140 m 220 230 l\n"
        "300 140 m 300 260 l\n"
        "S Q\n"
        "BT /F1 12 Tf 50 225 Td (Metric) Tj ET\n"
        "BT /F1 12 Tf 190 242 Td (2025) Tj ET\n"
        "BT /F1 12 Tf 150 212 Td (Rev) Tj ET\n"
        "BT /F1 12 Tf 230 212 Td (Cost) Tj ET\n"
        "BT /F1 12 Tf 50 182 Td (Sales) Tj ET\n"
        "BT /F1 12 Tf 150 182 Td (100) Tj ET\n"
        "BT /F1 12 Tf 230 182 Td (60) Tj ET\n"
        "BT /F1 12 Tf 50 152 Td (Profit) Tj ET\n"
        "BT /F1 12 Tf 150 152 Td (40) Tj ET\n"
        "BT /F1 12 Tf 230 152 Td (20) Tj ET"
    )
    [multi] = pyropdf.open(multi_pdf).pages[0].tables()
    assert multi.header_rows == 2
    assert multi.headers == ("Metric", "2025 Rev", "2025 Cost")
    assert multi.to_dicts() == [
        {"Metric": "Sales", "2025 Rev": "100", "2025 Cost": "60"},
        {"Metric": "Profit", "2025 Rev": "40", "2025 Cost": "20"},
    ]

    # 5. Structural two-row merged header in real corpus table.
    [senate] = pyropdf.open("tests/assets/real_world/senate-expenditures.pdf").pages[0].tables()
    assert senate.header_rows == 2
    assert senate.headers == (
        "DOCUMENT NO.",
        "DATE POSTED",
        "PAYEE NAME",
        "OBLIGATION/SERVICE DATES START",
        "OBLIGATION/SERVICE DATES END",
        "DESCRIPTION",
        "AMOUNT ($)",
    )
    assert senate.to_dicts()[0]["PAYEE NAME"] == "BAIN, J MATTHEW"
    assert senate.to_dicts()[0]["AMOUNT ($)"] == "37,499.96"


def test_table_to_dicts_and_to_dataframe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export rows via to_dicts() and optional to_dataframe() with header & span controls."""
    [table] = pyropdf.open(_bordered_table_pdf()).pages[0].tables()
    assert table.to_dicts() == [{"Name": "Alpha", "Value": "42"}]
    assert table.to_dicts(header_rows=0) == [
        {"column_0": "Name", "column_1": "Value"},
        {"column_0": "Alpha", "column_1": "42"},
    ]
    assert table.to_dicts(header_rows=2) == []

    # Duplicate and empty headers are disambiguated deterministically.
    diag = pyropdf.TableDiagnostics(
        strategy="lines", confidence=1.0, alignment_error_em=None, minimum_gutter_em=None, row_gap_variation_em=None
    )
    rect = pyropdf.Rect(0, 0, 10, 10)
    dup_table = pyropdf.Table(
        bbox=rect,
        rows=(("Amount", "Amount", ""), ("1", "2", "3")),
        cells=((rect, rect, rect), (rect, rect, rect)),
        diagnostics=diag,
        header_rows=1,
        headers=("Amount", "Amount", ""),
    )
    assert dup_table.to_dicts() == [{"Amount": "1", "Amount_2": "2", "column_2": "3"}]

    # Merged cell export honors fill_empty.
    rowspan_pdf = _bordered_table_pdf().replace(b"40 220 m 300 220 l", b"80 220 m 300 220 l")
    [rowspan] = pyropdf.open(rowspan_pdf).pages[0].tables()
    assert rowspan.to_dicts(header_rows=0, fill_empty=False) == [
        {"column_0": "Name\nAlpha", "column_1": "Value"},
        {"column_0": None, "column_1": "42"},
    ]
    assert rowspan.to_dicts(header_rows=0, fill_empty=True) == [
        {"column_0": "Name\nAlpha", "column_1": "Value"},
        {"column_0": "Name\nAlpha", "column_1": "42"},
    ]

    # Validation errors on export helpers.
    with pytest.raises(TypeError, match="header_rows"):
        table.to_dicts(header_rows=True)
    with pytest.raises(ValueError, match="header_rows"):
        table.to_dicts(header_rows=5)
    with pytest.raises(TypeError, match="fill_empty"):
        table.to_dicts(fill_empty=1)  # ty: ignore[invalid-argument-type]

    # to_dataframe() raises ImportError when pandas is absent.
    monkeypatch.setitem(sys.modules, "pandas", None)
    with pytest.raises(ImportError, match="pandas"):
        table.to_dataframe()

    # to_dataframe() delegates to pandas.DataFrame when pandas is installed.
    captured: dict[str, object] = {}

    class _FakeDataFrame:
        def __init__(self, data: object, columns: object) -> None:
            captured["data"] = data
            captured["columns"] = columns

    fake_pandas = types.ModuleType("pandas")
    fake_pandas.DataFrame = _FakeDataFrame  # ty: ignore[missing-attribute]
    monkeypatch.setitem(sys.modules, "pandas", fake_pandas)

    df = table.to_dataframe()
    assert isinstance(df, _FakeDataFrame)
    assert captured == {"data": [["Alpha", "42"]], "columns": ["Name", "Value"]}
    assert isinstance(table, pyropdf.Table)
    with pytest.raises(AttributeError):
        table.header_rows = 2  # ty: ignore[invalid-assignment]
