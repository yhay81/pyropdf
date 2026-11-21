"""Filtered, conditional and stitched output-device curves under ISO 32000-2 §10.4.2.4."""

from __future__ import annotations

import io
import math
import shutil
import subprocess
import zlib
from typing import TYPE_CHECKING

import pikepdf
import pytest
from PIL import Image
from test_recolor import pages_pdf, stream

import pyropdf

if TYPE_CHECKING:
    from pathlib import Path

    from _pytest.mark.structures import ParameterSet

FAMILIES = ("sampled", "calculator", "conditional", "nested")
FILL = (0.2, 0.45, 0.75)
STROKE = (0.25, 0.125, 0.0625)
PIXELS = ((204, 153, 128), (64, 32, 16))
PIXEL_DATA = bytes(value for pixel in PIXELS for value in pixel)
IMAGE = "/Type /XObject /Subtype /Image /Width 2 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceRGB"
IDENTITY = "<< /FunctionType 2 /Domain [0 1] /C0 [0] /C1 [1] /N 1 >>"
ZERO = "<< /FunctionType 2 /Domain [0 1] /C0 [0] /C1 [0] /N 1 >>"
STITCH = "/FunctionType 3 /Domain [0 1] /Range [0 1] /Bounds [0.5]"


def function_objects(family: str, *, filtered: bool) -> dict[int, str]:
    def leaf(data: bytes, entries: str) -> str:
        if filtered:
            return stream(zlib.compress(data), entries + " /Filter /FlateDecode")
        return stream(data, entries)

    common = "/Domain [0 1] /Range [0 1]"
    table = leaf(bytes([0, 255, 0]), f"/FunctionType 0 {common} /Size [3] /BitsPerSample 8")
    if family == "sampled":
        return {20: table}
    if family == "nested":
        return {
            20: f"<< {STITCH} /Functions [21 0 R {IDENTITY}] /Encode [0 1 1 0] >>",
            21: f"<< {STITCH} /Functions [22 0 R {IDENTITY}] /Encode [0 1 0 1] >>",
            22: table,
        }
    body = b"{ dup mul }" if family == "calculator" else b"{ dup 0.5 lt { dup mul } { 1 exch sub } ifelse }"
    return {20: leaf(body, f"/FunctionType 4 {common}")}


def expected_tint(family: str, x: float) -> float:
    """Calculate each tint directly, independently of PDF interpretation and stream decoding."""
    if family == "calculator":
        return x * x
    if family == "conditional":
        return x * x if x < 0.5 else 1 - x
    if family == "nested":
        if x >= 0.5:
            return 2 * (1 - x)
        if x >= 0.25:
            return 4 * x - 1
        x *= 4
    return 2 * min(x, 1 - x)


def expected_ink(family: str, role: str, rgb: tuple[float, ...]) -> tuple[float, ...]:
    complements = tuple(1 - value for value in rgb)
    tint = expected_tint(family, min(complements))
    if role == "BG2":
        return (*complements, tint)
    return (*(max(0, value - tint) for value in complements), 0.0)


def input_pdf(family: str, role: str, *, filtered: bool) -> bytes:
    other = "UCR2" if role == "BG2" else "BG2"
    resources = f"/ExtGState << /G << /{role} 20 0 R /{other} {ZERO} >> >> /XObject << /Im 23 0 R >>"
    paint = "/G gs .2 .45 .75 rg .25 .125 .0625 RG 10 10 20 20 re B /Im Do"
    return pages_pdf(
        paint,
        "/Im Do",
        resources=resources,
        extra={**function_objects(family, filtered=filtered), 23: stream(PIXEL_DATA, IMAGE)},
    )


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("renderer", ["gs", "pdftoppm"])
def test_gray_tint_transform_matches_analytic_controls(
    tmp_path: Path, family: str, renderer: str, *, filtered: bool
) -> None:
    executable = shutil.which(renderer)
    if executable is None:
        pytest.skip(f"{renderer} unavailable")
    samples = (0.0, 0.125, 0.2, 0.25, 0.4, 0.499, 0.5, 0.51, 0.75, 191 / 255, 1.0)
    # Match a tint patch to a directly painted DeviceGray patch on the same
    # page, under the same output profile and any device transfer function.
    content = " ".join(
        f"/Alt cs {x:.9f} scn {i * 8} 50 7 7 re f {expected_tint(family, x):.9f} g {i * 8} 30 7 7 re f"
        for i, x in enumerate(samples)
    )
    source = tmp_path / "tints.pdf"
    source.write_bytes(
        pages_pdf(
            content,
            resources="/ColorSpace << /Alt [/Separation /Proof /DeviceGray 20 0 R] >>",
            extra=function_objects(family, filtered=filtered),
        )
    )
    bitmap = tmp_path / "proof.pgm"
    if renderer == "gs":
        args = [executable, "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pgmraw", "-r72", f"-sOutputFile={bitmap}", source]
    else:
        args = [executable, "-r", "72", "-gray", "-singlefile", source, bitmap.with_suffix("")]
    subprocess.run(args, check=True, capture_output=True, timeout=15)  # noqa: S603 -- fixed render tools and flags
    with Image.open(bitmap) as image:
        assert image.size == (100, 100)
        for i, x in enumerate(samples):
            observed = image.getpixel((i * 8 + 3, 46))
            reference = image.getpixel((i * 8 + 3, 66))
            assert isinstance(observed, int)
            assert isinstance(reference, int)
            assert abs(observed - reference) <= 1, (x, observed, reference)


class BoundedFunctionPendingError(Exception):
    """Identify the specific fully retained, reported refusal pending #15 and #105."""


def rewrite_case(family: str, *, filtered: bool) -> ParameterSet:
    if not filtered and family in {"sampled", "calculator"}:
        return pytest.param(family, filtered)
    pending = pytest.mark.xfail(
        strict=True,
        raises=BoundedFunctionPendingError,
        reason="Adopt the released bounded function constructor and execution fuel in #1007",
    )
    return pytest.param(family, filtered, marks=pending)


@pytest.mark.parametrize(
    ("family", "filtered"),
    [rewrite_case(family, filtered=filtered) for family in FAMILIES for filtered in (False, True)],
)
@pytest.mark.parametrize("role", ["BG2", "UCR2"])
@pytest.mark.parametrize("incremental", [False, True])
def test_rewrite_device_curves_on_fill_stroke_and_rgb_image(
    family: str, role: str, *, filtered: bool, incremental: bool
) -> None:
    source = input_pdf(family, role, filtered=filtered)
    document = pyropdf.open(source)
    report = document.pages[0].recolor("cmyk")
    saved = document.to_bytes(incremental=incremental)
    if incremental:
        assert saved.startswith(source)
    with pikepdf.Pdf.open(io.BytesIO(saved)) as checked:
        page = checked.pages[0]
        if report.unconverted:
            events = [(entry.page, entry.kind, entry.detail) for entry in report.unconverted]
            reason = "active black generation or undercolor removal cannot be evaluated"
            assert events == [(0, "conversion", reason), (0, "conversion", f"/Im (object 23): {reason}")]
            assert report.operations == report.images == report.omitted == 0
            with pikepdf.Pdf.open(io.BytesIO(source)) as original:
                for before, after in zip(original.pages, checked.pages, strict=True):
                    assert before.Contents.read_bytes() == after.Contents.read_bytes()
                    assert (
                        before.Resources.XObject.Im.read_bytes()
                        == after.Resources.XObject.Im.read_bytes()
                        == PIXEL_DATA
                    )
                    assert (
                        before.Resources.XObject.Im.ColorSpace
                        == after.Resources.XObject.Im.ColorSpace
                        == pikepdf.Name.DeviceRGB
                    )
            raise BoundedFunctionPendingError
        ops = pikepdf.parse_content_stream(page)
        for operator, rgb in (("k", FILL), ("K", STROKE)):
            (selected,) = [op for op in ops if str(op.operator) == operator]
            assert tuple(float(v) for v in selected.operands) == pytest.approx(
                expected_ink(family, role, rgb), abs=0.0001
            )
        (name,) = [str(op.operands[0]) for op in ops if str(op.operator) == "Do"]
        image = page.Resources.XObject[name]
        assert image.ColorSpace == pikepdf.Name.DeviceCMYK
        assert (image.Width, image.Height, image.BitsPerComponent) == (2, 1, 8)
        expected = [
            math.floor(value * 255 + 0.5)
            for pixel in PIXELS
            for value in expected_ink(family, role, tuple(channel / 255 for channel in pixel))
        ]
        assert list(image.read_bytes()) == pytest.approx(expected, abs=1)
        second = checked.pages[1]
        assert second.Resources.XObject.Im.ColorSpace == pikepdf.Name.DeviceRGB
        assert second.Resources.XObject.Im.read_bytes() == PIXEL_DATA
        assert second.Contents.read_bytes() == b"/Im Do"
