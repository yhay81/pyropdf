"""Embedded OpenType faces: loading, metrics, and the Type0 font a document writes.

Ported from pylopdf ebb01d3c ``tests/test_font.py`` (the OpenType cases)
with the subset faces of ``crates/pyropdf-engine/src/fonts/testdata``.
Intended differences: the subset faces cover only a few characters, so the
texts differ; a variable face is drawn at its default instance only, so a
weight other than its default is refused rather than instanced; and canvas
text, installed font extras, and subsetting at save belong to later issues.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

import pyropdf
from pyropdf import _fonts

FONTS = Path(__file__).resolve().parent.parent / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
JAPANESE = FONTS / "NotoSansJP-subset.otf"
HEBREW = FONTS / "NotoSansHebrew-subset.ttf"
ARABIC = FONTS / "NotoSansArabic-Variable-subset.ttf"
LINE = "AVA office 日本語、かな。"


def _with_weight_class(data: bytes, weight: int) -> bytes:
    """Return a copy of an sfnt whose OS/2 table declares ``weight``."""
    (table_count,) = struct.unpack_from(">H", data, 4)
    for index in range(table_count):
        tag, _checksum, offset, _length = struct.unpack_from(">4sIII", data, 12 + 16 * index)
        if tag == b"OS/2":
            patched = bytearray(data)
            struct.pack_into(">H", patched, offset + 4, weight)
            return bytes(patched)
    msg = "font has no OS/2 table"
    raise AssertionError(msg)


@pytest.fixture(scope="module")
def japanese() -> pyropdf.Font:
    return pyropdf.Font.from_file(JAPANESE)


def test_from_file_reads_face_metadata_and_vertical_metrics(japanese: pyropdf.Font) -> None:
    assert japanese.family == "Noto Sans JP"
    assert japanese.weight == 400
    assert (japanese.is_bold, japanese.is_italic, japanese.is_serif) == (False, False, False)
    assert japanese.units_per_em == 1000
    # The hhea metrics; the face does not set USE_TYPO_METRICS.
    assert japanese.ascender == pytest.approx(1.16)
    assert japanese.descender == pytest.approx(-0.288)
    assert japanese.glyph_count > 20
    assert japanese.bbox.x0 < japanese.bbox.x1
    hebrew = pyropdf.Font.from_file(HEBREW)
    assert hebrew.name == "Noto Sans Hebrew Regular"
    assert repr(hebrew) == "Font('Noto Sans Hebrew Regular')"
    program = hebrew.program()
    assert program is not None
    assert program[:4] == b"\x00\x01\x00\x00"
    assert japanese.program()[:4] == b"OTTO"  # ty: ignore[not-subscriptable]


def test_a_path_or_bytes_loads_the_same_face(japanese: pyropdf.Font) -> None:
    data = JAPANESE.read_bytes()
    faces = [
        pyropdf.Font.from_file(str(JAPANESE)),
        pyropdf.Font.from_bytes(data),
        pyropdf.Font.from_bytes(bytearray(data)),
        pyropdf.Font.from_bytes(memoryview(data)),
    ]
    for face in faces:
        assert face.name == japanese.name
        assert face.text_width(LINE, 12) == japanese.text_width(LINE, 12)


def test_text_width_scales_with_size_and_includes_shaping(japanese: pyropdf.Font) -> None:
    assert japanese.text_width("", 11) == 0
    # Full-width CJK advances one em per character.
    assert japanese.text_width("日本語", 10) == pytest.approx(30)
    assert japanese.text_width(LINE, 22) == pytest.approx(2 * japanese.text_width(LINE, 11))
    # Kerning pulls "AV" closer than the two glyphs measured alone.
    assert japanese.text_width("AV", 100) < japanese.text_width("A", 100) + japanese.text_width("V", 100)
    assert japanese.char_widths("AV", 100) == pytest.approx(
        (japanese.text_width("A", 100), japanese.text_width("V", 100))
    )


def test_right_to_left_text_measures_its_shaped_glyphs() -> None:
    hebrew = pyropdf.Font.from_file(HEBREW)
    # The points do not advance.
    assert hebrew.text_width("שָׁלוֹם", 10) == pytest.approx(hebrew.text_width("שלום", 10))
    arabic = pyropdf.Font.from_file(ARABIC)
    assert arabic.text_width("مرحبا", 10) > 0
    box = hebrew.char_bbox("ש")
    assert 0 <= box.x0 < box.x1
    assert hebrew.char_bbox(" ") == pyropdf.Rect(0, 0, 0, 0)


def test_text_height_wraps_embedded_text(japanese: pyropdf.Font) -> None:
    one = japanese.text_height("日本語", 10, width=100)
    assert one == pytest.approx(10 * (japanese.ascender - japanese.descender))
    two = japanese.text_height("日本語 日本語", 10, width=35)
    assert two > one


def test_text_width_rejects_line_breaks_missing_glyphs_and_bad_input(japanese: pyropdf.Font) -> None:
    for text in ("日本\n語", "a\rb"):
        with pytest.raises(pyropdf.PdfError, match="one line"):
            japanese.text_width(text, 11)
    with pytest.raises(pyropdf.PdfError, match=r"does not contain all glyphs.*U\+1F980 \('🦀'\)"):
        japanese.text_width("cot 🦀", 11)
    with pytest.raises(pyropdf.PdfError, match=r"U\+1F980"):
        japanese.char_widths("🦀", 11)
    for size in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="size"):
            japanese.text_width("A", size)
    with pytest.raises(TypeError, match="text"):
        japanese.text_width(b"bytes", 11)  # ty: ignore[invalid-argument-type]
    with pytest.raises(pyropdf.LimitError, match="UTF-8") as exc_info:
        japanese.text_width("日" * 4, 11, max_text_size=11)
    assert exc_info.value.code == "text_input_size"
    assert japanese.text_width("日" * 4, 11, max_text_size=None) == pytest.approx(44)


def test_shaping_a_long_text_is_bounded(japanese: pyropdf.Font) -> None:
    with pytest.raises(pyropdf.LimitError) as exc_info:
        japanese.text_width("A" * 1_048_577, 11, max_text_size=None)
    assert exc_info.value.code == "shaped_glyph_count"


def test_has_glyph_takes_one_character(japanese: pyropdf.Font) -> None:
    assert japanese.has_glyph("語") is True
    assert japanese.has_glyph("A") is True
    assert japanese.has_glyph("🦀") is False
    assert "語" in japanese.chars()
    for value in ("ab", ""):
        with pytest.raises(ValueError, match="one-character"):
            japanese.has_glyph(value)


def test_font_has_no_public_constructor() -> None:
    for args, kwargs in [((), {}), (("helv",), {}), ((), {"fontfile": JAPANESE})]:
        with pytest.raises(TypeError, match=r"Font\.standard\(name\).*Font\.from_file\(path\)"):
            pyropdf.Font(*args, **kwargs)


def test_loading_validates_its_arguments() -> None:
    for index in (-1, 2**32, True, 1.0):
        with pytest.raises(ValueError, match="index"):
            pyropdf.Font.from_file(JAPANESE, index=index)  # ty: ignore[invalid-argument-type]
    for weight in (0, 1001):
        with pytest.raises(ValueError, match="weight"):
            pyropdf.Font.from_file(JAPANESE, weight=weight)
    for weight in (True, 700.0, "bold"):
        with pytest.raises(TypeError, match="weight"):
            pyropdf.Font.from_bytes(b"font", weight=weight)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="path"):
        pyropdf.Font.from_file(JAPANESE.read_bytes())  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="data"):
        pyropdf.Font.from_bytes(str(JAPANESE))  # ty: ignore[invalid-argument-type]
    with pytest.raises(FileNotFoundError):
        pyropdf.Font.from_file(FONTS / "missing.ttf")


def test_invalid_and_unsupported_fonts_raise_pdf_errors() -> None:
    data = HEBREW.read_bytes()
    cases = [b"", b"not a font", data[:40], data[: len(data) // 2], bytes(len(data))]
    for case in cases:
        with pytest.raises(pyropdf.PdfError, match="invalid font"):
            pyropdf.Font.from_bytes(case)
    with pytest.raises(pyropdf.PdfError, match="invalid font"):
        pyropdf.Font.from_file(JAPANESE, index=1)
    cff2 = bytearray(data)
    (table_count,) = struct.unpack_from(">H", data, 4)
    for index in range(table_count):
        if cff2[12 + 16 * index : 16 + 16 * index] == b"post":
            cff2[12 + 16 * index : 16 + 16 * index] = b"CFF2"
    with pytest.raises(pyropdf.PdfError, match="unsupported font: CFF2"):
        pyropdf.Font.from_bytes(bytes(cff2))


def test_an_explicit_weight_must_match_the_face() -> None:
    data = JAPANESE.read_bytes()
    for weight in (400, None):
        assert pyropdf.Font.from_bytes(data, weight=weight).weight == 400
    with pytest.raises(pyropdf.PdfError, match="no weight axis, and its only face is regular, not bold; load the bold"):
        pyropdf.Font.from_bytes(data, weight=700)
    with pytest.raises(pyropdf.PdfError, match="its only face is regular, not weight-500"):
        pyropdf.Font.from_bytes(data, weight=500)
    bold = _with_weight_class(data, 700)
    assert pyropdf.Font.from_bytes(bold, weight=700).is_bold
    with pytest.raises(pyropdf.PdfError, match="its only face is bold, not regular"):
        pyropdf.Font.from_bytes(bold, weight=400)
    # A variable face is drawn at its default instance only.
    assert pyropdf.Font.from_file(ARABIC, weight=400).weight == 400
    with pytest.raises(pyropdf.PdfError, match="default instance"):
        pyropdf.Font.from_file(ARABIC, weight=700)


def test_font_input_is_bounded() -> None:
    size = JAPANESE.stat().st_size
    data = JAPANESE.read_bytes()
    loads = [
        lambda limit: pyropdf.Font.from_file(JAPANESE, max_size=limit),
        lambda limit: pyropdf.Font.from_bytes(data, max_size=limit),
        lambda limit: pyropdf.Font.from_bytes(memoryview(data), max_size=limit),
    ]
    for load in loads:
        with pytest.raises(pyropdf.LimitError) as exc_info:
            load(size - 1)
        assert exc_info.value.code == "font_input_size"
        assert load(size).weight == 400
    for value in (0, -1, True, 1.5):
        with pytest.raises((TypeError, ValueError), match="max_size"):
            pyropdf.Font.from_file(JAPANESE, max_size=value)  # ty: ignore[invalid-argument-type]
    assert pyropdf.Font.from_file(os.fspath(JAPANESE), max_size=None).weight == 400


def _register(document: pyropdf.Document, font: pyropdf.Font, cache: dict[bytes, int]) -> int:
    with document.transaction("register"):
        return _fonts.embedded_font(document, font, cache)


@pytest.mark.parametrize(
    ("path", "subtype", "program"), [(HEBREW, "CIDFontType2", "FontFile2"), (JAPANESE, "CIDFontType0", "FontFile3")]
)
def test_a_face_is_written_once_as_a_type0_font(path: Path, subtype: str, program: str) -> None:
    document = pyropdf.Document()
    cache: dict[bytes, int] = {}
    font = pyropdf.Font.from_file(path)
    xref = _register(document, font, cache)

    type0 = document.objects[xref]
    assert (type0["Subtype"], type0["Encoding"]) == ("Type0", "Identity-H")
    descendant = type0["DescendantFonts"][0]
    assert descendant["Subtype"] == subtype
    assert descendant["CIDSystemInfo"]["Ordering"].text == "Identity"
    assert ("CIDToGIDMap" in descendant) is (subtype == "CIDFontType2")
    descriptor = descendant["FontDescriptor"]
    assert descriptor["FontName"] == type0["BaseFont"] == descendant["BaseFont"]
    stream = descriptor[program]
    assert stream.read() == font.program()
    assert (stream.get("Subtype") == "OpenType") is (program == "FontFile3")
    objects = len(document.objects)

    # The same face, loaded again, reuses the objects.
    again = pyropdf.Font.from_bytes(path.read_bytes())
    assert _register(document, again, cache) == xref
    assert _register(document, font, cache) == xref
    assert len(document.objects) == objects


def test_glyphs_add_widths_and_texts_and_undo_removes_them() -> None:
    document = pyropdf.Document()
    cache: dict[bytes, int] = {}
    font = pyropdf.Font.from_file(JAPANESE)
    xref = _register(document, font, cache)
    shaped = font._font.shape("AVA日本")  # noqa: SLF001
    glyphs = [(glyph, "AVA日本"[start:end]) for glyph, _x, _y, _dx, _dy, start, end, _rtl in shaped]

    with document.transaction("draw"):
        assert _fonts.add_glyphs(document, xref, font, glyphs) == set()
    descendant = document.objects[xref]["DescendantFonts"][0]
    widths = _fonts._widths(descendant, font.glyph_count)  # noqa: SLF001
    assert set(widths) == {glyph for glyph, _ in glyphs}
    assert widths[glyphs[3][0]] == 1000
    to_unicode = document.objects[xref]["ToUnicode"]
    assert _fonts._mapped(to_unicode, font.glyph_count) == dict(glyphs)  # noqa: SLF001

    # A glyph drawn for other text keeps its first mapping and needs ActualText.
    with document.transaction("draw again"):
        assert _fonts.add_glyphs(document, xref, font, [(glyphs[3][0], "X"), (glyphs[0][0], "A")]) == {glyphs[3][0]}
    assert _fonts._mapped(to_unicode, font.glyph_count)[glyphs[3][0]] == "日"  # noqa: SLF001

    # The second drawing wrote nothing, so one undo takes back the first.
    document.history.undo()
    type0 = document.objects[xref]
    descendant, to_unicode = type0["DescendantFonts"][0], type0["ToUnicode"]
    assert _fonts._widths(descendant, font.glyph_count) == {}  # noqa: SLF001
    assert _fonts._mapped(to_unicode, font.glyph_count) == {}  # noqa: SLF001


def test_an_undone_registration_is_written_again() -> None:
    document = pyropdf.Document()
    cache: dict[bytes, int] = {}
    font = pyropdf.Font.from_file(HEBREW)
    first = _register(document, font, cache)
    document.history.undo()
    assert first not in document.objects
    second = _register(document, font, cache)
    assert isinstance(document.objects[second], pyropdf.Dictionary)
