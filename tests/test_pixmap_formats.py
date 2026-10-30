"""Pixmap input and output beyond PNG and JPEG (issue 431), against Pillow, Ghostscript, and hand-built files."""

from __future__ import annotations

import base64
import io
import re
import shutil
import struct
import subprocess
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from pyropdf import Colorspace, Pixmap

if TYPE_CHECKING:
    from pathlib import Path

WIDTH, HEIGHT = 7, 5
MODES = {"L": (True, False), "LA": (True, True), "RGB": (False, False), "RGBA": (False, True)}


def _image(mode: str) -> Image.Image:
    """Return a WIDTH by HEIGHT image of ``mode`` whose samples vary by position and channel."""
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
    return image


def _pixmap(mode: str) -> Pixmap:
    out = io.BytesIO()
    _image(mode).save(out, "PNG", dpi=(150, 150))
    return Pixmap.from_bytes(out.getvalue())


def _saved(image: Image.Image, format: str) -> bytes:  # noqa: A002 - Pillow's name.
    out = io.BytesIO()
    image.save(out, format)
    return out.getvalue()


def _shape(pixmap: Pixmap) -> tuple[int, int, bool, bool]:
    return pixmap.width, pixmap.height, pixmap.colorspace is Colorspace.GRAY, pixmap.alpha


# Input.


@pytest.mark.parametrize("mode", ["1", "L", "RGB"])
def test_raw_portable_anymaps_decode_as_pillow_does(mode: str) -> None:
    image = _image("L" if mode == "1" else mode)
    if mode == "1":
        image = image.point(lambda value: 255 if value >= 128 else 0).convert("1")
    data = _saved(image, "PPM")
    assert data[:2] == {"1": b"P4", "L": b"P5", "RGB": b"P6"}[mode]

    pixmap = Pixmap.from_bytes(data)

    assert _shape(pixmap) == (WIDTH, HEIGHT, mode != "RGB", False)
    assert bytes(pixmap) == image.convert("RGB" if mode == "RGB" else "L").tobytes()
    assert pixmap.resolution == (96, 96)


def test_plain_portable_anymaps_decode() -> None:
    bitmap = Pixmap.from_bytes(b"P1\n# a comment\n3 2\n010\n1 0 # and another\n1")
    assert _shape(bitmap) == (3, 2, True, False)
    assert bytes(bitmap) == bytes([255, 0, 255, 0, 255, 0])

    gray = Pixmap.from_bytes(b"P2 2 2 # comment\n 1000\n0 500\n1000 1\n")
    assert bytes(gray) == bytes([0, 128, 255, 0])

    color = Pixmap.from_bytes(b"P3\n1 2\n15\n15 0 7\n1 2 3\n")
    assert _shape(color) == (1, 2, False, False)
    assert bytes(color) == bytes([255, 0, 119, 17, 34, 51])


def test_sixteen_bit_samples_scale_to_eight_bits() -> None:
    pixmap = Pixmap.from_bytes(b"P5\n2 1\n65535\n" + struct.pack(">HH", 65535, 257))
    assert bytes(pixmap) == bytes([255, 1])
    pixmap = Pixmap.from_bytes(b"P6\n1 1\n1023\n" + struct.pack(">HHH", 1023, 0, 512))
    assert bytes(pixmap) == bytes([255, 0, 128])


@pytest.mark.parametrize(
    ("tuple_type", "depth", "shape"),
    [
        ("GRAYSCALE", 1, (True, False)),
        ("GRAYSCALE_ALPHA", 2, (True, True)),
        ("RGB", 3, (False, False)),
        ("RGB_ALPHA", 4, (False, True)),
        ("", 4, (False, True)),
    ],
)
def test_pam_decodes_each_tuple_type(tuple_type: str, depth: int, shape: tuple[bool, bool]) -> None:
    samples = bytes(range(10, 10 + 2 * depth))
    line = f"TUPLTYPE {tuple_type}\n" if tuple_type else ""
    data = f"P7\nWIDTH 2\n# comment\nHEIGHT 1\nDEPTH {depth}\nMAXVAL 255\n{line}ENDHDR\n".encode() + samples

    pixmap = Pixmap.from_bytes(data)

    assert _shape(pixmap) == (2, 1, *shape)
    assert bytes(pixmap) == samples


def test_pam_black_and_white_scales_its_maximum() -> None:
    pixmap = Pixmap.from_bytes(b"P7\nWIDTH 2\nHEIGHT 1\nDEPTH 1\nMAXVAL 1\nTUPLTYPE BLACKANDWHITE\nENDHDR\n\x00\x01")
    assert bytes(pixmap) == bytes([0, 255])


@pytest.mark.parametrize("mode", ["1", "L", "P", "RGB"])
def test_bmp_decodes_as_pillow_does(mode: str) -> None:
    image = _image("RGB").convert(mode) if mode != "L" else _image("L")
    data = _saved(image, "BMP")
    reference = Image.open(io.BytesIO(data))
    gray = mode in {"1", "L"}

    pixmap = Pixmap.from_bytes(data)

    assert _shape(pixmap) == (WIDTH, HEIGHT, gray, False)
    assert bytes(pixmap) == reference.convert("L" if gray else "RGB").tobytes()


def _bmp(  # noqa: PLR0913 - one argument per header field.
    width: int,
    height: int,
    bits: int,
    rows: list[bytes],
    *,
    compression: int = 0,
    header: int = 40,
    masks: tuple[int, ...] = (),
    palette: bytes = b"",
    per_metre: int = 0,
) -> bytes:
    """Return a BMP of ``rows`` in file order, each padded to four bytes."""
    info = struct.pack("<IiiHHIIiiII", header, width, height, 1, bits, compression, 0, per_metre, per_metre, 0, 0)
    info += struct.pack(f"<{len(masks)}I", *masks)
    info = info.ljust(header if header > 40 else len(info), b"\0")
    pixels = b"".join(row.ljust((len(row) + 3) // 4 * 4, b"\0") for row in rows)
    offset = 14 + len(info) + len(palette)
    return b"BM" + struct.pack("<IHHI", offset + len(pixels), 0, 0, offset) + info + palette + pixels


def test_bmp_bit_fields_with_alpha_decode() -> None:
    # A V5 header whose masks put alpha in the low byte, rows top-down.
    pixel = struct.pack("<I", 0x11223344)
    data = _bmp(1, -2, 32, [pixel, pixel[::-1]], compression=3, header=124, masks=(0xFF000000, 0xFF0000, 0xFF00, 0xFF))

    pixmap = Pixmap.from_bytes(data)

    assert _shape(pixmap) == (1, 2, False, True)
    assert bytes(pixmap) == bytes([0x11, 0x22, 0x33, 0x44, 0x44, 0x33, 0x22, 0x11])


def test_bmp_sixteen_bits_and_palettes_decode() -> None:
    # 5-6-5 bit fields: pure red then full white.
    rgb565 = _bmp(2, 1, 16, [struct.pack("<HH", 0xF800, 0xFFFF)], compression=3, masks=(0xF800, 0x07E0, 0x001F))
    assert bytes(Pixmap.from_bytes(rgb565)) == bytes([255, 0, 0, 255, 255, 255])
    # The default 5-5-5 fields.
    rgb555 = _bmp(1, 1, 16, [struct.pack("<H", 0x03E0)])
    assert bytes(Pixmap.from_bytes(rgb555)) == bytes([0, 255, 0])
    # Four bits through a red and green palette, bottom-up rows, at 5906 pixels per metre.
    palette = bytes([0, 0, 255, 0, 0, 255, 0, 0])
    four = Pixmap.from_bytes(_bmp(3, 2, 4, [b"\x10\x10", b"\x01\x00"], palette=palette, per_metre=5906))
    red, green = [255, 0, 0], [0, 255, 0]
    assert _shape(four) == (3, 2, False, False)
    assert bytes(four) == bytes(red + green + red + green + red + green)
    assert four.resolution == (150, 150)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"P6\n70000 70000\n255\n", "pixmap holds 1 to"),
        (b"P5\n0 3\n255\n", "pixmap holds 1 to"),
        (b"P5\n2 2\n255\n\x00\x01", "ends before its samples"),
        (b"P2\n2 1\n10\n3 11\n", "ends before its samples"),
        (b"P1\n2 1\n2 0", "neither 0 nor 1"),
        (b"P5\n1 1\n99999999999\n", "passes"),
        (b"P5\n1 1\n10\n\x0b", "passes the maximum value"),
        (b"P7\nWIDTH 1\nHEIGHT 1\nDEPTH 4\nMAXVAL 255\nTUPLTYPE CMYK\nENDHDR\n\0\0\0\0", "CMYK"),
        (b"P7\nWIDTH 1\nHEIGHT 1\nDEPTH 3\nMAXVAL 255\nTUPLTYPE GRAYSCALE\nENDHDR\n\0\0\0", "describe no"),
        (b"P7\nWIDTH 1\nHEIGHT 1\n", "no ENDHDR"),
        (b"BM\0\0", "truncated"),
        (_bmp(1, 1, 8, [b"\0"], compression=1), "run-length"),
        (_bmp(1, 1, 8, [b"\0"], compression=4), "embedded JPEG or PNG"),
        (_bmp(1, 1, 8, [b"\x05"], palette=b"\0\0\0\0"), "past the palette"),
        (_bmp(40000, 40000, 24, []), "pixmap holds 1 to"),
        (_bmp(4, 4, 24, [b"\0" * 12]), "ends before its pixels"),
        (_bmp(1, 1, 32, [b"\0" * 4], compression=3, masks=(0b101, 0xFF00, 0xFF0000)), "not contiguous"),
    ],
)
def test_invalid_and_oversized_input_fails(data: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Pixmap.from_bytes(data)


def test_files_decode_as_bytes_do(tmp_path: Path) -> None:
    for name, data in [
        ("image.ppm", _saved(_image("RGB"), "PPM")),
        ("image.bmp", _saved(_image("RGB"), "BMP")),
        ("image.pam", _pixmap("RGBA").to_bytes("pam")),
    ]:
        path = tmp_path / name
        path.write_bytes(data)
        assert bytes(Pixmap.open(path)) == bytes(Pixmap.from_bytes(data))


# Output.


def _pam(data: bytes) -> tuple[dict[str, str], bytes]:
    """Parse PAM in plain Python: its header fields and its samples."""
    header, samples = data.split(b"ENDHDR\n", 1)
    lines = header.decode().splitlines()
    assert lines[0] == "P7"
    return dict(line.split(" ", 1) for line in lines[1:]), samples


@pytest.mark.parametrize("mode", list(MODES))
def test_pam_keeps_samples_and_alpha(mode: str) -> None:
    pixmap = _pixmap(mode)

    fields, samples = _pam(pixmap.to_bytes("pam"))

    tuple_type = {"L": "GRAYSCALE", "LA": "GRAYSCALE_ALPHA", "RGB": "RGB", "RGBA": "RGB_ALPHA"}[mode]
    assert fields == {
        "WIDTH": str(WIDTH),
        "HEIGHT": str(HEIGHT),
        "DEPTH": str(len(mode)),
        "MAXVAL": "255",
        "TUPLTYPE": tuple_type,
    }
    assert samples == _image(mode).tobytes()
    assert bytes(Pixmap.from_bytes(pixmap.to_bytes("pam"))) == bytes(pixmap)


@pytest.mark.parametrize(("format", "mode"), [("pgm", "L"), ("ppm", "RGB"), ("pnm", "L"), ("pnm", "RGB")])
def test_portable_anymaps_open_in_pillow(format: str, mode: str) -> None:  # noqa: A002
    data = _pixmap(mode).to_bytes(format)

    image = Image.open(io.BytesIO(data))

    assert image.mode == mode
    assert image.tobytes() == _image(mode).tobytes()


def test_pbm_thresholds_gray_at_128() -> None:
    data = _pixmap("L").to_bytes("pbm")

    image = Image.open(io.BytesIO(data))

    assert data.startswith(b"P4\n")
    assert image.mode == "1"
    assert image.convert("L").tobytes() == bytes(0 if value < 128 else 255 for value in _image("L").tobytes())


@pytest.mark.parametrize("mode", list(MODES))
def test_psd_opens_in_pillow(mode: str) -> None:
    data = _pixmap(mode).to_bytes("psd")

    image = Image.open(io.BytesIO(data))

    assert image.size == (WIDTH, HEIGHT)
    if mode == "LA":
        # Pillow reads the extra channel of a grayscale PSD as nothing.
        assert image.mode == "L"
        assert image.tobytes() == _image("L").tobytes()
    else:
        assert image.mode == mode
        assert image.tobytes() == _image(mode).tobytes()
    # ResolutionInfo: 150 pixels per inch in 16.16 fixed point, across and down.
    resolution = struct.pack(">IHHIHH", 150 << 16, 1, 1, 150 << 16, 1, 1)
    assert image.resources == [(1005, b"", resolution)]  # ty: ignore[unresolved-attribute]


def _ps_samples(data: bytes) -> bytes:
    """Decode the ASCII85 samples of PostScript output in plain Python."""
    text = data.decode("ascii")
    encoded = text.split("image\n", 1)[1].split("~>", 1)[0]
    return base64.a85decode("<~" + encoded + "~>", adobe=True)


@pytest.mark.parametrize("mode", ["L", "RGB"])
def test_postscript_holds_the_samples_at_the_resolution(mode: str) -> None:
    data = _pixmap(mode).to_bytes("ps")
    text = data.decode("ascii")

    assert text.startswith("%!PS-Adobe-3.0\n")
    assert "%%BoundingBox: 0 0 4 3\n" in text
    assert ("/DeviceGray" if mode == "L" else "/DeviceRGB") in text
    assert re.search(rf"/Width {WIDTH} /Height {HEIGHT} ", text)
    assert _ps_samples(data) == _image(mode).tobytes()
    assert text.endswith("%%EOF\n")


def test_postscript_encodes_zero_groups_and_tails() -> None:
    image = Image.new("RGB", (3, 3), (0, 0, 0))
    image.putpixel((2, 2), (1, 2, 3))
    out = io.BytesIO()
    image.save(out, "PNG")

    data = Pixmap.from_bytes(out.getvalue()).to_bytes("ps")

    assert b"zzzzz" in data
    assert _ps_samples(data) == image.tobytes()


@pytest.mark.skipif(shutil.which("gs") is None, reason="needs Ghostscript")
@pytest.mark.parametrize("mode", ["L", "RGB"])
def test_postscript_renders_in_ghostscript(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "image.ps"
    _pixmap(mode).save(path)
    device = "pgmraw" if mode == "L" else "ppmraw"

    gs = shutil.which("gs") or "gs"
    subprocess.run(  # noqa: S603 - a fixed command on a file of the test's own.
        [
            gs,
            "-q",
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            f"-sDEVICE={device}",
            "-r150",
            f"-sOutputFile={tmp_path / 'out'}",
            str(path),
        ],
        check=True,
    )

    rendered = Image.open(tmp_path / "out")
    assert rendered.size == (WIDTH, HEIGHT)
    # Ghostscript's gray color management moves a sample by up to 1.
    assert max(abs(a - b) for a, b in zip(rendered.tobytes(), _image(mode).tobytes(), strict=True)) <= 1


@pytest.mark.parametrize(
    ("format", "extension"),
    [("pam", "pam"), ("pbm", "pbm"), ("pgm", "pgm"), ("pnm", "pnm"), ("psd", "psd"), ("ps", "ps")],
)
def test_files_and_bytes_agree(format: str, extension: str, tmp_path: Path) -> None:  # noqa: A002
    pixmap = _pixmap("L")
    by_extension, by_name = tmp_path / f"image.{extension.upper()}", tmp_path / "image.out"

    pixmap.save(by_extension)
    pixmap.save(by_name, format=format)

    assert by_extension.read_bytes() == by_name.read_bytes() == pixmap.to_bytes(format)


def test_ppm_extension_writes_rgb(tmp_path: Path) -> None:
    pixmap = _pixmap("RGB")
    pixmap.save(tmp_path / "image.ppm")
    assert (tmp_path / "image.ppm").read_bytes() == pixmap.to_bytes("ppm")


@pytest.mark.parametrize(
    ("mode", "format", "message"),
    [
        ("RGBA", "ppm", "PNM has no alpha channel"),
        ("LA", "pgm", "PNM has no alpha channel"),
        ("RGBA", "ps", "PostScript has no alpha channel"),
        ("LA", "pbm", "PBM has no alpha channel"),
        ("RGB", "pgm", "PGM holds gray samples"),
        ("RGB", "pbm", "PBM holds gray samples"),
        ("L", "ppm", "PPM holds RGB samples"),
        ("RGB", "tiff", "format must be"),
    ],
)
def test_unsupported_combinations_fail(mode: str, format: str, message: str, tmp_path: Path) -> None:  # noqa: A002
    pixmap = _pixmap(mode)

    with pytest.raises(ValueError, match=message):
        pixmap.to_bytes(format)
    with pytest.raises(ValueError, match=message):
        pixmap.save(tmp_path / "image.out", format=format)
    assert not (tmp_path / "image.out").exists()


def test_psd_refuses_sides_past_30000_pixels() -> None:
    pixmap = Pixmap.from_bytes(b"P5\n30001 1\n255\n" + bytes(30001))

    with pytest.raises(Exception, match="30000 pixels"):
        pixmap.to_bytes("psd")


def test_output_stays_within_max_size() -> None:
    with pytest.raises(Exception, match="output limit") as raised:
        _pixmap("RGB").to_bytes("ps", max_size=100)
    assert getattr(raised.value, "code", None) == "pixmap_output_size"
