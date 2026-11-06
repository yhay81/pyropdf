"""Drawing paths, images, and placed pages with Page.draw (DESIGN.md section 7).

Ported from pylopdf's tests/test_canvas.py and tests/test_draw.py at
ebb01d3c, without text: canvas text in Standard 14 fonts is tested in
test_canvas_text.py and in embedded faces in test_canvas_embedded.py.
"""

from __future__ import annotations

import itertools
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import zlib
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import (
    BlendMode,
    Color,
    ColorStop,
    FillRule,
    GradientExtend,
    LinearGradient,
    Matrix,
    Name,
    PathBuilder,
    Point,
    RadialGradient,
    Rect,
    ShadingPattern,
    ShadingType,
    Stroke,
    _core,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import TypeAlias

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


def _interlaced_rgba_png(width: int, height: int) -> tuple[bytes, bytes]:
    """Return an Adam7-interlaced 8-bit RGBA PNG and its samples in row order."""
    samples = bytes(
        (x * 31 + y * 17 + channel * 59) % 256 for y in range(height) for x in range(width) for channel in range(4)
    )
    passes = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    raw = bytearray()
    for x0, y0, dx, dy in passes:
        columns = range(x0, width, dx)
        if not columns:
            continue
        for y in range(y0, height, dy):
            raw.append(0)
            for x in columns:
                start = (y * width + x) * 4
                raw += samples[start : start + 4]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 1)
    png = (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")
    )
    return png, samples


def test_interlaced_png_decodes_row_by_row_to_the_same_samples() -> None:
    png, samples = _interlaced_rgba_png(37, 29)
    core = pyropdf.Document()._core()  # noqa: SLF001
    width, height, gray, color, alpha = _core.decode_png(core, png)
    assert (width, height, gray) == (37, 29, False)
    assert alpha == samples[3::4]
    assert color == bytes(b for i, b in enumerate(samples) if i % 4 != 3)


def test_drawing_a_pixmap_past_the_memory_budget_is_refused_without_changes() -> None:
    _source_doc, source = _new_page(256, 256)
    pixmap = source.pixmap(dpi=144, alpha=True)
    doc = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=64 * 1024))
    page = doc.pages.new((100, 100))
    before = doc.to_bytes(), doc.history.steps
    with pytest.raises(pyropdf.LimitError) as raised, page.draw() as canvas:
        canvas.image((0, 0, 100, 100), pixmap)
    assert raised.value.code == "memory_size"
    assert (doc.to_bytes(), doc.history.steps) == before


def test_pixmap_png_counts_encoded_output_in_memory_budget() -> None:
    width = height = 256
    rng = random.Random(1847)  # noqa: S311 - reproducible image samples, not secrets.
    samples = rng.randbytes(width * height * 3)
    rows = b"".join(b"\x00" + samples[y * width * 3 : (y + 1) * width * 3] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    _source_doc, source = _new_page(width, height)
    with source.draw() as canvas:
        canvas.image((0, 0, width, height), png)
    pixmap = source.pixmap(dpi=72)
    assert len(_core.pixmap_png(pyropdf.Document()._core(), pixmap)) > 180_000  # noqa: SLF001

    document = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=500_000))
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.pixmap_png(document._core(), pixmap)  # noqa: SLF001
    assert raised.value.code == "memory_size"


# Gradients.

# The largest difference of one channel, in levels of 255, between a gradient
# and its formula at 72 dpi, away from what it paints's edges. The documentation
# of `Canvas` publishes the same tolerances.
GRADIENT_TOLERANCE = 8
REPEATING_TOLERANCE = 16

WHITE = Color(1, 1, 1)
RED_BLUE = (ColorStop(0, RED), ColorStop(1, BLUE))
FADING_RED = (ColorStop(0, RED), ColorStop(1, Color(1, 0, 0, 0)))

_Pixels: TypeAlias = "tuple[int, bytes]"


def _hayro(data: bytes) -> _Pixels:
    """Return the RGB samples and their row size of hayro's render of the first page at 72 dpi."""
    pixmap = pyropdf.open(data).pages[0].pixmap(dpi=72)
    return pixmap.stride, bytes(pixmap)


def _poppler(data: bytes) -> _Pixels:
    """Return the RGB samples and their row size of poppler's render of the first page at 72 dpi."""
    tool = shutil.which("pdftoppm")
    if tool is None:
        pytest.skip("poppler's pdftoppm is not installed")
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "page.pdf")  # noqa: PTH118 - the module's Path is a type only.
        with open(path, "wb") as file:  # noqa: PTH123 - as above.
            file.write(data)
        ppm = subprocess.run(  # noqa: S603 - a fixed tool on a file of the test's own.
            [tool, "-r", "72", "-singlefile", path],
            check=True,
            capture_output=True,
        ).stdout
    # A binary PPM: "P6", the width and height, the maximum, then the samples.
    header = ppm.split(maxsplit=4)
    width, height = int(header[1]), int(header[2])
    return width * 3, ppm[len(ppm) - width * height * 3 :]


RENDERERS = pytest.mark.parametrize("render", [_hayro, _poppler], ids=["hayro", "poppler"])


def _mix(first: Color, second: Color, at: float) -> tuple[float, float, float]:
    """Return the levels of 255 of two colors' mix, the place on the line held to its ends."""
    at = max(0.0, min(1.0, at))
    red, green, blue = (255 * (a + at * (b - a)) for a, b in zip(first.rgb, second.rgb, strict=True))
    return red, green, blue


def _largest_error(
    render: Callable[[bytes], _Pixels],
    data: bytes,
    box: tuple[int, int, int, int],
    expected: Callable[[float, float], tuple[float, float, float] | None],
) -> float:
    """Return the largest difference of a channel from ``expected`` at the pixel centres of ``box``."""
    stride, samples = render(data)
    x0, y0, x1, y1 = box
    largest = 0.0
    for y in range(y0, y1):
        for x in range(x0, x1):
            wanted = expected(x + 0.5, y + 0.5)
            if wanted is not None:
                at = y * stride + x * 3
                largest = max(largest, *(abs(samples[at + i] - wanted[i]) for i in range(3)))
    return largest


def _drawn(
    draw: Callable[[pyropdf.Canvas], None], size: tuple[float, float] = (200, 200), rotation: int = 0
) -> pyropdf.Document:
    doc = pyropdf.Document()
    page = doc.pages.new(size)
    if rotation:
        page.rotation = rotation
    with page.draw() as canvas:
        draw(canvas)
    return doc


def _objects_with(doc: pyropdf.Document, key: str) -> list[int]:
    return [number for number in doc.objects if key in doc.objects[number]]


def _linear(*stops: ColorStop, extend: GradientExtend = GradientExtend.PAD) -> LinearGradient:
    return LinearGradient(start=Point(20, 0), end=Point(180, 0), stops=stops, extend=extend)


def _rings(*stops: ColorStop, extend: GradientExtend = GradientExtend.PAD) -> RadialGradient:
    center = Point(100, 100)
    return RadialGradient(start=center, start_radius=10, end=center, end_radius=90, stops=stops, extend=extend)


def _along_the_line(x: float, _y: float) -> tuple[float, float, float]:
    return _mix(RED, BLUE, (x - 20) / 160)


def _around_the_centre(x: float, y: float) -> tuple[float, float, float]:
    return _mix(RED, BLUE, (math.hypot(x - 100, y - 100) - 10) / 80)


WHOLE_PAGE = (0, 0, 200, 200)
MOVED = Matrix(1, 0, 0, 1, 10, 0)
INSIDE_THE_PAGE = (2, 2, 198, 198)


@RENDERERS
def test_a_linear_gradient_fills_within_the_tolerance(render: Callable[[bytes], _Pixels]) -> None:
    data = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=_linear(*RED_BLUE))).to_bytes()
    assert _largest_error(render, data, INSIDE_THE_PAGE, _along_the_line) <= GRADIENT_TOLERANCE


@RENDERERS
def test_a_radial_gradient_fills_within_the_tolerance(render: Callable[[bytes], _Pixels]) -> None:
    data = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=_rings(*RED_BLUE))).to_bytes()
    assert _largest_error(render, data, INSIDE_THE_PAGE, _around_the_centre) <= GRADIENT_TOLERANCE


@RENDERERS
def test_a_linear_gradient_strokes_within_the_tolerance(render: Callable[[bytes], _Pixels]) -> None:
    line = Stroke(paint=_linear(*RED_BLUE), width=40)
    data = _drawn(lambda canvas: canvas.line((0, 100), (200, 100), stroke=line)).to_bytes()
    assert _largest_error(render, data, (2, 85, 198, 115), _along_the_line) <= GRADIENT_TOLERANCE


@RENDERERS
def test_a_radial_gradient_strokes_within_the_tolerance(render: Callable[[bytes], _Pixels]) -> None:
    line = Stroke(paint=_rings(*RED_BLUE), width=40)
    data = _drawn(lambda canvas: canvas.line((0, 100), (200, 100), stroke=line)).to_bytes()
    assert _largest_error(render, data, (2, 85, 198, 115), _around_the_centre) <= GRADIENT_TOLERANCE


@RENDERERS
def test_stops_between_the_ends_and_a_sharp_change_are_drawn(render: Callable[[bytes], _Pixels]) -> None:
    stops = (ColorStop(0.25, RED), ColorStop(0.5, BLUE), ColorStop(0.5, BLACK), ColorStop(0.75, WHITE))
    gradient = LinearGradient(start=Point(0, 0), end=Point(200, 0), stops=stops)
    data = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=gradient)).to_bytes()

    def expected(x: float, _y: float) -> tuple[float, float, float] | None:
        if abs(x - 100) < 2:
            return None
        return _mix(RED, BLUE, (x - 50) / 50) if x < 100 else _mix(BLACK, WHITE, (x - 100) / 50)

    assert _largest_error(render, data, INSIDE_THE_PAGE, expected) <= GRADIENT_TOLERANCE


@RENDERERS
@pytest.mark.parametrize("radial", [False, True], ids=["linear", "radial"])
def test_stops_with_alpha_match_the_same_colors_composited_on_white(
    render: Callable[[bytes], _Pixels], *, radial: bool
) -> None:
    gradient = _rings(*FADING_RED) if radial else _linear(*FADING_RED)
    doc = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=gradient))
    # A soft mask, not a shading in another colour.
    assert len(_objects_with(doc, "SMask")) == 1

    def expected(x: float, y: float) -> tuple[float, float, float]:
        at = (math.hypot(x - 100, y - 100) - 10) / 80 if radial else (x - 20) / 160
        return _mix(RED, WHITE, at)

    assert _largest_error(render, doc.to_bytes(), INSIDE_THE_PAGE, expected) <= GRADIENT_TOLERANCE


@RENDERERS
def test_a_stroke_with_alpha_paints_through_a_mask_that_holds_the_whole_line(
    render: Callable[[bytes], _Pixels],
) -> None:
    line = Stroke(paint=_linear(*FADING_RED), width=40)
    data = _drawn(lambda canvas: canvas.line((0, 100), (200, 100), stroke=line)).to_bytes()
    assert _largest_error(render, data, (2, 85, 198, 115), lambda x, _y: _mix(RED, WHITE, (x - 20) / 160)) <= (
        GRADIENT_TOLERANCE
    )


def test_a_fill_and_a_stroke_that_need_masks_paint_apart() -> None:
    fading_blue = (ColorStop(0, BLUE), ColorStop(1, Color(0, 0, 1, 0)))
    line = Stroke(paint=_linear(*fading_blue), width=10)
    doc = _drawn(lambda canvas: canvas.rect((50, 50, 150, 150), fill=_linear(*FADING_RED), stroke=line))
    assert len(_objects_with(doc, "SMask")) == 2
    page = doc.pages[0]
    paints = [operation.operator for operation in page.operations if operation.operator in ("f", "S", "B")]
    assert paints == ["f", "S"]
    # The stroke's mask holds what lies outside the path, as the fill's need not.
    outside = page.pixmap(dpi=72).pixel(46, 100)
    assert outside[2] > outside[0]


def test_stops_of_one_alpha_paint_through_the_graphics_state() -> None:
    half = (ColorStop(0, Color(1, 0, 0, 0.5)), ColorStop(1, Color(0, 0, 1, 0.5)))
    doc = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=_linear(*half)))
    assert _objects_with(doc, "SMask") == []
    fill = doc.pages[0].content().paths[0].fill
    assert isinstance(fill, ShadingPattern)
    assert fill.opacity == 0.5


@RENDERERS
def test_a_gradient_moves_with_the_canvas_transform(render: Callable[[bytes], _Pixels]) -> None:
    def draw(canvas: pyropdf.Canvas) -> None:
        with canvas.transform(Matrix(0.5, 0, 0, 0.5, 50, 50)):
            canvas.rect(WHOLE_PAGE, fill=_linear(*RED_BLUE))

    data = _drawn(draw).to_bytes()

    def expected(x: float, _y: float) -> tuple[float, float, float]:
        return _along_the_line((x - 50) * 2, 0)

    assert _largest_error(render, data, (52, 52, 148, 148), expected) <= GRADIENT_TOLERANCE


@RENDERERS
@pytest.mark.parametrize("stops", [RED_BLUE, FADING_RED], ids=["opaque", "alpha"])
def test_a_gradient_in_an_opacity_group_under_a_transform(
    render: Callable[[bytes], _Pixels], stops: tuple[ColorStop, ...]
) -> None:
    def draw(canvas: pyropdf.Canvas) -> None:
        # A group's Form has a space of its own, in which a pattern's matrix starts again.
        scopes = canvas.transform(Matrix(0.5, 0, 0, 0.5, 50, 50)), canvas.opacity(0.5), canvas.transform(MOVED)
        with scopes[0], scopes[1], scopes[2]:
            canvas.rect((-10, 0, 190, 200), fill=_linear(*stops))

    data = _drawn(draw).to_bytes()
    second = BLUE if stops is RED_BLUE else WHITE

    def expected(x: float, _y: float) -> tuple[float, float, float]:
        red, green, blue = _mix(RED, second, ((x - 50) * 2 - 30) / 160)
        return (red + 255) / 2, (green + 255) / 2, (blue + 255) / 2

    assert _largest_error(render, data, (52, 52, 142, 148), expected) <= GRADIENT_TOLERANCE


@RENDERERS
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_gradient_stays_in_display_space_on_a_turned_page(render: Callable[[bytes], _Pixels], rotation: int) -> None:
    size = (200, 200)
    data = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=_linear(*RED_BLUE)), size, rotation).to_bytes()
    assert _largest_error(render, data, INSIDE_THE_PAGE, _along_the_line) <= GRADIENT_TOLERANCE


@RENDERERS
def test_a_gradient_in_a_tag_scope_or_under_the_page_keeps_its_place(render: Callable[[bytes], _Pixels]) -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    with page.draw(underlay=True) as canvas, canvas.tag(pyropdf.TagRole.FIGURE, alt="A gradient"):
        canvas.rect(WHOLE_PAGE, fill=_linear(*RED_BLUE))
    assert _largest_error(render, doc.to_bytes(), INSIDE_THE_PAGE, _along_the_line) <= GRADIENT_TOLERANCE


@RENDERERS
@pytest.mark.parametrize("extend", [GradientExtend.REPEAT, GradientExtend.REFLECT])
def test_a_repeating_linear_gradient_runs_over_what_it_paints(
    render: Callable[[bytes], _Pixels], extend: GradientExtend
) -> None:
    gradient = LinearGradient(start=Point(10, 0), end=Point(60, 0), stops=RED_BLUE, extend=extend)
    data = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=gradient)).to_bytes()

    def expected(x: float, _y: float) -> tuple[float, float, float] | None:
        cycle, at = divmod((x - 10) / 50, 1)
        # A cycle's edges are too sharp to compare where it restarts.
        if extend is GradientExtend.REPEAT and not 0.1 < at < 0.9:
            return None
        return _mix(RED, BLUE, 1 - at if extend is GradientExtend.REFLECT and cycle % 2 else at)

    assert _largest_error(render, data, INSIDE_THE_PAGE, expected) <= REPEATING_TOLERANCE


@RENDERERS
@pytest.mark.parametrize("extend", [GradientExtend.REPEAT, GradientExtend.REFLECT])
def test_a_repeating_radial_gradient_runs_over_what_it_paints(
    render: Callable[[bytes], _Pixels], extend: GradientExtend
) -> None:
    gradient = RadialGradient(
        start=Point(100, 100), start_radius=20, end=Point(100, 100), end_radius=50, stops=RED_BLUE, extend=extend
    )
    data = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=gradient)).to_bytes()

    def expected(x: float, y: float) -> tuple[float, float, float] | None:
        cycle, at = divmod((math.hypot(x - 100, y - 100) - 20) / 30, 1)
        if not 0.15 < at < 0.85:
            return None
        return _mix(RED, BLUE, 1 - at if extend is GradientExtend.REFLECT and cycle % 2 else at)

    assert _largest_error(render, data, INSIDE_THE_PAGE, expected) <= REPEATING_TOLERANCE


@RENDERERS
def test_a_repeating_gradient_with_alpha_repeats_its_mask(render: Callable[[bytes], _Pixels]) -> None:
    gradient = LinearGradient(start=Point(0, 0), end=Point(50, 0), stops=FADING_RED, extend=GradientExtend.REFLECT)
    data = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=gradient)).to_bytes()

    def expected(x: float, _y: float) -> tuple[float, float, float]:
        cycle, at = divmod(x / 50, 1)
        return _mix(RED, WHITE, 1 - at if cycle % 2 else at)

    assert _largest_error(render, data, INSIDE_THE_PAGE, expected) <= REPEATING_TOLERANCE


@RENDERERS
def test_a_gradient_that_does_not_extend_paints_nothing_past_its_ends(render: Callable[[bytes], _Pixels]) -> None:
    gradient = _linear(*RED_BLUE, extend=GradientExtend.NONE)
    data = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=gradient)).to_bytes()

    def expected(x: float, _y: float) -> tuple[float, float, float] | None:
        if abs(x - 20) < 2 or abs(x - 180) < 2:
            return None
        return (255, 255, 255) if x < 20 or x > 180 else _along_the_line(x, 0)

    assert _largest_error(render, data, INSIDE_THE_PAGE, expected) <= GRADIENT_TOLERANCE


def test_equal_function_and_shading_objects_are_shared_within_a_canvas() -> None:
    def draw(canvas: pyropdf.Canvas) -> None:
        for y in (0, 50, 100):
            canvas.rect((0, y, 200, y + 40), fill=_linear(*RED_BLUE))
        # Another line of the same colors shares the function.
        canvas.rect((0, 150, 200, 190), fill=_rings(*RED_BLUE))
        # A stroke of the same gradient shares everything.
        canvas.line((0, 0), (200, 200), stroke=_linear(*RED_BLUE))

    doc = _drawn(draw)
    assert len(_objects_with(doc, "FunctionType")) == 1
    assert len(_objects_with(doc, "ShadingType")) == 2
    assert len(_objects_with(doc, "PatternType")) == 2


def test_equal_masks_are_shared_and_each_box_has_its_own() -> None:
    def draw(canvas: pyropdf.Canvas) -> None:
        canvas.rect((0, 0, 200, 40), fill=_linear(*FADING_RED))
        canvas.rect((0, 0, 200, 40), fill=_linear(*FADING_RED))
        canvas.rect((0, 50, 200, 90), fill=_linear(*FADING_RED))

    doc = _drawn(draw)
    # The color and the alpha, each with one shading.
    assert len(_objects_with(doc, "FunctionType")) == 2
    assert len(_objects_with(doc, "ShadingType")) == 2
    assert len(_objects_with(doc, "SMask")) == 2


def test_a_canvas_does_not_share_objects_with_another_canvas() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    with page.draw() as canvas:
        canvas.rect(WHOLE_PAGE, fill=_linear(*RED_BLUE))
    with page.draw() as canvas:
        canvas.rect(WHOLE_PAGE, fill=_linear(*RED_BLUE))
    assert len(_objects_with(doc, "ShadingType")) == 2


@RENDERERS
def test_a_second_canvas_renames_its_resources_and_keeps_its_gradient(render: Callable[[bytes], _Pixels]) -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    with page.draw() as canvas:
        canvas.rect((0, 0, 100, 100), fill=_linear(*RED_BLUE))
        canvas.rect((0, 100, 100, 200), fill=_linear(*FADING_RED))
    # The names of its pattern and its mask are the first canvas's, for other objects.
    down, fading_down = (LinearGradient(start=Point(0, 0), end=Point(0, 200), stops=s) for s in (RED_BLUE, FADING_RED))
    with page.draw() as canvas:
        canvas.rect((100, 0, 200, 100), fill=down)
        canvas.rect((100, 100, 200, 200), fill=fading_down)
    assert {"PyroP1", "PyroP1_1", "PyroM1", "PyroM1_1"} <= {
        *page.obj["Resources"]["Pattern"].keys(),
        *page.obj["Resources"]["ExtGState"].keys(),
    }

    def expected(x: float, y: float) -> tuple[float, float, float] | None:
        if abs(x - 100) < 2 or abs(y - 100) < 2:
            return None
        along = (x - 20) / 160 if x < 100 else y / 200
        return _mix(RED, BLUE if y < 100 else WHITE, along)

    assert _largest_error(render, doc.to_bytes(), INSIDE_THE_PAGE, expected) <= GRADIENT_TOLERANCE


def test_a_limit_refuses_a_gradient_without_writing() -> None:
    doc = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=64 * 1024))
    page = doc.pages.new((200, 200))
    before = doc.to_bytes(), doc.history.steps
    many = tuple(ColorStop(index / 1000, Color(index / 1000, 0, 0)) for index in range(1001))
    with pytest.raises(pyropdf.LimitError) as raised, page.draw() as canvas:
        canvas.rect(WHOLE_PAGE, fill=_linear(*many))
    assert raised.value.code == "memory_size"
    assert (doc.to_bytes(), doc.history.steps) == before


def test_a_gradient_charges_the_operation_before_it_allocates() -> None:
    doc = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=50_000_000))
    page = doc.pages.new((200, 200))
    many = tuple(ColorStop(index / 100, Color(index / 100, 0, 0)) for index in range(101))
    with page.draw() as canvas:
        operation = canvas._active  # noqa: SLF001
        before = operation.used("memory_size")
        canvas.rect(WHOLE_PAGE, fill=_linear(*many))
        # What the store holds, and the table's keys, stay charged.
        used = operation.used("memory_size")
        assert used > before + 5_000
        # What is shared costs only its buffered content.
        canvas.rect(WHOLE_PAGE, fill=_linear(*many))
        assert operation.used("memory_size") < used + 5_000


def test_a_cancelled_canvas_writes_no_gradient() -> None:
    token = pyropdf.CancellationToken()
    doc = pyropdf.Document(limits=pyropdf.DocumentLimits(cancellation=token))
    page = doc.pages.new((200, 200))
    steps = doc.history.steps

    def draw_after_cancelling(canvas: pyropdf.Canvas) -> None:
        token.cancel()
        canvas.rect(WHOLE_PAGE, fill=_linear(*RED_BLUE))

    with pytest.raises(pyropdf.LimitError), page.draw() as canvas:
        draw_after_cancelling(canvas)
    assert doc.history.steps == steps


def test_too_many_cycles_or_stops_refuse_with_a_limit_and_write_nothing() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    before = doc.to_bytes(), doc.history.steps
    thin = LinearGradient(start=Point(0, 0), end=Point(0.001, 0), stops=RED_BLUE, extend=GradientExtend.REPEAT)
    with pytest.raises(pyropdf.LimitError) as raised, page.draw() as canvas:
        canvas.rect(WHOLE_PAGE, fill=thin)
    assert raised.value.code == "content_element_count"
    many = (ColorStop(0, RED),) * 1_048_577
    with pytest.raises(pyropdf.LimitError) as raised, page.draw() as canvas:
        canvas.rect(WHOLE_PAGE, fill=_linear(*many))
    assert raised.value.code == "content_element_count"
    assert (doc.to_bytes(), doc.history.steps) == before


def test_an_exception_discards_the_objects_and_undo_removes_them() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))

    def draw_and_raise(canvas: pyropdf.Canvas) -> None:
        canvas.rect(WHOLE_PAGE, fill=_linear(*FADING_RED))
        raise RuntimeError

    with pytest.raises(RuntimeError), page.draw() as canvas:
        draw_and_raise(canvas)
    assert _objects_with(doc, "ShadingType") == []
    with page.draw() as canvas:
        canvas.rect(WHOLE_PAGE, fill=_linear(*FADING_RED))
    drawn = _objects_with(doc, "ShadingType")
    assert len(drawn) == 2
    doc.history.undo()
    assert _objects_with(doc, "ShadingType") == []
    assert _objects_with(doc, "SMask") == []
    doc.history.redo()
    assert _objects_with(doc, "ShadingType") == drawn


def test_page_content_reads_a_drawn_gradient_back() -> None:
    def draw(canvas: pyropdf.Canvas) -> None:
        canvas.rect((10, 20, 110, 70), fill=_linear(*RED_BLUE), stroke=Stroke(paint=_rings(*RED_BLUE), width=3))
        canvas.circle((100, 150), 30, fill=_linear(*FADING_RED))
        with canvas.transform(Matrix(2, 0, 0, 2, 5, 6)):
            canvas.rect((0, 0, 10, 10), fill=_linear(*RED_BLUE))

    page = _drawn(draw).pages[0]
    both, masked, moved = page.content().paths
    assert isinstance(both.fill, ShadingPattern)
    assert both.fill.shading_type is ShadingType.AXIAL
    assert both.stroke is not None
    assert isinstance(both.stroke.paint, ShadingPattern)
    assert both.stroke.paint.shading_type is ShadingType.RADIAL
    assert both.stroke.width == 3
    assert both.soft_mask is None
    _assert_rect(both.bbox, (10, 20, 110, 70))
    # The transform from shading space to display space.
    assert tuple(both.fill.matrix) == pytest.approx((1, 0, 0, 1, 0, 0))
    assert isinstance(masked.fill, ShadingPattern)
    assert masked.soft_mask == pyropdf.SoftMask(subtype=pyropdf.SoftMaskType.LUMINOSITY)
    assert isinstance(moved.fill, ShadingPattern)
    assert tuple(moved.fill.matrix) == pytest.approx((2, 0, 0, 2, 5, 6))
    # The columns hold the same paint.
    pattern = page.paths().patterns[0]
    assert isinstance(pattern, ShadingPattern)
    assert pattern.shading_type is ShadingType.AXIAL


def test_a_saved_gradient_opens_with_the_same_render_and_a_clean_check() -> None:
    def draw(canvas: pyropdf.Canvas) -> None:
        canvas.rect((0, 0, 200, 60), fill=_linear(*RED_BLUE), stroke=Stroke(paint=_linear(*FADING_RED), width=4))
        canvas.circle((100, 130), 50, fill=_rings(ColorStop(0, RED), ColorStop(0.5, BLUE), ColorStop(1, BLACK)))
        canvas.rect((0, 180, 200, 200), fill=_linear(*RED_BLUE, extend=GradientExtend.REFLECT))

    doc = _drawn(draw)
    reopened = pyropdf.open(doc.to_bytes())
    assert bytes(reopened.pages[0].pixmap(dpi=72)) == bytes(doc.pages[0].pixmap(dpi=72))
    assert reopened.check().defects == ()


def test_gradients_that_cannot_be_drawn_raise_and_draw_nothing() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    before = doc.to_bytes(), doc.history.steps
    point = Point(1, 2)
    cases: list[tuple[object, type[Exception]]] = [
        (LinearGradient(start=point, end=point, stops=RED_BLUE), ValueError),
        (RadialGradient(start=point, start_radius=3, end=point, end_radius=3, stops=RED_BLUE), ValueError),
        (RadialGradient(start=point, start_radius=0, end=Point(3, 4), end_radius=0, stops=RED_BLUE), ValueError),
        (RadialGradient(start=point, start_radius=-1, end=point, end_radius=3, stops=RED_BLUE), ValueError),
        (_linear(), ValueError),
        (_linear(ColorStop(0.5, RED), ColorStop(0.25, BLUE)), ValueError),
        (_linear(ColorStop(-0.5, RED)), ValueError),
        (_linear(ColorStop(1.5, RED)), ValueError),
        (_linear(ColorStop(float("nan"), RED)), ValueError),
        (_linear(ColorStop(0, Color(2, 0, 0))), ValueError),
        (_linear(ColorStop(0, "red")), TypeError),  # ty: ignore[invalid-argument-type]
        (_linear("red"), TypeError),  # ty: ignore[invalid-argument-type]
        (LinearGradient(start=Point(1e39, 0), end=point, stops=RED_BLUE), ValueError),
        (_linear(ColorStop(0, RED), extend="sideways"), ValueError),  # ty: ignore[invalid-argument-type]
        (LinearGradient(start=point, end=Point(5, 5), stops=iter(RED_BLUE)), TypeError),  # ty: ignore[invalid-argument-type]
    ]
    for gradient, error in cases:
        with pytest.raises(error), page.draw() as canvas:
            canvas.rect(WHOLE_PAGE, fill=gradient)  # ty: ignore[invalid-argument-type]
        with pytest.raises(error), page.draw() as canvas:
            canvas.line((0, 0), (9, 9), stroke=gradient)  # ty: ignore[invalid-argument-type]
    assert (doc.to_bytes(), doc.history.steps) == before


def test_patterns_without_retained_source_refuse_drawing() -> None:
    doc = _drawn(lambda canvas: canvas.rect(WHOLE_PAGE, fill=_linear(*RED_BLUE)))
    page = doc.pages[0]
    read_back = page.content().paths[0].fill
    assert isinstance(read_back, ShadingPattern)
    read_back = ShadingPattern(shading_type=read_back.shading_type, matrix=read_back.matrix, opacity=read_back.opacity)
    with pytest.raises(TypeError, match="ShadingPattern"), page.draw() as canvas:
        canvas.rect(WHOLE_PAGE, fill=read_back)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="ShadingPattern"), page.draw() as canvas:
        canvas.line((0, 0), (9, 9), stroke=Stroke(paint=read_back))


def test_a_degenerate_transform_paints_a_gradient_as_nothing_without_a_panic() -> None:
    def draw(canvas: pyropdf.Canvas) -> None:
        with canvas.transform(Matrix(0, 0, 0, 0, 0, 0)):
            canvas.rect(WHOLE_PAGE, fill=_linear(*FADING_RED), stroke=_rings(*RED_BLUE))

    assert _drawn(draw).pages[0].pixmap(dpi=72).pixel(100, 100)[:3] == (255, 255, 255)


def test_arbitrary_gradients_draw_or_refuse_and_a_render_survives_them() -> None:
    rng = random.Random(632)  # noqa: S311 - reproducible inputs, not secrets.
    extremes = (0.0, 1.0, -1.0, 1e-30, 1e30, 3.4e38, -3.4e38, 200.0)

    def number() -> float:
        return rng.choice(extremes) if rng.random() < 0.3 else rng.uniform(-500, 500)

    def stops() -> tuple[ColorStop, ...]:
        offsets = sorted(rng.random() for _ in range(rng.randrange(1, 7)))
        return tuple(
            ColorStop(offset, Color(rng.random(), rng.random(), rng.random(), rng.choice((1.0, rng.random()))))
            for offset in offsets
        )

    drawn = 0
    for _ in range(200):
        extend = rng.choice(tuple(GradientExtend))
        gradient: LinearGradient | RadialGradient
        if rng.random() < 0.5:
            gradient = LinearGradient(
                start=Point(number(), number()), end=Point(number(), number()), stops=stops(), extend=extend
            )
        else:
            gradient = RadialGradient(
                start=Point(number(), number()),
                start_radius=abs(number()),
                end=Point(number(), number()),
                end_radius=abs(number()),
                stops=stops(),
                extend=extend,
            )
        doc = pyropdf.Document()
        page = doc.pages.new((60, 60))
        try:
            with page.draw() as canvas:
                canvas.rect((5, 5, 55, 55), fill=gradient, stroke=Stroke(paint=gradient, width=2))
        except (ValueError, pyropdf.LimitError):
            assert _objects_with(doc, "ShadingType") == []
            continue
        drawn += 1
        for number_ in doc.objects:
            assert "nan" not in repr(doc.objects[number_]).lower()
            assert "inf " not in repr(doc.objects[number_]).lower()
        page.pixmap(dpi=36)
        pyropdf.open(doc.to_bytes()).pages[0].content()
    assert drawn > 20
