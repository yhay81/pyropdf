"""The options of page.pixmap: clip, background, annotations, and colorspace.

Ported from pylopdf's tests/test_rendering_api.py at 036be111. Intended
differences:

- ``doc.render`` assertions move to #119 and ``page.svg`` ones to #120.
- pyropdf has no ``page.annotations.add`` yet, so the annotated page is
  built with a Highlight whose appearance paints its rectangle red.
- ``test_rgb_pixmaps_insert_without_a_soft_mask`` waits on Phase 2's
  drawing API, which inserts a pixmap into a page.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from test_content_model import page_with, stream_object
from test_pixmap import png_header

import pyropdf
from pyropdf import Colorspace, Matrix, Pixmap

REAL_WORLD = Path(__file__).parent / "assets" / "real_world"

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


def test_a_clip_keeps_the_limits_of_the_whole_page() -> None:
    with pytest.raises(ValueError, match="65535-pixel limit per side"):
        _page().pixmap(dpi=72 * 400, clip=(0, 0, 1, 1))


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
