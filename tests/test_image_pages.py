"""Pages made from images with doc.pages.new_from_image (issue 361)."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from PIL import Image

import pyropdf

if TYPE_CHECKING:
    from pathlib import Path

    from pyropdf import Pixmap

#: The render tolerance of one 8-bit sample, wider for JPEG's loss.
TOLERANCE = 2
JPEG_TOLERANCE = 12


def _image(mode: str = "RGB", size: tuple[int, int] = (40, 20)) -> Image.Image:
    """Return an image whose left half is red and right half blue, translucent on the right with alpha."""
    width, height = size
    image = Image.new(mode, size)
    left = {"RGB": (255, 0, 0), "RGBA": (255, 0, 0, 255), "L": 60}[mode]
    right = {"RGB": (0, 0, 255), "RGBA": (0, 0, 255, 128), "L": 200}[mode]
    image.paste(left, (0, 0, width // 2, height))
    image.paste(right, (width // 2, 0, width, height))
    return image


def _encoded(image: Image.Image, kind: str, **options: object) -> bytes:
    out = io.BytesIO()
    image.save(out, kind, **options)
    return out.getvalue()


def _close(pixmap: Pixmap, image: Image.Image, tolerance: int) -> bool:
    mode = {(1, False): "L", (1, True): "LA", (3, False): "RGB", (3, True): "RGBA"}[
        (pixmap.channels - pixmap.alpha, pixmap.alpha)
    ]
    rendered = Image.frombytes(mode, (pixmap.width, pixmap.height), bytes(pixmap)).convert(image.mode).tobytes()
    return all(abs(a - b) <= tolerance for a, b in zip(rendered, image.tobytes(), strict=True))


def test_a_jpeg_is_embedded_as_it_is() -> None:
    jpeg = _encoded(_image(), "JPEG", quality=95, dpi=(144, 144))
    doc = pyropdf.Document()
    (page,) = doc.pages.new_from_image(jpeg)
    (image,) = page.images()
    stored = image.stored()
    assert stored is not None
    assert stored.filter == "DCTDecode"
    assert stored.data == jpeg
    # 40 by 20 pixels at 144 dpi are 20 by 10 points.
    assert tuple(page.rect) == pytest.approx((0, 0, 20, 10))
    assert _close(page.pixmap(dpi=144), Image.open(io.BytesIO(jpeg)).convert("RGB"), JPEG_TOLERANCE)


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L"])
def test_a_png_renders_like_the_image(mode: str) -> None:
    image = _image(mode)
    doc = pyropdf.Document()
    (page,) = doc.pages.new_from_image(_encoded(image, "PNG", dpi=(72, 72)))
    assert tuple(page.rect) == pytest.approx((0, 0, 40, 20))
    pixmap = page.pixmap(dpi=72, alpha=mode == "RGBA")
    assert _close(pixmap, image, TOLERANCE)


def test_the_page_size_follows_the_resolution() -> None:
    image = _image()
    doc = pyropdf.Document()
    # PNG records pixels per metre; 96 dpi rounds to 3780.
    (png,) = doc.pages.new_from_image(_encoded(image, "PNG", dpi=(96, 96)))
    assert png.rect.width == pytest.approx(40 * 72 / 96)
    (centimetres,) = doc.pages.new_from_image(_encoded(image, "JPEG"))
    assert centimetres.rect.width == pytest.approx(40 * 72 / 96)
    (stretched,) = doc.pages.new_from_image(_encoded(image, "PNG"), dpi=(72, 36))
    assert tuple(stretched.rect) == pytest.approx((0, 0, 40, 40))
    (overridden,) = doc.pages.new_from_image(_encoded(image, "JPEG", dpi=(300, 300)), dpi=144)
    assert tuple(overridden.rect) == pytest.approx((0, 0, 20, 10))
    with pytest.raises(ValueError, match="dpi"):
        doc.pages.new_from_image(_encoded(image, "PNG"), dpi=0)


def test_a_jfif_density_in_centimetres() -> None:
    jpeg = bytearray(_encoded(_image(), "JPEG", dpi=(72, 72)))
    assert jpeg[6:11] == b"JFIF\x00"
    jpeg[13] = 2
    jpeg[14:18] = (100).to_bytes(2) + (100).to_bytes(2)
    (page,) = pyropdf.Document().pages.new_from_image(bytes(jpeg))
    assert page.rect.width == pytest.approx(40 * 72 / 254)


def test_a_jpeg_2000_is_embedded_as_it_is() -> None:
    pytest.importorskip("PIL.Jpeg2KImagePlugin")
    try:
        jpx = _encoded(_image(), "JPEG2000")
    except OSError:
        pytest.skip("Pillow has no OpenJPEG")
    doc = pyropdf.Document()
    (page,) = doc.pages.new_from_image(jpx)
    stored = page.images()[0].stored()
    assert stored is not None
    assert stored.data == jpx
    assert tuple(page.rect) == pytest.approx((0, 0, 30, 15))
    assert _close(page.pixmap(dpi=96), _image(), JPEG_TOLERANCE)


def test_a_multi_page_tiff_adds_one_page_per_frame(tmp_path: Path) -> None:
    first, second = _image(), _image("L", (10, 30))
    path = tmp_path / "pages.tiff"
    first.save(path, save_all=True, append_images=[second], dpi=(72, 72))
    doc = pyropdf.Document()
    doc.pages.new()
    steps = len(doc.history.steps)
    pages = doc.pages.new_from_image(path, index=0)
    assert [tuple(page.rect) for page in pages] == [(0, 0, 40, 20), (0, 0, 10, 30)]
    assert [doc.pages.index(page) for page in pages] == [0, 1]
    assert len(doc.pages) == 3
    assert len(doc.history.steps) == steps + 1
    assert _close(pages[1].pixmap(dpi=72), second, TOLERANCE)


def test_frames_at_a_negative_index_keep_their_order(tmp_path: Path) -> None:
    path = tmp_path / "pages.tiff"
    _image().save(path, save_all=True, append_images=[_image("L", (10, 30))], dpi=(72, 72))
    for index, expected in ((-100, [0, 1]), (-1, [0, 1]), (100, [1, 2])):
        doc = pyropdf.Document()
        doc.pages.new()
        pages = doc.pages.new_from_image(path, index=index)
        assert [tuple(page.rect) for page in pages] == [(0, 0, 40, 20), (0, 0, 10, 30)]
        assert [doc.pages.index(page) for page in pages] == expected


def test_a_jpeg_exif_resolution_is_read() -> None:
    exif = Image.Exif()
    exif[0x011A], exif[0x011B], exif[0x0128] = 300.0, 150.0, 2
    doc = pyropdf.Document()
    (page,) = doc.pages.new_from_image(_encoded(_image(), "JPEG", exif=exif.tobytes()))
    assert tuple(page.rect) == pytest.approx((0, 0, 40 * 72 / 300, 20 * 72 / 150))
    # A JFIF density in inches comes first.
    (page,) = doc.pages.new_from_image(_encoded(_image(), "JPEG", exif=exif.tobytes(), dpi=(72, 72)))
    assert tuple(page.rect) == pytest.approx((0, 0, 40, 20))


@pytest.mark.parametrize("dpi", [0.0254, 0.0508, 0.0762])
def test_a_png_below_a_tenth_of_a_dpi(dpi: float) -> None:
    # One, two, and three pixels per metre.
    (page,) = pyropdf.Document().pages.new_from_image(_encoded(_image(), "PNG", dpi=(dpi, dpi)))
    assert tuple(page.rect) == pytest.approx((0, 0, 40 * 72 / dpi, 20 * 72 / dpi))


def test_a_jpeg_exif_resolution_after_fill_bytes() -> None:
    exif = Image.Exif()
    exif[0x011A], exif[0x011B], exif[0x0128] = 300.0, 150.0, 2
    data = _encoded(_image(), "JPEG", exif=exif.tobytes())
    # Fill bytes may come before any marker; the JFIF segment ends at 20.
    assert data[20:22] == b"\xff\xe1"
    (page,) = pyropdf.Document().pages.new_from_image(data[:20] + b"\xff\xff" + data[20:])
    assert tuple(page.rect) == pytest.approx((0, 0, 40 * 72 / 300, 20 * 72 / 150))


def test_image_data_is_limited_in_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _encoded(_image(), "PNG")
    monkeypatch.setattr("pyropdf._pages._MAX_IMAGE_INPUT", len(data) - 1)
    path = tmp_path / "image.png"
    path.write_bytes(data)
    pages = pyropdf.Document().pages
    for source in (data, path):
        with pytest.raises(pyropdf.LimitError) as raised:
            pages.new_from_image(source)
        assert raised.value.code == "image_input_size"
    assert len(pages) == 0


def test_the_frames_of_one_call_are_limited_in_pixels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyropdf._pages._MAX_FRAME_PIXELS", 1000)
    path = tmp_path / "pages.tiff"
    _image().save(path, save_all=True, append_images=[_image()])
    doc = pyropdf.Document()
    with pytest.raises(pyropdf.LimitError) as raised:
        doc.pages.new_from_image(path)
    assert raised.value.code == "content_element_count"
    assert len(doc.pages) == 0


def test_pixmaps_and_files(tmp_path: Path) -> None:
    doc = pyropdf.Document()
    (source,) = doc.pages.new_from_image(_encoded(_image(), "PNG", dpi=(72, 72)))
    pixmap = source.pixmap(dpi=144)
    (from_pixmap,) = doc.pages.new_from_image(pixmap)
    # The pixmap's 80 by 40 pixels at its 144 dpi.
    assert tuple(from_pixmap.rect) == pytest.approx((0, 0, 40, 20))
    path = tmp_path / "image.jpg"
    path.write_bytes(_encoded(_image(), "JPEG"))
    (from_path,) = doc.pages.new_from_image(path)
    (from_name,) = doc.pages.new_from_image(str(path))
    stored = [page.images()[0].stored() for page in (from_path, from_name)]
    assert stored[0] is not None
    assert stored[1] is not None
    assert stored[0].data == stored[1].data == path.read_bytes()
    reopened = pyropdf.open(doc.to_bytes())
    assert len(reopened.pages) == 4
    assert _close(reopened.pages[0].pixmap(dpi=72), _image(), TOLERANCE)


def test_unreadable_sources_are_refused() -> None:
    pages = pyropdf.Document().pages
    with pytest.raises(ValueError, match="Pillow reads"):
        pages.new_from_image(b"not an image")
    with pytest.raises(TypeError, match="source"):
        pages.new_from_image(42)  # ty: ignore[invalid-argument-type]
    assert len(pages) == 0
