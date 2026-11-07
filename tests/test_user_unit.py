"""A page's ``/UserUnit`` scales its display space to points (yhay81/pyropdf#729).

Display coordinates are points of 1/72 inch. A page whose MediaBox is 400
by 300 with ``/UserUnit 2`` is 800 by 600 points: its rectangle and boxes,
its text and paths, clips, annotations, links, canvas, and OCR layer use
those points, and it renders 800 by 600 pixels at 72 dpi. The tests run
each case with unit 1 and unit 2.
"""

from __future__ import annotations

import pytest
from test_pages import build_raw_pdf

import pyropdf
from pyropdf import Color, Pixmap, Rect, TextRun

# In user space: a red line along y=150 from x=150 to 250, a blue rectangle
# from (20, 20) to (60, 50), and "Hello" from (100, 200).
CONTENT = "1 0 0 RG 2 w 150 150 m 250 150 l S\n0 0 1 rg 20 20 40 30 re f\nBT /F1 12 Tf 100 200 Td (Hello) Tj ET"

UNITS = pytest.mark.parametrize("unit", [1, 2])


def page_pdf(user_unit: str | None, *, rotate: int = 0, parent: str = "") -> bytes:
    """Return one page with a ``[0 0 400 300]`` MediaBox whose ``/UserUnit`` is ``user_unit``.

    Object 6 holds the number 2, for a unit given by reference.
    """
    own = "" if user_unit is None else f" /UserUnit {user_unit}"
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: f"<< /Type /Pages /Kids [3 0 R] /Count 1{parent} >>",
            3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Rotate {rotate}{own}"
            " /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            4: f"<< /Length {len(CONTENT)} >>\nstream\n{CONTENT}\nendstream",
            5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            6: "2",
        }
    )


def open_page(unit: float, *, rotate: int = 0) -> pyropdf.Page:
    return pyropdf.open(page_pdf(str(unit), rotate=rotate)).pages[0]


def pixel(pixmap: Pixmap, x: float, y: float) -> tuple[int, ...]:
    data = bytes(memoryview(pixmap))
    at = int(y) * pixmap.stride + int(x) * pixmap.channels
    return tuple(data[at : at + 3])


def scaled(values: Rect | tuple[float, ...], factor: float) -> tuple[float, ...]:
    return tuple(value * factor for value in values)


def svg_root(svg: str) -> str:
    start = svg.index("<svg")
    return svg[start : svg.index(">", start)]


@UNITS
def test_the_page_and_its_boxes_are_in_points(unit: int) -> None:
    page = open_page(unit)

    assert page.rect == (0, 0, 400 * unit, 300 * unit)
    assert page.media_box == page.rect
    assert page.crop_box == page.rect
    assert page.trim_box == page.rect


@UNITS
def test_a_render_at_72_dpi_has_a_pixel_a_point(unit: int) -> None:
    page = open_page(unit)
    pixmap = page.pixmap()

    assert (pixmap.width, pixmap.height) == (400 * unit, 300 * unit)
    assert pixel(pixmap, 200 * unit, 150 * unit) == (255, 0, 0)
    assert pixel(pixmap, 40 * unit, 265 * unit) == (0, 0, 255)
    doubled = page.pixmap(dpi=144)
    assert (doubled.width, doubled.height) == (800 * unit, 600 * unit)


@UNITS
def test_a_clip_is_in_points(unit: int) -> None:
    page = open_page(unit)
    clipped = page.pixmap(clip=(150 * unit, 140 * unit, 250 * unit, 160 * unit))

    assert (clipped.width, clipped.height) == (100 * unit, 20 * unit)
    assert pixel(clipped, 50 * unit, 10 * unit) == (255, 0, 0)
    words = page.words(clip=(90 * unit, 80 * unit, 140 * unit, 105 * unit))
    assert [word.text for word in words] == ["Hello"]


@UNITS
def test_the_svg_is_as_many_points_wide(unit: int) -> None:
    root = svg_root(open_page(unit).svg())

    assert 'viewBox="0 0 400 300"' in root
    assert f'width="{400 * unit}" height="{300 * unit}"' in root


@UNITS
def test_a_rotated_page_turns_its_points(unit: int) -> None:
    page = open_page(unit, rotate=90)
    pixmap = page.pixmap()

    assert page.rect == (0, 0, 300 * unit, 400 * unit)
    assert (pixmap.width, pixmap.height) == (300 * unit, 400 * unit)
    # The line's middle, user (200, 150), turns to display (150, 200).
    assert pixel(pixmap, 150 * unit, 200 * unit) == (255, 0, 0)
    assert page.chars()[0].origin == pytest.approx((200 * unit, 100 * unit))


def test_text_and_paths_scale_with_the_unit() -> None:
    one, two = open_page(1), open_page(2)

    assert two.chars()[0].origin == pytest.approx((200, 200))
    (run_one,) = one.content(paths=False, images=False)
    (run_two,) = two.content(paths=False, images=False)
    assert isinstance(run_one, TextRun)
    assert isinstance(run_two, TextRun)
    assert run_two.size == pytest.approx(2 * run_one.size) == pytest.approx(24)
    words = list(zip(one.words(), two.words(), strict=True))
    assert [(a.text, b.text) for a, b in words] == [("Hello", "Hello")]
    for a, b in words:
        assert tuple(b.bbox) == pytest.approx(scaled(a.bbox, 2))
    for a, b in zip(one.search("Hello"), two.search("Hello"), strict=True):
        assert tuple(b) == pytest.approx(scaled(a, 2))
    paths = list(zip(one.content(text_runs=False), two.content(text_runs=False), strict=True))
    assert len(paths) == 2
    for a, b in paths:
        assert tuple(b.bbox) == pytest.approx(scaled(a.bbox, 2))


@UNITS
def test_a_box_set_in_points_writes_user_space(unit: int) -> None:
    page = open_page(unit)
    page.crop_box = (0, 0, 200 * unit, 150 * unit)

    assert list(page.obj["CropBox"]) == [0, 150, 200, 300]
    assert page.rect == (0, 0, 200 * unit, 150 * unit)
    assert page.pixmap().width == 200 * unit


def test_a_search_hit_highlights_the_same_text_at_any_unit() -> None:
    quad_points = []
    for unit in (1, 2):
        page = open_page(unit)
        hits = page.search("Hello")
        page.annotations.add(pyropdf.Highlight(hits))
        (annotation,) = page.annotations
        assert annotation.rect.contains(hits[0])
        quad_points.append([float(value) for value in annotation.obj["QuadPoints"]])

    assert quad_points[0] == pytest.approx(quad_points[1])


@UNITS
def test_a_link_takes_and_gives_points(unit: int) -> None:
    page = open_page(unit)
    link = page.links.add((10, 10, 50, 30), pyropdf.Destination(0, point=(100 * unit, 50 * unit)))

    assert tuple(link.rect) == pytest.approx((10, 10, 50, 30))
    assert [float(value) for value in link.obj["Rect"]] == pytest.approx(
        [10 / unit, 300 - 30 / unit, 50 / unit, 300 - 10 / unit]
    )
    assert [float(value) for value in link.obj["Dest"][2:4]] == pytest.approx([100, 250])
    target = link.target
    assert isinstance(target, pyropdf.Destination)
    assert target.point == pytest.approx((100 * unit, 50 * unit))


@UNITS
def test_a_canvas_draws_in_points(unit: int) -> None:
    page = open_page(unit)
    with page.draw() as canvas:
        canvas.rect((300, 20, 340, 60), fill=Color(0, 1, 0))

    assert tuple(page.content(text_runs=False)[-1].bbox) == pytest.approx((300, 20, 340, 60))
    assert pixel(page.pixmap(), 320, 40) == (0, 255, 0)


def test_a_placed_page_fits_its_points_whatever_its_unit() -> None:
    drawn = []
    for unit in (1, 2):
        source = open_page(unit)
        target = pyropdf.open(page_pdf(None)).pages[0]
        with target.draw() as canvas:
            canvas.page((200, 0, 400, 150), source)
        drawn.append([value for item in target.content(text_runs=False) for value in item.bbox])

    assert drawn[1] == pytest.approx(drawn[0])
    # The source's line, at half size from (275, 75) to (325, 75), and its rectangle.
    assert drawn[0][8:] == pytest.approx([275, 75, 325, 75, 210, 125, 230, 140])


def test_an_ocr_layer_in_points_scales_with_the_unit() -> None:
    found = []
    for unit in (1, 2):
        page = open_page(unit)
        box = Rect(250 * unit, 20 * unit, 330 * unit, 40 * unit)
        page.apply_ocr(words=[pyropdf.OcrWord(text="Scaled", bbox=box, confidence=1.0)], skip_existing=False)
        (hit,) = page.search("Scaled")
        assert hit.intersects(box)
        found.append(tuple(hit))

    assert found[1] == pytest.approx(scaled(found[0], 2))


def test_setting_the_unit_rescales_what_was_read() -> None:
    doc = pyropdf.open(page_pdf(None))
    page = doc.pages[0]
    before = page.words()[0].bbox
    assert page.rect == (0, 0, 400, 300)

    page.obj["UserUnit"] = 2

    assert page.rect == (0, 0, 800, 600)
    assert tuple(page.words()[0].bbox) == pytest.approx(scaled(before, 2))
    assert page.pixmap().width == 800
    doc.history.undo()
    assert page.rect == (0, 0, 400, 300)


def test_removing_the_rotation_keeps_the_points() -> None:
    page = open_page(2, rotate=90)
    rect, words = page.rect, [value for word in page.words() for value in word.bbox]

    page.remove_rotation()

    assert page.rotation == 0
    assert page.rect == rect
    assert [value for word in page.words() for value in word.bbox] == pytest.approx(words)


@pytest.mark.parametrize(("value", "unit"), [("6 0 R", 2), ("0.5", 0.5), ("2.5", 2.5)])
def test_a_positive_unit_scales_however_it_is_written(value: str, unit: float) -> None:
    page = pyropdf.open(page_pdf(value)).pages[0]

    assert page.rect == (0, 0, 400 * unit, 300 * unit)
    assert page.pixmap().width == 400 * unit
    assert f'width="{400 * unit:g}"' in svg_root(page.svg())


@pytest.mark.parametrize("value", ["0", "-2", "/Two", "(2)", "[2]", "null", "7 0 R"])
def test_a_unit_that_is_no_positive_number_is_one(value: str) -> None:
    page = pyropdf.open(page_pdf(value)).pages[0]

    assert page.rect == (0, 0, 400, 300)
    assert page.pixmap().width == 400
    assert 'width="400" height="300"' in svg_root(page.svg())


def test_a_page_does_not_inherit_a_unit() -> None:
    page = pyropdf.open(page_pdf(None, parent=" /UserUnit 2")).pages[0]

    assert page.rect == (0, 0, 400, 300)
    assert page.pixmap().width == 400
