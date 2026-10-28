"""Tables from vector rules and aligned text with ``Page.tables``.

Ported from pylopdf 036be111 ``tests/test_tables.py``. The differences:

- The Markdown tests and assertions are in ``tests/test_markdown.py``.
- pylopdf inserts a page from another document; these tests add a blank
  page with ``pages.new``.
- ``Page.tables`` also takes the ``max_glyphs`` and ``max_size`` budgets of
  the other text views, and a deleted page refuses it.
"""

from __future__ import annotations

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
