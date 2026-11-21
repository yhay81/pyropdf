"""Independent group, alpha and mask controls for ISO 32000-2 §11."""

from __future__ import annotations

import io
import shutil
import subprocess
from typing import TYPE_CHECKING

import pikepdf
import pytest
from PIL import Image
from test_page_transparency import _vector
from test_recolor import pages_pdf

import pyropdf
from pyropdf import Color, Name, typed

if TYPE_CHECKING:
    from pathlib import Path

_RED = Color(1, 0, 0)
_WHITE = Color(1, 1, 1)
_BLUE = Color(0, 0, 1)
_UNEDITED = "0 1 0 rg 0 0 4 4 re f"


def _display(data: bytes, tmp_path: Path, *points: tuple[int, int]) -> list[tuple[int, ...]]:
    binary = shutil.which("pdftoppm")
    if not binary:
        pytest.skip("Poppler is the independent transparency renderer")
    pdf, out = tmp_path / "transparency.pdf", tmp_path / "preview"
    pdf.write_bytes(data)
    subprocess.run(  # noqa: S603 - fixed independent renderer, test-owned paths.
        [binary, "-f", "1", "-l", "1", "-r", "72", "-png", "-singlefile", str(pdf), str(out)],
        capture_output=True,
        check=True,
        timeout=15,
    )
    with Image.open(out.with_suffix(".png")) as rendered:
        rgb = rendered.convert("RGB")
        return [tuple(rgb.getpixel(p)) for p in points]  # ty: ignore[invalid-argument-type] - RGB tuple.


def _ghostscript_pixel(data: bytes, tmp_path: Path) -> tuple[int, ...]:
    gs = shutil.which("gs")
    if not gs:
        pytest.skip("Ghostscript supplies the independent group compositing sample")
    pdf, png = tmp_path / "knockout.pdf", tmp_path / "knockout.png"
    pdf.write_bytes(data)
    subprocess.run(  # noqa: S603 - fixed device and test-owned paths.
        [
            gs,
            "-q",
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-dFirstPage=1",
            "-dLastPage=1",
            "-r72",
            "-sDEVICE=png16m",
            f"-sOutputFile={png}",
            str(pdf),
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )
    with Image.open(png) as image:
        pixel = image.getpixel((47, 30))
        assert isinstance(pixel, tuple)
        return pixel


def _canvas_group(*, isolated: bool, knockout: bool) -> pyropdf.Document:
    doc = pyropdf.open(pages_pdf("", _UNEDITED))
    with doc.pages[0].draw() as canvas:
        canvas.rect((0, 0, 100, 100), fill=_WHITE)
        canvas.rect((0, 0, 40, 100), fill=_BLUE)
        with canvas.opacity(1, isolated=isolated, knockout=knockout, colorspace="rgb" if isolated else None):
            with canvas.blend("multiply"):
                canvas.rect((10, 10, 55, 60), fill=_RED)
            canvas.rect((30, 10, 75, 60), fill=Color(0, 1, 0, 0.5))
    return doc


@pytest.mark.spec("transparency-groups", "create", "edit")
@pytest.mark.spec("transparency-color", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("alpha-compositing", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("knockout", [False, True])
@pytest.mark.parametrize("incremental", [False, True])
def test_isolation_knockout_and_group_blending_space(
    tmp_path: Path, *, isolated: bool, knockout: bool, incremental: bool
) -> None:
    source = _canvas_group(isolated=isolated, knockout=knockout)
    data = source.to_bytes(incremental=incremental)
    reopened = pyropdf.open(data)
    forms = reopened.pages[0].obj["Resources"]["XObject"]
    (form,) = [value for value in forms.values() if value.get("Group")]
    attr = typed.GroupAttributes(form["Group"])
    assert bool(attr.I) is isolated
    assert bool(attr.K) is knockout
    assert ("DeviceRGB" if isolated else None) == attr.CS
    with pikepdf.Pdf.open(io.BytesIO(data)) as reader:
        group = next(value.Group for value in reader.pages[0].Resources.XObject.values() if "/Group" in value)
        assert bool(group.get("/I")) is isolated
        assert bool(group.get("/K")) is knockout
        assert group.get("/CS") == (pikepdf.Name.DeviceRGB if isolated else None)
        assert reader.pages[1].Contents.read_bytes() == _UNEDITED.encode()
    # Red multiplied against inherited blue is black; against an isolated
    # transparent backdrop it stays red. Knockout composites green over the
    # group's initial backdrop instead of red.
    left, overlap, green = _display(data, tmp_path, (20, 30), (47, 30), (68, 30))
    assert left == pytest.approx((255, 0, 0) if isolated else (0, 0, 0), abs=2)
    if not knockout:
        assert overlap == pytest.approx((127, 128, 0), abs=2)
    if isolated and not knockout:
        assert reopened.pages[0].pixmap(dpi=72).pixel(20, 30) == pytest.approx(left, abs=2)
    assert green == pytest.approx((127, 255, 127), abs=2)
    attr.K = not knockout
    edited = reopened.to_bytes(incremental=incremental)
    assert pyropdf.open(edited).pages[0].uses_transparency()
    with pikepdf.Pdf.open(io.BytesIO(edited)) as reader:
        group = next(x.Group for x in reader.pages[0].Resources.XObject.values() if "/Group" in x)
        assert bool(group.K) is not knockout
    assert _ghostscript_pixel(data, tmp_path) == pytest.approx((127, 255, 127) if knockout else (127, 128, 0), abs=2)
    assert _ghostscript_pixel(edited, tmp_path) == pytest.approx((127, 128, 0) if knockout else (127, 255, 127), abs=2)


def _mask(doc: pyropdf.Document, *, alpha: bool) -> pyropdf.Stream:
    # The left half is half opaque for alpha, or painted 50% gray for luminosity.
    content = b"/Half gs 1 g 0 0 50 100 re f" if alpha else b".5 g 0 0 50 100 re f"
    number = doc.objects.add_stream(
        content,
        {
            "Type": Name("XObject"),
            "Subtype": Name("Form"),
            "BBox": [0, 0, 100, 100],
            "Group": {"S": Name("Transparency"), "I": True, "CS": Name("DeviceGray")},
            "Resources": {"ExtGState": {"Half": {"ca": 0.5}}},
        },
    )
    return doc.objects[number]


@pytest.mark.spec("soft-masks", "create", "edit")
@pytest.mark.parametrize("subtype", ["alpha", "luminosity"])
@pytest.mark.parametrize("incremental", [False, True])
def test_group_soft_masks_and_replacing_their_subtype(subtype: str, tmp_path: Path, *, incremental: bool) -> None:
    doc = pyropdf.open(pages_pdf("", _UNEDITED))
    group = _mask(doc, alpha=subtype == "alpha")
    with doc.pages[0].draw() as canvas:
        canvas.rect((0, 0, 100, 100), fill=_WHITE)
        with canvas.soft_mask(group, subtype=subtype):
            canvas.rect((0, 0, 100, 100), fill=_RED)
        canvas.rect((48, 48, 52, 52), fill=_BLUE)
    data = doc.to_bytes(incremental=incremental)
    reopened = pyropdf.open(data)
    states = reopened.pages[0].obj["Resources"]["ExtGState"]
    (mask,) = [entry["SMask"] for entry in states.values() if "SMask" in entry]
    assert mask["S"] == subtype.title()
    assert mask["G"]["Group"]["CS"] == "DeviceGray"
    expected = [(255, 127, 127), (255, 255, 255), (0, 0, 255)]
    points = [(20, 30), (80, 30), (50, 50)]
    for sample, control in zip(_display(data, tmp_path, *points), expected, strict=True):
        assert sample == pytest.approx(control, abs=2)
    for point, control in zip(points, expected, strict=True):
        assert reopened.pages[0].pixmap(dpi=72).pixel(*point) == pytest.approx(control, abs=3)
    mask["S"] = Name("Alpha")
    mask["G"].write(b"1 g 0 0 100 100 re f")
    edited = reopened.to_bytes(incremental=incremental)
    assert _display(edited, tmp_path, (80, 30)) == [(255, 0, 0)]
    with pikepdf.Pdf.open(io.BytesIO(edited)) as other:
        written = next(s.SMask for s in other.pages[0].Resources.ExtGState.values() if "/SMask" in s)
        assert pikepdf.Name.Alpha == written.S
        assert written.G.read_bytes() == b"1 g 0 0 100 100 re f"
        assert other.pages[1].Contents.read_bytes() == _UNEDITED.encode()


@pytest.mark.spec("blend-modes", "edit")
def test_edit_multiply_to_screen_and_nested_alpha(tmp_path: Path) -> None:
    doc = pyropdf.open(pages_pdf("", _UNEDITED))
    with doc.pages[0].draw() as canvas:
        canvas.rect((0, 0, 100, 100), fill=Color(0.2, 0.5, 0.8))
        with canvas.opacity(0.5, isolated=True), canvas.opacity(0.5, isolated=True):
            canvas.rect((0, 0, 30, 100), fill=Color(1, 0, 0))
        with canvas.blend("multiply"):
            canvas.rect((60, 0, 100, 100), fill=Color(0.8, 0.5, 0.2))
    state = next(s for s in doc.pages[0].obj["Resources"]["ExtGState"].values() if s.get("BM") == "Multiply")
    for mode, expected in (("Multiply", (41, 64, 41)), ("Screen", (214, 191, 214))):
        typed.GraphicsStateParameter(state).BM = mode
        data = doc.to_bytes()
        native = pyropdf.open(data).pages[0].pixmap(dpi=72)
        left, right = _display(data, tmp_path, (15, 30), (75, 30))
        assert left == pytest.approx((102, 96, 153), abs=2)
        assert right == pytest.approx(expected, abs=2)
        assert native.pixel(15, 30) == pytest.approx(left, abs=3)
        assert native.pixel(75, 30) == pytest.approx(right, abs=3)


def test_invalid_group_space_or_mask_refuses_before_changing_the_page() -> None:
    doc = pyropdf.open(pages_pdf("", _UNEDITED))
    other = pyropdf.open(pages_pdf(""))
    unrelated = _mask(other, alpha=True)
    before = doc.pages[0].operations.to_bytes()
    with doc.pages[0].draw() as canvas:
        with pytest.raises(ValueError, match="isolated group"):
            canvas.opacity(0.5, colorspace="rgb")
        with pytest.raises(ValueError, match="this document"):
            canvas.soft_mask(unrelated)
        plain = doc.objects[doc.objects.add_stream(b"", {"Subtype": Name("Form")})]
        with pytest.raises(ValueError, match="transparency group"):
            canvas.soft_mask(plain)
    assert doc.pages[0].operations.to_bytes() == before


@pytest.mark.spec("transparency-color", "read", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_edit_isolated_group_blending_space(tmp_path: Path, *, incremental: bool) -> None:
    doc = pyropdf.open(pages_pdf("", _UNEDITED))
    with doc.pages[0].draw() as canvas, canvas.opacity(1, isolated=True, colorspace="rgb"):
        canvas.rect((0, 0, 100, 100), fill=_RED)
    (form,) = doc.pages[0].obj["Resources"]["XObject"].values()
    group = typed.GroupAttributes(form["Group"])
    assert _ghostscript_pixel(doc.to_bytes(incremental=incremental), tmp_path) == (255, 0, 0)
    for space in ("DeviceGray", "DeviceCMYK"):
        group.CS = space
        data = doc.to_bytes(incremental=incremental)
        with pikepdf.Pdf.open(io.BytesIO(data)) as reader:
            (other,) = reader.pages[0].Resources.XObject.values()
            assert str(other.Group.CS) == "/" + space
            assert reader.pages[1].Contents.read_bytes() == _UNEDITED.encode()
        red, green, blue = _ghostscript_pixel(data, tmp_path)
        if space == "DeviceGray":
            assert red == green == blue
            assert 60 < red < 185
        else:
            assert red > 180
            assert max(green, blue) < 70


def test_luminosity_mask_validates_the_blending_space_and_version() -> None:
    doc = pyropdf.open(pages_pdf("", _UNEDITED).replace(b"%PDF-1.7", b"%PDF-1.3", 1))
    mask = _mask(doc, alpha=False)
    with doc.pages[0].draw() as canvas:
        for invalid in (None, Name("Lab"), Name("Pattern")):
            mask["Group"]["CS"] = invalid
            with pytest.raises(ValueError, match="blending colour space"):
                canvas.soft_mask(mask, subtype="luminosity")
        mask["Group"]["CS"] = Name("DeviceGray")
        with canvas.soft_mask(mask, subtype="luminosity"):
            canvas.rect((0, 0, 100, 100), fill=_RED)
    assert pyropdf.open(doc.to_bytes(incremental=True)).catalog["Version"] == "1.4"


def test_inline_image_fill_alpha_on_white_backdrop(tmp_path: Path) -> None:
    data = _vector("inline-image")
    for incremental in (False, True):
        saved = pyropdf.open(data).to_bytes(incremental=incremental)
        assert pyropdf.open(saved).pages[0].uses_transparency()
        # Byte 'x' is gray 120. At half opacity over white, the result is 187.5.
        assert _display(saved, tmp_path, (47, 30))[0] == pytest.approx((188, 188, 188), abs=2)
        assert _ghostscript_pixel(saved, tmp_path) == pytest.approx((188, 188, 188), abs=2)
