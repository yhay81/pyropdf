"""Native CMYK rasterization tests (issues #425 and #1274).

Includes the downstream public acceptance contract from commit 11e65eb6
plus fidelity, alpha, image, text, shading, encoding, and budget tests.
"""

from __future__ import annotations

import base64
import io
import zlib
from typing import TYPE_CHECKING

import pytest
from PIL import Image, ImageCms
from test_content import build_raw_pdf

import pyropdf
from pyropdf import Colorspace

if TYPE_CHECKING:
    from pathlib import Path

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


def _page_pdf(
    width: int,
    height: int,
    content: str,
    *,
    resources: str = "",
    extra: dict[int, str | bytes] | None = None,
) -> bytes:
    raw = content.encode("ascii")
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
        f"/Resources << {resources} >> /Contents 4 0 R >>",
        4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(raw), raw),
    }
    if extra:
        objects.update(extra)
    return build_raw_pdf(objects)


def test_cmyk_preserves_device_gray_strokes_text_and_blend_modes() -> None:
    content = (
        "0 g 0 60 12 12 re f "
        "0.5 g 12 60 12 12 re f "
        "1 g 24 60 12 12 re f "
        "0.5 0.5 0.5 rg 36 60 12 12 re f "
        "0 0 1 rg 48 60 12 12 re f "
        # DeviceCMYK stroke (pure magenta, 6 pt wide)
        "0 1 0 0 K 6 w 2 45 m 58 45 l S "
        # DeviceCMYK pure-K text
        "0 0 0 1 k BT /F1 28 Tf 4 10 Td (MMMM) Tj ET "
        # Multiply blend mode: K over Y -> (0, 0, 255, 255)
        "q 0 0 1 0 k 60 60 12 12 re f /Mul gs 0 0 0 1 k 60 60 12 12 re f Q"
    )
    pdf = _page_pdf(
        80,
        80,
        content,
        resources="/Font << /F1 5 0 R >> /ExtGState << /Mul << /BM /Multiply >> >>",
        extra={5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"},
    )
    pixmap = pyropdf.open(pdf).pages[0].pixmap(colorspace="cmyk")

    assert repr(pixmap) == "<Pixmap 80x80 cmyk>"
    assert pixmap.colorspace is Colorspace.CMYK
    # DeviceGray maps to pure K (C = M = Y = 0).
    assert pixmap.pixel(6, 14) == (0, 0, 0, 255)
    assert pixmap.pixel(18, 14) == (0, 0, 0, 128)
    assert pixmap.pixel(30, 14) == (0, 0, 0, 0)
    # Neutral DeviceRGB 0.5 gray -> pure K (0, 0, 0, 128); pure blue -> (255, 255, 0, 0).
    assert pixmap.pixel(42, 14) == (0, 0, 0, 128)
    assert pixmap.pixel(54, 14) == (255, 255, 0, 0)
    # DeviceCMYK stroke keeps pure magenta.
    assert pixmap.pixel(30, 35) == (0, 255, 0, 0)
    # Multiply K over Y keeps both Y and K at 255.
    assert pixmap.pixel(66, 14) == (0, 0, 255, 255)
    # Pure-K text region (raster y=48..72) has dark K pixels and zero C/M/Y ink.
    text_k_pixels = [pixmap.pixel(x, y) for y in range(48, 72) for x in range(4, 72) if pixmap.pixel(x, y)[3] > 200]
    assert text_k_pixels
    assert all(px[:3] == (0, 0, 0) for px in text_k_pixels)


def test_cmyk_images_preserve_exact_device_cmyk_and_gray_and_rgb() -> None:
    cmyk_k = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceCMYK /BitsPerComponent 8 /Length 4 >>\nstream\n\x00\x00\x00\xff\nendstream"
    )
    cmyk_tint = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceCMYK /BitsPerComponent 8 /Length 4 >>\nstream\n\x33\x66\x99\x1a\nendstream"
    )
    gray_img = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
    )
    rgb_img = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length 3 >>\nstream\n\xff\x00\x00\nendstream"
    )
    pdf = _page_pdf(
        40,
        10,
        "q 10 0 0 10 0 0 cm /Im0 Do Q "
        "q 10 0 0 10 10 0 cm /Im1 Do Q "
        "q 10 0 0 10 20 0 cm /Im2 Do Q "
        "q 10 0 0 10 30 0 cm /Im3 Do Q",
        resources="/XObject << /Im0 5 0 R /Im1 6 0 R /Im2 7 0 R /Im3 8 0 R >>",
        extra={5: cmyk_k, 6: cmyk_tint, 7: gray_img, 8: rgb_img},
    )
    pixmap = pyropdf.open(pdf).pages[0].pixmap(colorspace=Colorspace.CMYK)

    assert pixmap.pixel(5, 5) == (0, 0, 0, 255)
    assert pixmap.pixel(15, 5) == (51, 102, 153, 26)
    # DeviceGray 0x80 (128) -> K = 255 - 128 = 127, zero C/M/Y.
    assert pixmap.pixel(25, 5) == (0, 0, 0, 127)
    # DeviceRGB red -> (0, 255, 255, 0).
    assert pixmap.pixel(35, 5) == (0, 255, 255, 0)


def test_cmyk_converts_icc_based_and_calrgb_inputs() -> None:
    srgb_icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    compressed_icc = zlib.compress(srgb_icc)
    icc_stream = b"<< /N 3 /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream" % (
        len(compressed_icc),
        compressed_icc,
    )
    pdf = _page_pdf(
        60,
        10,
        "/CS1 cs 1 0 0 sc 0 0 20 10 re f /CS2 cs 0.5 0.5 0.5 sc 20 0 20 10 re f /CS3 cs 0.5 sc 40 0 20 10 re f",
        resources=(
            "/ColorSpace << "
            "/CS1 [/ICCBased 5 0 R] "
            "/CS2 [/CalRGB << /WhitePoint [0.9505 1 1.089] /Gamma [2.2 2.2 2.2] "
            "/Matrix [0.4124 0.2126 0.0193 0.3576 0.7152 0.1192 0.1805 0.0722 0.9505] >>] "
            "/CS3 [/CalGray << /WhitePoint [0.9505 1 1.089] /Gamma 1.0 >>] "
            ">>"
        ),
        extra={5: icc_stream},
    )
    pixmap = pyropdf.open(pdf).pages[0].pixmap(colorspace="cmyk")

    # sRGB ICC red converts cleanly to high M/Y with zero C and K.
    red = pixmap.pixel(10, 5)
    assert red[0] == 0
    assert red[1] >= 250
    assert red[2] >= 250
    assert red[3] == 0
    # Neutral D65 CalRGB gray stays near-neutral (K-dominated, minimal chromatic ink).
    gray = pixmap.pixel(30, 5)
    assert max(gray[:3]) <= 4
    assert gray[3] > 0
    # CalGray maps purely to black ink with zero chromatic channels.
    cal_gray = pixmap.pixel(50, 5)
    assert cal_gray[:3] == (0, 0, 0)
    assert cal_gray[3] > 0


def test_cmyk_alpha_produces_five_channel_cmyka_and_supports_pixel_ops() -> None:
    pdf = _page_pdf(
        30,
        10,
        "0 0 0 1 k 0 0 10 10 re f /Half gs 1 0 0 0 k 10 0 10 10 re f",
        resources="/ExtGState << /Half << /ca 0.5 >> >>",
    )
    pixmap = pyropdf.open(pdf).pages[0].pixmap(colorspace="cmyk", alpha=True)

    assert repr(pixmap) == "<Pixmap 30x10 cmyk alpha>"
    assert (pixmap.width, pixmap.height, pixmap.channels, pixmap.alpha, pixmap.stride) == (30, 10, 5, True, 150)
    assert memoryview(pixmap).shape == (10, 30, 5)
    assert pixmap.pixel(5, 5) == (0, 0, 0, 255, 255)
    assert pixmap.pixel(15, 5) == (255, 0, 0, 0, 128)
    assert pixmap.pixel(25, 5) == (0, 0, 0, 0, 0)

    # Pixel operations work across 4-channel CMYK and 5-channel CMYKA.
    flattened = pixmap.without_alpha()
    assert (flattened.colorspace, flattened.channels, flattened.alpha) == (Colorspace.CMYK, 4, False)
    assert flattened.pixel(5, 5) == (0, 0, 0, 255)
    assert flattened.pixel(15, 5) == (128, 0, 0, 0)
    assert flattened.pixel(25, 5) == (0, 0, 0, 0)

    re_alpha = flattened.with_alpha()
    assert (re_alpha.colorspace, re_alpha.channels, re_alpha.alpha) == (Colorspace.CMYK, 5, True)
    assert re_alpha.pixel(5, 5) == (0, 0, 0, 255, 255)

    assert pixmap.color_count() == 3
    assert pixmap.colors()[(0, 0, 0, 0, 0)] == 100
    assert pixmap.crop((0, 0, 10, 10)).pixel(0, 0) == (0, 0, 0, 255, 255)
    assert pixmap.scale(15, 5).channels == 5
    assert pixmap.rotate(90).width == 10
    assert pixmap.flip("horizontal").pixel(25, 5) == (0, 0, 0, 255, 255)
    assert flattened.invert().pixel(5, 5) == (255, 255, 255, 0)

    rgb = flattened.to_rgb()
    assert (rgb.colorspace, rgb.channels) == (Colorspace.RGB, 3)
    assert rgb.pixel(5, 5) == (0, 0, 0)
    assert rgb.pixel(25, 5) == (255, 255, 255)

    gray = flattened.to_gray()
    assert (gray.colorspace, gray.channels) == (Colorspace.GRAY, 1)
    assert gray.pixel(5, 5) == (0,)
    assert gray.pixel(25, 5) == (255,)


def test_cmyk_exports_to_jpeg_pam_psd_ps_pil_and_refuses_unsupported_formats(tmp_path: Path) -> None:
    pdf = _page_pdf(
        32,
        16,
        "1 0 0 0 k 0 0 16 16 re f 0 0 0 1 k 16 0 16 16 re f",
    )
    doc = pyropdf.open(pdf)
    page = doc.pages[0]
    cmyk = page.pixmap(colorspace="cmyk")
    cmyka = page.pixmap(colorspace="cmyk", alpha=True)

    # JPEG CMYK round-trips through Pillow as CMYK.
    jpeg_bytes = cmyk.to_bytes("jpeg", quality=98)
    jpeg_img = Image.open(io.BytesIO(jpeg_bytes))
    assert jpeg_img.mode == "CMYK"
    assert jpeg_img.size == (32, 16)
    cyan_px = jpeg_img.getpixel((4, 8))
    black_px = jpeg_img.getpixel((24, 8))
    assert isinstance(cyan_px, tuple)
    assert isinstance(black_px, tuple)
    assert cyan_px[0] >= 250
    assert max(cyan_px[1:]) <= 5
    assert black_px[3] >= 250
    assert max(black_px[:3]) <= 5

    # Document.render with colorspace="cmyk" and format="jpeg" matches Page.pixmap.
    assert doc.render(colorspace="cmyk", format="jpeg", quality=98) == [jpeg_bytes]

    # Save JPEG to file atomically.
    jpeg_path = tmp_path / "cmyk.jpg"
    cmyk.save(jpeg_path, quality=98)
    assert jpeg_path.read_bytes() == jpeg_bytes

    # PAM preserves exact CMYK and CMYKA bytes.
    pam_header, pam_samples = cmyk.to_bytes("pam").split(b"ENDHDR\n", 1)
    assert b"DEPTH 4\n" in pam_header
    assert b"TUPLTYPE CMYK\n" in pam_header
    assert pam_samples == bytes(cmyk)
    pama_header, pama_samples = cmyka.to_bytes("pam").split(b"ENDHDR\n", 1)
    assert b"DEPTH 5\n" in pama_header
    assert b"TUPLTYPE CMYK_ALPHA\n" in pama_header
    assert pama_samples == bytes(cmyka)

    # PSD CMYK opens in Pillow with exact CMYK bytes.
    psd_img = Image.open(io.BytesIO(cmyk.to_bytes("psd")))
    assert psd_img.mode == "CMYK"
    assert psd_img.tobytes() == bytes(cmyk)

    # PostScript emits /DeviceCMYK with exact ASCII85 ink bytes.
    ps_bytes = cmyk.to_bytes("ps")
    ps_text = ps_bytes.decode("ascii")
    assert "/DeviceCMYK setcolorspace" in ps_text
    encoded = ps_text.split("image\n", 1)[1].split("~>", 1)[0]
    assert base64.a85decode("<~" + encoded + "~>", adobe=True) == bytes(cmyk)

    # Pillow to_pil returns CMYK for opaque and refuses CMYKA explicitly.
    pil_img = cmyk.to_pil()
    assert pil_img.mode == "CMYK"
    assert pil_img.tobytes() == bytes(cmyk)
    with pytest.raises(ValueError, match="CMYK\\+alpha"):
        cmyka.to_pil()

    # Unsupported format / alpha combinations fail explicitly.
    for fmt in ("png", "pbm", "pgm", "ppm", "pnm"):
        with pytest.raises(ValueError, match="CMYK"):
            cmyk.to_bytes(fmt)
    with pytest.raises(ValueError, match="alpha"):
        cmyka.to_bytes("jpeg")
    with pytest.raises(ValueError, match="alpha"):
        cmyka.to_bytes("ps")
    with pytest.raises(ValueError, match="PNG does not support CMYK"):
        doc.render(colorspace="cmyk", format="png")


def test_cmyk_enforces_dimension_and_memory_limits() -> None:
    pdf = _page_pdf(100, 100, "0 0 0 1 k 0 0 100 100 re f")
    # Exceeding raster width/height limit fails with ValueError.
    with pytest.raises(ValueError, match="limit of 65408 pixels wide and 65532 pixels high"):
        pyropdf.open(pdf).pages[0].pixmap(colorspace="cmyk", matrix=(655, 0, 0, 1))

    # Tight memory limit fails cleanly with LimitError(code="memory_size").
    tight = pyropdf.DocumentLimits(max_memory_size=64 * 1024)
    with pyropdf.open(pdf, limits=tight) as doc:
        with pytest.raises(pyropdf.LimitError) as exc_info:
            doc.pages[0].pixmap(colorspace="cmyk", dpi=72)
        assert exc_info.value.code == "memory_size"

    # Clipped viewport on a page beyond clip_page_side raises LimitError("clip_page_side").
    with pytest.raises(pyropdf.LimitError) as side_exc:
        pyropdf.open(pdf).pages[0].pixmap(colorspace="cmyk", matrix=(6000, 0, 0, 6000), clip=(0, 0, 1, 1))
    assert side_exc.value.code == "clip_page_side"


def test_cmyk_content_and_annotation_pixmap() -> None:
    pdf = _page_pdf(
        60,
        40,
        "1 0 0 0 k 0 0 20 20 re f",
        extra={
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 60 40] /Contents 4 0 R /Annots [5 0 R] >>",
            5: "<< /Type /Annot /Subtype /Stamp /Rect [20 10 50 30] /AP << /N 6 0 R >> >>",
            6: b"<< /Type /XObject /Subtype /Form /BBox [0 0 30 20] /Length 22 >>\n"
            b"stream\n0 1 0 0 k 0 0 30 20 re f\nendstream",
        },
    )
    page = pyropdf.open(pdf).pages[0]

    content_cmyk = page.content().pixmap(colorspace="cmyk")
    assert bytes(content_cmyk) == bytes(page.pixmap(colorspace="cmyk"))

    annot_cmyk = page.annotations[0].pixmap(colorspace="cmyk")
    assert (annot_cmyk.colorspace, annot_cmyk.channels) == (Colorspace.CMYK, 4)
    assert annot_cmyk.pixel(15, 10) == (0, 255, 0, 0)
