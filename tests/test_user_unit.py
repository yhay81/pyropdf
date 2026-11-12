"""A page's ``/UserUnit`` scales its display space to points (yhay81/pyropdf#729).

Display coordinates are points of 1/72 inch. A page whose MediaBox is 400
by 300 with ``/UserUnit 2`` is 800 by 600 points: its rectangle and boxes,
its text and paths, clips, annotations, links, canvas, and OCR layer use
those points, and it renders 800 by 600 pixels at 72 dpi. The tests run
each case with unit 1 and unit 2.
"""

from __future__ import annotations

import pytest
from test_acroform import NOTO_SANS_HEBREW
from test_pages import build_raw_pdf

import pyropdf
from pyropdf import Color, Pixmap, Rect, TextRun

# In user space: a red line along y=150 from x=150 to 250, a blue rectangle
# from (20, 20) to (60, 50), and "Hello" from (100, 200).
CONTENT = "1 0 0 RG 2 w 150 150 m 250 150 l S\n0 0 1 rg 20 20 40 30 re f\nBT /F1 12 Tf 100 200 Td (Hello) Tj ET"

UNITS = pytest.mark.parametrize("unit", [1, 2])


def page_pdf(
    user_unit: str | None, *, rotate: int = 0, parent: str = "", references: dict[int, str] | None = None
) -> bytes:
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
            **(references or {}),
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


@pytest.mark.parametrize("value", ["0", "-2", "/Two", "(2)", "[2]", "<< /Unused [2] >>", "null", "7 0 R"])
def test_a_unit_that_is_no_positive_number_is_one(value: str) -> None:
    page = pyropdf.open(page_pdf(value)).pages[0]

    assert page.rect == (0, 0, 400, 300)
    assert page.pixmap().width == 400
    assert 'width="400" height="300"' in svg_root(page.svg())


def test_a_page_does_not_inherit_a_unit() -> None:
    page = pyropdf.open(page_pdf(None, parent=" /UserUnit 2")).pages[0]

    assert page.rect == (0, 0, 400, 300)
    assert page.pixmap().width == 400


def test_a_chain_resolves_to_the_same_unit_for_every_engine() -> None:
    doc = pyropdf.open(page_pdf("6 0 R", references={6: "7 0 R", 7: "2"}))
    page = doc.pages[0]
    for unit in (2, 3):
        if unit == 3:
            doc.objects[7] = unit
        assert page.rect == (0, 0, 400 * unit, 300 * unit)
        pixmap = page.pixmap()
        assert pixmap.width == 400 * unit
        assert doc.render([0]) == [pixmap.to_bytes()]
        assert f'width="{400 * unit}"' in svg_root(page.svg())
        assert page.chars()[0].origin == pytest.approx((100 * unit, 100 * unit))
        assert tuple(page.content().paths[0].bbox) == pytest.approx(scaled((150, 150, 250, 150), unit))


def test_scaled_page_boxes_follow_indirect_coordinates() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /CropBox [0 0 5 0 R 60] >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 4 0 R 100] /UserUnit 2 /Resources <<>> >>",
                4: "200",
                5: "150",
            }
        )
    )
    page = doc.pages[0]
    assert page.media_box.width == 400
    for width in (300, 240):
        if width == 240:
            doc.objects[5] = 120
        assert page.crop_box == page.rect == (0, 0, width, 120)
        assert page.pixmap().width == width
        assert f'width="{width}" height="120"' in svg_root(page.svg())


@pytest.mark.parametrize("unit", [750, 30_000])
def test_the_canvas_and_its_pattern_keep_the_inverse_unit(unit: int) -> None:
    gradient = pyropdf.LinearGradient(
        start=pyropdf.Point(10, 10),
        end=pyropdf.Point(50, 10),
        stops=(pyropdf.ColorStop(0, Color(1, 0, 0)), pyropdf.ColorStop(1, Color(0, 0, 1))),
    )
    rasters = []
    for size, factor in [(1, unit), (unit, 1)]:
        doc = pyropdf.Document()
        page = doc.pages.new((size, size))
        page.obj["UserUnit"] = factor
        with page.draw() as canvas:
            canvas.rect((10, 10, 50, 50), fill=gradient)
        saved = pyropdf.open(doc.to_bytes()).pages[0]
        assert tuple(saved.content().paths[0].bbox) == pytest.approx((10, 10, 50, 50))
        pixmap = saved.pixmap(clip=(0, 0, 64, 64))
        assert pixel(pixmap, 20, 20) != pixel(pixmap, 40, 20)
        rasters.append(bytes(memoryview(pixmap)))
    assert rasters[0] == rasters[1]


def test_a_one_point_crop_can_be_less_than_one_user_unit() -> None:
    page = open_page(2)
    page.crop_box = (0, 0, 1, 100)
    assert page.rect == page.crop_box == (0, 0, 1, 100)
    assert page.pixmap().width == 1


@pytest.mark.parametrize(
    ("rotation", "size", "view_box"),
    [
        (0, (2, 3), "0 0.25 0.5 0.75"),
        (90, (3, 2), "0 0 0.75 0.5"),
        (180, (2, 3), "0.5 0 0.5 0.75"),
        (270, (3, 2), "0.25 0.5 0.75 0.5"),
    ],
)
def test_a_fractional_crop_has_the_same_visible_origin_after_rotation(
    rotation: int, size: tuple[int, int], view_box: str
) -> None:
    page = open_page(4, rotate=rotation)
    page.obj["CropBox"] = [10, 20, 10.5, 20.75]
    assert page.rect == page.crop_box == (0, 0, *size)
    with page.draw() as canvas:
        canvas.rect((0, 0, 1, 1), fill=Color(0, 1, 0))
    pixmap = page.pixmap()
    assert (pixmap.width, pixmap.height) == size
    assert pixel(pixmap, 0, 0) == (0, 255, 0)
    assert f'viewBox="{view_box}"' in svg_root(page.svg())


def test_the_last_pixel_of_a_decimal_page_is_visible() -> None:
    page = open_page(100)
    page.obj["MediaBox"] = [0, 0, 520.1, 1]
    assert page.rect.width == 52_010
    assert page.pixmap().width == 52_010
    assert page.pixmap(clip=(52_009, 0, 52_010, 1)).width == 1
    svg = page.svg()
    assert 'width="52010" height="100"' in svg_root(svg)
    assert page.svg(max_size=len(svg.encode())) == svg
    with pytest.raises(pyropdf.LimitError) as refused:
        page.svg(max_size=len(svg.encode()) - 1)
    assert refused.value.code == "svg_output_size"


def _same_display_pages() -> tuple[pyropdf.Page, pyropdf.Page]:
    """Return a unit-1 ``[0 0 800 600]`` page and a unit-2 ``[0 0 400 300]`` page."""
    one_doc = pyropdf.Document()
    one = one_doc.pages.new((800, 600))
    two_doc = pyropdf.Document()
    two = two_doc.pages.new((400, 300))
    two.obj["UserUnit"] = 2
    assert one.rect == two.rect == (0, 0, 800, 600)
    return one, two


def _assert_rasters_close(first: Pixmap, second: Pixmap, *, max_channel_diff: int = 2) -> None:
    assert (first.width, first.height) == (second.width, second.height)
    diff = max(abs(a - b) for a, b in zip(bytes(memoryview(first)), bytes(memoryview(second)), strict=True))
    assert diff <= max_channel_diff


def test_annotation_lengths_render_the_same_pixels_at_72_dpi() -> None:
    one, two = _same_display_pages()
    specs: list[pyropdf.AnnotationSpecification] = [
        pyropdf.Square((40, 40, 240, 160), border=pyropdf.Border(width=3, dashes=(6, 3))),
        pyropdf.FreeText(
            (300, 40, 600, 140),
            "Scaled free text",
            size=12,
            border=pyropdf.Border(width=2, dashes=(4, 2)),
        ),
        pyropdf.FreeText(
            (300, 200, 600, 300),
            "Callout note",
            size=12,
            border=pyropdf.Border(width=2),
            callout=((220, 250), (300, 250)),
            callout_ending=pyropdf.LineEnding.OPEN_ARROW,
        ),
        pyropdf.LineAnnotation(
            (40, 240),
            (180, 240),
            border=pyropdf.Border(width=2, dashes=(6, 2)),
            line_endings=(pyropdf.LineEnding.NONE, pyropdf.LineEnding.CLOSED_ARROW),
        ),
        pyropdf.Polygon(
            ((40, 340), (180, 340), (180, 460), (40, 460)),
            border=pyropdf.Border(width=2),
            clouds=1.5,
        ),
    ]
    for page in (one, two):
        for spec in specs:
            page.annotations.add(spec)

    assert two.annotations[1].obj["DA"].text == "/Helv 6 Tf 0 0 0 rg"
    assert two.annotations[1].text().strip() == "Scaled free text"
    for a, b in zip(one.annotations, two.annotations, strict=True):
        assert tuple(a.rect) == pytest.approx(tuple(b.rect))
    _assert_rasters_close(one.pixmap(), two.pixmap())

    # Render-time synthesis without stored /AP draws the same display geometry.
    for page in (one, two):
        for annot in page.annotations:
            del annot.obj["AP"]
    _assert_rasters_close(one.pixmap(), two.pixmap())
    _assert_rasters_close(one.annotations[0].pixmap(), two.annotations[0].pixmap())


def test_border_width_and_dashes_round_trip_in_points() -> None:
    one, two = _same_display_pages()
    solid = pyropdf.Border(width=2)
    annot_one = one.annotations.add(pyropdf.Square((20, 20, 120, 80), border=solid))
    annot_two = two.annotations.add(pyropdf.Square((20, 20, 120, 80), border=solid))

    assert float(annot_one.obj["BS"]["W"]) == pytest.approx(2)
    assert float(annot_two.obj["BS"]["W"]) == pytest.approx(1)
    assert annot_one.border == annot_two.border == solid

    dashed = pyropdf.Border(width=3, dashes=(6, 3))
    annot_one.border = dashed
    annot_two.border = dashed

    assert float(annot_one.obj["BS"]["W"]) == pytest.approx(3)
    assert [float(d) for d in annot_one.obj["BS"]["D"]] == pytest.approx([6, 3])
    assert float(annot_two.obj["BS"]["W"]) == pytest.approx(1.5)
    assert [float(d) for d in annot_two.obj["BS"]["D"]] == pytest.approx([3, 1.5])
    assert annot_one.border == annot_two.border == dashed

    # Legacy /Border and omitted /BS defaults also convert from user space to points.
    del annot_two.obj["BS"]
    annot_two.obj["Border"] = [0, 0, 1.5, [3, 1]]
    assert annot_two.border == pyropdf.Border(width=3, dashes=(6, 2))
    del annot_two.obj["Border"]
    annot_two.obj["BS"] = {pyropdf.Name("S"): pyropdf.Name("D")}
    assert annot_two.border == pyropdf.Border(width=2, dashes=(6,))


def test_widget_font_size_and_border_scale_by_each_widgets_page_unit() -> None:
    doc = pyropdf.Document()
    page_one = doc.pages.new((800, 600))
    page_two = doc.pages.new((400, 300))
    page_two.obj["UserUnit"] = 2

    border = pyropdf.Border(width=2, dashes=(6, 4))
    w1 = page_one.widgets.add(
        pyropdf.TextField("shared", (40, 40, 300, 80), value="Hello", font_size=12, border=border)
    )
    w2 = page_two.widgets.add(
        pyropdf.TextField("shared", (40, 40, 300, 80), value="Hello", font_size=12, border=border)
    )
    f1 = page_one.widgets.add(
        pyropdf.TextField("fitted", (40, 120, 300, 160), value="Auto", font_size=0, border=pyropdf.Border(width=2))
    )
    f2 = page_two.widgets.add(
        pyropdf.TextField("fitted", (40, 120, 300, 160), value="Auto", font_size=0, border=pyropdf.Border(width=2))
    )
    page_one.widgets.add(pyropdf.CheckBox("agreed", (40, 200, 70, 230), checked=True, border=pyropdf.Border(width=2)))
    page_two.widgets.add(pyropdf.CheckBox("agreed", (40, 200, 70, 230), checked=True, border=pyropdf.Border(width=2)))
    c1 = page_one.widgets.add(
        pyropdf.ComboBox(
            "combo", (340, 40, 600, 80), choices=("Alpha", "Beta"), value="Alpha", font_size=12, border=border
        )
    )
    c2 = page_two.widgets.add(
        pyropdf.ComboBox(
            "combo", (340, 40, 600, 80), choices=("Alpha", "Beta"), value="Alpha", font_size=12, border=border
        )
    )
    l1 = page_one.widgets.add(
        pyropdf.ListBox(
            "listbox", (340, 120, 600, 220), choices=("Alpha", "Beta"), value="Beta", font_size=12, border=border
        )
    )
    l2 = page_two.widgets.add(
        pyropdf.ListBox(
            "listbox", (340, 120, 600, 220), choices=("Alpha", "Beta"), value="Beta", font_size=12, border=border
        )
    )
    p1 = page_one.widgets.add(
        pyropdf.PushButton("submit", (340, 260, 500, 300), caption="Submit", font_size=12, border=border)
    )
    p2 = page_two.widgets.add(
        pyropdf.PushButton("submit", (340, 260, 500, 300), caption="Submit", font_size=12, border=border)
    )

    assert w1.obj["DA"].text == "/Helv 12 Tf 0 0 0 rg"
    assert w2.obj["DA"].text == "/Helv 6 Tf 0 0 0 rg"
    assert c2.obj["DA"].text == "/Helv 6 Tf 0 0 0 rg"
    assert l2.obj["DA"].text == "/Helv 6 Tf 0 0 0 rg"
    assert p2.obj["DA"].text == "/Helv 6 Tf 0 0 0 rg"
    assert float(w1.obj["BS"]["W"]) == pytest.approx(2)
    assert float(w2.obj["BS"]["W"]) == pytest.approx(1)
    assert [float(d) for d in w2.obj["BS"]["D"]] == pytest.approx([3, 2])
    assert w1.font_size == w2.font_size == c1.font_size == c2.font_size == pytest.approx(12)
    assert l1.font_size == l2.font_size == p1.font_size == p2.font_size == pytest.approx(12)
    assert [w.font_size for w in doc.form["shared"].widgets] == pytest.approx([12, 12])
    assert w1.border == w2.border == border
    assert f1.font_size == f2.font_size == 0
    _assert_rasters_close(page_one.pixmap(), page_two.pixmap())

    # Without /P on the unit-2 widget, filling still resolves its page through /Annots.
    del f2.obj["P"]
    doc.form["fitted"].value = "Refilled without /P"
    _assert_rasters_close(page_one.pixmap(), page_two.pixmap())

    # Editing another style property preserves the scaled /DA size; editing font_size rescales it.
    w2.text_color = Color(1, 0, 0)
    assert w2.obj["DA"].text == "/Helv 6 Tf 1 0 0 rg"
    assert w2.font_size == pytest.approx(12)
    w1.text_color = Color(1, 0, 0)
    w1.font_size = 16
    w2.font_size = 16
    assert w2.obj["DA"].text == "/Helv 8 Tf 1 0 0 rg"
    assert [w.font_size for w in doc.form["shared"].widgets] == pytest.approx([16, 16])

    doc.form.font = pyropdf.Font.from_file(NOTO_SANS_HEBREW)
    doc.form["shared"].value = "שלום"
    _assert_rasters_close(page_one.pixmap(), page_two.pixmap())
