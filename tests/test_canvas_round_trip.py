"""The Canvas round trip: redraw what ``Page.content()`` reads of the corpus.

Ported from pylopdf ebb01d3c. Every supported text, path, and image item of every page of
the bundled corpus is drawn again with ``Canvas.draw`` on a blank page of
the same size in another document, and the two renderings agree within
``ROUND_TRIP_TOLERANCE`` levels per channel at 72 dpi outside the boxes of
the items that a canvas cannot draw yet: runs without a reusable font or paint, items under a soft
mask, and function-based or mesh shadings; and of annotations
without an appearance stream, which the renderer draws but
``Page.content()`` does not report. Those items are
listed with the reason in ``_not_drawn``, and the documentation of
``Canvas.draw`` publishes the same tolerance and exceptions.

Axial and radial shadings are sampled only when redrawn. Tiling patterns,
inline raster images, and indirect or inline stencil image masks are redrawn
directly. Only soft masks and function-based or mesh shadings remain outside
the round trip.
"""

from __future__ import annotations

import dataclasses
import io
import math
import operator
import pickle
import re
import shutil
import struct
import subprocess
import zlib
from collections import Counter
from pathlib import Path as FilePath

import pikepdf
import pytest
from test_canvas_embedded import JAPANESE
from test_content_model import page_with, stencil_page, stream_object

import pyropdf
from pyropdf import (
    Color,
    ColorStop,
    Image,
    LinearGradient,
    Matrix,
    Path,
    Point,
    Rect,
    ShadingPattern,
    ShadingType,
    Stroke,
    TextRun,
    TilingPattern,
)

CORPUS = FilePath(__file__).parent / "assets" / "real_world"

# The largest difference of one channel, in levels of 255, outside the
# boxes of the items not drawn.
ROUND_TRIP_TOLERANCE = 16


def _unsupported_path_paint(paint: object) -> bool:
    """Return whether a path or stencil paint is outside the round trip."""
    if paint is None or isinstance(paint, (Color, TilingPattern)):
        return False
    return not (isinstance(paint, ShadingPattern) and paint.shading_type in (ShadingType.AXIAL, ShadingType.RADIAL))


def _not_drawn(item: TextRun | Path | Image) -> str | None:
    """Return why a canvas cannot draw an item yet, or ``None``."""
    if item.soft_mask is not None or any(group.soft_mask is not None for group in item.context.groups):
        return "soft mask"
    if isinstance(item, Image):
        unsupported = item.is_stencil and (item.fill is None or _unsupported_path_paint(item.fill))
    elif isinstance(item, TextRun):
        paints = (item.fill, None if item.stroke is None else item.stroke.paint)
        unsupported = any(paint is not None and not isinstance(paint, Color) for paint in paints)
    else:
        paints = (item.fill, None if item.stroke is None else item.stroke.paint)
        unsupported = any(_unsupported_path_paint(paint) for paint in paints)
    return "pattern paint" if unsupported else None


def _largest_difference(original: pyropdf.Pixmap, redrawn: pyropdf.Pixmap, hidden: list[pyropdf.Rect]) -> int:
    """Return the largest channel difference of two RGB rasters outside ``hidden``."""
    width, height, stride = original.width, original.height, original.stride
    first, second = bytes(original), bytes(redrawn)
    covered: list[list[tuple[int, int]]] = [[] for _ in range(height)]
    for box in hidden:
        x0, x1 = max(0, math.floor(box.x0) - 2), min(width, math.ceil(box.x1) + 2)
        for y in range(max(0, math.floor(box.y0) - 2), min(height, math.ceil(box.y1) + 2)):
            covered[y].append((x0, x1))
    largest = 0
    for y, spans in enumerate(covered):
        start = 0
        for x0, x1 in [*sorted(spans), (width, width)]:
            if x0 > start:
                a, b = y * stride + start * 3, y * stride + x0 * 3
                if first[a:b] != second[a:b]:
                    largest = max(largest, *map(abs, map(operator.sub, first[a:b], second[a:b])))
            start = max(start, x1)
    return largest


@pytest.mark.parametrize("name", sorted(path.name for path in CORPUS.glob("*.pdf")))
def test_canvas_redraws_the_supported_items_of_every_page(name: str) -> None:
    doc = pyropdf.open(CORPUS / name)
    excluded: Counter[str] = Counter()
    for page in doc.pages:
        target = pyropdf.Document()
        copy = target.pages.new((page.rect.width, page.rect.height))
        # The renderer draws an annotation without an appearance stream,
        # which ``Page.content()`` does not report.
        hidden = [annotation.rect for annotation in page.annotations if "AP" not in doc.objects[annotation.xref]]
        with copy.draw() as canvas:
            for item in page.content():
                reason = _not_drawn(item)
                if reason is not None:
                    if isinstance(item, TextRun):
                        excluded[reason] += 1
                    hidden.append(item.bbox)
                elif isinstance(item, TextRun):
                    try:
                        canvas.draw(item)
                    except ValueError as error:
                        excluded[str(error)] += 1
                        hidden.append(item.bbox)
                else:
                    canvas.draw(item)
        original = page.pixmap(dpi=72)
        redrawn = copy.pixmap(dpi=72)
        assert (redrawn.width, redrawn.height) == (original.width, original.height)
        difference = _largest_difference(original, redrawn, hidden)
        assert difference <= ROUND_TRIP_TOLERANCE, (name, page.index, difference)
    assert excluded == ({"the font is neither embedded nor a standard 14 font": 8} if name == "mhlw-doc.pdf" else {})


def _png(width: int, height: int) -> bytes:
    """Return an opaque RGB PNG of one color."""
    raw = b"".join(b"\x00" + bytes([200, 30, 60] * width) for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _drawn() -> pyropdf.Document:
    """Return a document whose page shows an image and a filled, stroked rectangle."""
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    with page.draw() as canvas:
        canvas.image((10, 20, 110, 70), _png(4, 2))
        canvas.rect((120, 120, 180, 190), fill=Color(0, 0.5, 1), stroke=Stroke(paint=Color(1, 0, 0), width=3))
    return doc


def test_draw_copies_an_image_of_another_document_once() -> None:
    page = _drawn().pages[0]
    image = page.content().images[0]
    target = pyropdf.Document()
    copy = target.pages.new((200, 200))
    with copy.draw() as canvas:
        canvas.draw(image)
        canvas.draw(image)
    images = copy.content().images
    assert [item.bbox for item in images] == [image.bbox] * 2
    assert len({item.xref for item in images}) == 1
    reopened = pyropdf.open(target.to_bytes()).pages[0]
    assert reopened.pixmap(dpi=72).pixel(50, 40) == page.pixmap(dpi=72).pixel(50, 40)


def test_draw_redraws_an_image_of_the_same_document_without_a_copy() -> None:
    doc = _drawn()
    image = doc.pages[0].content().images[0]
    with doc.pages.new((200, 200)).draw() as canvas:
        canvas.draw(image)
    assert doc.pages[1].content().images[0].xref == image.xref
    images = [
        number
        for number in doc.objects
        if isinstance(doc.objects[number], pyropdf.Stream) and doc.objects[number].get("Subtype") == "Image"
    ]
    assert images == [image.xref]


def _gradient() -> LinearGradient:
    stops = (ColorStop(0, Color(1, 0, 0)), ColorStop(1, Color(0, 0, 1)))
    return LinearGradient(start=Point(0, 0), end=Point(200, 0), stops=stops)


@pytest.mark.spec("exponential-functions", "create", "preserve")
@pytest.mark.spec("stitching-functions", "read", "interpret", "create", "preserve")
def test_draw_repaints_a_gradient_read_back_in_fill_and_stroke() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    with page.draw() as canvas:
        gradient = dataclasses.replace(
            _gradient(),
            stops=(
                ColorStop(0, Color(1, 0, 0)),
                ColorStop(0.3, Color(0, 0.5, 0)),
                ColorStop(0.7, Color(1, 0.8, 0)),
                ColorStop(1, Color(0, 0, 1)),
            ),
        )
        canvas.rect((0, 0, 200, 100), fill=gradient, stroke=Stroke(paint=_gradient(), width=2))
    item = page.content().paths[0]
    assert isinstance(item.fill, ShadingPattern)
    assert item.stroke is not None
    copy = _redraw(page)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE


def _redraw(page: pyropdf.Page, *, columns: bool = False) -> pyropdf.Page:
    """Paint read items on a page in another document, including after saving."""
    target = pyropdf.Document()
    copy = target.pages.new((page.rect.width, page.rect.height))
    with copy.draw() as canvas:
        for item in page.paths() if columns else page.content():
            assert isinstance(item, (Path, Image))
            canvas.draw(item)
    return pyropdf.open(target.to_bytes()).pages[0]


def _shaded(  # noqa: PLR0913 - independent dimensions of a shading round trip.
    shading_type: int,
    function: int,
    extend: tuple[bool, bool],
    *,
    stroke: bool = False,
    matrix: str = "1 0 0 1 0 0",
    rotate: int = 0,
) -> pyropdf.Page:
    """Build a shading with a non-default domain and an interior start/end boundary."""
    if function == 0:
        samples = bytes([255, 0, 30, 0, 180, 60, 60, 30, 220, 200, 240, 0]).decode("latin-1")
        func = stream_object(
            samples,
            "/FunctionType 0 /Domain [-2 3] /Range [0 1 0 1 0 1] "
            "/Size [4] /BitsPerSample 8 /Encode [0 3] /Decode [0 1 0 1 0 1]",
        )
    else:
        func = stream_object(
            "{ 2 add 5 div dup dup mul exch 1 exch sub 0.2 }",
            "/FunctionType 4 /Domain [-2 3] /Range [0 1 0 1 0 1]",
        )
    coords = "40 50 150 50" if shading_type == 2 else "90 50 12 100 50 45"
    ends = " ".join(str(value).lower() for value in extend)
    content = "/Pattern CS /P SCN 18 w 10 10 180 80 re S" if stroke else "/Pattern cs /P scn 0 0 200 100 re f"
    return page_with(
        content,
        resources="/Pattern << /P 6 0 R >>",
        rotate=rotate,
        objects={
            6: f"<< /Type /Pattern /PatternType 2 /Matrix [{matrix}] /Shading 7 0 R >>",
            7: f"<< /ShadingType {shading_type} /ColorSpace /DeviceRGB /Coords [{coords}] "
            f"/Domain [-2 3] /Extend [{ends}] /Function 8 0 R >>",
            8: func,
        },
    )


@pytest.mark.parametrize("shading_type", [2, 3])
@pytest.mark.parametrize("function", [0, 4])
@pytest.mark.parametrize("extend", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("stroke", [False, True])
@pytest.mark.spec("axial-shading", "read", "interpret")
@pytest.mark.spec("calculator-functions", "read", "interpret")
@pytest.mark.spec("radial-shading", "read", "interpret")
@pytest.mark.spec("sampled-functions", "read", "interpret")
def test_repaint_shading_domains_functions_and_each_extension(
    shading_type: int,
    function: int,
    extend: tuple[bool, bool],
    *,
    stroke: bool,
) -> None:
    page = _shaded(shading_type, function, extend, stroke=stroke)
    copy = _redraw(page)
    difference = _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), [])
    assert difference <= ROUND_TRIP_TOLERANCE


@pytest.mark.parametrize("shading_type", [2, 3])
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.parametrize("columns", [False, True])
def test_repaint_preserves_pattern_matrix_and_page_rotation(
    shading_type: int,
    rotate: int,
    *,
    columns: bool,
) -> None:
    page = _shaded(shading_type, 4, (True, True), matrix="0.9 0.2 -0.15 0.7 15 3", rotate=rotate)
    copy = _redraw(page, columns=columns)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE


def test_reading_defers_sampling_and_keeps_the_original_version_after_close() -> None:
    page = _shaded(2, 4, (True, True))
    expected = page.pixmap(dpi=72)
    # The single rectangle costs seven content elements; sampling would add
    # stops and exceed this limit if it happened during extraction.
    (item,) = page.content(max_elements=8)
    assert isinstance(item, Path)
    page.document.objects[8].write(b"{ pop 0 0 0 }")
    page.document.close()
    target = pyropdf.Document()
    copy = target.pages.new((200, 100))
    with copy.draw() as canvas:
        canvas.draw(item)
    assert _largest_difference(expected, copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE


def test_distinct_shading_functions_with_identical_metadata_do_not_share_a_paint() -> None:
    page = page_with(
        "/Pattern cs /P1 scn 0 0 100 100 re f /P2 scn 100 0 100 100 re f",
        resources="/Pattern << /P1 6 0 R /P2 7 0 R >>",
        objects={
            6: "<< /Type /Pattern /PatternType 2 /Shading 8 0 R >>",
            7: "<< /Type /Pattern /PatternType 2 /Shading 9 0 R >>",
            8: "<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 200 0] /Extend [true true] "
            "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >>",
            9: "<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 200 0] /Extend [true true] "
            "/Function << /FunctionType 2 /Domain [0 1] /C0 [0 1 0] /C1 [1 0 0] /N 1 >> >>",
        },
    )
    assert len(page.paths().patterns) == 2
    copy = _redraw(page)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE
    # Filtering compacts the shared pattern table. The surviving source must
    # still address the second function, for both item and column readers.
    expected = page.pixmap(dpi=72).pixel(150, 50)
    for items in (page.content(clip=(101, 0, 200, 100)), page.paths(clip=(101, 0, 200, 100))):
        (item,) = items
        assert isinstance(item, Path)
        target = pyropdf.Document().pages.new((200, 100))
        with target.draw() as canvas:
            canvas.draw(item)
        assert max(abs(a - b) for a, b in zip(expected, target.pixmap(dpi=72).pixel(150, 50), strict=True)) <= 16


def test_unretained_and_unsupported_shadings_refuse_without_writing() -> None:
    item = _shaded(2, 4, (True, True)).content().paths[0]
    assert isinstance(item.fill, ShadingPattern)
    target = pyropdf.Document()
    copy = target.pages.new((200, 100))
    before = target.to_bytes(), target.history.steps
    for kind in (ShadingType.AXIAL, ShadingType.FUNCTION, ShadingType.TRIANGLE_MESH):
        unretained = dataclasses.replace(item.fill, shading_type=kind)
        with pytest.raises(TypeError, match="ShadingPattern"), copy.draw() as canvas:
            canvas.draw(dataclasses.replace(item, fill=unretained))
    assert (target.to_bytes(), target.history.steps) == before


@pytest.mark.parametrize("shading_type", [2, 3])
@pytest.mark.parametrize(
    ("space", "start", "end"),
    [
        ("/DeviceGray", "0.1", "0.9"),
        ("/DeviceCMYK", "0.1 0.8 0.2 0", "0.8 0 0.5 0.1"),
        ("[/CalGray << /WhitePoint [0.9505 1 1.089] /Gamma 2.2 >>]", "0.1", "0.9"),
        ("[/CalRGB << /WhitePoint [0.9505 1 1.089] /Gamma [2.2 2.2 2.2] >>]", "0.1 0.8 0.3", "0.9 0.2 0.7"),
        ("[/Lab << /WhitePoint [0.9505 1 1.089] /Range [-128 127 -128 127] >>]", "20 -20 60", "90 40 -40"),
        ("[/Separation /Ink /DeviceRGB << /FunctionType 2 /Domain [0 1] /C0 [1 1 1] /C1 [1 0 0] /N 1 >>]", "0", "1"),
        ("[/Indexed /DeviceRGB 3 <ff000000ff000000ffffffff>]", "0", "3"),
        ("[/DeviceN [/Ink1 /Ink2] /DeviceRGB 8 0 R]", "0.1 0.8", "0.9 0.2"),
    ],
)
@pytest.mark.spec("calgray-color-space", "read", "interpret")
@pytest.mark.spec("device-color-conversion", "read", "interpret")
@pytest.mark.spec("devicen-color-space", "read", "interpret")
@pytest.mark.spec("lab-color-space", "read", "interpret")
def test_repaint_converts_shading_color_spaces_through_hayro(
    shading_type: int,
    space: str,
    start: str,
    end: str,
) -> None:
    coords = "0 0 200 0" if shading_type == 2 else "100 50 0 100 50 100"
    page = page_with(
        "/Pattern cs /P scn 0 0 200 100 re f",
        resources="/Pattern << /P 6 0 R >>",
        objects={
            6: "<< /Type /Pattern /PatternType 2 /Shading 7 0 R >>",
            7: f"<< /ShadingType {shading_type} /ColorSpace {space} /Coords [{coords}] /Extend [true true] "
            f"/Function << /FunctionType 2 /Domain [0 1] /C0 [{start}] /C1 [{end}] /N 1 >> >>",
            8: stream_object("{ 0.2 }", "/FunctionType 4 /Domain [0 1 0 1] /Range [0 1 0 1 0 1]"),
        },
    )
    copy = _redraw(page)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE


@pytest.mark.parametrize("shading_type", [2, 3])
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_repaint_keeps_a_shadings_bbox(shading_type: int, rotate: int) -> None:
    page = _shaded(shading_type, 4, (True, True), matrix="0.9 0.2 -0.15 0.7 15 3", rotate=rotate)
    page.document.objects[7]["BBox"] = [30, 20, 150, 70]
    copy = _redraw(page)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE


@pytest.mark.parametrize("matrix", ["0 0 0 0 0 0", "1 0 0 0 10 20", "1 1 1 1 0 0"])
def test_sample_shading_returns_no_bounds_for_singular_pattern_matrix(matrix: str) -> None:
    page = _shaded(2, 4, (True, True), matrix=matrix)
    page.document.objects[7]["BBox"] = [30, 20, 150, 70]
    (item,) = page.content().paths
    assert isinstance(item.fill, ShadingPattern)
    source = item.fill._source  # noqa: SLF001 - verify sampled bounds directly.
    assert source is not None
    operation = pyropdf._core.begin_canvas(page.document._core())  # noqa: SLF001
    gradient, bbox, _background = source.gradient(operation)
    assert isinstance(gradient, LinearGradient)
    assert bbox is None


@pytest.mark.parametrize("shading_type", [2, 3])
@pytest.mark.parametrize("extend", [(False, False), (False, True), (True, False), (True, True)])
def test_repaint_keeps_background_and_opacity(shading_type: int, extend: tuple[bool, bool]) -> None:
    page = _shaded(shading_type, 4, extend)
    page.document.objects[7]["Background"] = [0.1, 0.4, 0.6]
    page.document.objects[2]["Resources"]["ExtGState"] = {"Half": {"ca": 0.5, "CA": 0.5}}
    stream = page.document.objects[4]
    stream.write(b"/Half gs\n" + stream.read())
    copy = _redraw(page)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE


def test_repaint_keeps_distinct_transfer_functions_of_one_shading() -> None:
    page = page_with(
        "q /A gs /Pattern cs /P scn 0 0 100 100 re f Q q /B gs /Pattern cs /P scn 100 0 100 100 re f Q",
        resources="/Pattern << /P 6 0 R >> /ExtGState << /A << /TR 8 0 R >> /B << /TR /Identity >> >>",
        objects={
            6: "<< /Type /Pattern /PatternType 2 /Shading 7 0 R >>",
            7: "<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 200 0] /Extend [true true] "
            "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >>",
            8: "<< /FunctionType 2 /Domain [0 1] /C0 [1] /C1 [0] /N 1 >>",
        },
    )
    assert len(page.paths().patterns) == 2
    copy = _redraw(page)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE


@pytest.mark.parametrize("shading_type", [2, 3])
def test_repaint_a_direct_shading_operator(shading_type: int) -> None:
    page = _shaded(shading_type, 4, (False, True))
    page.document.objects[2]["Resources"]["Shading"] = {"S": page.document.objects[7]}
    page.document.objects[4].write(b"/S sh")
    copy = _redraw(page)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE


def test_a_sampled_shading_is_reused_and_refuses_an_exhausted_canvas_budget() -> None:
    page = _shaded(2, 4, (True, True))
    (item,) = page.content().paths
    target = pyropdf.Document()
    copy = target.pages.new((200, 100))
    with copy.draw() as canvas:
        canvas.draw(item)
        sampled = canvas._active.used("interpretation_fuel")  # noqa: SLF001 - count work, not elapsed time.
        assert sampled > 0
        canvas.draw(item)
        assert canvas._active.used("interpretation_fuel") == sampled  # noqa: SLF001

    target = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=16_384))
    copy = target.pages.new((200, 100))
    before = target.to_bytes(), target.history.steps
    (fresh,) = page.content().paths
    with pytest.raises(pyropdf.LimitError) as caught, copy.draw() as canvas:
        canvas.draw(fresh)
    assert caught.value.code == "memory_size"
    assert (target.to_bytes(), target.history.steps) == before


def test_draw_paints_a_read_back_path_with_a_gradient_in_place_of_its_paint() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    with page.draw() as canvas:
        canvas.rect((0, 0, 200, 200), fill=_gradient())
    item = page.content().paths[0]
    target = pyropdf.Document()
    copy = target.pages.new((200, 200))
    with copy.draw() as canvas:
        canvas.draw(dataclasses.replace(item, fill=_gradient()))
    assert bytes(copy.pixmap(dpi=72)) == bytes(page.pixmap(dpi=72))


def test_draw_refuses_items_it_cannot_draw_and_writes_nothing() -> None:
    page = _drawn().pages[0]
    content = page.content()
    path, image = content.paths[0], content.images[0]
    assert path.stroke is not None
    target = pyropdf.Document()
    copy = target.pages.new((200, 200))
    before = target.to_bytes(), target.history.steps
    cases: list[tuple[object, type[Exception]]] = [
        ("a text run", TypeError),
        (dataclasses.replace(path, fill=object()), TypeError),
        (dataclasses.replace(path, stroke=dataclasses.replace(path.stroke, paint=object())), TypeError),
        (pickle.loads(pickle.dumps(image)), ValueError),  # noqa: S301 - a value pickled here.
        (dataclasses.replace(image, xref=0), ValueError),
    ]
    for item, error in cases:
        with pytest.raises(error), copy.draw() as canvas:
            canvas.draw(item)  # ty: ignore[invalid-argument-type]
    assert (target.to_bytes(), target.history.steps) == before
    with copy.draw() as canvas:
        canvas.draw(path)
    assert copy.content().paths[0].bbox == path.bbox


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("same_document", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.spec("image-masks", "read", "interpret", "create", "preserve")
def test_draw_redraws_stencil_images(*, inline: bool, same_document: bool, nested: bool) -> None:
    page = stencil_page(inline=inline, nested=nested)
    images = page.content().images
    assert len(images) == 2
    assert all(img.is_stencil and img.fill == Color(0, 0, 1) for img in images)
    target = page.document if same_document else pyropdf.Document()
    copy = target.pages.new((page.rect.width, page.rect.height))
    with copy.draw() as canvas:
        for image in images:
            canvas.draw(image)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE
    reopened = pyropdf.open(target.to_bytes()).pages[copy.index]
    assert _largest_difference(page.pixmap(dpi=72), reopened.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE
    redrawn = copy.content().images
    assert len(redrawn) == 2
    assert all(img.is_stencil for img in redrawn)
    # Deduplicated: both placements reference one image XObject in the target.
    assert len({img.xref for img in redrawn}) == 1
    if not inline and same_document:
        assert redrawn[0].xref == images[0].xref
    if inline:
        stream = target.objects[redrawn[0].xref]
        assert isinstance(stream, pyropdf.Stream)
        assert stream.get("ImageMask") is True
        assert "ColorSpace" not in stream


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_draw_redraws_stencil_with_opacity_decode_and_pattern_fill(*, inline: bool, rotate: int) -> None:
    samples = "\x40\x00"
    objects: dict[int, str] = {
        6: "<< /Type /ExtGState /ca 0.5 >>",
        7: "<< /Type /Pattern /PatternType 2 /Shading 8 0 R >>",
        8: "<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 200 0] /Extend [true true] "
        "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 1 0] /N 1 >> >>",
        9: stream_object(
            "0.9 0.1 0.2 rg 0 0 5 5 re f 0.1 0.2 0.9 rg 5 5 5 5 re f",
            "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
            "/BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources << >>",
        ),
    }
    if inline:
        im_grad = f"BI /W 2 /H 2 /IM true /BPC 1 /D [1 0] ID {samples} EI"
        im_tile = f"BI /W 2 /H 2 /IM true /BPC 1 ID {samples} EI"
        xobj_res = ""
    else:
        objects[10] = stream_object(
            samples,
            "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ImageMask true /BitsPerComponent 1 /Decode [1 0]",
        )
        objects[11] = stream_object(
            samples,
            "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ImageMask true /BitsPerComponent 1",
        )
        im_grad, im_tile = "/ImG Do", "/ImT Do"
        xobj_res = "/XObject << /ImG 10 0 R /ImT 11 0 R >>"
    page = page_with(
        f"q /GS1 gs /Pattern cs /PG scn 40 0 0 40 10 10 cm {im_grad} Q\n"
        f"q /Pattern cs /PT scn 40 0 0 40 70 10 cm {im_tile} Q",
        resources=f"/ExtGState << /GS1 6 0 R >> /Pattern << /PG 7 0 R /PT 9 0 R >> {xobj_res}",
        objects=objects,
        rotate=rotate,
    )
    images = page.content().images
    assert isinstance(images[0].fill, ShadingPattern)
    assert isinstance(images[1].fill, TilingPattern)
    copy = _redraw(page)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE


def test_draw_refuses_invalid_stencil_and_inline_images_without_writing() -> None:
    stencil = stencil_page(inline=True).content().images[0]
    raster_page = page_with("q 20 0 0 20 10 10 cm BI /W 2 /H 1 /BPC 8 /CS /RGB ID \xff\x00\x00\x00\xff\x00 EI Q")
    inline_raster = raster_page.content().images[0]
    target = pyropdf.Document()
    copy = target.pages.new((200, 100))
    before = target.to_bytes(), target.history.steps
    cases = [
        (dataclasses.replace(stencil, fill=None), "fill paint"),
        (dataclasses.replace(inline_raster, is_stencil=True, fill=Color(1, 0, 0)), "captured data"),
        (pickle.loads(pickle.dumps(inline_raster)), "captured data"),  # noqa: S301 - value created here.
        (pickle.loads(pickle.dumps(stencil)), "captured data"),  # noqa: S301 - value created here.
    ]
    for bad, pattern in cases:
        with copy.draw() as canvas, pytest.raises(ValueError, match=pattern):
            canvas.draw(bad)
    assert (target.to_bytes(), target.history.steps) == before


@pytest.mark.parametrize("same_document", [False, True])
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.spec("inline-images", "read", "interpret", "create", "preserve")
def test_draw_redraws_inline_raster_images(tmp_path: FilePath, *, same_document: bool, rotate: int) -> None:
    rgb_hex = "FF0000 00FF00 0000FF FFFF00>"
    gray_hex = "20 E0>"
    # Color-key masked inline RGB: black pixel masked out, red pixel kept.
    masked_hex = "000000 FF2020>"
    content = (
        "0.85 0.9 0.95 rg 0 0 200 100 re f\n"
        f"q 40 0 0 40 10 10 cm BI /W 2 /H 2 /BPC 8 /CS /RGB /F /AHx ID {rgb_hex} EI Q\n"
        f"q 40 0 0 40 60 10 cm BI /W 2 /H 1 /BPC 8 /CS /G /F /AHx ID {gray_hex} EI Q\n"
        f"q 40 0 0 40 110 10 cm BI /W 2 /H 1 /BPC 8 /CS /RGB /F /AHx /Mask [0 10 0 10 0 10] ID {masked_hex} EI Q\n"
        f"q 20 0 0 20 160 10 cm BI /W 2 /H 2 /BPC 8 /CS /RGB /F /AHx ID {rgb_hex} EI Q"
    )
    page = page_with(content, rotate=rotate)
    page_size = (page.rect.width, page.rect.height)
    images = page.content().images
    assert len(images) == 4
    assert all(img.xref == 0 and not img.is_stencil and img.fill is None for img in images)
    target = page.document if same_document else pyropdf.Document()
    copy = target.pages.new(page_size)
    original_pix = page.pixmap(dpi=72)
    if not same_document:
        # Inline payloads are owned and survive closing the source document.
        page.document.close()
    with copy.draw() as canvas:
        canvas.rect((0, 0, page_size[0], page_size[1]), fill=Color(0.85, 0.9, 0.95))
        for image in images:
            canvas.draw(image)
    assert _largest_difference(original_pix, copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE
    reopened = pyropdf.open(target.to_bytes()).pages[copy.index]
    assert _largest_difference(original_pix, reopened.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE
    # Images 0 and 3 share the same payload -> deduplicated to one XObject.
    redrawn_images = copy.content().images
    assert redrawn_images[0].xref == redrawn_images[3].xref > 0
    if not same_document and rotate == 0 and shutil.which("pdftoppm"):
        out_path = tmp_path / "inline.pdf"
        target.save(out_path)
        _, pixels = _poppler_page(out_path, 1)
        assert min(pixels) < 100


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.spec("tiling-patterns", "read", "interpret", "create", "preserve")
def test_draw_redraws_colored_tiling_pattern_fill_and_stroke(tmp_path: FilePath, rotate: int) -> None:
    pattern_stream = stream_object(
        "0.9 0.1 0.1 rg 0 0 6 6 re f 0.1 0.4 0.9 rg 6 6 6 6 re f 0.1 0.7 0.2 RG 1 w 0 12 m 12 0 l S",
        "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
        "/BBox [0 0 12 12] /XStep 14 /YStep 14 /Matrix [1.2 0.2 -0.15 1.1 5 3] /Resources << >>",
    )
    page = page_with(
        "q /GS1 gs /Pattern cs /P1 scn 10 10 80 70 re f Q\nq /Pattern CS /P1 SCN 10 w 110 15 70 60 re S Q",
        resources="/ExtGState << /GS1 6 0 R >> /Pattern << /P1 7 0 R >>",
        objects={6: "<< /Type /ExtGState /ca 0.65 >>", 7: pattern_stream},
        rotate=rotate,
    )
    page_size = (page.rect.width, page.rect.height)
    original_pix = page.pixmap(dpi=72)
    content = page.content()
    paths_col = page.paths()
    assert isinstance(content.paths[0].fill, TilingPattern)
    assert isinstance(paths_col[0].fill, TilingPattern)
    assert content.paths[0].fill.opacity == pytest.approx(0.65, abs=1 / 255)
    # Redraw from both Page.content() and Page.paths(), including after closing the source.
    items_from_content = list(content.paths)
    items_from_paths = list(paths_col)
    if rotate == 0 and shutil.which("pdftoppm"):
        src_path = tmp_path / "tiling_src.pdf"
        page.document.save(src_path)
    page.document.close()
    for batch in (items_from_content, items_from_paths):
        target = pyropdf.Document()
        copy = target.pages.new(page_size)
        with copy.draw() as canvas:
            for item in batch:
                canvas.draw(item)
        assert _largest_difference(original_pix, copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE
        reopened = pyropdf.open(target.to_bytes()).pages[0]
        assert _largest_difference(original_pix, reopened.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE
        if rotate == 0 and batch is items_from_content and shutil.which("pdftoppm"):
            dst_path = tmp_path / "tiling_dst.pdf"
            target.save(dst_path)
            _, src_ppm = _poppler_page(src_path, 1)
            _, dst_ppm = _poppler_page(dst_path, 1)
            diffs = [abs(a - b) for a, b in zip(src_ppm, dst_ppm, strict=True)]
            assert sum(diffs) / len(diffs) < 1.0
            assert max(diffs) <= 24


@pytest.mark.spec("tiling-patterns", "read", "interpret", "create", "preserve")
def test_draw_redraws_uncolored_tiling_pattern_with_distinct_fill_and_stroke_colors() -> None:
    pattern_stream = stream_object(
        "0 0 6 6 re f 6 6 6 6 re f",
        "/Type /Pattern /PatternType 1 /PaintType 2 /TilingType 1 "
        "/BBox [0 0 12 12] /XStep 12 /YStep 12 /Resources << >>",
    )
    page = page_with(
        "/PCS cs 0 0 1 /P1 scn /PCS CS 1 0 0 /P1 SCN 12 w 20 20 140 60 re B",
        resources="/ColorSpace << /PCS [/Pattern /DeviceRGB] >> /Pattern << /P1 6 0 R >>",
        objects={6: pattern_stream},
    )
    copy = _redraw(page)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE
    # Fill interior is blue, stroke border is red.
    assert copy.pixmap(dpi=72).pixel(50, 50) == page.pixmap(dpi=72).pixel(50, 50)
    assert copy.pixmap(dpi=72).pixel(20, 20) == page.pixmap(dpi=72).pixel(20, 20)


@pytest.mark.spec("tiling-patterns", "read", "interpret", "create", "preserve")
def test_draw_redraws_tiling_cell_with_clips_images_text_shadings_and_nested_tiling() -> None:
    nested_tiling = stream_object(
        "0.2 0.7 0.3 rg 0 0 4 4 re f",
        "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 8 8] /XStep 8 /YStep 8 /Resources << >>",
    )
    shading = (
        "<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 24 0] /Extend [true true] "
        "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 0.8 0] /C1 [0.2 0.2 1] /N 1 >> >>"
    )
    shading_pat = "<< /Type /Pattern /PatternType 2 /Shading 8 0 R >>"
    raster_img = stream_object(
        "\xff\x00\x00\x00\xff\x00\x00\x00\xff\xff\xff\x00",
        "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB /BitsPerComponent 8",
    )
    stencil_img = stream_object(
        "\x40\x00",
        "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ImageMask true /BitsPerComponent 1",
    )
    outer_tiling = stream_object(
        "q 2 2 28 28 re W n "
        "q /Pattern cs /PS scn 0 0 32 16 re f Q "
        "q /Pattern cs /PN scn 0 16 32 16 re f Q "
        "q /GS1 gs 10 0 0 10 2 2 cm /ImR Do Q "
        "q 0.9 0 0.6 rg 10 0 0 10 18 18 cm /ImS Do Q "
        "q 0 0 0 rg BT /F1 10 Tf 4 12 Td (X) Tj ET Q Q",
        "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
        "/BBox [0 0 32 32] /XStep 34 /YStep 34 "
        "/Resources << /Font << /F1 5 0 R >> /ExtGState << /GS1 12 0 R >> "
        "/Pattern << /PN 6 0 R /PS 7 0 R >> /XObject << /ImR 9 0 R /ImS 10 0 R >> >>",
    )
    page = page_with(
        "/Pattern cs /Outer scn 10 10 180 80 re f",
        resources="/Pattern << /Outer 11 0 R >>",
        objects={
            6: nested_tiling,
            7: shading_pat,
            8: shading,
            9: raster_img,
            10: stencil_img,
            11: outer_tiling,
            12: "<< /Type /ExtGState /ca 0.7 >>",
        },
    )
    copy = _redraw(page)
    assert _largest_difference(page.pixmap(dpi=72), copy.pixmap(dpi=72), []) <= ROUND_TRIP_TOLERANCE


def test_draw_tiling_pattern_clip_filtering_and_deduplication() -> None:
    p1 = stream_object(
        "1 0 0 rg 0 0 10 10 re f",
        "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
        "/BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources << >>",
    )
    p2 = stream_object(
        "0 0 1 rg 0 0 10 10 re f",
        "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
        "/BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources << >>",
    )
    stencil = stream_object(
        "\x00\x00",
        "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ImageMask true /BitsPerComponent 1",
    )
    page = page_with(
        "/Pattern cs /P1 scn 0 0 50 100 re f "
        "/Pattern cs /P2 scn 120 0 80 100 re f "
        "q /Pattern cs /P2 scn 40 0 0 40 130 30 cm /Im Do Q",
        resources="/Pattern << /P1 6 0 R /P2 7 0 R >> /XObject << /Im 8 0 R >>",
        objects={6: p1, 7: p2, 8: stencil},
    )
    # Clipping drops P1 and keeps both the path and stencil image referencing P2.
    clipped = page.content(clip=(110, 0, 200, 100))
    assert len(clipped.paths) == 1
    assert len(clipped.images) == 1
    target = pyropdf.Document()
    copy = target.pages.new((200, 100))
    with copy.draw() as canvas:
        for item in clipped:
            canvas.draw(item)
    assert copy.pixmap(dpi=72).pixel(150, 50) == page.pixmap(dpi=72).pixel(150, 50)
    # Both path and stencil share one written /PatternType 1 object in the target.
    tiling_objs = [
        xref
        for xref in target.objects
        if isinstance(target.objects[xref], pyropdf.Stream) and target.objects[xref].get("PatternType") == 1
    ]
    assert len(tiling_objs) == 1


def test_draw_refuses_unretained_and_unsupported_tiling_patterns_without_writing() -> None:
    # 1. Unretained TilingPattern raises TypeError.
    plain_path = _drawn().pages[0].content().paths[0]
    unretained = TilingPattern(bbox=Rect(0, 0, 10, 10), x_step=10, y_step=10, matrix=Matrix(1, 0, 0, 1, 0, 0))
    # 2. Tiling cell with a soft mask.
    smask_group = stream_object(
        "0.5 g 0 0 10 10 re f",
        "/Type /XObject /Subtype /Form /BBox [0 0 10 10] /Group << /S /Transparency /CS /DeviceGray >>",
    )
    smask_cell = stream_object(
        "/GS1 gs 1 0 0 rg 0 0 10 10 re f",
        "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
        "/BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources << /ExtGState << /GS1 7 0 R >> >>",
    )
    smask_page = page_with(
        "q 5 5 100 80 re W n /Pattern cs /P scn 0 0 100 80 re f Q",
        resources="/Pattern << /P 6 0 R >>",
        objects={
            6: smask_cell,
            7: "<< /Type /ExtGState /SMask << /Type /Mask /S /Luminosity /G 8 0 R >> >>",
            8: smask_group,
        },
    )
    smask_path = smask_page.content().paths[0]
    # 3. Tiling cell with a Type 1 function-based shading.
    fn_cell = stream_object(
        "/Pattern cs /PS scn 0 0 10 10 re f",
        "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
        "/BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources << /Pattern << /PS 7 0 R >> >>",
    )
    fn_page = page_with(
        "/Pattern cs /P scn 0 0 50 50 re f",
        resources="/Pattern << /P 6 0 R >>",
        objects={
            6: fn_cell,
            7: "<< /Type /Pattern /PatternType 2 /Shading 8 0 R >>",
            8: "<< /ShadingType 1 /ColorSpace /DeviceRGB /Domain [0 1 0 1] /Function 9 0 R >>",
            9: stream_object("{ pop 0 0 }", "/FunctionType 4 /Domain [0 1 0 1] /Range [0 1 0 1 0 1]"),
        },
    )
    fn_path = fn_page.content().paths[0]
    # 4. Excessively nested tiling patterns (> MAX_TILING_DEPTH).
    deep_objects: dict[int, str] = {
        12: stream_object(
            "1 0 0 rg 0 0 10 10 re f",
            "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
            "/BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources << >>",
        )
    }
    for obj_num in range(11, 5, -1):
        deep_objects[obj_num] = stream_object(
            "/Pattern cs /Next scn 0 0 10 10 re f",
            "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
            f"/BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources << /Pattern << /Next {obj_num + 1} 0 R >> >>",
        )
    deep_page = page_with(
        "/Pattern cs /P scn 0 0 40 40 re f",
        resources="/Pattern << /P 6 0 R >>",
        objects=deep_objects,
    )
    deep_path = deep_page.content().paths[0]

    target = pyropdf.Document()
    copy = target.pages.new((200, 100))
    before = target.to_bytes(), target.history.steps
    with copy.draw() as canvas, pytest.raises(TypeError, match="TilingPattern"):
        canvas.draw(dataclasses.replace(plain_path, fill=unretained))
    for bad_path, pattern in (
        (smask_path, "soft mask"),
        (fn_path, "shading"),
        (deep_path, "deeply"),
    ):
        with copy.draw() as canvas, pytest.raises(ValueError, match=pattern):
            canvas.draw(bad_path)
    assert (target.to_bytes(), target.history.steps) == before


def test_inline_image_and_tiling_pattern_enforce_budgets_atomically() -> None:
    # 1. Extraction enforces total_decompressed_size on inline images.
    big_inline_pdf = page_with(
        "q 50 0 0 50 0 0 cm BI /W 128 /H 128 /BPC 8 /CS /G /F /Fl ID "
        + zlib.compress(b"\x80" * (128 * 128)).decode("latin1")
        + " EI Q"
    ).document.to_bytes()
    limited_src = pyropdf.open(
        big_inline_pdf,
        limits=pyropdf.DocumentLimits(max_total_decompressed_size=1024),
    )
    with pytest.raises(pyropdf.LimitError) as refused:
        limited_src.pages[0].content()
    assert refused.value.code == "total_decompressed_size"

    # 2. Target canvas enforces memory_size atomically when drawing inline image.
    inline_item = pyropdf.open(big_inline_pdf).pages[0].content().images[0]
    tight_target = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=8_192))
    blank = tight_target.pages.new((200, 100))
    before = tight_target.to_bytes(), tight_target.history.steps
    with pytest.raises(pyropdf.LimitError) as refused, blank.draw() as canvas:
        canvas.draw(inline_item)
    assert refused.value.code == "memory_size"
    assert (tight_target.to_bytes(), tight_target.history.steps) == before

    # 3. Target canvas enforces memory_size and cancellation atomically on TilingPattern.
    many_rects = " ".join(f"{i % 10} {i // 10} 1 1 re f" for i in range(400))
    tiling_pdf = page_with(
        "/Pattern cs /P scn 0 0 100 100 re f",
        resources="/Pattern << /P 6 0 R >>",
        objects={
            6: stream_object(
                f"1 0 0 rg {many_rects}",
                "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
                "/BBox [0 0 20 50] /XStep 20 /YStep 50 /Resources << >>",
            )
        },
    ).document.to_bytes()
    tiling_path = pyropdf.open(tiling_pdf).pages[0].content().paths[0]
    tight_tiling = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=8_192))
    blank_tiling = tight_tiling.pages.new((200, 100))
    before_tiling = tight_tiling.to_bytes(), tight_tiling.history.steps
    with pytest.raises(pyropdf.LimitError) as refused, blank_tiling.draw() as canvas:
        canvas.draw(tiling_path)
    assert refused.value.code == "memory_size"
    assert (tight_tiling.to_bytes(), tight_tiling.history.steps) == before_tiling

    # 4. Cancellation refuses tiling draw cleanly without writing.
    token = pyropdf.CancellationToken()
    cancelled_target = pyropdf.Document(limits=pyropdf.DocumentLimits(cancellation=token))
    blank_cancel = cancelled_target.pages.new((200, 100))
    before_steps = cancelled_target.history.steps
    with pytest.raises(pyropdf.LimitError) as refused, blank_cancel.draw() as canvas:  # noqa: PT012
        token.cancel()
        canvas.draw(tiling_path)
    assert refused.value.code == "cancelled"
    assert cancelled_target.history.steps == before_steps


def _shared_font_document(*, vertical: bool = False) -> bytes:
    """Two dictionaries select the same subset glyphs but encode distinct Unicode text."""
    base = pyropdf.Document()
    page = base.pages.new((220, 200))
    with page.draw() as canvas:
        canvas.text("AVA", (20, 80), font=pyropdf.Font.from_file(JAPANESE), size=17)
    (subset,) = base.subset_fonts()
    assert subset.kept < subset.glyphs
    with pikepdf.open(io.BytesIO(base.to_bytes())) as pdf:
        page_dict = pdf.pages[0]
        first = next(iter(page_dict.Resources.Font.values()))
        cmap = first.ToUnicode.read_bytes()
        codes = {text: code for code, text in re.findall(rb"<([0-9A-Fa-f]{4})> <([0-9A-Fa-f]{4})>", cmap)}
        a, v = codes[b"0041"], codes[b"0056"]
        if vertical:
            first.Encoding = pikepdf.Name("/Identity-V")
        second = pdf.make_indirect(pikepdf.Dictionary(dict(first.items())))
        second.ToUnicode = pdf.make_stream(cmap.replace(b"<0041>", b"<0058>"))
        page_dict.Resources.Font = pikepdf.Dictionary(First=first, Second=second)
        page_dict.Contents = pdf.make_stream(
            b"q .12 .35 .64 rg BT /First 17 Tf 1 .1 -.05 1 45 140 Tm [<"
            + a
            + b"> 180 <"
            + v
            + a
            + b">] TJ /Second 17 Tf .95 .2 -.10 1 118 131 Tm <"
            + a
            + b"> Tj ET Q"
        )
        pdf.add_blank_page(page_size=(220, 200))
        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue()


def _poppler_page(path: FilePath, page: int) -> tuple[str, bytes]:
    """Read one saved page's text and RGB pixels independently."""
    select = ["-f", str(page), "-l", str(page)]
    text = subprocess.run(  # noqa: S603 - fixed development oracle and a local fixture.
        [shutil.which("pdftotext") or "pdftotext", *select, str(path), "-"], check=True, capture_output=True
    ).stdout.decode()
    ppm = subprocess.run(  # noqa: S603
        [shutil.which("pdftoppm") or "pdftoppm", *select, "-r", "72", "-singlefile", str(path)],
        check=True,
        capture_output=True,
    ).stdout
    return "".join(text.split()), ppm.split(b"\n", 3)[3]


@pytest.mark.spec("text-objects", "read", "interpret", "create", "preserve")
@pytest.mark.spec("type0-fonts", "read", "interpret", "preserve")
@pytest.mark.parametrize("same_document", [False, True])
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("vertical", [False, True])
@pytest.mark.skipif(not shutil.which("pdftoppm") or not shutil.which("pdftotext"), reason="Poppler is unavailable")
def test_redraw_positioned_subset_runs_with_shared_program_and_distinct_mappings(
    tmp_path: FilePath, *, same_document: bool, incremental: bool, vertical: bool
) -> None:
    original = _shared_font_document(vertical=vertical)
    source = pyropdf.open(original)
    target = source if same_document else pyropdf.open(original)
    runs = source.pages[0].content().text_runs
    assert [run.text for run in runs] == ["AVA", "X"]
    assert len(runs[0].glyphs) == 3
    with target.pages[1].draw() as canvas:
        for run in runs:
            canvas.draw(run)
    output = target.to_bytes(incremental=incremental)
    if incremental:
        assert output.startswith(original)
    reopened = pyropdf.open(output)
    original_glyphs = [glyph for run in runs for glyph in run.glyphs]
    copied_glyphs = [glyph for run in reopened.pages[1].content().text_runs for glyph in run.glyphs]
    assert [(g.text, g.glyph_id) for g in copied_glyphs] == [(g.text, g.glyph_id) for g in original_glyphs]
    for actual, expected in zip(copied_glyphs, original_glyphs, strict=True):
        assert tuple(actual.matrix) == pytest.approx(tuple(expected.matrix), abs=0.0002)
        assert actual.advance == pytest.approx(expected.advance, abs=0.0002)
    source_path, result_path = tmp_path / "source.pdf", tmp_path / "result.pdf"
    source_path.write_bytes(original)
    result_path.write_bytes(output)
    text, pixels = _poppler_page(source_path, 1)
    assert "X" in text
    assert min(pixels) < 220
    assert _poppler_page(result_path, 1) == (text, pixels)
    copied_text, copied_pixels = _poppler_page(result_path, 2)
    assert copied_text == text
    assert max(abs(a - b) for a, b in zip(pixels, copied_pixels, strict=True)) <= ROUND_TRIP_TOLERANCE
    with pikepdf.open(result_path) as pdf:
        fonts = list(pdf.pages[1].Resources.Font.values())
        assert len(fonts) == 2
        assert fonts[0].objgen != fonts[1].objgen
        assert fonts[0].DescendantFonts[0].objgen == fonts[1].DescendantFonts[0].objgen
        assert {font.ToUnicode.read_bytes() for font in fonts} == {
            font.ToUnicode.read_bytes() for font in pdf.pages[0].Resources.Font.values()
        }
        assert all(str(font.Encoding) == ("/Identity-V" if vertical else "/Identity-H") for font in fonts)


def test_a_redrawn_run_keeps_its_captured_font_after_a_descendant_changes() -> None:
    source = pyropdf.open(_shared_font_document())
    old = source.pages[0].content().text_runs[0]
    to_unicode = source.pages[0].obj["Resources"]["Font"]["First"]["ToUnicode"]
    to_unicode.write(to_unicode.read().replace(b"<0041>", b"<005a>"))
    assert source.pages[0].content().text_runs[0].text == "ZVZ"
    with source.pages[1].draw() as canvas:
        canvas.draw(old)
    assert pyropdf.open(source.to_bytes()).pages[1].text().replace(" ", "").strip() == "AVA"


def test_redraw_refuses_unavailable_or_fallback_sources_without_an_edit() -> None:
    page = page_with("BT /F1 17 Tf 12 42 Td (Font) Tj ET")
    run = page.content().text_runs[0]
    fallback = page_with("BT /Missing 17 Tf 12 42 Td (Font) Tj ET").content().text_runs[0]
    target = pyropdf.Document()
    blank = target.pages.new((200, 100))
    before = target.to_bytes(), target.history.steps
    with blank.draw() as canvas, pytest.raises(ValueError, match="fallback"):
        canvas.draw(fallback)
    with blank.draw() as canvas, pytest.raises(ValueError, match="source"):
        canvas.draw(pickle.loads(pickle.dumps(run)))  # noqa: S301 - value created here.
    page.document.close()
    with blank.draw() as canvas, pytest.raises(pyropdf.DocumentClosedError):
        canvas.draw(run)
    assert (target.to_bytes(), target.history.steps) == before


def test_redraw_charges_text_commands_to_the_target_canvas() -> None:
    page = page_with("BT /F1 10 Tf 2 80 Td (" + "A" * 128 + ") Tj ET")
    target = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=40_000))
    blank = target.pages.new((200, 100))
    before = target.to_bytes(), target.history.steps
    with pytest.raises(pyropdf.LimitError) as refused, blank.draw() as canvas:
        canvas.draw(page.content().text_runs[0])
    assert refused.value.code == "memory_size"
    assert (target.to_bytes(), target.history.steps) == before


def test_copying_a_captured_font_observes_source_cancellation() -> None:
    token = pyropdf.CancellationToken()
    source = pyropdf.open(_shared_font_document(), limits=pyropdf.DocumentLimits(cancellation=token))
    run = source.pages[0].content().text_runs[0]
    target = pyropdf.Document()
    blank = target.pages.new((220, 200))
    before = target.to_bytes(), target.history.steps
    with pytest.raises(pyropdf.LimitError) as refused, blank.draw() as canvas:  # noqa: PT012 - cancel before commit.
        canvas.draw(run)
        token.cancel()
    assert refused.value.code == "cancelled"
    assert (target.to_bytes(), target.history.steps) == before


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
@pytest.mark.skipif(not shutil.which("pdftoppm") or not shutil.which("pdftotext"), reason="Poppler is unavailable")
def test_redraw_preserves_text_paint_and_clipping(tmp_path: FilePath, mode: int) -> None:
    page = page_with(
        f"q 9 8 84 79 re W n 1 .1 0 1.3 0 -12 cm .1 .5 .75 rg .8 .2 .1 RG .7 w "
        f"BT /F1 24 Tf {mode} Tr 1 .1 -.1 1 14 55 Tm [(Pa) 140 (int)] TJ ET Q"
    )
    target = pyropdf.Document()
    blank = target.pages.new((page.rect.width, page.rect.height))
    with blank.draw() as canvas:
        canvas.draw(page.content().text_runs[0])
    source_path, result_path = tmp_path / "paint.pdf", tmp_path / "redrawn.pdf"
    source_path.write_bytes(page.document.to_bytes())
    result_path.write_bytes(target.to_bytes())
    text, pixels = _poppler_page(source_path, 1)
    copied_text, copied_pixels = _poppler_page(result_path, 1)
    assert copied_text == text == "Paint"
    assert max(abs(a - b) for a, b in zip(pixels, copied_pixels, strict=True)) <= ROUND_TRIP_TOLERANCE
