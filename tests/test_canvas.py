"""Drawing paths, images, and placed pages with Page.draw (DESIGN.md section 7).

Ported from pylopdf's tests/test_canvas.py and tests/test_draw.py at
ebb01d3c, without text: canvas text in Standard 14 fonts is tested in
test_canvas_text.py and in embedded faces in test_canvas_embedded.py.
"""

from __future__ import annotations

import itertools
import sys
import threading
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import BlendMode, Color, FillRule, Matrix, Name, PathBuilder, Rect, Stroke, _core

if TYPE_CHECKING:
    from pathlib import Path

RED = Color(1, 0, 0)
BLUE = Color(0, 0, 1)
BLACK = Color(0, 0, 0)


def _new_page(width: float = 300, height: float = 200) -> tuple[pyropdf.Document, pyropdf.Page]:
    doc = pyropdf.Document()
    return doc, doc.pages.new((width, height))


def _pixel(page: pyropdf.Page, x: float, y: float) -> tuple[int, int, int]:
    pixmap = page.pixmap(dpi=72)
    samples = memoryview(pixmap).cast("B")
    offset = int(y) * pixmap.stride + int(x) * pixmap.channels
    return tuple(samples[offset : offset + 3])  # ty: ignore[invalid-return-type]


def _assert_rect(actual: Rect, expected: tuple[float, float, float, float]) -> None:
    assert tuple(actual) == pytest.approx(expected, abs=0.01)


def _assert_color(actual: object, expected: Color) -> None:
    assert isinstance(actual, Color)
    channels = ("red", "green", "blue", "alpha")
    assert [getattr(actual, name) for name in channels] == pytest.approx(
        [getattr(expected, name) for name in channels], abs=1 / 255
    )


def _filled(page: pyropdf.Page, color: Color) -> list[pyropdf.Path]:
    return [path for path in page.content().paths if path.fill == color]


def _clipped_to(item: pyropdf.ContentItem, expected: tuple[float, float, float, float]) -> bool:
    return any(tuple(clip.bbox) == pytest.approx(expected, abs=0.01) for clip in item.context.clips)


def _png() -> bytes:
    """Return a 2 by 1 red PNG."""
    _doc, page = _new_page(2, 1)
    with page.draw() as canvas:
        canvas.rect((0, 0, 2, 1), fill=RED)
    return page.pixmap(dpi=72).to_bytes("png")


def _text_page(text: str = "Source page", size: tuple[float, float] = (200, 100)) -> pyropdf.Document:
    """Return a document whose one page shows ``text`` in Helvetica and a red frame."""
    doc, page = _new_page(*size)
    font = {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica")}
    page.obj["Resources"] = {"Font": {"F1": font}}
    page.obj["Contents"] = doc.objects[doc.objects.add_stream(f"BT /F1 14 Tf 20 50 Td ({text}) Tj ET".encode())]
    with page.draw() as canvas:
        canvas.rect((0, 0, *size), stroke=RED)
    return pyropdf.open(doc.to_bytes())


def _marked_source(rotation: int = 0) -> pyropdf.Document:
    """Return a 300 by 200 display-point page with a red and a blue square."""
    doc = pyropdf.Document()
    page = doc.pages.new((300, 200) if rotation in (0, 180) else (200, 300))
    page.rotation = rotation
    with page.draw() as canvas:
        canvas.rect((0, 0, 30, 30), fill=RED)
        canvas.rect((200, 100, 260, 160), fill=BLUE)
    return pyropdf.open(doc.to_bytes())


# Paths.


def test_shapes_draw_paths_in_display_space() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.rect((10, 20, 110, 70), fill=RED)
        canvas.circle((200, 100), 40, fill=BLUE, stroke=Stroke(paint=BLACK, width=3))
        canvas.line((10, 150), (290, 150), stroke=BLUE)
        canvas.polygon([(10, 180), (50, 160), (90, 180)], fill=BLACK)
        canvas.ellipse((150, 160, 250, 190), stroke=RED)
        canvas.bezier((10, 10), (50, 0), (100, 0), (140, 10))
    rect, circle, line, polygon, ellipse, curve = page.content().paths
    _assert_rect(rect.bbox, (10, 20, 110, 70))
    _assert_color(rect.fill, RED)
    assert rect.stroke is None
    _assert_rect(circle.bbox, (160, 60, 240, 140))
    assert circle.stroke is not None
    assert circle.stroke.width == pytest.approx(3)
    _assert_rect(line.bbox, (10, 150, 290, 150))
    _assert_rect(polygon.bbox, (10, 160, 90, 180))
    _assert_rect(ellipse.bbox, (150, 160, 250, 190))
    assert curve.fill is None
    assert _pixel(page, 60, 45) == (255, 0, 0)
    assert _pixel(page, 200, 100) == (0, 0, 255)


def test_a_path_without_fill_or_stroke_is_stroked_black() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.path(PathBuilder().move_to((10, 10)).line_to((100, 10)))
    (path,) = page.content().paths
    assert path.fill is None
    assert path.stroke is not None
    _assert_color(path.stroke.paint, BLACK)
    assert path.stroke.width == pytest.approx(1)


def test_stroke_style_and_alpha_round_trip() -> None:
    _doc, page = _new_page()
    stroke = Stroke(
        paint=Color(0, 0, 1, 0.5),
        width=4,
        cap=pyropdf.LineCap.ROUND,
        join=pyropdf.LineJoin.BEVEL,
        dashes=(6, 2),
        dash_offset=1,
    )
    with page.draw() as canvas:
        canvas.rect((10, 10, 100, 100), fill=Color(1, 0, 0, 0.25), stroke=stroke)
    (path,) = page.content().paths
    _assert_color(path.fill, Color(1, 0, 0, 0.25))
    assert path.stroke is not None
    _assert_color(path.stroke.paint, Color(0, 0, 1, 0.5))
    assert path.stroke.width == pytest.approx(4)
    assert path.stroke.cap is pyropdf.LineCap.ROUND
    assert path.stroke.join is pyropdf.LineJoin.BEVEL
    assert path.stroke.dashes == pytest.approx((6, 2))
    assert path.stroke.dash_offset == pytest.approx(1)


def test_a_path_item_draws_again() -> None:
    _source_doc, source = _new_page()
    with source.draw() as canvas:
        canvas.path(PathBuilder().move_to((10, 10)).curve_to((20, 0), (40, 0), (50, 10)).close(), fill=RED)
    (item,) = source.content().paths
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.path(item, fill=BLUE, fill_rule=FillRule.EVEN_ODD)
    (copy,) = page.content().paths
    _assert_rect(copy.bbox, tuple(item.bbox))
    assert copy.fill_rule is FillRule.EVEN_ODD


def test_path_builder_constructors() -> None:
    builder = (
        PathBuilder()
        .rect((0, 0, 40, 20), radius=5)
        .circle((100, 100), 10)
        .sector((150, 50), 20, 0, 90)
        .zigzag((0, 150), (100, 150))
        .wave((0, 180), (100, 180))
        .quad_to((120, 160), (140, 180))
    )
    assert builder.current_point == pytest.approx((140, 180))
    assert len(builder.segments) > 20
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.path(builder, stroke=BLACK)
    (path,) = page.content().paths
    assert path.bbox.x0 == pytest.approx(-0.5, abs=0.5)
    with pytest.raises(ValueError, match="move_to"):
        PathBuilder().line_to((1, 1))
    with pytest.raises(ValueError, match="radius"):
        PathBuilder().rect((0, 0, 10, 10), radius=-1)


def test_invalid_arguments() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        with pytest.raises(ValueError, match="x0 < x1"):
            canvas.rect((10, 10, 5, 50), fill=RED)
        with pytest.raises(TypeError, match="fill"):
            canvas.rect((0, 0, 10, 10), fill=(1, 0, 0))  # ty: ignore[invalid-argument-type]
        with pytest.raises(pyropdf.PdfError, match="finite"):
            canvas.line((0, 0), (float("nan"), 1))
        with pytest.raises(ValueError, match="alpha"):
            canvas.opacity(1.5)
        with pytest.raises(ValueError, match="BlendMode"):
            canvas.blend("burn")
        with pytest.raises(ValueError, match="matrix"):
            canvas.transform((1, 2, 3))
    assert page.content().paths == ()


def test_a_zero_width_stroke_is_drawn_as_a_hairline() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.line((10, 10), (100, 10), stroke=Stroke(paint=RED, width=0))
    (path,) = page.content().paths
    assert path.stroke is not None
    assert path.stroke.width == 0


def test_small_transforms_keep_their_precision() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas, canvas.transform(Matrix(0.0001, 0, 0, 0.0001, 10, 10)):
        canvas.rect((0, 0, 100000, 200000), fill=RED)
    (path,) = page.content().paths
    _assert_rect(path.bbox, (10, 10, 20, 30))


# Scopes.


def test_transform_scopes_compose_inner_first() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        with canvas.transform(Matrix(1, 0, 0, 1, 100, 50)), canvas.transform(Matrix(2, 0, 0, 2, 0, 0)):
            canvas.rect((10, 10, 20, 20), fill=RED)
        canvas.rect((10, 10, 20, 20), fill=BLUE)
    scaled, plain = page.content().paths
    # The inner scale applies first, then the outer translation.
    _assert_rect(scaled.bbox, (120, 70, 140, 90))
    _assert_rect(plain.bbox, (10, 10, 20, 20))


def test_clip_scope_lists_the_clip_in_the_context() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        with canvas.clip(PathBuilder().rect((0, 0, 100, 100))):
            canvas.circle((100, 100), 50, fill=RED)
        canvas.rect((200, 10, 250, 60), fill=BLUE)
    clipped, free = page.content().paths
    assert free.context.clips == ()
    (clip,) = clipped.context.clips
    _assert_rect(clip.bbox, (0, 0, 100, 100))
    assert _pixel(page, 90, 90) == (255, 0, 0)
    assert _pixel(page, 110, 110) == (255, 255, 255)


def test_opacity_scopes_are_transparency_groups() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        with canvas.opacity(0.5):
            canvas.rect((10, 10, 60, 60), fill=BLACK)
            with canvas.opacity(0.5):
                canvas.rect((70, 10, 120, 60), fill=BLACK)
        with canvas.opacity(0.3):
            pass
        canvas.rect((130, 10, 180, 60), fill=BLACK)
    outer, nested, opaque = page.content().paths
    assert outer.context.opacity == pytest.approx(0.5, abs=1 / 255)
    assert nested.context.opacity == pytest.approx(0.25, abs=1 / 255)
    assert opaque.context.groups == ()
    gray = _pixel(page, 35, 35)
    assert 120 <= gray[0] <= 135
    assert gray[0] == gray[1] == gray[2]


def test_opacity_inside_a_transform_keeps_the_group_on_the_page() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas, canvas.transform(Matrix(0.5, 0, 0, 0.5, 150, 100)), canvas.opacity(0.5):
        canvas.rect((0, 0, 300, 200), fill=BLACK)
    (item,) = page.content().paths
    _assert_rect(item.bbox, (150, 100, 300, 200))
    assert item.context.opacity == pytest.approx(0.5, abs=1 / 255)
    assert _pixel(page, 250, 150)[0] < 200


def test_blend_scope_and_alpha_inside_it() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.rect((10, 10, 100, 100), fill=Color(1, 1, 0))
        with canvas.blend(BlendMode.MULTIPLY):
            canvas.rect((50, 50, 150, 150), fill=Color(0, 1, 1, 0.5))
        canvas.rect((200, 10, 250, 60), fill=RED)
    _below, blended, after = page.content().paths
    # A translucent fill keeps the blend mode of its scope.
    assert blended.blend_mode is BlendMode.MULTIPLY
    _assert_color(blended.fill, Color(0, 1, 1, 0.5))
    assert after.blend_mode is BlendMode.NORMAL


def test_a_scope_left_open_refuses_to_write() -> None:
    doc, page = _new_page()
    before = doc.to_bytes()

    def leave_a_scope_open() -> None:
        with page.draw() as canvas:
            canvas.clip(PathBuilder().rect((0, 0, 10, 10))).__enter__()
            canvas.rect((0, 0, 10, 10), fill=RED)

    with pytest.raises(RuntimeError, match="still open"):
        leave_a_scope_open()
    assert doc.to_bytes() == before


def test_scope_depth_limit() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        scopes = [canvas.transform(Matrix(1, 0, 0, 1, 1, 0)) for _ in range(17)]
        for scope in scopes[:16]:
            scope.__enter__()
        with pytest.raises(ValueError, match="16"):
            scopes[16].__enter__()
        for scope in reversed(scopes[:16]):
            scope.__exit__(None, None, None)
        canvas.rect((0, 0, 10, 10), fill=RED)
    (item,) = page.content().paths
    _assert_rect(item.bbox, (0, 0, 10, 10))


def test_path_point_limit() -> None:
    _doc, page = _new_page()
    builder = PathBuilder().move_to((0, 0))
    for index in range(1_048_576):
        builder.line_to((index % 300, 1))
    with page.draw() as canvas, pytest.raises(pyropdf.LimitError) as excinfo:
        canvas.path(builder)
    assert excinfo.value.code == "content_element_count"


# Images.


def test_images_are_stored_once() -> None:
    doc, page = _new_page()
    png = _png()
    with page.draw() as canvas:
        canvas.image((10, 10, 50, 30), png)
        canvas.image((60, 10, 100, 30), png)
        canvas.image((10, 50, 50, 130), png, rotate=90)
    first, second, rotated = page.content().images
    assert first.xref == second.xref == rotated.xref
    _assert_rect(first.bbox, (10, 10, 50, 30))
    _assert_rect(second.bbox, (60, 10, 100, 30))
    # Rotated a quarter turn, the 2:1 image fills the 1:2 rectangle.
    _assert_rect(rotated.bbox, (10, 50, 50, 130))
    # A later canvas places the stored image again.
    with page.draw() as canvas:
        canvas.image((200, 10, 240, 30), png)
    assert len({image.xref for image in page.content().images}) == 1
    assert _pixel(page, 15, 20) == (255, 0, 0)
    assert len(doc.pages) == 1


def test_keep_aspect_centres_the_image() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.image((0, 0, 100, 100), _png())
        canvas.image((100, 0, 200, 100), _png(), keep_aspect=False)
    centred, stretched = page.content().images
    _assert_rect(centred.bbox, (0, 25, 100, 75))
    _assert_rect(stretched.bbox, (100, 0, 200, 100))


def test_image_sources(tmp_path: Path) -> None:
    doc, page = _new_page()
    png = _png()
    path = tmp_path / "image.png"
    path.write_bytes(png)
    pixmap = _marked_source().pages[0].pixmap(dpi=36)
    with page.draw() as canvas:
        canvas.image((10, 10, 50, 30), path)
        canvas.image((60, 10, 100, 30), str(path))
        canvas.image((110, 10, 150, 100), pixmap, keep_aspect=False)
        canvas.image((160, 10, 200, 100), memoryview(png))
    images = page.content().images
    assert len(images) == 4
    _assert_rect(images[2].bbox, (110, 10, 150, 100))
    # An image of the document draws again without a copy, by item or number.
    count = len(doc.objects)
    with page.draw() as canvas:
        canvas.image((0, 150, 40, 170), page.images()[0])
        canvas.image((50, 150, 90, 170), images[2].xref)
    assert len(doc.objects) - count < 4
    _other_doc, other = _new_page()
    with other.draw() as canvas:
        canvas.image((10, 10, 50, 30), png)
    with page.draw() as canvas:
        with pytest.raises(ValueError, match="multiple of 90"):
            canvas.image((10, 10, 50, 30), png, rotate=45)
        with pytest.raises(TypeError, match="source"):
            canvas.image((10, 10, 50, 30), 4.2)  # ty: ignore[invalid-argument-type]
        with pytest.raises(ValueError, match="another document"):
            canvas.image((10, 10, 50, 30), other.images()[0])


def test_an_image_survives_a_save() -> None:
    doc, page = _new_page()
    with page.draw() as canvas:
        canvas.image((10, 10, 110, 60), _png())
    reopened = pyropdf.open(doc.to_bytes()).pages[0]
    assert _pixel(reopened, 20, 20) == (255, 0, 0)
    assert _pixel(reopened, 20, 70) == (255, 255, 255)


# Placed pages.


def test_pages_of_other_documents_are_stored_once() -> None:
    source = _text_page()
    doc, page = _new_page(400, 300)
    with page.draw() as canvas:
        canvas.page((0, 0, 200, 100), source.pages[0])
        canvas.page((200, 100, 400, 200), source.pages[0])
    assert page.text().count("Source page") == 2
    runs = page.content().text_runs
    assert runs[0].bbox.x0 < 200 < runs[1].bbox.x0
    # Drawing the unchanged page again reuses the Form of the first canvas.
    count = len(doc.objects)
    with page.draw() as canvas:
        canvas.page((0, 200, 200, 300), source.pages[0])
    assert len(doc.objects) - count < 5
    assert page.text().count("Source page") == 3


@pytest.mark.parametrize(
    ("rotate", "rect", "red"),
    [
        (0, (0, 0, 300, 200), (0, 0, 30, 30)),
        (90, (0, 0, 200, 300), (170, 0, 200, 30)),
        (180, (0, 0, 300, 200), (270, 170, 300, 200)),
        (270, (0, 0, 200, 300), (0, 270, 30, 300)),
        (-90, (0, 0, 200, 300), (0, 270, 30, 300)),
    ],
)
def test_a_placed_page_turns_clockwise(
    rotate: int, rect: tuple[float, float, float, float], red: tuple[float, float, float, float]
) -> None:
    source = _marked_source()
    _doc, page = _new_page(300, 300)
    with page.draw() as canvas:
        canvas.page(rect, source.pages[0], rotate=rotate)
    (square,) = _filled(page, RED)
    _assert_rect(square.bbox, red)
    x0, y0, x1, y1 = red
    assert _pixel(page, (x0 + x1) / 2, (y0 + y1) / 2) == (255, 0, 0)


def test_a_clip_draws_only_part_of_the_page_scaled_into_the_rectangle() -> None:
    source = _marked_source()
    _doc, page = _new_page(400, 400)
    with page.draw() as canvas:
        canvas.page((100, 100, 250, 250), source.pages[0], clip=(150, 50, 300, 200))
    # The 150-point square clip fills the rectangle at scale 1.
    (blue,) = _filled(page, BLUE)
    _assert_rect(blue.bbox, (150, 150, 210, 210))
    assert _clipped_to(blue, (100, 100, 250, 250))
    assert _pixel(page, 180, 180) == (0, 0, 255)
    # The red square lies outside the clip and maps outside the rectangle.
    (red,) = _filled(page, RED)
    _assert_rect(red.bbox, (-50, 50, -20, 80))
    assert _clipped_to(red, (100, 100, 250, 250))


def test_a_clip_turns_with_the_page_and_may_stretch() -> None:
    source = _marked_source()
    _doc, page = _new_page(400, 400)
    with page.draw() as canvas:
        canvas.page((100, 100, 250, 250), source.pages[0], clip=(150, 50, 300, 200), rotate=90)
        canvas.page((0, 300, 300, 375), source.pages[0], clip=(150, 50, 300, 200), keep_aspect=False)
    turned, stretched = _filled(page, BLUE)
    _assert_rect(turned.bbox, (140, 150, 200, 210))
    # The 150 by 150 clip stretches to 300 by 75.
    _assert_rect(stretched.bbox, (100, 325, 220, 355))
    assert _clipped_to(stretched, (0, 300, 300, 375))
    assert _pixel(page, 170, 180) == (0, 0, 255)
    assert _pixel(page, 160, 340) == (0, 0, 255)


def test_a_clip_is_cut_to_the_page_and_must_overlap_it() -> None:
    source = _marked_source()
    _doc, page = _new_page(400, 400)
    with page.draw() as canvas:
        # Only the 50-point corner (250, 150, 300, 200) lies on the page.
        canvas.page((0, 0, 100, 100), source.pages[0], clip=(250, 150, 400, 300))
        with pytest.raises(ValueError, match="overlap the source page"):
            canvas.page((0, 0, 100, 100), source.pages[0], clip=(300, 0, 400, 100))
        with pytest.raises(ValueError, match="clip must have x0 < x1"):
            canvas.page((0, 0, 100, 100), source.pages[0], clip=(50, 50, 10, 10))
        with pytest.raises(ValueError, match="multiple of 90"):
            canvas.page((0, 0, 100, 100), source.pages[0], rotate=45)
        with pytest.raises(TypeError, match="source"):
            canvas.page((0, 0, 100, 100), source)  # ty: ignore[invalid-argument-type]
    (blue,) = _filled(page, BLUE)
    # Blue's part from (250, 150) to (260, 160) at scale 2.
    assert _clipped_to(blue, (0, 0, 100, 100))
    assert blue.bbox.x0 == pytest.approx(-100, abs=0.01)
    assert _pixel(page, 10, 10) == (0, 0, 255)
    assert _pixel(page, 30, 30) == (255, 255, 255)


def test_a_clip_uses_the_source_pages_display_space() -> None:
    source = _marked_source(rotation=90)
    _doc, page = _new_page(400, 400)
    with page.draw() as canvas:
        canvas.page((0, 0, 100, 100), source.pages[0], clip=(0, 0, 100, 100))
    (red,) = _filled(page, RED)
    _assert_rect(red.bbox, (0, 0, 30, 30))
    assert _pixel(page, 15, 15) == (255, 0, 0)


def test_clips_and_turns_of_one_page_share_its_form() -> None:
    source = _marked_source()
    single, single_page = _new_page(400, 400)
    with single_page.draw() as canvas:
        canvas.page((0, 0, 150, 100), source.pages[0])
    several, several_page = _new_page(400, 400)
    with several_page.draw() as canvas:
        canvas.page((0, 0, 150, 100), source.pages[0])
        canvas.page((0, 100, 100, 250), source.pages[0], rotate=90)
        canvas.page((200, 0, 300, 100), source.pages[0], clip=(150, 50, 300, 200), rotate=180)
    assert len(several.objects) == len(single.objects)
    assert len(_filled(several_page, BLUE)) == 3


def test_a_clipped_and_turned_page_survives_a_save() -> None:
    source = _marked_source()
    doc, page = _new_page(400, 400)
    with page.draw() as canvas:
        canvas.page((100, 100, 250, 250), source.pages[0], clip=(150, 50, 300, 200), rotate=270)
    reopened = pyropdf.open(doc.to_bytes()).pages[0]
    assert [path.bbox for path in reopened.content().paths] == [path.bbox for path in page.content().paths]
    assert bytes(reopened.pixmap()) == bytes(page.pixmap())


def test_a_page_is_drawn_as_it_was_recorded() -> None:
    doc = _text_page("Before")
    target = doc.pages.new((300, 200))
    source = doc.pages[0]
    with target.draw() as canvas:
        canvas.page((0, 0, 150, 75), source)
        canvas.rect((200, 10, 250, 60), fill=RED)
    assert "Before" in target.text()
    # Placing a page on itself draws its earlier content.
    with source.draw() as canvas:
        canvas.page((100, 50, 200, 100), source)
    assert source.text().count("Before") == 2


# Writing.


def test_underlay_draws_below_existing_content() -> None:
    _doc, page = _new_page()
    with page.draw() as canvas:
        canvas.rect((10, 10, 100, 100), fill=RED)
    with page.draw(underlay=True) as canvas:
        canvas.rect((50, 50, 150, 150), fill=BLUE)
    under, over = page.content().paths
    _assert_color(under.fill, BLUE)
    _assert_color(over.fill, RED)
    assert _pixel(page, 75, 75) == (255, 0, 0)


def test_drawing_over_unbalanced_content_is_not_transformed_by_it() -> None:
    doc, page = _new_page()
    page.obj["Contents"] = doc.objects[doc.objects.add_stream(b"2 0 0 2 0 0 cm 0 0 1 rg 0 0 10 10 re f")]
    with page.draw() as canvas:
        canvas.rect((10, 10, 50, 50), fill=RED)
    _blue, red = page.content().paths
    _assert_rect(red.bbox, (10, 10, 50, 50))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_paths_use_display_space_at_every_rotation(rotation: int) -> None:
    _doc, page = _new_page(400, 300)
    page.rotation = rotation
    with page.draw() as canvas:
        canvas.rect((20, 30, 120, 80), fill=RED)
        canvas.image((200, 30, 240, 50), _png())
    (path,) = page.content().paths
    _assert_rect(path.bbox, (20, 30, 120, 80))
    (image,) = page.content().images
    _assert_rect(image.bbox, (200, 30, 240, 50))
    assert _pixel(page, 25, 35) == (255, 0, 0)


def test_a_drawing_is_one_undo_step() -> None:
    doc, page = _new_page()
    steps = len(doc.history.steps)
    with page.draw() as canvas:
        canvas.image((0, 0, 20, 10), _png())
        with canvas.opacity(0.5):
            canvas.rect((10, 10, 50, 50), fill=RED)
    assert doc.history.steps[steps:] == ("draw",)
    doc.history.undo()
    assert len(page.content()) == 0


def test_an_exception_discards_the_drawing() -> None:
    doc, page = _new_page()
    before = doc.to_bytes()

    def draw_then_fail() -> None:
        with page.draw() as canvas:
            canvas.rect((10, 10, 50, 50), fill=RED)
            canvas.image((0, 0, 20, 10), _png())
            msg = "stop"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="stop"):
        draw_then_fail()
    assert doc.to_bytes() == before
    assert len(page.content()) == 0


def test_an_empty_canvas_changes_nothing() -> None:
    doc, page = _new_page()
    before = doc.to_bytes()
    steps = doc.history.steps
    with page.draw():
        pass
    assert doc.to_bytes() == before
    assert doc.history.steps == steps


def test_a_canvas_draws_in_one_with_block() -> None:
    _doc, page = _new_page()
    canvas = page.draw()
    with pytest.raises(ValueError, match="with block"):
        canvas.rect((10, 10, 50, 50), fill=RED)
    with canvas:
        canvas.rect((10, 10, 50, 50), fill=RED)
    with pytest.raises(ValueError, match="with block"):
        canvas.rect((10, 10, 50, 50), fill=RED)
    with pytest.raises(ValueError, match="one with block"), canvas:
        pass
    assert len(page.content()) == 1


def test_a_canvas_on_a_stale_page_cannot_write() -> None:
    doc, page = _new_page()
    del doc.pages[0]
    with pytest.raises(pyropdf.StalePageError), page.draw() as canvas:
        canvas.rect((10, 10, 50, 50), fill=RED)


def test_resource_names_of_the_page_are_kept() -> None:
    doc, page = _new_page()
    png = _png()
    with page.draw() as canvas:
        canvas.image((0, 0, 20, 10), png)
    other = doc.objects.add_stream(
        b"0 0 1 rg 0 0 1 1 re f", {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 1, 1]}
    )
    page.obj["Resources"]["XObject"]["PyroX2"] = doc.objects[other]
    with page.draw() as canvas:
        canvas.image((30, 0, 50, 10), png)
        canvas.page((100, 0, 200, 100), _marked_source().pages[0])
    assert len(page.content().images) == 2
    assert len(_filled(page, RED)) == 1
    assert page.obj["Resources"]["XObject"]["PyroX2"].xref == other


def test_existing_content_is_wrapped_only_once() -> None:
    doc, page = _new_page()
    page.obj["Contents"] = doc.objects[doc.objects.add_stream(b"2 0 0 2 0 0 cm")]
    counts = [len(doc.objects)]
    for y in (20, 40, 60):
        with page.draw() as canvas:
            canvas.rect((10, y, 20, y + 10), fill=RED)
        counts.append(len(doc.objects))
    first, second, third = (after - before for before, after in itertools.pairwise(counts))
    # The first drawing wraps the unbalanced content in one q and one Q
    # stream; the later ones find it balanced.
    assert first == second + 2
    assert third == second


def test_drawing_leaves_pages_that_share_content_and_resources() -> None:
    doc, first = _new_page()
    second = doc.pages.new((300, 200))
    resources = doc.objects.add({"ExtGState": {"G0": {"CA": 1}}})
    content = doc.objects.add_stream(b"1 0 0 rg 0 0 300 200 re f")
    for page in (first, second):
        page.obj["Resources"] = doc.objects[resources]
        page.obj["Contents"] = doc.objects[content]
    with first.draw() as canvas:
        canvas.image((0, 0, 300, 200), _png(), keep_aspect=False)
        canvas.rect((0, 0, 10, 10), fill=BLUE)
    with second.draw(underlay=True) as canvas:
        canvas.rect((0, 0, 300, 200), fill=BLUE)
    assert _pixel(first, 100, 50) == (255, 0, 0)
    assert _pixel(first, 5, 5) == (0, 0, 255)
    assert _pixel(second, 100, 50) == (255, 0, 0)
    assert "XObject" not in doc.objects[resources]
    assert "G0" in first.obj["Resources"]["ExtGState"]


def test_a_jpeg_is_stored_as_it_is() -> None:
    _doc, page = _new_page()
    jpeg = _marked_source().pages[0].pixmap(dpi=36).to_bytes("jpeg")
    with page.draw() as canvas:
        canvas.image((10, 10, 160, 110), jpeg)
    (image,) = page.images()
    stored = image.stored()
    assert stored is not None
    assert stored.data == jpeg


def test_png_alpha_is_kept() -> None:
    _source_doc, source = _new_page(20, 10)
    with source.draw() as canvas:
        canvas.rect((0, 0, 10, 10), fill=RED)
    png = source.pixmap(dpi=72, alpha=True).to_bytes("png")
    _doc, page = _new_page(20, 10)
    with page.draw() as canvas:
        canvas.rect((0, 0, 20, 10), fill=BLUE)
        canvas.image((0, 0, 20, 10), png)
    assert _pixel(page, 5, 5) == (255, 0, 0)
    assert _pixel(page, 15, 5) == (0, 0, 255)


@pytest.mark.parametrize("gray", [False, True])
@pytest.mark.parametrize("alpha", [False, True])
def test_larger_png_keeps_color_and_alpha_across_the_frame(*, gray: bool, alpha: bool) -> None:
    _source_doc, source = _new_page(100, 100)
    with source.draw() as canvas:
        canvas.rect((0, 0, 50, 100), fill=RED)
        canvas.rect((50, 50, 100, 100), fill=BLUE)
    original = source.pixmap(dpi=72, alpha=alpha)
    if gray:
        original = original.to_gray()
    png = original.to_bytes("png")
    _doc, page = _new_page(100, 100)
    with page.draw() as canvas:
        canvas.image((0, 0, 100, 100), png)
    (image,) = page.images()
    assert bytes(image.pixmap()) == bytes(original)


def test_another_thread_can_cancel_png_decoding() -> None:
    _source_doc, source = _new_page(256, 256)
    png = source.pixmap(dpi=72, alpha=True).to_bytes("png")
    token = pyropdf.CancellationToken()
    document = pyropdf.Document(limits=pyropdf.DocumentLimits(cancellation=token))
    core = document._core()  # noqa: SLF001
    started = threading.Event()
    outcome: list[BaseException | None] = []

    def decode_until_cancelled() -> None:
        started.set()
        try:
            for _ in range(100):
                _core.decode_png(core, png)
        except pyropdf.LimitError as error:
            outcome.append(error)
        else:
            outcome.append(None)

    # Keep Python from switching threads between the event and a decoder
    # call, so progress depends on the decoder releasing the GIL.
    previous_interval = sys.getswitchinterval()
    try:
        sys.setswitchinterval(5.0)
        worker = threading.Thread(target=decode_until_cancelled)
        worker.start()
        assert started.wait(timeout=10)
        token.cancel()
        worker.join(timeout=10)
        assert not worker.is_alive()
    finally:
        sys.setswitchinterval(previous_interval)
    assert len(outcome) == 1
    error = outcome[0]
    assert isinstance(error, pyropdf.LimitError)
    assert error.code == "cancelled"


def test_a_placed_page_shows_its_crop_box() -> None:
    source = _marked_source()
    source.pages[0].crop_box = (150, 50, 300, 200)
    _doc, page = _new_page(400, 400)
    with page.draw() as canvas:
        canvas.page((0, 0, 150, 150), source.pages[0])
    (blue,) = _filled(page, BLUE)
    _assert_rect(blue.bbox, (50, 50, 110, 110))
    assert _pixel(page, 80, 80) == (0, 0, 255)
    assert _pixel(page, 10, 10) == (255, 255, 255)
