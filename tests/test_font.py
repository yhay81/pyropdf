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
import tracemalloc
from typing import TYPE_CHECKING, Any, cast, get_args

import pytest

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyropdf import StandardFontName


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


def test_standard_14_fonts_measure_with_afm_widths() -> None:
    font = pyropdf.Font.standard("Helvetica")
    assert font.text_width("Hello World", 11) == pytest.approx(56.837)
    assert font.has_glyph("A")
    assert font.has_glyph("é")
    assert not font.has_glyph("あ")
    with pytest.raises(pyropdf.PdfError, match="WinAnsi"):
        font.text_width("あ", 11)


def test_text_fit_is_public_and_immutable() -> None:
    assert pyropdf.TextFit(spare_height=0).fits
    assert not pyropdf.TextFit(spare_height=-1).fits
    with pytest.raises(AttributeError):
        pyropdf.TextFit(spare_height=0).spare_height = 1  # ty: ignore[invalid-assignment]


def test_text_limit_counts_utf8_bytes() -> None:
    font = pyropdf.Font.standard("Helvetica")
    assert font.text_width("é€", 10, max_text_size=5) > 0
    with pytest.raises(pyropdf.LimitError) as caught:
        font.text_width("é€", 10, max_text_size=4)
    assert caught.value.code == "text_input_size"
    with pytest.raises(pyropdf.LimitError) as caught:
        font.text_width("a" * 100_000, 10, max_text_size=1)
    assert caught.value.code == "text_input_size"


@pytest.mark.parametrize("operation", ["widths", "width", "encode", "layout"])
def test_core_font_operations_refuse_text_over_their_budget(operation: str) -> None:
    face = pyropdf._core._Font.standard("Helvetica")  # noqa: SLF001 - exercise the binding directly

    def call() -> None:
        if operation == "widths":
            face.char_widths("abc", 10, 2)
        elif operation == "width":
            face.text_width("abc", 10, 2)
        elif operation == "encode":
            face.encode("abc", 2)
        else:
            face.layout("abc", 100, 100, 10, None, False, 100, 2)  # noqa: FBT003 - the core flag

    with pytest.raises(pyropdf.LimitError) as caught:
        call()
    assert caught.value.code == "text_input_size"


def test_core_font_operations_can_lift_the_text_limit() -> None:
    face = pyropdf._core._Font.standard("Helvetica")  # noqa: SLF001 - exercise the binding directly
    assert face.char_widths("abc", 10, None) == pyropdf.Font.standard("Helvetica").char_widths("abc", 10)
    assert face.encode("abc", None) == b"abc"


def test_core_text_limit_does_not_trust_a_string_subclasses_length() -> None:
    class MisleadingLength(str):
        __slots__ = ()

        def __len__(self) -> int:
            return 1

    face = pyropdf._core._Font.standard("Helvetica")  # noqa: SLF001 - exercise the binding directly
    with pytest.raises(pyropdf.LimitError) as caught:
        face.text_width(MisleadingLength("x" * 100), 10, 2)
    assert caught.value.code == "text_input_size"


def test_core_rejects_large_unicode_before_creating_a_utf8_cache() -> None:
    if tracemalloc.is_tracing():
        pytest.skip("the test measures allocations in an isolated tracer")
    face = pyropdf._core._Font.standard("Helvetica")  # noqa: SLF001 - exercise the binding directly
    text = "é" * 2_000_000
    tracemalloc.start()
    try:
        with pytest.raises(pyropdf.LimitError):
            face.text_width(text, 10, 1)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 256_000


def test_public_text_limit_uses_the_string_contents_not_overrides() -> None:
    class MisleadingText(str):
        __slots__ = ()

        def __len__(self) -> int:
            return 100

        def __iter__(self) -> Iterator[str]:
            msg = "the limit must not call overridden iteration"
            raise AssertionError(msg)

    font = pyropdf.Font.standard("Helvetica")
    with pytest.raises(pyropdf.LimitError) as caught:
        font.text_width(MisleadingText("abc"), 10, max_text_size=2)
    assert caught.value.code == "text_input_size"


def test_core_encoding_rejects_glyphs_missing_from_the_selected_font() -> None:
    face = pyropdf._core._Font.standard("Symbol")  # noqa: SLF001 - exercise the binding directly
    assert not face.has_glyph(ord("ð"))
    with pytest.raises(pyropdf.PdfError, match="no glyph"):
        face.encode("ð")
