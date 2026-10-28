"""Cloudy borders (/BE) of squares, circles, and polygons.

pylopdf's tests/test_annotation_clouds.py at ebb01d3c. Intended
differences: the core's checks are called as ``_core.annotation_add`` with
the page's object, and the render-time clouds of squares that a file stores
without an appearance are #169's: this file reads their intensities only.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import pyropdf
from pyropdf import Border, Circle, Color, LineAnnotation, Polygon, Rect, Square, _core

WHITE = (255, 255, 255)


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _pixel(pixmap: pyropdf.Pixmap, x: float, y: float) -> tuple[int, int, int]:
    offset = int(y) * pixmap.stride + int(x) * pixmap.channels
    red, green, blue = bytes(pixmap)[offset : offset + 3]
    return red, green, blue


def _ink_columns(pixmap: pyropdf.Pixmap, y: int) -> list[int]:
    return [x for x in range(pixmap.width) if _pixel(pixmap, x, y) != WHITE]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_cloudy_square_scallops_its_edges_inside_its_rectangle(rotation: int) -> None:
    page = pyropdf.Document().pages.new((300, 300))
    page.rotation = rotation
    plain = page.annotations.add(Square((20, 20, 140, 100)))
    cloudy = page.annotations.add(Square((160, 20, 280, 100), clouds=2, interior_color=Color(1, 1, 0)))

    assert plain.clouds == 0
    assert cloudy.clouds == 2
    assert cloudy.obj["BE"]["S"] == "C"
    assert cloudy.rect == Rect(160, 20, 280, 100)
    pixmap = page.pixmap(dpi=72)
    # The plain top edge is one straight line; the cloud's top row is broken
    # into the tops of its bumps, and its fill reaches past the bump bases.
    plain_top = [x for x in _ink_columns(pixmap, 20) if x < 150]
    assert plain_top == list(range(20, 140))
    tops = [x for x in _ink_columns(pixmap, 21) if x > 150]
    assert tops
    assert len(tops) < 100
    assert min(tops) >= 160
    assert max(tops) < 280
    assert _pixel(pixmap, 220, 60) == (255, 255, 0)
    assert {_pixel(pixmap, x, y) for x in (158, 282) for y in range(20, 100)} == {WHITE}


def test_cloudy_circles_and_polygons_draw_bumps() -> None:
    page = pyropdf.Document().pages.new((300, 200))
    circle = page.annotations.add(Circle((20, 20, 140, 120), clouds=1))
    polygon = page.annotations.add(Polygon([(160, 30), (280, 30), (220, 150)], clouds=1.5))

    assert circle.clouds == 1
    assert polygon.clouds == 1.5
    # A polygon's rectangle grows by the bumps beyond its vertices.
    radius = 4 * 1.5 + 1
    assert tuple(polygon.rect) == pytest.approx(
        (160 - 1.5 - radius, 30 - 1.5 - radius, 280 + 1.5 + radius, 150 + 1.5 + radius)
    )
    pixmap = page.pixmap(dpi=72)
    # Bumps reach above the polygon's top edge.
    assert any(_pixel(pixmap, x, 25) != WHITE for x in range(170, 270))
    assert {_pixel(pixmap, x, 70) for x in range(70, 90)} == {WHITE}


def test_clouds_survive_save_and_edits() -> None:
    page = pyropdf.Document().pages.new((300, 200))
    square = page.annotations.add(Square((20, 20, 140, 100), clouds=1))
    polygon = page.annotations.add(Polygon([(160, 30), (280, 30), (220, 150)]))
    before = tuple(polygon.rect)

    polygon.clouds = 2
    assert polygon.clouds == 2
    assert polygon.rect.x0 < before[0]
    square.clouds = 0.5
    reopened = pyropdf.open(page.document.to_bytes()).pages[0]
    assert [annotation.clouds for annotation in reopened.annotations] == [0.5, 2]

    del polygon.clouds
    assert polygon.clouds == 0
    assert "BE" not in polygon.obj
    assert tuple(polygon.rect) == pytest.approx(before)
    square.clouds = 0
    assert "BE" not in square.obj


def test_clouds_are_validated() -> None:
    page = pyropdf.Document().pages.new((300, 200))
    for value, error in [
        (-1, ValueError),
        (2.5, ValueError),
        (cast("Any", "1"), TypeError),
        (cast("Any", True), TypeError),  # noqa: FBT003 - a bool is no intensity.
    ]:
        with pytest.raises(error, match="clouds"):
            Square((0, 0, 10, 10), clouds=value)
    line = page.annotations.add(LineAnnotation((0, 0), (10, 10)))
    with pytest.raises(pyropdf.PdfError, match="cloudy border"):
        line.clouds = 1
    location = (page.document._core(), page._number, page._generation)  # noqa: SLF001
    with pytest.raises(pyropdf.PdfError, match="cloudy border"):
        _core.annotation_add(*location, {"subtype": "Ink", "points": [[(0.0, 0.0), (1.0, 1.0)]], "clouds": 1.0})
    with pytest.raises(pyropdf.PdfError, match="intensity"):
        _core.annotation_add(*location, {"subtype": "Square", "rect": (0.0, 0.0, 9.0, 9.0), "clouds": 3.0})
    assert Square((0, 0, 1, 1), clouds=1, border=Border(2)).clouds == 1.0


def test_other_producers_clouds_are_read() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [4 0 R 5 0 R 6 0 R] >>",
                4: "<< /Type /Annot /Subtype /Square /Rect [10 10 90 90] /C [1 0 0] /BE << /S /C /I 9 >> >>",
                5: "<< /Type /Annot /Subtype /Square /Rect [110 10 190 90] /C [1 0 0] /BE << /S /S /I 1 >> >>",
                6: "<< /Type /Annot /Subtype /Circle /Rect [0 0 1 1] /BE << /S /C /I (x) >> >>",
            }
        )
    )

    assert [annotation.clouds for annotation in doc.pages[0].annotations] == [2.0, 0.0, 0.0]


def test_overflowing_cloud_geometry_refuses_atomically() -> None:
    page = pyropdf.Document().pages.new((100, 100))
    before = page.document.to_bytes()
    with pytest.raises(pyropdf.MalformedPdfError, match="nonfinite"):
        page.annotations.add(pyropdf.Circle((0, 0, 1e308, 1e308), clouds=2))
    assert page.document.to_bytes() == before
