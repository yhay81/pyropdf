"""Pixel operations of Pixmap (issue 398), against references in plain Python and Pillow."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from PIL import Image

import pyropdf
from pyropdf import Colorspace, Pixmap

if TYPE_CHECKING:
    from pathlib import Path

WIDTH, HEIGHT = 7, 5


def _pixmap(mode: str) -> Pixmap:
    """Return a WIDTH by HEIGHT pixmap of ``mode`` whose samples vary by position and channel."""
    image = Image.new(mode, (WIDTH, HEIGHT))
    channels = len(mode)
    image.putdata(
        [
            tuple((x * 37 + y * 53 + channel * 71) % 256 for channel in range(channels))
            if channels > 1
            else (x * 37 + y * 53) % 256
            for y in range(HEIGHT)
            for x in range(WIDTH)
        ]
    )
    return Pixmap.from_bytes(_png(image))


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _pixels(pixmap: Pixmap) -> list[tuple[int, ...]]:
    samples = bytes(memoryview(pixmap).cast("B"))
    channels = pixmap.channels
    return [tuple(samples[at : at + channels]) for at in range(0, len(samples), channels)]


def _colors(pixel: tuple[int, ...], *, alpha: bool) -> tuple[int, ...]:
    return pixel[:-1] if alpha else pixel


MODES = ["L", "LA", "RGB", "RGBA"]


@pytest.mark.parametrize("mode", MODES)
def test_from_bytes_and_to_pil_round_trip(mode: str) -> None:
    pixmap = _pixmap(mode)
    assert (pixmap.width, pixmap.height, pixmap.alpha) == (WIDTH, HEIGHT, mode.endswith("A"))
    assert pixmap.colorspace is (Colorspace.GRAY if mode.startswith("L") else Colorspace.RGB)
    image = pixmap.to_pil()
    assert image.mode == mode
    again = Pixmap.from_bytes(_png(image))
    assert bytes(again) == bytes(pixmap)
    assert Pixmap.from_bytes(memoryview(pixmap.to_bytes("png"))).to_pil().tobytes() == image.tobytes()


@pytest.mark.parametrize("mode", MODES)
def test_invert(mode: str) -> None:
    pixmap = _pixmap(mode)
    alpha = pixmap.alpha
    for before, after in zip(_pixels(pixmap), _pixels(pixmap.invert()), strict=True):
        assert _colors(after, alpha=alpha) == tuple(255 - value for value in _colors(before, alpha=alpha))
        if alpha:
            assert after[-1] == before[-1]


def test_invert_with_a_rect_changes_only_that_rect() -> None:
    pixmap = _pixmap("RGB")
    inverted = pixmap.invert((2, 1, 5, 3))
    for index, (before, after) in enumerate(zip(_pixels(pixmap), _pixels(inverted), strict=True)):
        x, y = index % WIDTH, index // WIDTH
        inside = 2 <= x < 5 and 1 <= y < 3
        assert after == (tuple(255 - value for value in before) if inside else before)
    # A rect is cut to the pixmap; one outside it changes nothing.
    assert bytes(pixmap.invert((-10, -10, 100, 100))) == bytes(pixmap.invert())
    assert bytes(pixmap.invert((50, 50, 60, 60))) == bytes(pixmap)
    with pytest.raises(ValueError, match="x0 < x1"):
        pixmap.invert((5, 0, 2, 3))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("value", [0.5, 1.0, 2.2])
def test_gamma(mode: str, value: float) -> None:
    pixmap = _pixmap(mode)
    alpha = pixmap.alpha
    for before, after in zip(_pixels(pixmap), _pixels(pixmap.gamma(value)), strict=True):
        expected = tuple(round(255 * (sample / 255) ** value) for sample in _colors(before, alpha=alpha))
        assert _colors(after, alpha=alpha) == expected
    with pytest.raises(ValueError, match="positive"):
        pixmap.gamma(0)


@pytest.mark.parametrize("mode", MODES)
def test_tint(mode: str) -> None:
    pixmap = _pixmap(mode)
    alpha = pixmap.alpha
    black, white = (40, 0, 120), (255, 200, 80)
    if pixmap.colorspace is Colorspace.GRAY:
        black_ends = (round(0.299 * 40 + 0.114 * 120),)
        white_ends = (round(0.299 * 255 + 0.587 * 200 + 0.114 * 80),)
    else:
        black_ends, white_ends = black, white
    for before, after in zip(_pixels(pixmap), _pixels(pixmap.tint(black, white)), strict=True):
        expected = tuple(
            round(low + (high - low) * sample / 255)
            for sample, low, high in zip(_colors(before, alpha=alpha), black_ends, white_ends, strict=True)
        )
        assert _colors(after, alpha=alpha) == expected
    assert bytes(pixmap.tint()) == bytes(pixmap)


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_to_gray_uses_bt601_luma(mode: str) -> None:
    pixmap = _pixmap(mode)
    gray = pixmap.to_gray()
    assert gray.colorspace is Colorspace.GRAY
    assert gray.alpha == pixmap.alpha
    for before, after in zip(_pixels(pixmap), _pixels(gray), strict=True):
        red, green, blue = before[:3]
        assert after[0] == round(0.299 * red + 0.587 * green + 0.114 * blue)
        if pixmap.alpha:
            assert after[1] == before[3]


@pytest.mark.parametrize("mode", ["L", "LA"])
def test_to_rgb_repeats_gray(mode: str) -> None:
    pixmap = _pixmap(mode)
    rgb = pixmap.to_rgb()
    assert rgb.colorspace is Colorspace.RGB
    assert rgb.to_pil().tobytes() == pixmap.to_pil().convert("RGBA" if pixmap.alpha else "RGB").tobytes()
    assert bytes(rgb.to_gray()) == bytes(pixmap)
    assert bytes(pixmap.to_gray()) == bytes(pixmap)


@pytest.mark.parametrize("mode", ["L", "RGB"])
def test_alpha_is_added_and_composited_away(mode: str) -> None:
    pixmap = _pixmap(mode)
    opaque = pixmap.with_alpha()
    assert opaque.alpha
    assert opaque.to_pil().tobytes() == pixmap.to_pil().convert(mode + "A").tobytes()
    assert bytes(opaque.without_alpha()) == bytes(pixmap)


def test_without_alpha_composites_over_the_background() -> None:
    image = Image.new("RGBA", (2, 1))
    image.putdata([(200, 100, 0, 0), (200, 100, 0, 128)])
    pixmap = Pixmap.from_bytes(_png(image))
    over_white = _pixels(pixmap.without_alpha())
    assert over_white[0] == (255, 255, 255)
    assert over_white[1] == tuple(round(value * 128 / 255 + 255 * (1 - 128 / 255)) for value in (200, 100, 0))
    over_blue = _pixels(pixmap.without_alpha((0, 0, 255)))
    assert over_blue[0] == (0, 0, 255)
    gray = _pixels(pixmap.to_gray().without_alpha((0, 0, 255)))
    assert gray[0] == (round(0.114 * 255),)


@pytest.mark.parametrize("mode", MODES)
def test_crop(mode: str) -> None:
    pixmap = _pixmap(mode)
    cropped = pixmap.crop((1, 2, 4, 5))
    assert (cropped.width, cropped.height) == (3, 3)
    assert cropped.to_pil().tobytes() == pixmap.to_pil().crop((1, 2, 4, 5)).tobytes()
    assert (pixmap.crop((-5, -5, 3, 2)).width, pixmap.crop((-5, -5, 3, 2)).height) == (3, 2)
    with pytest.raises(ValueError, match="does not overlap"):
        pixmap.crop((10, 10, 20, 20))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("size", [(3, 2), (14, 10), (20, 3), (1, 1)])
def test_scale_matches_pillow_bilinear(mode: str, size: tuple[int, int]) -> None:
    pixmap = _pixmap(mode)
    scaled = pixmap.scale(*size)
    assert (scaled.width, scaled.height) == size
    reference = pixmap.to_pil().resize(size, Image.Resampling.BILINEAR).tobytes()
    # Pillow premultiplies in fixed point, a few levels apart at low alpha.
    tolerance = 4 if pixmap.alpha else 2
    assert all(abs(a - b) <= tolerance for a, b in zip(bytes(scaled), reference, strict=True))


def test_scale_follows_the_resolution_and_refuses_empty_sizes() -> None:
    pixmap = _pixmap("RGB")
    assert pixmap.scale(WIDTH * 2, HEIGHT).resolution == (pixmap.resolution[0] * 2, pixmap.resolution[1])
    with pytest.raises(ValueError, match="pixels"):
        pixmap.scale(0, 3)
    with pytest.raises(ValueError, match="pixels"):
        pixmap.scale(65535, 65535)


def test_rendered_pixmaps_take_the_operations() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((20, 10))
    page.obj["Contents"] = doc.objects[doc.objects.add_stream(b"1 0 0 rg 0 0 10 10 re f")]
    pixmap = page.pixmap(dpi=72)
    assert _pixels(pixmap.invert())[0] == (0, 255, 255)
    assert _pixels(pixmap.to_gray())[0] == (round(0.299 * 255),)
    assert pixmap.crop((0, 0, 10, 10)).to_pil().getcolors() == [(100, (255, 0, 0))]
    assert pixmap.resolution == pixmap.invert().resolution


def test_jpeg_and_files(tmp_path: Path) -> None:
    image = _pixmap("RGB").to_pil()
    jpeg = io.BytesIO()
    image.save(jpeg, "JPEG", quality=95)
    pixmap = Pixmap.from_bytes(jpeg.getvalue())
    assert (pixmap.width, pixmap.height, pixmap.colorspace, pixmap.alpha) == (WIDTH, HEIGHT, Colorspace.RGB, False)
    reference = Image.open(io.BytesIO(jpeg.getvalue())).convert("RGB").tobytes()
    assert all(abs(a - b) <= 8 for a, b in zip(bytes(pixmap), reference, strict=True))
    gray = io.BytesIO()
    image.convert("L").save(gray, "JPEG")
    assert Pixmap.from_bytes(gray.getvalue()).colorspace is Colorspace.GRAY
    path = tmp_path / "image.png"
    path.write_bytes(_png(image))
    assert bytes(Pixmap.open(path)) == bytes(Pixmap.open(str(path))) == bytes(_pixmap("RGB"))
    with pytest.raises(ValueError, match="not PNG, JPEG"):
        Pixmap.from_bytes(b"RIFF\0\0\0\0WEBP")
    with pytest.raises(ValueError, match="not a PNG"):
        Pixmap.from_bytes(b"\x89PNG\r\n\x1a\nbroken")
