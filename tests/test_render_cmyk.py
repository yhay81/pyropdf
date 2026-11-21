"""Native CMYK acceptance for the upstream viewport proposal and its release adoption."""

from __future__ import annotations

import pytest
from test_content import build_raw_pdf

import pyropdf

_WIDTH, _HEIGHT = 43, 27
_PATCHES = [
    ("1 0 0 0 k", (255, 0, 0, 0)),
    ("0 1 0 0 k", (0, 255, 0, 0)),
    ("0 0 1 0 k", (0, 0, 255, 0)),
    ("0 0 0 1 k", (0, 0, 0, 255)),
    ("0.2 0.4 0.6 0.8 k", (51, 102, 153, 204)),
    ("0 0 0 0 k", (0, 0, 0, 0)),
    ("/Half gs 0 0 0 1 k", (0, 0, 0, 128)),
    ("1 0 0 rg", (0, 255, 255, 0)),
]


def _patch_page(width: int, height: int, left: int, top: int) -> bytes:
    operations = []
    for i, (paint, _) in enumerate(_PATCHES):
        x, y = left + 2 + 8 * (i % 5), top + 3 + 12 * (i // 5)
        operations.append(f"q {paint} {x} {height - y - 7} 6 7 re f Q")
    content = "\n".join(operations).encode("ascii")
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            "/Resources << /ExtGState << /Half << /ca 0.5 >> >> >> /Contents 4 0 R >>",
            4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        }
    )


@pytest.mark.parametrize(("width", "height", "left", "top"), [(96, 72, 23, 18), (10_000, 9_000, 4_700, 5_900)])
def test_cmyk_viewport_retains_ink_channels_and_pixel_coordinates(width: int, height: int, left: int, top: int) -> None:
    page = pyropdf.open(_patch_page(width, height, left, top)).pages[0]
    clip = (left, top, left + _WIDTH, top + _HEIGHT)
    colorspace = pyropdf.Colorspace("cmyk")
    direct = page.pixmap(colorspace=colorspace, clip=clip)
    rotated = page.pixmap(colorspace=colorspace, clip=clip, matrix=(0, -1, 1, 0))
    assert (direct.width, direct.height) == (_WIDTH, _HEIGHT)
    assert (rotated.width, rotated.height) == (_HEIGHT, _WIDTH)
    for pixmap in (direct, rotated):
        assert pixmap.colorspace == colorspace
        assert pixmap.channels == 4
        assert not pixmap.alpha
        assert pixmap.stride == 4 * pixmap.width
        assert len(bytes(pixmap)) == 4 * pixmap.width * pixmap.height
    _check_samples(bytes(direct), bytes(rotated))


def _check_samples(raw: bytes, turned: bytes) -> None:
    assert raw[:4] == bytes(4)
    for i, (_, inks) in enumerate(_PATCHES):
        x, y = 4 + 8 * (i % 5), 5 + 12 * (i // 5)
        offset = (y * _WIDTH + x) * 4
        assert tuple(raw[offset : offset + 4]) == inks
    for y in range(_WIDTH):
        for x in range(_HEIGHT):
            source = (x * _WIDTH + _WIDTH - 1 - y) * 4
            target = (y * _HEIGHT + x) * 4
            assert turned[target : target + 4] == raw[source : source + 4]


def test_whole_page_cmyk_uses_the_existing_raster_pixel_limit() -> None:
    page = pyropdf.open(_patch_page(10_000, 9_000, 4_700, 5_900)).pages[0]
    with pytest.raises(ValueError, match="64000000-pixel limit"):
        page.pixmap(colorspace=pyropdf.Colorspace("cmyk"))


def test_opaque_cmyk_rejects_a_transparent_background() -> None:
    page = pyropdf.open(_patch_page(96, 72, 23, 18)).pages[0]
    with pytest.raises(ValueError, match="opaque background"):
        page.pixmap(colorspace=pyropdf.Colorspace("cmyk"), alpha=False, background=(255, 255, 255, 128))
