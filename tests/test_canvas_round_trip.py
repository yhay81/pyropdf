"""The Canvas round trip: redraw what ``Page.content()`` reads of the corpus.

Ported from pylopdf ebb01d3c. Every path and image item of every page of
the bundled corpus is drawn again with ``Canvas.draw`` on a blank page of
the same size in another document, and the two renderings agree within
``ROUND_TRIP_TOLERANCE`` levels per channel at 72 dpi outside the boxes of
the items that a canvas cannot draw yet: text runs; items under a soft
mask, unsupported pattern paints, inline images, and image masks; and of annotations
without an appearance stream, which the renderer draws but
``Page.content()`` does not report. Those items are
listed with the reason in ``_not_drawn``, and the documentation of
``Canvas.draw`` publishes the same tolerance and exceptions.

Axial and radial shadings are sampled only when redrawn. Function-based and
mesh shadings and tiling cells remain outside the round trip.
"""

from __future__ import annotations

import dataclasses
import math
import operator
import pickle
import struct
import zlib
from pathlib import Path as FilePath

import pytest
from test_content_model import page_with, stencil_page, stream_object

import pyropdf
from pyropdf import Color, ColorStop, Image, LinearGradient, Path, Point, ShadingPattern, ShadingType, Stroke, TextRun

CORPUS = FilePath(__file__).parent / "assets" / "real_world"

# The largest difference of one channel, in levels of 255, outside the
# boxes of the items not drawn.
ROUND_TRIP_TOLERANCE = 16


def _not_drawn(item: TextRun | Path | Image) -> str | None:
    """Return why a canvas cannot draw an item yet, or ``None``."""
    if isinstance(item, TextRun):
        return "text run"
    if item.soft_mask is not None or any(group.soft_mask is not None for group in item.context.groups):
        return "soft mask"
    if isinstance(item, Image):
        return "inline image or image mask" if item.xref == 0 or item.is_stencil else None
    paints = (item.fill, None if item.stroke is None else item.stroke.paint)
    if any(
        paint is not None
        and not isinstance(paint, Color)
        and not (isinstance(paint, ShadingPattern) and paint.shading_type in (ShadingType.AXIAL, ShadingType.RADIAL))
        for paint in paints
    ):
        return "pattern paint"
    return None


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
def test_canvas_redraws_the_paths_and_images_of_every_page(name: str) -> None:
    doc = pyropdf.open(CORPUS / name)
    for page in doc.pages:
        target = pyropdf.Document()
        copy = target.pages.new((page.rect.width, page.rect.height))
        # The renderer draws an annotation without an appearance stream,
        # which ``Page.content()`` does not report.
        hidden = [annotation.rect for annotation in page.annotations if "AP" not in doc.objects[annotation.xref]]
        with copy.draw() as canvas:
            for item in page.content():
                if isinstance(item, TextRun) or _not_drawn(item) is not None:
                    hidden.append(item.bbox)
                else:
                    canvas.draw(item)
        original = page.pixmap(dpi=72)
        redrawn = copy.pixmap(dpi=72)
        assert (redrawn.width, redrawn.height) == (original.width, original.height)
        difference = _largest_difference(original, redrawn, hidden)
        assert difference <= ROUND_TRIP_TOLERANCE, (name, page.index, difference)


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
def test_draw_refuses_stencil_images_without_writing(*, inline: bool) -> None:
    page = stencil_page(inline=inline)
    image = page.content().images[0]
    assert image.xref == (0 if inline else 6)
    target = pyropdf.Document()
    copy = target.pages.new((200, 100))
    before = target.to_bytes(), target.history.steps
    with copy.draw() as canvas, pytest.raises(ValueError, match="stencil"):
        canvas.draw(image)
    assert (target.to_bytes(), target.history.steps) == before
