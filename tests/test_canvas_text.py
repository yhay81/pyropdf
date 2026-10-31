"""Drawing text in the Standard 14 fonts with Page.draw (DESIGN.md section 7).

Ported from pylopdf's tests/test_canvas.py and tests/test_draw.py at
ebb01d3c, the cases without an embedded font (issue 467). Intended
differences:

- text without a ``font`` is drawn in Helvetica, and text outside WinAnsi
  needs a font; pyropdf installs no font extras, and embedded fonts come
  with issue 153, which keeps the embedded cases;
- Standard 14 strings are written in hexadecimal, so no string holds a
  resource name that the commit renames;
- pyropdf has no ``Document.complexity``; atomicity is checked on the
  document's objects;
- a new document is drawn like an open one, so the cases of ``create()``
  do not apply.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

import pyropdf
from pyropdf import Color, Matrix, Stroke, TextFit, TextRenderMode

RED = Color(1.0, 0.0, 0.0)
BLUE = Color(0.0, 0.0, 1.0)
GREEN = Color(0.0, 128 / 255, 0.0)
WHITE = Color(1.0, 1.0, 1.0)
_TURNS = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


def _new_page(width: float = 300, height: float = 200) -> tuple[pyropdf.Document, pyropdf.Page]:
    doc = pyropdf.Document()
    return doc, doc.pages.new((width, height))


def _objects(doc: pyropdf.Document) -> int:
    return len(doc.objects)


def _assert_color(actual: object, expected: Color) -> None:
    assert isinstance(actual, Color)
    channels = ("red", "green", "blue", "alpha")
    assert [getattr(actual, name) for name in channels] == pytest.approx(
        [getattr(expected, name) for name in channels], abs=1 / 255
    )


def _direction(glyph: pyropdf.Glyph) -> tuple[float, float]:
    """Return the unit baseline direction of a glyph in display space."""
    a, b = glyph.matrix.a, glyph.matrix.b
    length = math.hypot(a, b)
    return a / length, b / length


def _ink(page: pyropdf.Page) -> tuple[int, int, int, int]:
    """Return the pixel box of everything darker than white."""
    pixmap = page.pixmap(dpi=72)
    data = bytes(pixmap)
    xs: list[int] = []
    ys: list[int] = []
    for y in range(pixmap.height):
        row = data[y * pixmap.stride : y * pixmap.stride + pixmap.width * pixmap.channels]
        dark = [x for x in range(pixmap.width) if min(row[x * pixmap.channels : x * pixmap.channels + 3]) < 128]
        if dark:
            xs.extend((dark[0], dark[-1]))
            ys.append(y)
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def _green_page() -> tuple[pyropdf.Document, pyropdf.Page]:
    doc, page = _new_page(160, 80)
    with page.draw() as canvas:
        canvas.rect((0, 0, 160, 80), fill=GREEN)
    return doc, page


def _rgb(page: pyropdf.Page) -> set[int]:
    pixmap = page.pixmap(dpi=72)
    data = bytes(pixmap)
    return {data[offset] for offset in range(len(data)) if offset % pixmap.channels < 3}


def test_text_draws_in_paint_order_with_paths() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.rect((10, 10, 60, 60), fill=RED)
        canvas.text("first", (10, 90))
        canvas.rect((70, 10, 120, 60), fill=BLUE)
        canvas.text("second", (10, 120), font=pyropdf.Font.standard("Times-Roman"))
    kinds = [type(item).__name__ for item in page.content()]
    assert kinds == ["Path", "TextRun", "Path", "TextRun"]
    assert page.text() == "first\nsecond\n"


@pytest.mark.parametrize("prior_state", [b"3 Tr", b"10 Tc", b"10 Tw", b"50 Tz", b"30 Ts"])
def test_text_does_not_inherit_existing_page_text_state(prior_state: bytes) -> None:
    doc, page = _new_page()
    page.obj["Contents"] = doc.objects[doc.objects.add_stream(b"BT " + prior_state + b" ET")]
    with page.draw() as canvas:
        canvas.text("a b", (20, 60), size=20)

    _reference_doc, reference = _new_page()
    with reference.draw() as canvas:
        canvas.text("a b", (20, 60), size=20)
    assert bytes(page.pixmap(dpi=72)) == bytes(reference.pixmap(dpi=72))


def test_text_in_standard_fonts() -> None:
    _doc, page = _new_page(400, 200)
    with page.draw() as canvas:
        canvas.text("Helvetica text", (20, 40), size=12, fill=RED)
        canvas.text("Courier text", (20, 80), font=pyropdf.Font.standard("Courier"), size=16, fill=BLUE)
        canvas.text("two\nlines", (20, 120), size=10, line_height=2)
    runs = page.content().text_runs
    # hayro gives no name for a font that is not embedded, so the page's fonts tell them.
    assert {font.base_font for font in page.fonts()} == {"Helvetica", "Courier"}
    _assert_color(runs[0].fill, RED)
    _assert_color(runs[1].fill, BLUE)
    assert runs[0].glyphs[0].origin.y == pytest.approx(40, abs=0.01)
    assert runs[1].glyphs[0].origin.y == pytest.approx(80, abs=0.01)
    assert runs[1].glyphs[0].size == pytest.approx(16, abs=0.01)
    two, lines = runs[2], runs[3]
    assert lines.glyphs[0].origin.y - two.glyphs[0].origin.y == pytest.approx(20, abs=0.01)
    assert page.text() == "Helvetica text\nCourier text\ntwo\nlines\n"


def test_text_can_be_stroked() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.text("Outline", (20, 40), size=20, fill=None, stroke=Stroke(paint=RED, width=0.5))
        canvas.text("Both", (20, 80), size=20, fill=BLUE, stroke=RED)
    outline, both = page.content().text_runs
    assert outline.stroke is not None
    assert both.stroke is not None
    assert outline.render_mode is TextRenderMode.STROKE
    assert outline.fill is None
    _assert_color(outline.stroke.paint, RED)
    assert outline.stroke.width == pytest.approx(0.5)
    assert both.render_mode is TextRenderMode.FILL_STROKE
    _assert_color(both.fill, BLUE)
    _assert_color(both.stroke.paint, RED)


def test_translucent_text() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.text("faint", (20, 40), fill=Color(0, 0, 0, 0.5))
    (run,) = page.content().text_runs
    _assert_color(run.fill, Color(0, 0, 0, 0.5))


def test_text_needs_a_fill_or_a_stroke() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas, pytest.raises(ValueError, match="fill or a stroke"):
        canvas.text("nothing", (10, 10), fill=None)


def test_standard_14_text_must_be_winansi() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        with pytest.raises(ValueError, match="WinAnsi"):
            canvas.text("日本", (10, 10), font=pyropdf.Font.standard("Helvetica"))
        with pytest.raises(ValueError, match="WinAnsi"):
            canvas.textbox((10, 10, 100, 100), "日本")
    assert page.text() == ""


def test_textbox_fits_or_draws_nothing() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        fit = canvas.textbox((10, 10, 150, 100), "A short paragraph that wraps onto lines.", size=10)
        overflow = canvas.textbox((10, 120, 60, 130), "Far too much text for this box", size=10)
        empty = canvas.textbox((10, 140, 60, 160), "")
    assert isinstance(fit, TextFit)
    assert fit.fits
    assert fit.spare_height > 0
    assert not overflow.fits
    assert overflow.spare_height < 0
    assert empty == TextFit(spare_height=20)
    text = page.text()
    assert "A short paragraph" in text
    assert "Far too much" not in text
    assert all(run.bbox.y1 <= 100 for run in page.content().text_runs)


def test_textbox_alignment() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.textbox((0, 10, 300, 40), "left", align="left")
        canvas.textbox((0, 50, 300, 80), "center", align="center")
        canvas.textbox((0, 90, 300, 120), "right", align="right")
    left, center, right = page.content().text_runs
    assert left.bbox.x0 < 5
    assert abs((center.bbox.x0 + center.bbox.x1) / 2 - 150) < 2
    assert 295 < right.bbox.x1 <= 300
    with page.draw() as canvas, pytest.raises(ValueError, match="align"):
        canvas.textbox((0, 0, 100, 100), "x", align="middle")  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("rotate", [0, 90, 180, 270, -90, 450])
def test_text_turns_clockwise_about_its_origin(rotate: int) -> None:
    _doc, page = _new_page(300, 300)
    with page.draw() as canvas:
        canvas.text("Turned\nsecond", (150, 150), size=12, line_height=2, rotate=rotate)
    first, second = page.content().text_runs
    cos, sin = _TURNS[rotate % 360]
    origin = first.glyphs[0].origin
    assert (origin.x, origin.y) == pytest.approx((150, 150), abs=0.01)
    for glyph in (*first.glyphs, *second.glyphs):
        assert _direction(glyph) == pytest.approx((cos, sin), abs=1e-6)
        assert glyph.size == pytest.approx(12, abs=0.01)
    # The second line is 24 points further along the turned downward axis.
    below = second.glyphs[0].origin
    assert (below.x, below.y) == pytest.approx((150 - 24 * sin, 150 + 24 * cos), abs=0.01)
    assert page.text() == "Turned\nsecond\n"


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_turned_text_renders_along_its_direction(rotate: int) -> None:
    _doc, page = _new_page(200, 200)
    with page.draw() as canvas:
        canvas.text("MMMMMM", (100, 100), size=20, rotate=rotate)
    x0, y0, x1, y1 = _ink(page)
    cos, sin = _TURNS[rotate]
    # The ink runs away from the origin along the baseline, and the glyphs
    # stand on the side opposite the turned downward axis.
    if cos:
        assert x1 - x0 > 2 * (y1 - y0)
        assert (x0 >= 99) if cos > 0 else (x1 <= 101)
        assert (y1 <= 101) if cos > 0 else (y0 >= 99)
    else:
        assert y1 - y0 > 2 * (x1 - x0)
        assert (y0 >= 99) if sin > 0 else (y1 <= 101)
        assert (x0 >= 99) if sin > 0 else (x1 <= 101)


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_turned_textbox_matches_an_upright_box_of_the_turned_size(rotate: int) -> None:
    text = "A paragraph long enough to wrap onto several lines of a narrow box."
    rect = (40, 20, 100, 180)
    width, height = (60, 160) if rotate == 180 else (160, 60)
    _doc, upright = _new_page(300, 300)
    with upright.draw() as canvas:
        expected = canvas.textbox((0, 0, width, height), text, size=10, align="center")
    _doc, page = _new_page(300, 300)
    with page.draw() as canvas:
        fit = canvas.textbox(rect, text, size=10, align="center", rotate=rotate)
    assert fit.spare_height == pytest.approx(expected.spare_height)
    assert fit.fits
    # Each glyph is the upright box's glyph, moved by the turn that maps
    # the upright box onto ``rect``.
    x0, y0, x1, y1 = rect
    a, b, c, d, e, f = {
        90: (0, 1, -1, 0, x1, y0),
        180: (-1, 0, 0, -1, x1, y1),
        270: (0, -1, 1, 0, x0, y1),
    }[rotate]
    upright_glyphs = [glyph for run in upright.content().text_runs for glyph in run.glyphs]
    glyphs = [glyph for run in page.content().text_runs for glyph in run.glyphs]
    assert len(glyphs) == len(upright_glyphs) > 0
    for glyph, source in zip(glyphs, upright_glyphs, strict=True):
        x, y = source.origin
        assert (glyph.origin.x, glyph.origin.y) == pytest.approx((a * x + c * y + e, b * x + d * y + f), abs=0.01)
        assert _direction(glyph) == pytest.approx((a, b), abs=1e-6)
    for run in page.content().text_runs:
        assert run.bbox.x0 >= x0 - 0.5
        assert run.bbox.x1 <= x1 + 0.5
        assert run.bbox.y0 >= y0 - 0.5
        assert run.bbox.y1 <= y1 + 0.5
    assert page.text().split() == text.split()


def test_turned_textbox_overflow_and_empty_text_measure_across_the_lines() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        text = "Far too much text for this turned box, which wraps onto lines"
        overflow = canvas.textbox((10, 10, 25, 190), text, size=10, rotate=90)
        empty = canvas.textbox((10, 10, 40, 190), "", rotate=270)
    assert not overflow.fits
    assert empty == TextFit(spare_height=30)
    assert list(page.content()) == []


def test_turned_text_survives_a_save() -> None:
    doc, page = _new_page(300, 300)
    with page.draw() as canvas:
        canvas.text("down", (150, 20), size=12, rotate=90)
        canvas.textbox((20, 20, 60, 280), "up the box", size=10, rotate=270)
    before = [(run.text, run.glyphs[0].origin, _direction(run.glyphs[0])) for run in page.content().text_runs]
    reopened = pyropdf.open(doc.to_bytes()).pages[0]
    after = [(run.text, run.glyphs[0].origin, _direction(run.glyphs[0])) for run in reopened.content().text_runs]
    assert [item[0] for item in after] == [item[0] for item in before]
    for (_, origin, direction), (_, expected_origin, expected_direction) in zip(after, before, strict=True):
        assert tuple(origin) == pytest.approx(tuple(expected_origin), abs=1e-3)
        assert direction == pytest.approx(expected_direction, abs=1e-6)
    assert reopened.text() == page.text()


def test_turned_text_needs_no_scope() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        scopes = [canvas.transform(Matrix(1, 0, 0, 1, 1, 0)) for _ in range(16)]
        for scope in scopes:
            scope.__enter__()
        canvas.text("deep", (10, 40), rotate=90)
        canvas.textbox((50, 10, 80, 190), "deep box", rotate=270)
        for scope in reversed(scopes):
            scope.__exit__(None, None, None)
    upright, boxed = page.content().text_runs
    assert _direction(upright.glyphs[0]) == pytest.approx((0, 1), abs=1e-6)
    assert upright.glyphs[0].origin.x == pytest.approx(26, abs=0.01)
    assert _direction(boxed.glyphs[0]) == pytest.approx((0, -1), abs=1e-6)


@pytest.mark.parametrize("rotate", [45, 1, True, 90.0, "90"])
def test_text_turns_only_by_right_angles(rotate: object) -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        with pytest.raises(ValueError, match="multiple of 90"):
            canvas.text("x", (10, 10), rotate=rotate)  # ty: ignore[invalid-argument-type]
        with pytest.raises(ValueError, match="multiple of 90"):
            canvas.textbox((10, 10, 50, 50), "x", rotate=rotate)  # ty: ignore[invalid-argument-type]
    assert list(page.content()) == []


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_text_uses_display_space_at_every_page_rotation(rotation: int) -> None:
    _doc, page = _new_page(200, 300)
    page.rotation = rotation
    with page.draw() as canvas:
        canvas.text("Upright", (20, 50), size=12)
    (run,) = page.content().text_runs
    assert (run.glyphs[0].origin.x, run.glyphs[0].origin.y) == pytest.approx((20, 50), abs=0.01)
    assert _direction(run.glyphs[0]) == pytest.approx((1, 0), abs=1e-6)
    (word,) = page.words()
    assert word.text == "Upright"
    assert abs(word.bbox.x0 - 20) < 2
    assert word.bbox.y0 < 50 < word.bbox.y1


def test_text_is_extractable_at_position() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new()
    with page.draw() as canvas:
        canvas.text("Confidential", (50, 100), size=12)
    words = page.words()
    assert [w.text for w in words] == ["Confidential"]
    x0, y0, _, y1 = words[0].bbox
    assert abs(x0 - 50) < 2
    assert y0 < 100 < y1


def test_text_lines_stack_downward() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new()
    with page.draw() as canvas:
        canvas.text("First\nSecond", (50, 100), size=10)
    words = {w.text: w for w in page.words()}
    assert words["Second"].bbox.y0 > words["First"].bbox.y0


def test_text_on_rotated_page_reads_upright() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((100, 200))
    page.rotation = 90
    with page.draw() as canvas:
        canvas.text("Rotated", (20, 50))
    pix = page.pixmap(dpi=72)
    assert (pix.width, pix.height) == (200, 100)
    words = page.words()
    assert [w.text for w in words] == ["Rotated"]
    assert abs(words[0].bbox.x0 - 20) < 2
    assert words[0].bbox.y0 < 50 < words[0].bbox.y1
    assert page.search("Rotated")


@pytest.mark.parametrize("underlay", [False, True])
def test_text_overlay_order(underlay: bool) -> None:  # noqa: FBT001 - parametrized.
    _doc, page = _green_page()
    with page.draw(underlay=underlay) as canvas:
        canvas.text("TEXT", (10, 55), size=48, fill=RED)
    # Text drawn below the green rectangle is hidden by it.
    assert (_rgb(page) == {0, 128}) is underlay


def test_textbox_underlay_stays_below_existing_content() -> None:
    _doc, page = _green_page()
    with page.draw(underlay=True) as canvas:
        canvas.textbox((5, 5, 155, 75), "TEXT", size=48, fill=RED)
    assert _rgb(page) == {0, 128}


def test_textbox_wraps_aligns_and_reports_spare_height() -> None:
    _doc, page = _new_page(200, 120)
    rect = (10, 10, 110, 90)
    with page.draw() as canvas:
        spare = canvas.textbox(rect, "one two three four five six", align="right", size=10).spare_height
    assert spare > 0
    lines = page.layout().blocks[0].lines
    assert len(lines) == 2
    assert " ".join(span.text for span in lines[0].spans) == "one two three four five"
    assert lines[0].bbox.x1 == pytest.approx(rect[2], abs=0.1)
    assert lines[1].bbox.x1 == pytest.approx(rect[2], abs=0.1)
    assert all(rect[0] <= line.bbox.x0 < line.bbox.x1 <= rect[2] + 0.1 for line in lines)
    assert all(rect[1] <= line.bbox.y0 < line.bbox.y1 <= rect[3] for line in lines)


def test_textbox_justifies_only_soft_wrapped_lines() -> None:
    _doc, page = _new_page(200, 120)
    rect = (10, 10, 110, 90)
    with page.draw() as canvas:
        canvas.textbox(rect, "one two three four five six", align="justify", size=10)
    first, last = page.layout().blocks[0].lines
    assert first.bbox.x0 == pytest.approx(rect[0], abs=0.1)
    assert first.bbox.x1 == pytest.approx(rect[2], abs=0.1)
    assert last.bbox.x0 == pytest.approx(rect[0], abs=0.1)
    assert last.bbox.x1 < rect[2] - 20


def test_textbox_overflow_is_non_drawing_and_empty_text_is_free() -> None:
    doc, page = _new_page(200, 100)
    before = _objects(doc)
    with page.draw() as canvas:
        overflow = canvas.textbox((10, 10, 110, 15), "one two three four", size=10)
        empty = canvas.textbox((10, 10, 110, 25), "", size=10)
    assert overflow.spare_height < 0
    assert empty == TextFit(spare_height=15)
    assert page.text() == ""
    assert _objects(doc) == before


def test_textbox_tabs_custom_leading_and_bad_arguments() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        first = canvas.textbox((10, 10, 150, 80), "a\tb\nc", size=10, line_height=1.0).spare_height
    assert first > 0
    # Each tab becomes one space.
    assert page.text().splitlines() == ["a b", "c"]
    with pytest.raises(ValueError, match="align"), page.draw() as canvas:
        canvas.textbox((10, 10, 100, 80), "x", align="middle")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="line_height"), page.draw() as canvas:
        canvas.textbox((10, 10, 100, 80), "x", line_height=0)


def test_text_survives_save_roundtrip() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new()
    with page.draw() as canvas:
        canvas.text("Persistent", (72, 72))
    reopened = pyropdf.open(doc.to_bytes())
    assert "Persistent" in reopened.pages[0].text()


def test_standard_14_text_is_written_in_hexadecimal() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new()
    with page.draw() as canvas:
        canvas.text("\x01(A)\\€ /PyroF1 ", (10, 20))
    content = b"".join(stream.read() for stream in page.obj["Contents"])
    assert b"<" + "\x01(A)\\€ /PyroF1 ".encode("cp1252").hex().encode() + b"> Tj" in content
    data = doc.to_bytes()
    assert pyropdf.open(data).pages[0].text(unmapped="replacement").strip() == "\ufffd(A)\\€ /PyroF1"


def test_text_page_numbering_recipe() -> None:
    doc = pyropdf.Document()
    for _ in range(3):
        doc.pages.new((200, 100))
    for number, page in enumerate(doc.pages, start=1):
        with page.draw() as canvas:
            canvas.text(f"Page {number} of {len(doc.pages)}", (10, 90), size=9)
    assert [page.text() for page in doc.pages] == [f"Page {n} of 3\n" for n in (1, 2, 3)]


def test_text_refuses_bad_arguments() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new()
    with pytest.raises(ValueError, match="PostScript name"):
        pyropdf.Font.standard("nosuch")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="size"), page.draw() as canvas:
        canvas.text("x", (50, 50), size=0)
    with pytest.raises(ValueError, match="from 0 to 1"), page.draw() as canvas:
        canvas.text("x", (50, 50), fill=Color(2.0, 0.0, 0.0))
    with pytest.raises(TypeError, match="font"), page.draw() as canvas:
        canvas.text("x", (50, 50), font="Helvetica")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="text"), page.draw() as canvas:
        canvas.text(b"x", (50, 50))  # ty: ignore[invalid-argument-type]
    assert page.text() == ""


def _draw_generated_text(method: str, page: pyropdf.Page, text: str, **options: Any) -> TextFit | None:  # noqa: ANN401
    """Draw text with ``Canvas.text`` or ``Canvas.textbox``, as ``method`` names."""
    with page.draw() as canvas:
        if method == "text":
            canvas.text(text, (10, 20), **options)
            return None
        return canvas.textbox((10, 10, 100, 80), text, **options)


@pytest.mark.parametrize("method", ["text", "textbox"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_generated_text_validates_text_budget(method: str, value: object) -> None:
    _doc, page = _new_page()
    with pytest.raises((TypeError, ValueError), match="max_text_size"):
        _draw_generated_text(method, page, "x", max_text_size=value)


def test_text_bounds_utf8_input_atomically() -> None:
    doc, page = _new_page()
    before = _objects(doc)
    with pytest.raises(pyropdf.LimitError) as caught, page.draw() as canvas:
        canvas.text("é", (10, 20), max_text_size=1)
    assert caught.value.code == "text_input_size"
    assert _objects(doc) == before
    assert page.text() == ""

    with page.draw() as canvas:
        canvas.text("é", (10, 20), max_text_size=2)
        canvas.text("unbounded", (10, 40), max_text_size=None)
    assert page.text().splitlines() == ["é", "unbounded"]


@pytest.mark.parametrize("method", ["text", "textbox"])
def test_generated_text_bounds_physical_lines_atomically(method: str) -> None:
    doc, page = _new_page()
    before = _objects(doc)
    # 4,097 lines, both for text, which splits text with str.splitlines(), and
    # for textboxes, which split it at line breaks.
    text = "\n" * 4096 + "x"
    with pytest.raises(pyropdf.LimitError) as caught:
        _draw_generated_text(method, page, text, max_text_size=len(text))
    assert caught.value.code == "text_line_count"
    assert _objects(doc) == before
    assert page.text() == ""


def test_generated_text_line_limit_boundary_and_opt_out_do_not_draw_on_overflow() -> None:
    bounded, page = _new_page()
    before = _objects(bounded)
    fit = _draw_generated_text("textbox", page, "\n" * 4095, max_text_size=4095)
    assert fit is not None
    assert fit.spare_height < 0
    assert _objects(bounded) == before

    unbounded, page = _new_page()
    before = _objects(unbounded)
    fit = _draw_generated_text("textbox", page, "\n" * 4096, max_text_size=None)
    assert fit is not None
    assert fit.spare_height < 0
    assert _objects(unbounded) == before


def test_textbox_bounds_soft_wrapped_lines_atomically() -> None:
    doc, page = _new_page()
    before = _objects(doc)
    text = "a" * 4097
    with pytest.raises(pyropdf.LimitError) as caught, page.draw() as canvas:
        canvas.textbox((10, 10, 16, 80), text, size=10, max_text_size=len(text))
    assert caught.value.code == "text_line_count"
    assert _objects(doc) == before
    assert page.text() == ""


def test_textbox_bounds_text_before_expanding_tabs() -> None:
    rejected, page = _new_page()
    before = _objects(rejected)
    with pytest.raises(pyropdf.LimitError) as caught, page.draw() as canvas:
        canvas.textbox((10, 10, 150, 80), "ab\tcd", max_text_size=4)
    assert caught.value.code == "text_input_size"
    assert _objects(rejected) == before

    _accepted, page = _new_page()
    fit = _draw_generated_text("textbox", page, "a\tb", max_text_size=3)
    assert fit is not None
    assert fit.fits
    assert page.text().strip() == "a b"


def test_textbox_wide_single_line_preserves_content() -> None:
    _doc, page = _new_page(1_000_000, 100)
    text = "word " * 10_000
    with page.draw() as canvas:
        spare = canvas.textbox((10, 10, 999_990, 90), text, max_text_size=len(text)).spare_height
    assert spare > 0
    assert page.text().split() == ["word"] * 10_000


def test_textbox_dense_break_indexes_preserve_content() -> None:
    _doc, page = _new_page(220, 2_500)
    text = " ".join(["a"] * 2_048)
    with page.draw() as canvas:
        spare = canvas.textbox((10, 10, 210, 2_490), text, size=10, max_text_size=len(text)).spare_height
    assert spare > 0
    assert page.text().split() == ["a"] * 2_048


def test_drawing_text_is_one_transaction_and_one_undo_step() -> None:
    doc, page = _new_page()
    with page.draw() as canvas:
        canvas.text("Undone", (20, 40))
        canvas.textbox((20, 60, 200, 120), "Boxed too")
    assert page.text().split() == ["Undone", "Boxed", "too"]
    pixmap = page.pixmap(dpi=72)
    assert bytes(pixmap) != bytes(pyropdf.Document().pages.new((300, 200)).pixmap(dpi=72))
    doc.history.undo()
    assert page.text() == ""
