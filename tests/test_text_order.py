"""The reading order of page.text(): rotated, vertical, right-to-left, and column lines.

Ported from pylopdf's tests/test_layout.py at
036be111cafe77755db4bb2be17bc70a8f2f9d82, lines 424 to 795. pyropdf has no
words, search, layout dictionary, or Markdown yet, so these tests read
``page.text()`` and ``doc.text()`` only.
"""

from __future__ import annotations

import pytest
from test_content import build_raw_pdf

import pyropdf

_RTL_CMAP = """/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
21 beginbfchar
<0001> <05DD>
<0002> <05DC>
<0003> <05D5>
<0004> <05E2>
<0005> <0020>
<0006> <05E9>
<0007> <05B0>
<0010> <0645>
<0011> <0644>
<0012> <0627>
<0013> <0639>
<0014> <0628>
<0015> <062D>
<0016> <0631>
<002D> <002D>
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

_MINCHO = (
    "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MS-Mincho "
    "/CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >> /FontDescriptor 7 0 R /DW 1000 >>"
)
_MINCHO_DESCRIPTOR = (
    "<< /Type /FontDescriptor /FontName /MS-Mincho /Flags 6 /FontBBox [0 -137 1000 859] "
    "/ItalicAngle 0 /Ascent 859 /Descent -140 /CapHeight 769 /StemV 78 >>"
)


def _octal(text: str) -> str:
    """Return ``text`` as Shift-JIS bytes in a literal string's octal escapes."""
    return "".join(f"\\{byte:03o}" for byte in text.encode("cp932"))


def _simple_pdf(stream: str, *, width: int, height: int, font: str = "Helvetica") -> bytes:
    """Build one page that draws ``stream`` with a standard font as /F1."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                f"<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 {width} {height}] "
                "/Resources << /Font << /F1 3 0 R >> >> >>"
            ),
            3: f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} >>",
            4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
            5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        }
    )


def build_nonembedded_cjk_pdf(text: str = "こんにちは日本語", *, encoding: str = "90ms-RKSJ-H") -> bytes:
    """Build one page that draws ``text`` with a non-embedded MS-Mincho."""
    stream = f"BT /F1 24 Tf 72 720 Td ({_octal(text)}) Tj ET"
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R >> >> >>"
            ),
            3: f"<< /Type /Font /Subtype /Type0 /BaseFont /MS-Mincho /Encoding /{encoding} /DescendantFonts [6 0 R] >>",
            4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
            5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            6: _MINCHO,
            7: _MINCHO_DESCRIPTOR,
        }
    )


def build_vertical_cjk_columns_pdf() -> bytes:
    """Build two Japanese vertical columns between a horizontal heading and footer."""
    stream = (
        "BT /F2 18 Tf 40 750 Td (Heading) Tj ET\n"
        f"BT /F1 24 Tf 200 720 Td ({_octal('右側列')}) Tj ET\n"
        f"BT /F1 24 Tf 100 720 Td ({_octal('左側列')}) Tj ET\n"
        "BT /F2 18 Tf 40 20 Td (Footer) Tj ET"
    )
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R /F2 8 0 R >> >> >>"
            ),
            3: "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Mincho /Encoding /90ms-RKSJ-V /DescendantFonts [6 0 R] >>",
            4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
            5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            6: _MINCHO,
            7: _MINCHO_DESCRIPTOR,
            8: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        }
    )


def build_visual_rtl_pdf(stream: str, *, width: int = 340, height: int = 300) -> bytes:
    """Build Hebrew and Arabic drawn in visual order, with an Identity-H ToUnicode CMap."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                f"<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 {width} {height}] "
                "/Resources << /Font << /F1 3 0 R /F2 9 0 R >> >> >>"
            ),
            3: (
                "<< /Type /Font /Subtype /Type0 /BaseFont /Arial /Encoding /Identity-H "
                "/DescendantFonts [6 0 R] /ToUnicode 8 0 R >>"
            ),
            4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
            5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            6: (
                "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Arial "
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                "/FontDescriptor 7 0 R /DW 1000 /CIDToGIDMap /Identity >>"
            ),
            7: (
                "<< /Type /FontDescriptor /FontName /Arial /Flags 4 /FontBBox [0 -200 1000 900] "
                "/ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>"
            ),
            8: f"<< /Length {len(_RTL_CMAP)} >>\nstream\n{_RTL_CMAP}\nendstream",
            9: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        }
    )


def _multicolumn_pdf() -> bytes:
    """Build a heading, two sustained columns, and a footer."""
    stream = (
        "BT /F1 18 Tf 20 270 Td (A heading spanning both columns) Tj ET\n"
        "BT /F1 12 Tf 40 230 Td (Left one) Tj ET\n"
        "BT /F1 12 Tf 40 210 Td (Left two) Tj ET\n"
        "BT /F1 12 Tf 200 230 Td (Right one) Tj ET\n"
        "BT /F1 12 Tf 200 210 Td (Right two) Tj ET\n"
        "BT /F1 18 Tf 20 30 Td (A footer spanning both columns) Tj ET"
    )
    return _simple_pdf(stream, width=360, height=300)


def test_a_rotated_baseline_reads_as_one_line() -> None:
    page = pyropdf.open(_simple_pdf("BT /F1 24 Tf 0 1 -1 0 100 100 Tm (Vertical) Tj ET", width=300, height=300)).pages[
        0
    ]

    assert page.text() == "Vertical\n"


def test_vertical_cjk_reads_as_one_line() -> None:
    page = pyropdf.open(build_nonembedded_cjk_pdf(encoding="90ms-RKSJ-V")).pages[0]

    assert page.text() == "こんにちは日本語\n"


def test_horizontal_cjk_reads_as_one_line() -> None:
    page = pyropdf.open(build_nonembedded_cjk_pdf()).pages[0]

    assert page.text() == "こんにちは日本語\n"


def test_rtl_lines_read_in_logical_order() -> None:
    hebrew_visual = "0001000200030004000500010003000200060007"
    arabic_visual = "0010001100120013001100120014000500120014001500160010"
    embedded_number_visual = "0001000200030004000500310032003300050001000300020006"
    embedded_latin_visual = "0001000200030004000500500044004600050001000300020006"
    embedded_separator_visual = "0001000200030004000500310032002D0033003100050001000300020006"
    embedded_descending_visual = "0001000200030004000500330032003100050001000300020006"
    ambiguous_visual = "00010002000300040005005000440046000500310032003300050001000300020006"
    stream = (
        f"BT /F1 20 Tf 40 330 Td <{hebrew_visual}> Tj ET\n"
        f"BT /F1 20 Tf 40 285 Td <{arabic_visual}> Tj ET\n"
        f"BT /F1 20 Tf 40 240 Td <{embedded_number_visual}> Tj ET\n"
        f"BT /F1 20 Tf 40 195 Td <{embedded_latin_visual}> Tj ET\n"
        f"BT /F1 20 Tf 40 150 Td <{embedded_separator_visual}> Tj ET\n"
        f"BT /F1 20 Tf 40 105 Td <{embedded_descending_visual}> Tj ET\n"
        f"BT /F1 20 Tf 40 60 Td <{ambiguous_visual}> Tj ET"
    )
    document = pyropdf.open(build_visual_rtl_pdf(stream, width=420, height=360))
    expected = [
        "שְלום עולם",
        "مرحبا بالعالم",
        "שלום 123 עולם",
        "שלום PDF עולם",
        "שלום 12-31 עולם",
        "שלום 321 עולם",
        # A line with two forward tokens is ambiguous and stays in drawing order.
        "םלוע PDF 123 םולש",
    ]

    assert document.pages[0].text().splitlines() == expected
    assert document.text() == "".join(f"{line}\n" for line in expected)


def test_an_rtl_edge_token_and_an_ltr_base_line_stay_in_drawing_order() -> None:
    ltr_base = "00500044004600050001000300020006"
    rtl_edge = "00010002000300040005005000440046"
    stream = f"BT /F1 20 Tf 40 200 Td <{ltr_base}> Tj ET\nBT /F1 20 Tf 40 140 Td <{rtl_edge}> Tj ET"
    page = pyropdf.open(build_visual_rtl_pdf(stream)).pages[0]

    assert page.text().splitlines() == ["PDF םולש", "םלוע PDF"]


def test_vertical_cjk_inference_stops_at_its_candidate_bound() -> None:
    text = "日" * 4097
    page = pyropdf.open(build_nonembedded_cjk_pdf(text)).pages[0]

    # The run leaves the page, so the glyphs it hides are asked for.
    assert page.text(clipped=True).rstrip("\n") == text


def test_vertical_cjk_columns_read_right_to_left_between_page_furniture() -> None:
    page = pyropdf.open(build_vertical_cjk_columns_pdf()).pages[0]

    assert page.text().splitlines() == ["Heading", "右側列", "左側列", "Footer"]


def test_sustained_columns_read_top_to_bottom_before_left_to_right() -> None:
    page = pyropdf.open(_multicolumn_pdf()).pages[0]

    assert page.text().splitlines() == [
        "A heading spanning both columns",
        "Left one",
        "Left two",
        "Right one",
        "Right two",
        "A footer spanning both columns",
    ]


def test_a_repeated_narrow_gutter_separates_columns() -> None:
    rows = [(f"Left column row {row}", f"Right column row {row}") for row in range(1, 5)]
    stream = "BT /F1 14 Tf 20 270 Td (A heading spanning both columns) Tj ET\n"
    for offset, (left, right) in enumerate(rows):
        y = 230 - offset * 20
        stream += f"BT /F1 12 Tf 20 {y} Td ({left}) Tj ET\nBT /F1 12 Tf 148 {y} Td ({right}) Tj ET\n"
    stream += "BT /F1 14 Tf 20 30 Td (A footer spanning both columns) Tj ET"
    page = pyropdf.open(_simple_pdf(stream, width=300, height=300, font="Courier")).pages[0]

    assert page.text().splitlines() == [
        "A heading spanning both columns",
        *(left for left, _ in rows),
        *(right for _, right in rows),
        "A footer spanning both columns",
    ]


def test_an_established_narrow_gutter_follows_a_small_drift() -> None:
    rows = [(f"Left column row {row}", f"Right column row {row}", 148 if row <= 4 else 144) for row in range(1, 7)]
    stream = ""
    for offset, (left, right, right_x) in enumerate(rows):
        y = 250 - offset * 20
        stream += f"BT /F1 12 Tf 20 {y} Td ({left}) Tj ET\nBT /F1 12 Tf {right_x} {y} Td ({right}) Tj ET\n"
    page = pyropdf.open(_simple_pdf(stream, width=300, height=300, font="Courier")).pages[0]

    assert page.text().splitlines() == [*(left for left, _, _ in rows), *(right for _, right, _ in rows)]


def test_rows_with_several_separators_stay_row_major() -> None:
    rows = [(f"First field 000{row}", f"Second field 000{row}", f"Third field 000{row}") for row in range(1, 5)]
    stream = ""
    for offset, row in enumerate(rows):
        y = 170 - offset * 20
        for x, text in zip((10, 73, 136), row, strict=True):
            stream += f"BT /F1 6 Tf {x} {y} Td ({text}) Tj ET\n"
    page = pyropdf.open(_simple_pdf(stream, width=200, height=200, font="Courier")).pages[0]

    assert page.text().splitlines() == [" ".join(row) for row in rows]


def test_table_rows_with_a_wide_gap_stay_row_major() -> None:
    labels = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"]
    rows = [(label, *(f"{row}{column}" for column in range(1, 5))) for row, label in enumerate(labels, start=1)]
    stream = ""
    for offset, row in enumerate(rows):
        y = 250 - offset * 20
        for x, text in zip((20, 120, 160, 200, 240), row, strict=True):
            stream += f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET\n"
    page = pyropdf.open(_simple_pdf(stream, width=300, height=300, font="Courier")).pages[0]

    assert page.text().splitlines() == [" ".join(row) for row in rows]


def test_an_isolated_wide_gap_stays_on_one_line() -> None:
    stream = "BT /F1 12 Tf 40 260 Td (Header) Tj ET\nBT /F1 12 Tf 300 260 Td (1) Tj ET"
    page = pyropdf.open(_simple_pdf(stream, width=360, height=300)).pages[0]

    assert page.text() == "Header 1\n"


def test_ordering_stays_within_the_glyph_budget_and_a_refusal_keeps_nothing() -> None:
    # Ordering grows its lines, segments, and columns from the collected
    # glyphs only, so the glyph budget of the page bounds them.
    page = pyropdf.open(_multicolumn_pdf()).pages[0]
    text = page.text()
    # Each line is one string drawn with its spaces, so a line's glyphs are its characters.
    glyphs = sum(len(line) for line in text.splitlines())

    assert page.text(max_glyphs=glyphs) == text
    with pytest.raises(pyropdf.LimitError) as error:
        page.text(max_glyphs=glyphs - 1)
    assert error.value.code == "text_glyph_count"
    with pytest.raises(pyropdf.LimitError) as error:
        page.text(max_size=len(text.encode()) - 1)
    assert error.value.code == "text_size"
    assert page.text() == text
