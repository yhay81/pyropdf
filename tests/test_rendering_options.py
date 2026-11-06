"""The options of page.pixmap: clip, background, annotations, colorspace, hinting, and glyph_cache.

Ported from pylopdf's tests/test_rendering_api.py at 036be111. Intended
differences:

- ``doc.render`` assertions move to #119 and ``page.svg`` ones to #120.
- pyropdf has no ``page.annotations.add`` yet, so the annotated page is
  built with a Highlight whose appearance paints its rectangle red.
- ``test_rgb_pixmaps_insert_without_a_soft_mask`` waits on Phase 2's
  drawing API, which inserts a pixmap into a page.
- A clip's raster has the size limits of one raster, as PyMuPDF renders a
  clip, where pylopdf applies them to the whole page (issue 605).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from test_content import build_raw_pdf
from test_content_model import page_with, stream_object
from test_pixmap import png_header

import pyropdf
from pyropdf import Colorspace, Matrix, Pixmap

REAL_WORLD = Path(__file__).parent / "assets" / "real_world"

A1 = (2384, 1684)
"""A landscape A1 page, in points."""

PAGE_BOUND = 524_288
"""The longest side, in pixels, of a page under the matrix that a clip renders."""

PNG_GRAY = 0
PNG_RGB = 2
PNG_GRAY_ALPHA = 4
PNG_RGBA = 6


def _page() -> pyropdf.Page:
    """Open a 200 by 100 page that shows one line of text."""
    return page_with("BT /F1 24 Tf 10 40 Td (Rendered) Tj ET")


def _annotated_page() -> pyropdf.Page:
    """Open a blank 200 by 100 page with a Highlight that paints its rectangle red."""
    return page_with(
        "",
        annots="6 0 R",
        objects={
            6: "<< /Type /Annot /Subtype /Highlight /Rect [20 20 180 80] /AP << /N 7 0 R >> >>",
            7: stream_object("1 0 0 rg 0 0 160 60 re f", "/Type /XObject /Subtype /Form /BBox [0 0 160 60]"),
        },
    )


def _crop(pixmap: Pixmap, x0: int, y0: int, x1: int, y1: int) -> bytes:
    """Return the samples of the pixels from ``(x0, y0)`` up to ``(x1, y1)``."""
    data = bytes(pixmap)
    channels = pixmap.channels
    return b"".join(data[y * pixmap.stride + x0 * channels : y * pixmap.stride + x1 * channels] for y in range(y0, y1))


def test_pixmap_defaults_to_opaque_rgb_on_white() -> None:
    pixmap = _page().pixmap()

    assert (pixmap.width, pixmap.height) == (200, 100)
    assert (pixmap.channels, pixmap.alpha, pixmap.stride) == (3, False, 600)
    assert pixmap.colorspace is Colorspace.RGB
    assert len(bytes(pixmap)) == 200 * 100 * 3
    assert bytes(pixmap)[:3] == b"\xff\xff\xff"
    assert repr(pixmap) == "<Pixmap 200x100 rgb>"
    assert png_header(pixmap.to_bytes())[2] == PNG_RGB
    view = memoryview(pixmap)
    assert view.nbytes == len(bytes(pixmap))


def test_pixmap_with_alpha_keeps_transparency() -> None:
    pixmap = _page().pixmap(alpha=True)

    assert (pixmap.channels, pixmap.alpha, pixmap.stride) == (4, True, 800)
    assert bytes(pixmap)[:4] == b"\x00\x00\x00\x00"
    assert repr(pixmap) == "<Pixmap 200x100 rgb alpha>"
    assert png_header(pixmap.to_bytes())[2] == PNG_RGBA


def test_background_paints_beneath_the_page() -> None:
    page = _page()

    assert bytes(page.pixmap(background=(255, 0, 0)))[:3] == b"\xff\x00\x00"
    assert bytes(page.pixmap(alpha=True, background=(0, 0, 255, 128)))[:4] == b"\x00\x00\xff\x80"
    with pytest.raises(ValueError, match="alpha=False needs an opaque background"):
        page.pixmap(background=(0, 0, 255, 128))


@pytest.mark.parametrize(
    "background", [(0, 0), (0, 0, 0, 0, 0), (0, 0, 256), (0, -1, 0), (0, 0, 1.0), (True, 0, 0), "red"]
)
def test_background_takes_three_or_four_bytes(background: object) -> None:
    with pytest.raises(ValueError, match="background"):
        _page().pixmap(background=background)  # ty: ignore[invalid-argument-type]


def test_matrix_scales_each_axis_and_excludes_dpi() -> None:
    page = _page()

    pixmap = page.pixmap(matrix=Matrix(2, 3))
    assert (pixmap.width, pixmap.height) == (400, 300)
    assert page.pixmap(matrix=(0.5, 0, 0, 0.5, 0, 0)).width == 100
    # A translation moves the pixmap's origin in pymupdf, not its pixels.
    assert bytes(page.pixmap(matrix=Matrix(1, 0, 0, 1, 30, 40))) == bytes(page.pixmap())
    assert page.pixmap(dpi=36).width == 100
    with pytest.raises(ValueError, match="dpi or matrix, not both"):
        page.pixmap(matrix=Matrix(4, 4), dpi=36)


def test_negative_indexes_render_the_same_page() -> None:
    doc = pyropdf.open((REAL_WORLD / "pdfium-type3.pdf").read_bytes())

    assert bytes(doc.pages[-1].pixmap(dpi=36, alpha=True)) == bytes(
        doc.pages[len(doc.pages) - 1].pixmap(dpi=36, alpha=True)
    )


def test_annotations_false_leaves_annotation_appearances_out() -> None:
    page = _annotated_page()
    blank = page_with("").pixmap(annotations=False)

    with_annotations = page.pixmap()
    without = page.pixmap(annotations=False)

    assert bytes(with_annotations) != bytes(without)
    assert set(bytes(without)) == {255}
    assert len(bytes(without)) == len(bytes(blank))
    # The highlight covers (20, 20) to (180, 80) in display space too.
    assert _crop(with_annotations, 100, 50, 101, 51) == b"\xff\x00\x00"


@pytest.mark.spec("appearance-streams", "interpret")
def test_annotations_false_changes_a_corpus_page_with_annotations() -> None:
    # The second page of the 1040 form shows its widgets.
    page = pyropdf.open((REAL_WORLD / "f1040.pdf").read_bytes()).pages[1]

    with_annotations = page.pixmap(dpi=36)
    without = page.pixmap(dpi=36, annotations=False)

    assert (with_annotations.width, with_annotations.height) == (without.width, without.height)
    assert bytes(with_annotations) != bytes(without)


def test_annotations_must_be_a_bool() -> None:
    with pytest.raises(TypeError, match="annotations must be a bool"):
        _page().pixmap(annotations=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="alpha must be a bool"):
        _page().pixmap(alpha=None)  # ty: ignore[invalid-argument-type]


def _corpus_page() -> pyropdf.Page:
    """Open a corpus page with text in an embedded font."""
    return pyropdf.open(REAL_WORLD / "pdf20-simple.pdf").pages[0]


def test_hinting_changes_text_and_is_off_by_default() -> None:
    page = _corpus_page()
    unhinted = bytes(page.pixmap())
    assert bytes(page.pixmap(hinting=False)) == unhinted
    assert bytes(page.pixmap(hinting=True)) != unhinted


def test_a_render_with_one_hinting_after_the_other_equals_a_fresh_one() -> None:
    page = _corpus_page()
    fresh_hinted = bytes(_corpus_page().pixmap(hinting=True))
    fresh_unhinted = bytes(_corpus_page().pixmap())
    assert bytes(page.pixmap()) == fresh_unhinted
    assert bytes(page.pixmap(hinting=True)) == fresh_hinted
    assert bytes(page.pixmap()) == fresh_unhinted


def test_document_render_hints_as_page_pixmap_does() -> None:
    doc = pyropdf.open(REAL_WORLD / "pdf20-simple.pdf")
    hinted = [page.pixmap(hinting=True).to_bytes("png") for page in doc.pages]
    assert doc.render(hinting=True) == hinted
    assert doc.render() == [page.pixmap().to_bytes("png") for page in doc.pages]
    assert doc.render() != hinted


def test_hinting_must_be_a_bool() -> None:
    with pytest.raises(TypeError, match="hinting must be a bool"):
        _page().pixmap(hinting=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="hinting must be a bool"):
        pyropdf.open(REAL_WORLD / "pdf20-simple.pdf").render(hinting=None)  # ty: ignore[invalid-argument-type]


def test_glyph_cache_changes_text_and_is_off_by_default() -> None:
    page = _corpus_page()
    filled = bytes(page.pixmap())
    assert bytes(page.pixmap(glyph_cache=False)) == filled
    cached = bytes(page.pixmap(glyph_cache=True))
    assert cached != filled
    assert cached != bytes(page.pixmap(hinting=True))


def test_a_render_with_one_glyph_cache_setting_after_the_other_equals_a_fresh_one() -> None:
    page = _corpus_page()
    fresh_cached = bytes(_corpus_page().pixmap(glyph_cache=True))
    fresh_filled = bytes(_corpus_page().pixmap())
    assert bytes(page.pixmap()) == fresh_filled
    assert bytes(page.pixmap(glyph_cache=True)) == fresh_cached
    assert bytes(page.pixmap()) == fresh_filled


def test_document_render_caches_glyphs_as_page_pixmap_does() -> None:
    doc = pyropdf.open(REAL_WORLD / "f1040.pdf")
    cached = [page.pixmap(glyph_cache=True).to_bytes("png") for page in doc.pages]
    assert doc.render(glyph_cache=True) == cached
    assert doc.render(glyph_cache=False) == [page.pixmap().to_bytes("png") for page in doc.pages]
    assert doc.render() != cached


def test_the_web_limits_accept_the_glyph_cache() -> None:
    """``DocumentLimits.web()`` does not refuse the cache, whose memory is outside the budget (#685)."""
    with pyropdf.open(REAL_WORLD / "pdf20-simple.pdf", limits=pyropdf.DocumentLimits.web()) as doc:
        assert bytes(doc.pages[0].pixmap(glyph_cache=True)) == bytes(_corpus_page().pixmap(glyph_cache=True))


def test_glyph_cache_must_be_a_bool() -> None:
    with pytest.raises(TypeError, match="glyph_cache must be a bool"):
        _page().pixmap(glyph_cache=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="glyph_cache must be a bool"):
        pyropdf.open(REAL_WORLD / "pdf20-simple.pdf").render(glyph_cache=None)  # ty: ignore[invalid-argument-type]


def test_gray_holds_the_luma_of_each_color() -> None:
    page = _page()

    gray = page.pixmap(colorspace="gray")
    assert (gray.channels, gray.stride, gray.colorspace) == (1, 200, Colorspace.GRAY)
    assert repr(gray) == "<Pixmap 200x100 gray>"
    assert png_header(gray.to_bytes())[2] == PNG_GRAY
    rgb = bytes(page.pixmap())
    assert bytes(gray) == bytes(
        (77 * r + 150 * g + 29 * b + 128) >> 8 for r, g, b in zip(rgb[::3], rgb[1::3], rgb[2::3], strict=True)
    )
    # BT.601 luma of red, rounded as pylopdf rounds it.
    assert bytes(page.pixmap(background=(255, 0, 0), colorspace=Colorspace.GRAY))[0] == 77

    gray_alpha = page.pixmap(colorspace="gray", alpha=True)
    assert (gray_alpha.channels, gray_alpha.stride) == (2, 400)
    assert repr(gray_alpha) == "<Pixmap 200x100 gray alpha>"
    assert png_header(gray_alpha.to_bytes())[2] == PNG_GRAY_ALPHA
    rgba = bytes(page.pixmap(alpha=True))
    assert bytes(gray_alpha)[1::2] == rgba[3::4]
    with pytest.raises(ValueError, match="colorspace must be 'gray' or 'rgb'"):
        page.pixmap(colorspace="cmyk")  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("matrix", [(1, 0, 0, 1), (1.5, 0, 0, 1.5), (0, 2, -2, 0), (1, 0.5, 0, 1)])
def test_a_clip_renders_the_pixels_of_the_full_raster(matrix: tuple[float, float, float, float]) -> None:
    page = pyropdf.open((REAL_WORLD / "pdfium-type3.pdf").read_bytes()).pages[0]
    full = page.pixmap(matrix=matrix, alpha=True)
    clip = (20.3, 15.7, 140.2, 90.9)

    clipped = page.pixmap(matrix=matrix, alpha=True, clip=clip)

    # The raster starts at the first whole pixel of the page, and the clip
    # at the pixel holding its bounding box's corner.
    a, b, c, d = matrix
    width, height = page.rect.x1, page.rect.y1
    left = math.ceil(min(a * x + c * y for x in (0, width) for y in (0, height)) - 0.001)
    top = math.ceil(min(b * x + d * y for x in (0, width) for y in (0, height)) - 0.001)
    x0 = math.floor(min(a * x + c * y for x in clip[::2] for y in clip[1::2])) - left
    y0 = math.floor(min(b * x + d * y for x in clip[::2] for y in clip[1::2])) - top
    assert clipped.width < full.width
    assert clipped.height < full.height
    assert clipped.resolution == full.resolution
    assert bytes(clipped) == _crop(full, x0, y0, x0 + clipped.width, y0 + clipped.height)


def test_a_clip_outside_the_page_raises() -> None:
    page = _page()

    with pytest.raises(pyropdf.PdfError, match="clip does not intersect the rendered page"):
        page.pixmap(clip=(300, 300, 400, 400))
    with pytest.raises(ValueError, match="clip must be"):
        page.pixmap(clip=(10, 10, 5, 20))
    with pytest.raises(ValueError, match="clip must be"):
        page.pixmap(clip=(0, 0, 1))


def _sized(size: tuple[float, float], content: str) -> pyropdf.Page:
    """Open a page of ``size`` points that paints ``content``, with Helvetica as /F1."""
    width, height = size
    return pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: f"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 {width} {height}] >>",
                3: "<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
                4: stream_object(content),
                5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            }
        )
    ).pages[0]


@pytest.mark.parametrize("scale", [4, 5, 20])
def test_a_clip_of_a_large_page_renders_at_scales_the_whole_page_cannot(scale: int) -> None:
    # A line from display (10, 10) to (90, 90) on an A1 page, which passes
    # 64,000,000 pixels from 4 times on (issue 605).
    page = _sized(A1, "2 w 10 1674 m 90 1594 l S")
    with pytest.raises(ValueError, match="exceeds the 64000000-pixel limit"):
        page.pixmap(matrix=(scale, 0, 0, scale))

    pixmap = page.pixmap(matrix=(scale, 0, 0, scale), clip=(0, 0, 100, 100))

    assert (pixmap.width, pixmap.height) == (100 * scale, 100 * scale)
    assert _crop(pixmap, 50 * scale, 50 * scale, 50 * scale + 1, 50 * scale + 1) == b"\x00\x00\x00"
    assert _crop(pixmap, 20 * scale, 80 * scale, 20 * scale + 1, 80 * scale + 1) == b"\xff\xff\xff"


def test_a_clip_of_a_large_page_holds_the_pixels_of_the_whole_page() -> None:
    page = _sized(A1, "2 w 10 1674 m 90 1594 l S")
    full = page.pixmap(matrix=(3, 0, 0, 3))

    clipped = page.pixmap(matrix=(3, 0, 0, 3), clip=(0, 0, 100, 100))

    assert (clipped.width, clipped.height) == (300, 300)
    assert bytes(clipped) == _crop(full, 0, 0, 300, 300)


def test_ocr_reads_a_clip_of_a_large_page_at_300_dpi() -> None:
    pytest.importorskip("pylopdf_ocr_models")
    page = _sized(A1, "BT /F1 36 Tf 20 1609 Td (Region 605) Tj ET")
    with pytest.raises(ValueError, match="exceeds the 64000000-pixel limit"):
        page.pixmap(dpi=300)

    # One tile holds the whole clip, 1,667 pixels square at 300 dpi.
    words = page.ocr(clip=(0, 0, 400, 400), tile_size=1792)

    assert [word.text for word in words] == ["Region 605"]
    assert 0 < words[0].bbox.x0 < words[0].bbox.x1 < 400
    assert 0 < words[0].bbox.y0 < words[0].bbox.y1 < 400


def _line(start: tuple[float, float], end: tuple[float, float], width: float) -> str:
    """Return a content stream that strokes from ``start`` to ``end`` ``width`` wide."""
    return f"{width!r} w {start[0]!r} {start[1]!r} m {end[0]!r} {end[1]!r} l S"


def test_a_clip_renders_a_page_up_to_the_bound() -> None:
    # The page's long side measures the bound, and a 64 pixel clip sits in
    # its far corner. A line 2 pixels wide comes from near the opposite
    # corner, which lies a page away; drawn from 256 pixels away instead,
    # it moves by less than 1/16 of a pixel (evidence/clip-precision-605).
    scale = PAGE_BOUND / A1[0]
    unit = 1 / scale
    clip = (A1[0] - 64 * unit, A1[1] - 64 * unit, *A1)
    apex = (0.3141592653589793, A1[1] - 0.2718281828459045)
    tip = (A1[0] + 16 * unit, -13 * unit)
    start = (tip[0] + (apex[0] - tip[0]) / 2**11, tip[1] + (apex[1] - tip[1]) / 2**11)

    far = _sized(A1, _line(apex, tip, 2 * unit)).pixmap(matrix=(scale, 0, 0, scale), clip=clip)
    near = _sized(A1, _line(start, tip, 2 * unit)).pixmap(matrix=(scale, 0, 0, scale), clip=clip)

    assert (far.width, far.height) == (64, 64)
    assert min(bytes(far)) == 0
    assert max(abs(a - b) for a, b in zip(bytes(far), bytes(near), strict=True)) < 16


@pytest.mark.parametrize("size", [A1, A1[::-1]], ids=["width", "height"])
def test_a_clip_of_a_page_beyond_the_bound_raises(size: tuple[int, int]) -> None:
    scale = (PAGE_BOUND + 1) / max(size)

    with pytest.raises(pyropdf.LimitError, match=rf"exceeds the limit of {PAGE_BOUND} pixels") as error:
        _sized(size, "").pixmap(matrix=(scale, 0, 0, scale), clip=(0, 0, 1, 1))
    assert error.value.code == "clip_page_side"
    assert str(PAGE_BOUND + 1) in str(error.value)


@pytest.mark.parametrize(
    ("size", "clip", "message"),
    [
        ((70000, 2), (0, 0, 65409, 1), "render size 65409x1 exceeds the limit of 65408 pixels wide and 65532 pixels"),
        ((2, 70000), (0, 0, 1, 65533), "render size 1x65533 exceeds the limit of 65408 pixels wide and 65532 pixels"),
        ((20000, 20000), (0, 0, 8001, 8000), r"render size 8001x8000 \(64008000 pixels\) exceeds the 64000000-pixel"),
    ],
)
def test_a_clip_keeps_the_limits_of_its_own_raster(
    size: tuple[float, float], clip: tuple[float, float, float, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _strip(*size).pixmap(dpi=72, clip=clip)


def test_a_clip_renders_the_widest_raster_of_a_wider_page() -> None:
    pixmap = _strip(70000, 1).pixmap(dpi=72, clip=(70000 - 65408, 0, 70000, 1))

    assert (pixmap.width, pixmap.height) == (65408, 1)
    assert bytes(memoryview(pixmap).cast("B")[:3]) == b"\xff\x00\x00"


def _strip(width: float, height: float) -> pyropdf.Page:
    doc = pyropdf.Document()
    page = doc.pages.new((10, 10))
    page.obj["MediaBox"] = [0, 0, width, height]
    page.obj["Contents"] = doc.objects[doc.objects.add_stream(f"1 0 0 rg 0 0 {width} {height} re f".encode())]
    return page


@pytest.mark.parametrize(("width", "height"), [(65409, 1), (65533, 1), (1, 65533)])
def test_rasters_that_vello_cannot_render_are_refused(width: int, height: int) -> None:
    # vello_cpu 0.3 panics on these sizes (issue 392).
    with pytest.raises(ValueError, match="limit of 65408 pixels wide and 65532 pixels high"):
        _strip(width, height).pixmap(dpi=72)


@pytest.mark.parametrize(("width", "height"), [(65408, 1), (1, 65532)])
def test_the_largest_rasters_render(width: int, height: int) -> None:
    pixmap = _strip(width, height).pixmap(dpi=72)
    assert (pixmap.width, pixmap.height) == (width, height)
    assert bytes(memoryview(pixmap).cast("B")[:3]) == b"\xff\x00\x00"


@pytest.mark.parametrize("size", [(1, 1), (3, 5), (65408, 1), (1, 65532)])
@pytest.mark.parametrize(
    ("colorspace", "alpha", "background", "pixel"),
    [
        ("rgb", False, None, b"\xff\xff\xff"),
        ("gray", False, None, b"\xff"),
        ("rgb", True, None, b"\x00\x00\x00\x00"),
        ("gray", True, None, b"\x00\x00"),
        ("rgb", False, (255, 0, 0), b"\xff\x00\x00"),
        ("gray", False, (255, 0, 0), b"\x4d"),
        ("rgb", True, (0, 0, 255, 128), b"\x00\x00\xff\x80"),
        ("gray", True, (0, 0, 255, 128), b"\x1d\x80"),
    ],
)
def test_blank_raster_initializes_every_pixel(
    size: tuple[int, int],
    colorspace: str,
    background: tuple[int, int, int] | tuple[int, int, int, int] | None,
    pixel: bytes,
    *,
    alpha: bool,
) -> None:
    width, height = size
    with pyropdf.Document() as doc:
        page = doc.pages.new((10, 10))
        page.obj["MediaBox"] = [0, 0, width, height]
        pixmap = page.pixmap(dpi=72, colorspace=Colorspace(colorspace), alpha=alpha, background=background)
        assert (pixmap.width, pixmap.height, pixmap.channels) == (width, height, len(pixel))
        assert bytes(pixmap) == pixel * (width * height)


def test_old_names_are_removed() -> None:
    for name in ("render_page", "render_page_svg", "render_pages", "get_page_pixmap"):
        assert not hasattr(pyropdf.Document, name)
    for name in ("render", "render_svg", "get_pixmap", "get_svg_image"):
        assert not hasattr(pyropdf.Page, name)
    with pytest.raises(TypeError):
        _page().pixmap(2)  # ty: ignore[too-many-positional-arguments]


def test_colorspace_is_a_string_enum() -> None:
    assert list(Colorspace) == [Colorspace.GRAY, Colorspace.RGB]
    assert Colorspace("gray") is Colorspace.GRAY
    assert Colorspace.RGB == "rgb"
    assert Colorspace.__module__ == "pyropdf"
    for name in ("csRGB", "csGRAY", "csCMYK", "CS_RGB", "CS_GRAY", "CS_CMYK"):
        assert not hasattr(pyropdf, name)
