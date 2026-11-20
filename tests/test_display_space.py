"""Physical page landmarks in the public top-left point coordinate system (#851)."""

from __future__ import annotations

import io
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from PIL import Image, ImageChops
from test_pages import build_raw_pdf

import pyropdf

if TYPE_CHECKING:
    from pathlib import Path

# The first page inherits both boxes and its rotation. Clipping CropBox to
# MediaBox gives [10, 30.5, 190.5, 160]. At two points per user unit the
# visible sheet is 361 x 259 points. The asymmetric red rectangle is
# [43, 63, 53, 69] in the source. These are its four physical placements.
LANDMARKS = {
    0: (66, 182, 86, 194),
    90: (65, 66, 77, 86),
    180: (275, 65, 295, 77),
    270: (182, 275, 194, 295),
}
# The target page has crop [2, 4, 62, 84], /Rotate 270 and /UserUnit 3.
# Its blue square is centered at raw (19, 48), or 108 pt from the displayed
# left edge and 129 pt from the top.
TARGET = (108, 129)
RAW = [43, 63, 53, 69]


def _stream(operators: str) -> str:
    return f"<< /Length {len(operators)} >>\nstream\n{operators}\nendstream"


def _fixture(rotation: int) -> bytes:
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [10 0 R] >> >>",
            2: "<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
            3: f"<< /Type /Pages /Parent 2 0 R /Kids [4 0 R] /Count 1 /Rotate {rotation} "
            "/MediaBox [10 20 210 160] /CropBox [5.5 30.5 190.5 170.5] >>",
            4: "<< /Type /Page /Parent 3 0 R /UserUnit 2 /Contents 6 0 R /Annots [9 0 R 10 0 R] "
            "/VP [11 0 R] /Resources << /Font << /F1 8 0 R >> >> >>",
            5: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 70 100] /CropBox [2 4 62 84] "
            "/Rotate 270 /UserUnit 3 /Contents 7 0 R /Resources << /XObject << /Dots 12 0 R >> >> >>",
            6: _stream("1 0 0 rg 43 63 10 6 re f 0 g BT /F1 5 Tf 43 83 Td (Mark) Tj ET"),
            7: _stream("0 0 1 rg 17 46 4 4 re f q 8 0 0 6 30 12 cm /Dots Do Q"),
            8: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            9: "<< /Type /Annot /Subtype /Link /Rect [43 63 53 69] /Border [0 0 0] /Dest [5 0 R /XYZ 19 48 0] >>",
            10: "<< /Type /Annot /Subtype /Widget /FT /Btn /T (control) /F 2 /Rect [43 63 53 69] /P 4 0 R >>",
            11: "<< /Type /Viewport /Name (mark) /BBox [43 63 53 69] >>",
            12: "<< /Type /XObject /Subtype /Image /Width 2 /Height 1 /ColorSpace /DeviceRGB "
            "/BitsPerComponent 8 /Filter /ASCIIHexDecode /Length 13 >>\nstream\n00FF00FF00FF>\nendstream",
        }
    )


def _check_landmarks(doc: pyropdf.Document, rotation: int) -> None:
    page, target = doc.pages
    box = LANDMARKS[rotation]
    assert tuple(page.links[0].rect) == pytest.approx(box)
    assert tuple(page.widgets[0].rect) == pytest.approx(box)
    assert tuple(page.viewports[0].bbox) == pytest.approx(box)
    destination = page.links[0].target
    assert isinstance(destination, pyropdf.Destination)
    assert destination.page == 1
    assert destination.point == pytest.approx(TARGET)
    x0, y0, x1, y1 = box
    assert page.pixmap().pixel((x0 + x1) // 2, (y0 + y1) // 2) == (255, 0, 0)
    target_pixels = target.pixmap()
    assert target_pixels.pixel(*TARGET) == (0, 0, 255)
    # Two native samples, drawn in an 8x6 user-space rectangle. Rotation
    # places the left sample below the right sample on the target page.
    samples = target.images()[0].pixmap()
    assert (samples.width, samples.height) == (2, 1)
    assert samples.pixel(0, 0) == target_pixels.pixel(207, 90) == (0, 255, 0)
    assert samples.pixel(1, 0) == target_pixels.pixel(207, 78) == (255, 0, 255)


@pytest.mark.parametrize("rotation", LANDMARKS)
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("coordinate-systems", "read", "interpret", "create", "edit", "preserve")
def test_physical_landmarks_agree_after_edit_and_rotation_removal(rotation: int, *, incremental: bool) -> None:
    doc = pyropdf.open(_fixture(rotation))
    _check_landmarks(doc, rotation)
    page, target = doc.pages
    (hit,) = page.search("Mark")
    page.annotations.add(pyropdf.Highlight([hit]))
    link = page.links.add(LANDMARKS[rotation], pyropdf.Destination(1, point=TARGET))
    assert [float(n) for n in link.obj["Rect"]] == pytest.approx(RAW)
    assert [float(n) for n in link.obj["Dest"][2:4]] == pytest.approx([19, 48])

    def check_saved() -> None:
        reopened = pyropdf.open(doc.to_bytes(incremental=incremental))
        _check_landmarks(reopened, rotation)
        current = reopened.pages[0]
        (found,) = current.search("Mark")
        (highlight,) = [a for a in current.annotations if a.obj["Subtype"] == "Highlight"]
        assert found in highlight.rect
        assert tuple(found) == pytest.approx(tuple(hit), abs=0.01)

    check_saved()
    page.remove_rotation()
    target.remove_rotation()
    assert page.rotation == target.rotation == 0
    check_saved()


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="requires Poppler")
@pytest.mark.parametrize("rotation", LANDMARKS)
def test_poppler_places_the_red_landmark_at_the_same_physical_point(tmp_path: Path, rotation: int) -> None:
    reader = shutil.which("pdftoppm")
    assert reader is not None
    path = tmp_path / "marks.pdf"
    # Request the physical sheet size explicitly (one pixel per point);
    # Poppler versions differ in whether their default DPI honors /UserUnit.
    # Its axes before page rotation are 361 x 259 points for the source.
    doc = pyropdf.open(_fixture(rotation))
    original = doc.to_bytes()
    doc.pages[0].remove_rotation()
    displayed = (361, 259) if rotation in (0, 180) else (259, 361)
    for data, (width, height) in ((original, (361, 259)), (doc.to_bytes(incremental=True), displayed)):
        path.write_bytes(data)
        result = subprocess.run(  # noqa: S603 - fixed reader and generated input
            [
                reader,
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                "-cropbox",
                "-scale-to-x",
                str(width),
                "-scale-to-y",
                str(height),
                "-png",
                str(path),
            ],
            capture_output=True,
            check=True,
        )
        with Image.open(io.BytesIO(result.stdout)) as img:
            assert img.size == displayed
            red, green, blue = img.convert("RGB").split()
            ink = ImageChops.subtract(red, ImageChops.lighter(green, blue)).point(lambda v: 255 if v > 240 else 0)
            assert ink.getbbox() == pytest.approx(LANDMARKS[rotation], abs=1)


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_unrotating_shared_viewports_keeps_measuring_axes_and_the_other_page(*, incremental: bool) -> None:
    data = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 100] >>",
            3: "<< /Type /Page /Parent 2 0 R /Rotate 90 /VP 5 0 R >>",
            4: "<< /Type /Page /Parent 2 0 R /VP 5 0 R >>",
            5: "[6 0 R << /Type /Viewport /BBox [100 37 20 5] /Measure 9 0 R >>]",
            6: "<< /Type /Viewport /BBox 7 0 R /Measure 8 0 R /Custom (retained) >>",
            7: "[20 5 100 37]",
            8: "<< /Type /Measure /R (1:1) /X [<< /U (pt) /C 1 >>] /D [<< /U (pt) /C 1 >>] "
            "/A [<< /U (sq pt) /C 1 >>] /O [24 10] >>",
            9: "<< /Type /Measure /Subtype /GEO /GCS << /Type /GEOGCS /EPSG 4326 >> "
            "/GPTS [35.125 139.625 36.25 140.75] /LPTS [0 0 1 1] >>",
        }
    )
    doc = pyropdf.open(data)
    doc.pages[0].remove_rotation()
    reopened = pyropdf.open(doc.to_bytes(incremental=incremental))
    rotated, original = [p.obj["VP"] for p in reopened.pages]
    assert list(rotated[0]["BBox"]) == [5, 180, 37, 100]
    assert list(rotated[0]["Measure"]["O"]) == [10, 176]
    assert list(rotated[1]["BBox"]) == [37, 100, 5, 180]
    assert list(original[0]["BBox"]) == [20, 5, 100, 37]
    assert list(original[0]["Measure"]["O"]) == [24, 10]
    assert list(original[1]["BBox"]) == [100, 37, 20, 5]
    for entries in (rotated, original):
        assert bytes(entries[0]["Custom"]) == b"retained"
        assert list(entries[1]["Measure"]["GPTS"]) == [35.125, 139.625, 36.25, 140.75]
        assert list(entries[1]["Measure"]["LPTS"]) == [0, 0, 1, 1]
