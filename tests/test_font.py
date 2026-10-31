"""Fonts: the faces that draw text, and the measurement of one line and of wrapped text.

Ported from pylopdf's tests/test_font.py at ebb01d3c, the Standard 14
cases (issue 467). Intended differences:

- pyropdf writes ``/BaseFont /Name`` with a space, so the tests match the
  name with a pattern;
- the cases of OpenType faces, ``Font.from_file``, ``Font.from_bytes``,
  ``Font.installed``, and variable instances wait for embedded fonts
  (issue 153);
- ``glyph_name`` and ``glyph_text`` are not ported here: they name glyphs
  by the Adobe Glyph List, which no Standard 14 drawing needs.
"""

from __future__ import annotations

import math
import re
from itertools import pairwise
from typing import TYPE_CHECKING, Any, cast, get_args

import pytest

import pyropdf

if TYPE_CHECKING:
    from pyropdf import StandardFontName


def _new_page(width: float = 400, height: float = 200) -> tuple[pyropdf.Document, pyropdf.Page]:
    doc = pyropdf.Document()
    return doc, doc.pages.new((width, height))


def _end(glyph: pyropdf.Glyph) -> float:
    assert glyph.advance is not None
    return glyph.origin.x + glyph.advance


def _baselines(page: pyropdf.Page) -> list[float]:
    return [run.glyphs[0].origin.y for run in page.content().text_runs]


def _base_font(data: bytes, name: str) -> bool:
    return re.search(rb"/BaseFont\s*/" + re.escape(name.encode()) + rb"[\s/>]", data) is not None


@pytest.mark.parametrize(
    ("name", "family", "flags"),
    [
        ("Helvetica", "Helvetica", (False, False, False)),
        ("Helvetica-Bold", "Helvetica", (True, False, False)),
        ("Helvetica-Oblique", "Helvetica", (False, True, False)),
        ("Times-Roman", "Times", (False, False, True)),
        ("Times-BoldItalic", "Times", (True, True, True)),
        ("Courier", "Courier", (False, False, False)),
        ("Symbol", "Symbol", (False, False, False)),
        ("ZapfDingbats", "ZapfDingbats", (False, False, False)),
    ],
)
def test_standard_14_fonts_by_postscript_name(
    name: StandardFontName,
    family: str,
    flags: tuple[bool, bool, bool],
) -> None:
    font = pyropdf.Font.standard(name)

    assert (font.name, font.family) == (name, family)
    assert (font.is_bold, font.is_italic, font.is_serif) == flags
    assert font.weight == (700 if flags[0] else 400)
    assert font.units_per_em == 1000
    assert font.ascender > 0 > font.descender


def test_standard_font_names_are_the_14_postscript_names() -> None:
    assert get_args(pyropdf.StandardFontName) == (
        "Courier",
        "Courier-Oblique",
        "Courier-Bold",
        "Courier-BoldOblique",
        "Helvetica",
        "Helvetica-Oblique",
        "Helvetica-Bold",
        "Helvetica-BoldOblique",
        "Times-Roman",
        "Times-Italic",
        "Times-Bold",
        "Times-BoldItalic",
        "Symbol",
        "ZapfDingbats",
    )
    for name in get_args(pyropdf.StandardFontName):
        assert pyropdf.Font.standard(name).name == name


def test_standard_takes_only_the_14_postscript_names() -> None:
    for name in ("helv", "helvetica", "Arial", "cjk", ""):
        with pytest.raises(ValueError, match="PostScript name of a Standard 14 font"):
            pyropdf.Font.standard(name)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="name"):
        pyropdf.Font.standard(None)  # ty: ignore[invalid-argument-type]


def test_standard_14_fonts_measure_and_draw_with_afm_widths() -> None:
    helvetica = pyropdf.Font.standard("Helvetica")
    bold = pyropdf.Font.standard("Helvetica-Bold")
    # Adobe AFM widths: H 722, e 556, l 222, o 556, space 278, W 944, r 333, d 556.
    assert helvetica.text_width("Hello World", 11) == pytest.approx(56.837)
    assert helvetica.has_glyph("A")
    assert helvetica.has_glyph("é")
    assert not helvetica.has_glyph("あ")
    with pytest.raises(pyropdf.PdfError, match="WinAnsi"):
        helvetica.text_width("あ", 11)

    doc, page = _new_page()
    with page.draw() as canvas:
        canvas.text("Hello", (10, 40), font=bold, size=14)
        with pytest.raises(ValueError, match="WinAnsi"):
            canvas.text("日本語", (10, 80), font=helvetica)
    assert _base_font(doc.to_bytes(compress=False), "Helvetica-Bold")
    (run,) = page.content().text_runs
    assert _end(run.glyphs[-1]) == pytest.approx(10 + bold.text_width("Hello", 14), abs=1e-2)


def test_one_font_draws_on_several_documents() -> None:
    font = pyropdf.Font.standard("Times-Roman")
    for _ in range(2):
        _doc, page = _new_page()
        with page.draw() as canvas:
            canvas.text("Shared", (10, 40), font=font)
        assert page.text() == "Shared\n"


def test_text_width_rejects_line_breaks_missing_glyphs_and_bad_input() -> None:
    font = pyropdf.Font.standard("Helvetica")
    with pytest.raises(pyropdf.PdfError, match="one line"):
        font.text_width("a\nb", 10)
    with pytest.raises(pyropdf.PdfError, match=r"U\+1F980"):
        font.text_width("🦀", 10)
    for size in (0, -1, math.nan, math.inf):
        with pytest.raises(ValueError, match="size"):
            font.text_width("a", size)
    with pytest.raises(TypeError, match="size"):
        font.text_width("a", "10")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="text"):
        font.text_width(b"a", 10)  # ty: ignore[invalid-argument-type]
    with pytest.raises(pyropdf.LimitError) as caught:
        font.text_width("aa", 10, max_text_size=1)
    assert caught.value.code == "text_input_size"


def test_has_glyph_takes_one_character() -> None:
    font = pyropdf.Font.standard("Helvetica")
    assert font.has_glyph("A") is True
    assert font.has_glyph("🦀") is False
    for value in ("ab", ""):
        with pytest.raises(ValueError, match="one-character"):
            font.has_glyph(value)
    for value in (65, None, b"A"):
        with pytest.raises(TypeError, match="one-character"):
            font.has_glyph(value)  # ty: ignore[invalid-argument-type]


def test_font_has_no_public_constructor() -> None:
    for args, kwargs in (((), {}), (("helv",), {}), ((), {"fontfile": "x.otf"})):
        with pytest.raises(TypeError, match=r"Font has no public constructor: Font\.standard\(name\)"):
            pyropdf.Font(*args, **kwargs)
    with pytest.raises(AttributeError, match="text_length"):
        cast("Any", pyropdf.Font.standard("Helvetica")).text_length("x", 11)


@pytest.mark.parametrize("name", ["Times-Roman", "Helvetica"])
def test_text_height_is_the_height_that_a_textbox_needs(name: StandardFontName) -> None:
    font = pyropdf.Font.standard(name)
    text = "A paragraph that wraps onto several lines in a narrow box. " * 3
    size, width, x, top = 10.5, 150.0, 30.0, 40.25

    height = font.text_height(text, size, width=width)
    _doc, page = _new_page(400, 400)
    with page.draw() as canvas:
        exact = canvas.textbox((x, top, x + width, top + height), text, font=font, size=size)
        short = canvas.textbox((x, 300, x + width, 300 + height - 0.5), text, font=font, size=size)

    assert exact == pyropdf.TextFit(spare_height=0)
    assert short.spare_height == pytest.approx(-0.5)
    # Lines lie one line height apart, the font's ascender minus its descender.
    line_height = (font.ascender - font.descender) * size
    baselines = _baselines(page)
    assert len(baselines) > 2
    assert height == pytest.approx(len(baselines) * line_height)
    assert baselines[0] == pytest.approx(top + font.ascender * size, abs=1e-3)
    gaps = [later - earlier for earlier, later in pairwise(baselines)]
    assert gaps == pytest.approx([line_height] * len(gaps), abs=1e-3)
    # The descender of the last line ends at the bottom of the box.
    assert baselines[-1] - font.descender * size == pytest.approx(top + height, abs=1e-3)


def test_text_height_follows_line_height_and_line_breaks() -> None:
    font = pyropdf.Font.standard("Helvetica")
    size = 10.0
    extent = (font.ascender - font.descender) * size

    assert font.text_height("one", size, width=100) == pytest.approx(extent)
    assert font.text_height("1\n2\r\n3\r4", size, width=100) == pytest.approx(4 * extent)
    # An empty line between paragraphs counts, and so does a trailing line break.
    assert font.text_height("1\n\n3", size, width=100) == pytest.approx(3 * extent)
    assert font.text_height("1\n", size, width=100) == pytest.approx(2 * extent)
    # A line height spaces the baselines; the first and last lines keep the font's extent.
    assert font.text_height("1\n2\n3", size, width=100, line_height=2) == pytest.approx(extent + 2 * 2 * size)
    # Tabs are single spaces, as in the textbox.
    assert font.text_height("a\tb", size, width=100) == font.text_height("a b", size, width=100)
    assert font.text_height("", size, width=100) == 0
    paragraph = "words that wrap " * 8
    heights = [font.text_height(paragraph, size, width=width) for width in (1000, 200, 100, 50)]
    assert heights[0] == pytest.approx(extent)
    assert heights == sorted(heights)
    assert heights[-1] == pytest.approx(round(heights[-1] / extent) * extent)


def test_text_and_textbox_space_lines_alike() -> None:
    font = pyropdf.Font.standard("Helvetica")
    lines = "first\nsecond\nthird"
    size = 12.0
    top = 50 - font.ascender * size
    _doc, page = _new_page(400, 300)
    with page.draw() as canvas:
        canvas.text(lines, (20, 50), font=font, size=size)
        canvas.textbox((200, top, 380, 280), lines, font=font, size=size)
        canvas.text(lines, (20, 150), font=font, size=size, line_height=1.5)
        canvas.textbox((200, top + 100, 380, 280), lines, font=font, size=size, line_height=1.5)
    by_column: dict[bool, list[float]] = {False: [], True: []}
    for run in page.content().text_runs:
        by_column[run.glyphs[0].origin.x > 100].append(run.glyphs[0].origin.y)
    assert len(by_column[False]) == 6
    assert sorted(by_column[False]) == pytest.approx(sorted(by_column[True]), abs=1e-3)


def test_text_height_validates_its_input() -> None:
    font = pyropdf.Font.standard("Helvetica")
    with pytest.raises(ValueError, match="size"):
        font.text_height("x", 0, width=100)
    with pytest.raises(ValueError, match="width"):
        font.text_height("x", 10, width=-1)
    with pytest.raises(ValueError, match="line_height"):
        font.text_height("x", 10, width=100, line_height=0)
    with pytest.raises(TypeError, match="width"):
        font.text_height("x", 10, width="wide")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="text"):
        font.text_height(None, 10, width=100)  # ty: ignore[invalid-argument-type]
    with pytest.raises(pyropdf.PdfError, match="too narrow"):
        font.text_height("Wide", 10, width=5)
    with pytest.raises(pyropdf.PdfError, match="WinAnsi"):
        font.text_height("🦀", 10, width=100)
    with pytest.raises(pyropdf.LimitError) as exc_info:
        font.text_height("é" * 6, 10, width=100, max_text_size=11)
    assert exc_info.value.code == "text_input_size"
    with pytest.raises(pyropdf.LimitError) as exc_info:
        font.text_height("\n" * 4096, 10, width=100)
    assert exc_info.value.code == "text_line_count"
    with pytest.raises(pyropdf.LimitError) as exc_info:
        font.text_height("W" * 5000, 10, width=10)
    assert exc_info.value.code == "text_line_count"
    assert font.text_height("\n" * 4096, 10, width=100, max_text_size=None) > 0


@pytest.mark.parametrize(
    "name",
    ["Helvetica", "Times-Roman", "Times-Bold", "Courier-Bold", "Courier-BoldOblique"],
)
def test_standard_14_fonts_draw_under_their_postscript_names(name: StandardFontName) -> None:
    doc = pyropdf.Document()
    page = doc.pages.new()
    font = pyropdf.Font.standard(name)

    with page.draw() as canvas:
        canvas.text("Named", (40, 60), font=font)
        assert canvas.textbox((40, 100, 300, 160), "Boxed", font=font).fits

    data = doc.to_bytes(compress=False)
    assert _base_font(data, name)
    text = pyropdf.open(data).pages[0].text()
    assert all(word in text for word in ("Named", "Boxed"))


def test_symbol_fonts_draw_with_their_built_in_encoding() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new()

    with page.draw() as canvas:
        assert canvas.textbox((40, 40, 300, 100), "abc", font=pyropdf.Font.standard("Symbol")).fits
        canvas.text("abc", (40, 140), font=pyropdf.Font.standard("ZapfDingbats"))

    data = doc.to_bytes(compress=False)
    assert _base_font(data, "Symbol")
    assert _base_font(data, "ZapfDingbats")
    assert b"WinAnsiEncoding" not in data


@pytest.mark.parametrize("name", get_args(pyropdf.StandardFontName))
def test_standard_14_fonts_report_their_urw_box_and_afm_metrics(name: StandardFontName) -> None:
    font = pyropdf.Font.standard(name)
    assert font.bbox.y1 == pytest.approx(font.ascender)
    assert font.bbox.y0 == pytest.approx(font.descender)
    assert font.is_monospaced is name.startswith("Courier")
    assert font.program() is None
    chars = font.chars()
    assert chars
    assert all(font.has_glyph(char) for char in chars)
    assert sum(font.char_widths(chars, 9)) == pytest.approx(
        font.text_width(chars.replace("\n", "").replace("\r", ""), 9)
    )


def test_standard_14_metrics_are_adobes() -> None:
    helvetica = pyropdf.Font.standard("Helvetica")
    # The URW box, as pymupdf reports it; Adobe's FontBBox is -166 -225 1000 931.
    assert tuple(helvetica.bbox) == pytest.approx((-0.21, -0.299, 1.032, 1.075))
    assert helvetica.glyph_count == 315
    assert helvetica.char_widths("Hi!", 10) == pytest.approx((7.22, 2.22, 2.78))
    assert tuple(helvetica.char_bbox("A")) == pytest.approx((0.014, 0.0, 0.654, 0.718))
    assert "é" in helvetica.chars()
    assert "Ā" not in helvetica.chars()
    symbol = pyropdf.Font.standard("Symbol")
    # Symbol draws its built-in encoding, addressed through WinAnsi bytes: "a" is alpha.
    assert symbol.char_widths("a", 10) == pytest.approx((6.31,))


def test_character_metrics_validate_their_input() -> None:
    font = pyropdf.Font.standard("Helvetica")
    with pytest.raises(pyropdf.PdfError, match=r"no glyph for '🦀' \(U\+1F980\)"):
        font.char_widths("A🦀", 10)
    with pytest.raises(pyropdf.PdfError, match="no glyph"):
        font.char_bbox("🦀")
    with pytest.raises(pyropdf.PdfError, match="no glyph"):
        font.char_widths("Ā", 10)
    for value in ("ab", ""):
        with pytest.raises(ValueError, match="one-character"):
            font.char_bbox(value)
    with pytest.raises(TypeError, match="one-character"):
        font.char_bbox(65)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="text must be a string"):
        font.char_widths(b"A", 10)  # ty: ignore[invalid-argument-type]
    for size in (0, -1, math.nan):
        with pytest.raises(ValueError, match="size"):
            font.char_widths("A", size)
    with pytest.raises(pyropdf.LimitError) as excinfo:
        font.char_widths("A" * 11, 10, max_text_size=10)
    assert excinfo.value.code == "text_input_size"
    assert len(font.char_widths("A" * 11, 10, max_text_size=None)) == 11
