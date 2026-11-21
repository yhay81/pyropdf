"""Compare a pair of controlled 16-point swatches on continuous-tone, screened and plate devices."""

from __future__ import annotations

import io
import shutil
import subprocess
from typing import TYPE_CHECKING

import pikepdf
import pytest
from PIL import Image
from test_device_rendering import _sample
from test_pdfa_corpus import GENERATOR

import pyropdf
from pyropdf import Name

if TYPE_CHECKING:
    from pathlib import Path

_CAL = (
    "/ColorSpace << /Cal [/CalRGB << /WhitePoint [0.9505 1 1.089] /Gamma [1 1 1] "
    "/Matrix [0.4124 0.2126 0.0193 0.3576 0.7152 0.1192 0.1805 0.0722 0.9505] >>] >>"
)
_SPOT = (
    "/ColorSpace << /Ink [/Separation /Varnish /DeviceCMYK << /FunctionType 2 /Domain [0 1] "
    "/C0 [0 0 0 0] /C1 [0 1 0 0] /N 1 >>] >>"
)
_SPOT_PAINT = b"/Ink cs 0 scn 0 0 16 16 re f 1 scn 16 0 16 16 re f"
_INVERT = "<< /FunctionType 2 /Domain [0 1] /C0 [1] /C1 [0] /N 1 >>"
_IDENTITY = "<< /FunctionType 2 /Domain [0 1] /C0 [0] /C1 [1] /N 1 >>"


def _program(name: str) -> str:
    command = shutil.which(name)
    if command is None:
        pytest.skip(f"{name} is required for this independent device")
    return command


def _poppler(data: bytes, tmp_path: Path) -> Image.Image:
    path = tmp_path / "source.pdf"
    path.write_bytes(data)
    run = subprocess.run(  # noqa: S603 - fixed arguments and a test-owned PDF.
        [_program("pdftoppm"), "-r", "72", "-singlefile", str(path)], capture_output=True, check=True, timeout=15
    )
    with Image.open(io.BytesIO(run.stdout)) as image:
        return image.copy()


def _preview(data: bytes) -> Image.Image:
    with pyropdf.open(data) as doc:
        pixmap = doc.pages[0].pixmap()
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), bytes(pixmap))


def _ghostscript(data: bytes, tmp_path: Path, device: str, *options: str) -> Path:
    source = tmp_path / "source.pdf"
    source.write_bytes(data)
    output = tmp_path / "device.tif"
    subprocess.run(  # noqa: S603 - fixed Ghostscript devices and test-owned files.
        [
            _program("gs"),
            "-q",
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-r72",
            f"-sDEVICE={device}",
            *options,
            f"-sOutputFile={output}",
            str(source),
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )
    return output


def _swatches(image: Image.Image) -> tuple[bytes, bytes]:
    assert image.size == (32, 16)
    return image.crop((8, 8, 9, 9)).tobytes(), image.crop((24, 8, 25, 9)).tobytes()


@pytest.mark.spec("transfer-functions", "interpret")
@pytest.mark.parametrize(
    ("state", "right"),
    [
        (f"/TR {_IDENTITY} /TR2 {_INVERT}", (191, 191, 191)),
        (f"/TR {_INVERT} /TR2 /Default", (64, 64, 64)),
        (f"/TR2 [{_INVERT} {_IDENTITY} {_INVERT} {_IDENTITY}]", (191, 64, 191)),
    ],
)
def test_transfer_precedence_and_component_curves(state: str, right: tuple[int, ...], tmp_path: Path) -> None:
    pdf = _sample(paint=b"0.25 0.25 0.25 rg 0 0 16 16 re f /Press gs 16 0 16 16 re f", state=state)
    reference = _swatches(_poppler(pdf, tmp_path))
    assert reference == (bytes([64] * 3), bytes(right))
    assert _swatches(_preview(pdf)) == reference


@pytest.mark.spec("color-conversion", "interpret")
def test_calrgb_linear_light_is_converted_for_rgb_display(tmp_path: Path) -> None:
    pdf = _sample(_CAL, b"0.5 0.5 0.5 rg 0 0 16 16 re f /Cal cs 0.5 0.5 0.5 sc 16 0 16 16 re f")
    reference = _swatches(_poppler(pdf, tmp_path))
    left, right = _swatches(_preview(pdf))
    assert left == reference[0] == bytes([128] * 3)
    assert tuple(right) == pytest.approx((188, 188, 188), abs=2)
    assert tuple(right) == pytest.approx(tuple(reference[1]), abs=2)


@pytest.mark.spec("separation-rendering", "interpret")
def test_named_ink_uses_its_tint_transform_on_an_rgb_display(tmp_path: Path) -> None:
    pdf = _sample(_SPOT, _SPOT_PAINT)
    reference = _swatches(_poppler(pdf, tmp_path))
    left, right = _swatches(_preview(pdf))
    assert left == reference[0] == bytes([255] * 3)
    assert right[0] > right[2] > right[1]
    assert tuple(right) == pytest.approx(tuple(reference[1]), abs=5)


@pytest.mark.spec("separation-rendering", "interpret")
def test_named_ink_plate_is_separate_from_all_four_process_plates(tmp_path: Path) -> None:
    output = _ghostscript(_sample(_SPOT, _SPOT_PAINT), tmp_path, "tiffsep")
    for plate in ("Cyan", "Magenta", "Yellow", "Black", "Varnish"):
        with Image.open(output.with_name(f"device({plate}).tif")) as image:
            assert _swatches(image) == (b"\xff", b"\x00" if plate == "Varnish" else b"\xff")


@pytest.mark.spec("overprint", "interpret")
def test_zero_cyan_keeps_the_backdrop_only_in_overprint_mode_one(tmp_path: Path) -> None:
    states = "/ExtGState << /Mode0 << /op true /OPM 0 >> /Mode1 << /op true /OPM 1 >> >>"
    paint = b"1 0 0 0 k 0 0 32 16 re f 0 1 0 0 k /Mode0 gs 0 0 16 16 re f /Mode1 gs 16 0 16 16 re f"
    output = _ghostscript(_sample(states, paint), tmp_path, "tiffsep")
    with Image.open(output) as image:
        assert _swatches(image) == (bytes([0, 255, 0, 0]), bytes([255, 255, 0, 0]))
    with Image.open(output.with_name("device(Cyan).tif")) as image:
        assert _swatches(image) == (b"\xff", b"\x00")


@pytest.mark.spec("rendering-intents", "interpret")
def test_output_device_can_distinguish_absolute_and_relative_rendering_intent(tmp_path: Path) -> None:
    pdf = _sample(
        _CAL,
        b"/Cal cs /RelativeColorimetric ri 1 0.1 1 sc 0 0 16 16 re f "
        b"/AbsoluteColorimetric ri 1 0.1 1 sc 16 0 16 16 re f",
    )
    with Image.open(_ghostscript(pdf, tmp_path, "tiff32nc")) as image:
        relative, absolute = _swatches(image)
        assert relative != absolute
        assert relative[1] > absolute[1]
    # The RGB preview of the first patch is independently comparable.
    preview, _ = _swatches(_preview(pdf))
    reference, _ = _swatches(_poppler(pdf, tmp_path))
    assert tuple(preview) == pytest.approx(tuple(reference), abs=4)


@pytest.mark.spec("halftones", "interpret")
def test_explicit_line_screen_changes_dot_placement_on_a_bilevel_device(tmp_path: Path) -> None:
    pdf = _sample(
        paint=b"0.5 g 0 0 16 16 re f /Press gs 16 0 16 16 re f",
        state="/HT << /HalftoneType 1 /Frequency 6 /Angle 0 /SpotFunction /Line >>",
    )
    with Image.open(_ghostscript(pdf, tmp_path, "pbmraw")).convert("L") as image:
        left = image.crop((0, 0, 16, 16))
        right = image.crop((16, 0, 32, 16))
        assert left.tobytes() != right.tobytes()
        assert set(right.tobytes()) == {0, 255}
        assert all(len(set(right.crop((0, y, 16, y + 1)).tobytes())) == 1 for y in range(16))
    assert _swatches(_poppler(pdf, tmp_path)) == _swatches(_preview(pdf)) == (bytes([128] * 3),) * 2


@pytest.mark.spec("scan-conversion", "interpret")
def test_thin_stroke_adjustment_changes_antialiased_device_coverage(tmp_path: Path) -> None:
    states = "/ExtGState << /Off << /SA false >> /On << /SA true /FL 0.5 /SM 0.5 >> >>"
    paint = b"/Off gs 0.2 w 3 1 m 3 15 l S /On gs 19 1 m 19 15 l S"
    pdf = _sample(states, paint)
    with Image.open(_ghostscript(pdf, tmp_path, "pgmraw", "-dGraphicsAlphaBits=4")) as image:
        left = image.crop((0, 8, 16, 9)).tobytes()
        right = image.crop((16, 8, 32, 9)).tobytes()
        assert left != right
        assert sum(tone < 255 for tone in left) > sum(tone < 255 for tone in right)
    preview = _preview(pdf).convert("L")
    assert min(preview.crop((0, 8, 16, 9)).tobytes()) < 255
    assert min(preview.crop((16, 8, 32, 9)).tobytes()) < 255


def test_classic_black_generation_controls_and_the_pdf_device(tmp_path: Path) -> None:
    # PostScript exposes the same classic conversion directly, independent of a PDF device's ICC policy.
    program = (
        "{pop 0} setblackgeneration {pop 0} setundercolorremoval "
        ".5 .5 .5 setrgbcolor currentcmykcolor 4 array astore == "
        "{} setblackgeneration {} setundercolorremoval .5 .5 .5 setrgbcolor currentcmykcolor 4 array astore == quit"
    )
    run = subprocess.run(  # noqa: S603 - fixed interpreter input with no document content.
        [_program("gs"), "-q", "-dSAFER", "-dNODISPLAY", "-dUseFastColor", "-c", program],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    results = [tuple(map(float, row.strip("[]").split())) for row in run.stdout.splitlines()]
    assert results == [(0.5, 0.5, 0.5, 0.0), (0.0, 0.0, 0.0, 0.5)]
    zero = "<< /FunctionType 2 /Domain [0 1] /C0 [0] /C1 [0] /N 1 >>"
    states = f"/ExtGState << /Zero << /BG2 {zero} /UCR2 {zero} >> /Full << /BG2 {_IDENTITY} /UCR2 {_IDENTITY} >> >>"
    paint = b"/Zero gs .5 .5 .5 rg 0 0 16 16 re f /Full gs .5 .5 .5 rg 16 0 16 16 re f"
    pdf = _sample(states, paint)
    with Image.open(_ghostscript(pdf, tmp_path, "tiff32nc", "-dUseFastColor")) as image:
        left, right = _swatches(image)
        assert tuple(left) == pytest.approx((127, 127, 127, 0), abs=1)
        # An independent PDF renderer may select device defaults for these functions.
        assert right == left or tuple(right) == pytest.approx((0, 0, 0, 127), abs=1)
    left, right = _swatches(_preview(pdf))
    assert left == right == bytes([128] * 3)


@pytest.mark.spec("color-conversion", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_embedded_icc_profile_is_used_and_its_bytes_survive_editing(tmp_path: Path, *, incremental: bool) -> None:
    profile = GENERATOR["icc"]()
    # The calibrated patch is intentionally chromatic to detect component or profile changes.
    paint = b"0.2 0.7 0.4 rg 0 0 16 16 re f /Paint cs 0.2 0.7 0.4 sc 16 0 16 16 re f"
    doc = pyropdf.open(_sample(paint=paint))
    number = doc.objects.add_stream(profile, {"N": 3, "Alternate": Name("DeviceRGB")})
    doc.pages[0].obj["Resources"]["ColorSpace"]["Paint"] = [Name("ICCBased"), doc.objects[number]]
    first = doc.to_bytes(incremental=incremental)
    reopened = pyropdf.open(first)
    reopened.pages[0].obj["Resources"]["ExtGState"]["Press"]["RI"] = Name("Saturation")
    saved = reopened.to_bytes(incremental=incremental)
    with pikepdf.Pdf.open(io.BytesIO(saved)) as other:
        space, stream = other.pages[0].Resources.ColorSpace.Paint
        assert space == pikepdf.Name.ICCBased
        assert stream.N == 3
        assert stream.read_bytes() == profile
        assert pikepdf.Name.Saturation == other.pages[0].Resources.ExtGState.Press.RI
    for data in (first, saved):
        preview = _swatches(_preview(data))
        reference = _swatches(_poppler(data, tmp_path))
        assert preview[0] != preview[1]
        assert tuple(preview[1]) == pytest.approx(tuple(reference[1]), abs=5)
