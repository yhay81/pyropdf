"""FreeText annotations: text, fonts, alignment, rotation, and callouts.

pylopdf's tests/test_annotation_free_text.py at ebb01d3c. Intended
differences: the core's checks are called as ``_core.annotation_add`` with
the page's object; ``Page.text`` does not draw annotations stored without
an appearance, so the text that rendering draws for them is read with
``Annotation.text``, which does; text outside WinAnsi in a Standard 14
font raises ``ValueError``, and pyropdf has no installed face, so other
text needs an embedded ``Font`` (the tests from
``test_free_text_draws_in_a_standard_14_font_only`` on are pyropdf's own).
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import tempfile
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

import pyropdf
from pyropdf import Border, Color, Font, FreeText, LineEnding, Point, Rect, _core, _fonts

if TYPE_CHECKING:
    from collections.abc import Iterator

WHITE = (255, 255, 255)
FONTS = Path(__file__).resolve().parent.parent / "crates/pyropdf-engine/src/fonts/testdata"
JAPANESE = FONTS / "NotoSansJP-subset.otf"
ARABIC = FONTS / "NotoSansArabic-Variable-subset.ttf"
HEBREW = FONTS / "NotoSansHebrew-subset.ttf"
ARABIC_WORD = "مرحبا"
#: The refusal of a colour glyph with no outline, for one character.
NO_OUTLINE = r"cannot draw U\+{:04X} \('{}'\): its glyph is a colour glyph with no outline"


def test_callout_stops_after_four_points_even_when_sequence_misreports_length() -> None:
    class Endless(Sequence[tuple[int, int]]):
        def __init__(self) -> None:
            self.read = 0

        def __len__(self) -> int:
            return 2

        def __getitem__(self, index: int) -> tuple[int, int]:  # ty: ignore[invalid-method-override]
            self.read += 1
            return (10, 10)

    callout = Endless()
    with pytest.raises(ValueError, match="2 or 3 points"):
        FreeText((10, 10, 100, 50), "Hello", callout=callout)
    assert callout.read == 4


def test_core_callout_stops_after_four_points() -> None:
    class Endless:
        def __init__(self) -> None:
            self.read = 0

        def __iter__(self) -> Iterator[tuple[float, float]]:
            while True:
                self.read += 1
                if self.read > 4:
                    msg = "the core continued past the fourth point"
                    raise AssertionError(msg)
                yield (10.0, 10.0)

    doc = pyropdf.Document()
    page = doc.pages.new((200, 100))
    callout = Endless()
    before = doc.history.position
    with pytest.raises(pyropdf.PdfError, match="2 or 3 finite points"):
        _core.annotation_add(
            doc._core(),  # noqa: SLF001 - exercise the core's input boundary.
            page._number,  # noqa: SLF001
            page._generation,  # noqa: SLF001
            {"subtype": "FreeText", "rect": (10.0, 10.0, 100.0, 50.0), "callout": callout},
        )
    assert callout.read == 4
    assert doc.history.position == before
    assert "Annots" not in page.obj


@pytest.mark.parametrize("text", ["x" * (1024 * 1024 + 1), "漢" * 350_000], ids=["ascii", "multibyte"])
def test_free_text_input_limit_uses_actual_string_contents(text: str) -> None:
    class Misleading(str):
        __slots__ = ()

        def __len__(self) -> int:
            return 0

        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:  # noqa: ARG002
            return b""

    with pytest.raises(pyropdf.LimitError) as caught:
        FreeText((10, 10, 100, 50), Misleading(text))
    assert caught.value.code == "annotation_input_size"
    with pytest.raises(pyropdf.LimitError) as caught:
        pyropdf.Stamp((10, 10, 100, 50), contents=Misleading(text))
    assert caught.value.code == "annotation_input_size"


def test_free_text_encoding_checks_actual_string_contents() -> None:
    class Misleading(str):
        __slots__ = ()

        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:  # noqa: ARG002
            return b""

    page = pyropdf.Document().pages.new((200, 100))
    with pytest.raises(ValueError, match="WinAnsi"):
        page.annotations.add(FreeText((10, 10, 190, 90), Misleading("日本語")))
    annotation = page.annotations.add(FreeText((10, 10, 190, 90), Misleading("Hello")))
    assert annotation.contents == "Hello"


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _pixel(pixmap: pyropdf.Pixmap, x: float, y: float) -> tuple[int, int, int]:
    offset = int(y) * pixmap.stride + int(x) * pixmap.channels
    red, green, blue = bytes(pixmap)[offset : offset + 3]
    return red, green, blue


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_free_text_reads_upright_on_every_page_rotation(rotation: int) -> None:
    page = pyropdf.Document().pages.new((300, 300))
    page.rotation = rotation

    annotation = page.annotations.add(
        FreeText((20, 20, 200, 80), "Upright words", size=14, color=Color(1, 1, 0), border=Border(1))
    )

    assert annotation.type is pyropdf.AnnotationType.FREE_TEXT
    assert annotation.rect == pytest.approx(Rect(20, 20, 200, 80), abs=1e-3)
    assert annotation.contents == "Upright words"
    assert annotation.rotation == 0
    words = [(word.text, word.bbox) for word in page.words()]
    assert [text for text, _ in words] == ["Upright", "words"]
    first = words[0][1]
    # The words run left to right along the top of the box.
    assert first.x0 >= 20
    assert first.x0 < words[1][1].x0
    assert first.y0 >= 20
    assert first.y1 <= 50
    pixmap = page.pixmap(dpi=72)
    assert _pixel(pixmap, 150, 70) == (255, 255, 0)
    assert _pixel(pixmap, 20, 50) == (0, 0, 0)


def test_free_text_turns_and_aligns() -> None:
    page = pyropdf.Document().pages.new((300, 300))

    turned = page.annotations.add(FreeText((20, 20, 60, 280), "Down the side", rotate=90))
    centered = page.annotations.add(FreeText((100, 20, 280, 60), "Mid", align="center", size=20))

    assert turned.rotation == 90
    assert turned.obj["Rotate"] == 270
    assert centered.obj["Q"] == 1
    lines = [(line.text, line.direction) for block in page.layout().blocks for line in block.lines]
    assert ("Down the side", pytest.approx((0, 1), abs=1e-6)) in [(text, tuple(d)) for text, d in lines]
    (mid,) = [word for word in page.words() if word.text == "Mid"]
    assert abs((mid.bbox.x0 + mid.bbox.x1) / 2 - 190) < 3


def test_callouts_point_at_what_the_text_explains() -> None:
    page = pyropdf.Document().pages.new((300, 300))

    annotation = page.annotations.add(
        FreeText(
            (150, 150, 280, 200),
            "Look",
            callout=[Point(40, 250), (100, 220), (150, 175)],
            callout_ending=LineEnding.CLOSED_ARROW,
            border=Border(1),
        )
    )

    assert annotation.intent == "FreeTextCallout"
    # The rectangle holds the box, the line, and its ending.
    assert annotation.rect.contains(Rect(35, 150, 280, 255))
    assert len(annotation.obj["CL"]) == 6
    assert annotation.obj["LE"] == "ClosedArrow"
    pixmap = page.pixmap(dpi=72)
    assert _pixel(pixmap, 70, 235) != WHITE
    assert _pixel(pixmap, 100, 220) != WHITE


def test_free_text_survives_save_and_reopen() -> None:
    page = pyropdf.Document().pages.new((300, 300))
    page.annotations.add(
        FreeText(
            (20, 20, 200, 80),
            "Saved",
            font=Font.standard("Courier-Bold"),
            size=13,
            text_color=Color(0, 0, 1),
            opacity=0.5,
        )
    )

    (annotation,) = pyropdf.open(page.document.to_bytes()).pages[0].annotations

    assert annotation.contents == "Saved"
    assert annotation.obj["DA"].text == "/CoBo 13 Tf 0 0 1 rg"
    assert annotation.opacity == pytest.approx(0.5)
    assert "Saved" in pyropdf.open(page.document.to_bytes()).pages[0].text()


def test_free_text_validates_its_fields() -> None:
    text = FreeText((0, 0, 100, 50), "x")
    assert (text.size, text.align, text.rotate, text.callout, text.color) == (11.0, "left", 0, None, None)
    assert text.border == Border(0)
    assert FreeText((0, 0, 100, 50), "x", rotate=-90).rotate == 270
    for kwargs, error in [
        ({"size": 0}, ValueError),
        ({"align": "justify"}, ValueError),
        ({"rotate": 45}, ValueError),
        ({"callout": [(0, 0)]}, ValueError),
        ({"font": "Helvetica"}, TypeError),
        ({"text_color": (0, 0, 0)}, TypeError),
        ({"callout_ending": "wave"}, ValueError),
    ]:
        with pytest.raises(error):
            FreeText((0, 0, 100, 50), "x", **cast("dict[str, Any]", kwargs))
    with pytest.raises(TypeError, match="text must be a str"):
        FreeText((0, 0, 100, 50), cast("Any", None))
    with pytest.raises(pyropdf.LimitError) as raised:
        FreeText((0, 0, 100, 50), "x" * (1024 * 1024))
    assert raised.value.code == "annotation_input_size"


def test_text_that_does_not_fit_changes_nothing() -> None:
    page = pyropdf.Document().pages.new((300, 300))

    with pytest.raises(pyropdf.PdfError, match="does not fit"):
        page.annotations.add(FreeText((20, 20, 60, 30), "far too much text for so small a box", size=20))
    assert "Annots" not in page.obj


def test_the_core_repeats_the_free_text_checks() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((100, 100))
    location = (doc._core(), page._number, page._generation)  # noqa: SLF001
    base: dict[str, Any] = {"subtype": "FreeText", "rect": (0.0, 0.0, 90.0, 40.0), "contents": "x"}
    for spec, message in [
        ({"font_size": 0.0}, "font size"),
        ({"font_name": "Comic"}, "unknown Standard 14 font"),
        ({"align": 3}, "aligns left"),
        ({"rotate": 45}, "multiple of 90"),
        ({"callout": [(0.0, 0.0)]}, "2 or 3 finite points"),
        ({"callout_ending": "ClosedArrow"}, "needs a callout line"),
        ({"text_color": (2.0, 0.0, 0.0)}, "components from 0 to 1"),
    ]:
        with pytest.raises(pyropdf.PdfError, match=message):
            _core.annotation_add(*location, {**base, **spec})
    with pytest.raises(pyropdf.PdfError, match="belong to free text"):
        _core.annotation_add(*location, {"subtype": "Square", "rect": (0.0, 0.0, 9.0, 9.0), "font_size": 9.0})
    assert "Annots" not in doc.pages[0].obj


def test_rendering_draws_a_missing_free_text_appearance() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [4 0 R] >>",
                4: (
                    "<< /Type /Annot /Subtype /FreeText /Rect [10 10 190 90] /Contents (Drawn later) "
                    "/DA (/TiRo 18 Tf 1 0 0 rg) /C [0 0 1] >>"
                ),
            }
        )
    )
    before = doc.to_bytes()

    pixmap = doc.pages[0].pixmap(dpi=72)

    assert _pixel(pixmap, 100, 80) == (0, 0, 255)
    assert doc.pages[0].annotations[0].text().strip() == "Drawn later"
    assert doc.to_bytes() == before


@pytest.mark.parametrize(
    "default_appearance",
    ["(0 g /HeBo 0 Tf)", "(1 0 0 1 k)", "(garbage Tf rg)", "<FFFE>", "7"],
)
def test_odd_default_appearances_still_draw(default_appearance: str) -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [4 0 R] >>",
                4: (
                    "<< /Type /Annot /Subtype /FreeText /Rect [10 10 190 90] /Contents (Odd) "
                    f"/DA {default_appearance} >>"
                ),
            }
        )
    )

    assert doc.pages[0].annotations[0].text().strip() == "Odd"


def test_free_text_draws_in_a_standard_14_font_only() -> None:
    page = pyropdf.Document().pages.new((300, 300))
    core = (page.document._core(), page._number, page._generation)  # noqa: SLF001

    with pytest.raises(ValueError, match="WinAnsi"):
        page.annotations.add(FreeText((20, 20, 200, 60), "日本語"))
    with pytest.raises(pyropdf.PdfError, match="WinAnsi"):
        _core.annotation_add(*core, {"subtype": "FreeText", "rect": (20.0, 20.0, 200.0, 60.0), "contents": "日本語"})
    free_text = page.annotations.add(FreeText((20, 20, 200, 60), "Latin"))
    with pytest.raises(pyropdf.PdfError, match="WinAnsi"):
        free_text.contents = "更新"
    assert free_text.contents == "Latin"
    assert "Annots" in page.obj
    assert len(page.annotations) == 1


def test_free_text_appearance_names_its_font() -> None:
    page = pyropdf.Document().pages.new((300, 300))

    annotation = page.annotations.add(FreeText((20, 20, 200, 60), "Named", font=Font.standard("Times-Roman")))

    form = annotation.obj["AP"]["N"]
    font = form["Resources"]["Font"]["TiRo"]
    assert (font["BaseFont"], font["Encoding"]) == ("Times-Roman", "WinAnsiEncoding")
    assert page.text().strip() == "Named"


def test_free_text_in_an_embedded_face_survives_save_and_reopen() -> None:
    page = pyropdf.Document().pages.new((300, 300))

    annotation = page.annotations.add(FreeText((20, 20, 200, 60), "日本語", font=Font.from_file(JAPANESE), size=14))

    font = annotation.obj["AP"]["N"]["Resources"]["Font"]["PyroFreeText"]
    assert (font["Subtype"], font["Encoding"]) == ("Type0", "Identity-H")
    assert page.text().strip() == "日本語"
    reopened = pyropdf.open(page.document.to_bytes()).pages[0]
    (saved,) = reopened.annotations
    assert saved.contents == "日本語"
    assert saved.obj["AP"]["N"]["Resources"]["Font"]["PyroFreeText"]["Subtype"] == "Type0"
    assert reopened.text().strip() == "日本語"


def test_free_text_in_an_embedded_face_adds_and_edits_in_one_undo_step_each() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    start = doc.history.position

    annotation = page.annotations.add(FreeText((20, 20, 200, 60), "日本語", font=Font.from_file(JAPANESE)))
    assert doc.history.position == start + 1
    annotation.contents = "テキスト"
    assert doc.history.position == start + 2
    assert page.text().strip() == "テキスト"

    doc.history.undo()
    assert annotation.contents == "日本語"
    assert page.text().strip() == "日本語"
    doc.history.undo()
    assert len(page.annotations) == 0


def test_free_text_in_an_embedded_face_keeps_it_through_other_edits() -> None:
    page = pyropdf.Document().pages.new((300, 300))
    annotation = page.annotations.add(FreeText((20, 20, 200, 60), "", font=Font.from_file(JAPANESE)))

    annotation.color = Color(1, 1, 0)
    annotation.contents = "本日"

    assert page.text().strip() == "本日"
    assert annotation.obj["AP"]["N"]["Resources"]["Font"]["PyroFreeText"]["Subtype"] == "Type0"


def test_a_face_without_a_glyph_for_the_text_changes_nothing() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    face = Font.from_file(JAPANESE)
    start = doc.history.position

    with pytest.raises(pyropdf.PdfError, match=r"no glyph for U\+66F4"):
        page.annotations.add(FreeText((20, 20, 200, 60), "更新", font=face))
    assert "Annots" not in page.obj
    assert doc.history.position == start

    annotation = page.annotations.add(FreeText((20, 20, 200, 60), "日本", font=face))
    before = doc.to_bytes()
    with pytest.raises(pyropdf.PdfError, match=r"no glyph for U\+66F4"):
        annotation.contents = "更新"
    with pytest.raises(pyropdf.PdfError, match="does not fit"):
        annotation.contents = "日本語" * 100
    assert annotation.contents == "日本"
    assert doc.to_bytes() == before


@pytest.fixture(
    params=["colr0.ttf", "colr1.ttf", "sbix.ttf", "cbdt.ttf", "svg.ttf", "svg-cff.otf"],
    ids=["colr0", "colr1", "sbix", "cbdt", "svg", "svg-cff"],
)
def colour(request: pytest.FixtureRequest) -> Font:
    """Return a face of ``colour_faces.py``, whose docstring says what its glyphs are."""
    return Font.from_file(FONTS / f"colour-{request.param}")


def _opaque_pixels(page: pyropdf.Page) -> int:
    return sum(alpha != 0 for alpha in bytes(page.pixmap(alpha=True))[3::4])


@pytest.mark.parametrize(
    ("text", "character"),
    [("A", "A"), ("CA", "A"), ("CD", "D"), ("C\nCA", "A")],
    ids=["alone", "after-a-glyph", "two-points", "second-line"],
)
def test_adding_a_free_text_refuses_a_colour_glyph_with_no_outline(colour: Font, text: str, character: str) -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    before = doc.to_bytes()
    start = doc.history.position

    with pytest.raises(pyropdf.PdfError, match=NO_OUTLINE.format(ord(character), character)):
        page.annotations.add(FreeText((20, 20, 200, 60), text, font=colour))

    assert len(page.annotations) == 0
    assert "Annots" not in page.obj
    assert doc.history.position == start
    assert doc.to_bytes() == before


def test_changing_a_free_text_to_a_colour_glyph_with_no_outline_keeps_what_it_had(colour: Font) -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    annotation = page.annotations.add(FreeText((20, 20, 200, 60), "C", font=colour))
    appearance = annotation.obj["AP"]["N"]
    written = appearance.read()
    pixels = bytes(page.pixmap())
    start = doc.history.position

    with pytest.raises(pyropdf.PdfError, match=NO_OUTLINE.format(ord("A"), "A")):
        annotation.contents = "A"

    assert annotation.contents == "C"
    assert page.text().strip() == "C"
    assert annotation.obj["AP"]["N"].xref == appearance.xref
    assert annotation.obj["AP"]["N"].read() == written
    assert _opaque_pixels(page) > 0
    assert bytes(page.pixmap()) == pixels
    assert doc.history.position == start


def test_a_refused_change_of_a_free_text_leaves_the_saved_bytes() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    face = Font.from_file(FONTS / "colour-svg-cff.otf")
    annotation = page.annotations.add(FreeText((20, 20, 200, 60), "C", font=face))
    before = doc.to_bytes()

    with pytest.raises(pyropdf.PdfError, match=NO_OUTLINE.format(ord("A"), "A")):
        annotation.contents = "A"

    assert doc.to_bytes() == before


def test_a_free_text_draws_the_fallback_outline_of_a_colour_glyph(colour: Font) -> None:
    page = _free_text_in(colour, "B")

    assert _opaque_pixels(page) > 0
    reopened = pyropdf.open(page.document.to_bytes()).pages[0]
    assert _opaque_pixels(reopened) > 0
    assert reopened.text().strip() == "B"


def test_changing_a_free_text_to_a_colour_glyph_with_a_fallback_outline_draws_it(colour: Font) -> None:
    page = _free_text_in(colour, "")
    (annotation,) = page.annotations
    assert _opaque_pixels(page) == 0

    annotation.contents = "B"

    assert _opaque_pixels(page) > 0
    assert page.text().strip() == "B"


@pytest.mark.parametrize("text", ["C C", "C\u00a0C", "E"], ids=["space", "covered-space", "empty-uncovered"])
def test_a_free_text_does_not_refuse_a_glyph_that_asks_for_no_ink(colour: Font, text: str) -> None:
    (annotation,) = _free_text_in(colour, text).annotations

    assert annotation.contents == text


def _with_sbix_strikes(program: bytes, strikes: int) -> bytes:
    """Return OpenType ``program`` with an ``sbix`` table of ``strikes`` offsets, which is all a lookup counts."""
    tables = {}
    for index in range(struct.unpack_from(">H", program, 4)[0]):
        tag, _, offset, length = struct.unpack_from(">4sIII", program, 12 + 16 * index)
        tables[tag] = program[offset : offset + length]
    tables[b"sbix"] = struct.pack(">HHI", 1, 1, strikes) + struct.pack(">I", 8 + 4 * strikes) * strikes
    directory = program[:4] + struct.pack(">HHHH", len(tables), 0, 0, 0)
    body = b""
    for tag in sorted(tables):
        at = 12 + 16 * len(tables) + len(body)
        directory += struct.pack(">4sIII", tag, 0, at, len(tables[tag]))
        body += tables[tag] + bytes(-len(tables[tag]) % 4)
    return directory + body


def test_a_face_whose_colour_tables_ask_for_too_much_is_an_invalid_font_not_an_unfit_text() -> None:
    face = Font.from_bytes(_with_sbix_strikes((FONTS / "colour-colr1.ttf").read_bytes(), (1 << 16) + 1))
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    before = doc.to_bytes()

    with pytest.raises(pyropdf.PdfError, match="invalid font: the face's colour tables ask for more work"):
        page.annotations.add(FreeText((20, 20, 200, 60), "C", font=face))

    assert len(page.annotations) == 0
    assert doc.to_bytes() == before


def _free_text_in(face: Font, text: str, *, size: float = 14) -> pyropdf.Page:
    """Return a new page with one free text that holds ``text``, drawn in ``face``."""
    page = pyropdf.Document().pages.new((300, 100))
    page.annotations.add(FreeText((20, 20, 280, 60), text, font=face, size=size))
    return page


def test_a_free_text_in_an_arabic_face_has_no_unmapped_glyph() -> None:
    face = Font.from_file(ARABIC)
    # The shaper splits the letter beh into a base and a dot in one cluster.
    dot = next(glyph[0] for glyph in face._font.shape(ARABIC_WORD) if glyph[5] == glyph[6])  # noqa: SLF001
    reopened = pyropdf.open(_free_text_in(face, ARABIC_WORD).document.to_bytes())
    page = reopened.pages[0]

    with warnings.catch_warnings():
        warnings.simplefilter("error", pyropdf.PyropdfWarning)
        assert page.text().strip() == ARABIC_WORD
    assert page.text(unmapped="replacement").strip() == ARABIC_WORD
    (annotation,) = page.annotations
    type0 = annotation.obj["AP"]["N"]["Resources"]["Font"]["PyroFreeText"]
    mapped = _fonts._mapped(type0["ToUnicode"], face.glyph_count)  # noqa: SLF001
    drawn = _fonts._widths(type0["DescendantFonts"][0], face.glyph_count)  # noqa: SLF001
    assert dot in drawn
    assert set(drawn) <= set(mapped)
    assert mapped[dot] == _fonts._NO_TEXT  # noqa: SLF001


def test_a_free_texts_cluster_is_one_actual_text_and_its_glyphs_stay_where_the_shaper_put_them() -> None:
    size = 14.0
    face = Font.from_file(ARABIC)
    page = _free_text_in(face, ARABIC_WORD, size=size)

    scale = size / face._font.units_per_em  # noqa: SLF001
    pen = 0.0
    expected = {}
    for glyph, advance, _y_advance, x_offset, y_offset, *_ in face._font.shape(ARABIC_WORD):  # noqa: SLF001
        expected[glyph] = (pen + x_offset * scale, -y_offset * scale)
        pen += advance * scale
    glyphs = [glyph for run in page.content(paths=False, images=False).text_runs for glyph in run.glyphs]
    assert {glyph.glyph_id for glyph in glyphs} == set(expected)
    first = glyphs[0].origin
    for glyph in glyphs:
        assert glyph.glyph_id is not None
        moved = (glyph.origin.x - first.x, glyph.origin.y - first.y)
        assert moved == pytest.approx(expected[glyph.glyph_id], abs=1e-2)

    # Only the letter that the face splits needs ActualText, and its glyphs lie inside.
    (annotation,) = page.annotations
    appearance = annotation.obj["AP"]["N"].read()
    assert re.findall(rb"/ActualText <FEFF([0-9A-F]*)>", appearance) == [b"0628"]
    assert (appearance.count(b"BDC"), appearance.count(b"EMC")) == (1, 1)
    inside = appearance[appearance.index(b"BDC") : appearance.index(b"EMC")]
    assert len(re.findall(rb"<[0-9A-F]{4}>", inside)) == 2


def _shown(stream: bytes) -> list[bytes | float]:
    """Return what shows the text after its matrix, without selecting its font, with each number as a float."""
    start = stream.index(b" Tm") + 3
    shown = re.sub(rb"/\S+ \S+ Tf", b"", stream[start : stream.index(b"ET", start)])
    tokens = re.findall(rb"<[0-9A-F]+>|[^\s\[\]<>]+", shown)
    return [float(token) if re.fullmatch(rb"-?[0-9.]+", token) else token for token in tokens]


@pytest.mark.parametrize(
    ("path", "text"),
    [(ARABIC, ARABIC_WORD), (ARABIC, "عالم مرحبا"), (HEBREW, "שָׁלוֹם עולם"), (JAPANESE, "AVA 日本語、かな。")],
    ids=["arabic", "arabic-words", "hebrew", "japanese"],
)
def test_a_free_text_writes_its_text_as_canvas_text_does(path: Path, text: str) -> None:
    # The rule of a cluster lives in two writers, the canvas's in Python and this one in Rust.
    size = 14.0
    face = Font.from_file(path)
    (annotation,) = _free_text_in(face, text, size=size).annotations
    page = pyropdf.Document().pages.new((300, 100))
    with page.draw() as canvas:
        canvas.text(text, (20, 60), font=face, size=size)

    written = _shown(annotation.obj["AP"]["N"].read())
    contents = b"\n".join(part.read() for part in page.obj["Contents"])
    assert written == pytest.approx(_shown(contents), abs=1e-3)


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext is not installed")
def test_pdftotext_reads_a_free_texts_cluster_as_its_text() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "page.pdf"
        path.write_bytes(_free_text_in(Font.from_file(ARABIC), ARABIC_WORD).document.to_bytes())
        read = subprocess.run(  # noqa: S603 - a fixed tool on a file of the test's own.
            [shutil.which("pdftotext") or "pdftotext", "-enc", "UTF-8", str(path), "-"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
    # Xpdf marks the direction of a run with embedding controls.
    assert read.translate(dict.fromkeys(map(ord, "\u202a\u202b\u202c"))).strip() == ARABIC_WORD


def test_a_later_session_redraws_in_the_default_appearance_font() -> None:
    page = pyropdf.Document().pages.new((300, 300))
    page.annotations.add(FreeText((20, 20, 200, 60), "日本語", font=Font.from_file(JAPANESE)))
    reopened = pyropdf.open(page.document.to_bytes()).pages[0]
    (annotation,) = reopened.annotations

    with pytest.raises(pyropdf.PdfError, match="WinAnsi"):
        annotation.contents = "本日"
    assert reopened.text().strip() == "日本語"
    annotation.contents = "Latin"
    assert reopened.text().strip() == "Latin"
