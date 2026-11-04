"""The Canvas round trip: redraw what ``Page.content()`` reads of the corpus.

Ported from pylopdf ebb01d3c. Every path and image item of every page of
the bundled corpus is drawn again with ``Canvas.draw`` on a blank page of
the same size in another document, and the two renderings agree within
``ROUND_TRIP_TOLERANCE`` levels per channel at 72 dpi outside the boxes of
the items that a canvas cannot draw yet: text runs; items under a soft
mask, pattern paints, inline images, and image masks; and of annotations
without an appearance stream, which the renderer draws but
``Page.content()`` does not report. Those items are
listed with the reason in ``_not_drawn``, and the documentation of
``Canvas.draw`` publishes the same tolerance and exceptions.

A pattern paint reports its kind and its matrix, not its stops or its cell, so
a canvas cannot draw one again, a gradient that it drew itself included. The
corpus has none today, so the exception costs its pages nothing.
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

import pyropdf
from pyropdf import Color, ColorStop, Image, LinearGradient, Path, Point, ShadingPattern, Stroke, TextRun

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
        return "inline image or image mask" if item.xref == 0 else None
    paints = (item.fill, None if item.stroke is None else item.stroke.paint)
    if any(paint is not None and not isinstance(paint, Color) for paint in paints):
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


def test_draw_refuses_a_gradient_read_back_and_writes_nothing() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    with page.draw() as canvas:
        canvas.rect((0, 0, 200, 100), fill=_gradient(), stroke=Stroke(paint=_gradient(), width=2))
    item = page.content().paths[0]
    assert isinstance(item.fill, ShadingPattern)
    assert item.stroke is not None
    target = pyropdf.Document()
    copy = target.pages.new((200, 200))
    before = target.to_bytes(), target.history.steps
    for read_back in (item, dataclasses.replace(item, fill=None), dataclasses.replace(item, stroke=None)):
        with pytest.raises(TypeError, match="ShadingPattern"), copy.draw() as canvas:
            canvas.draw(read_back)
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
