"""Stamp and Caret annotations.

pylopdf's tests/test_annotation_stamps.py at ebb01d3c. Intended
differences: pyropdf's ``Caret`` takes its display rectangle rather than a
point, so the carets here are 20 points square at the point pylopdf gave;
the core's checks are called as ``_core.annotation_add`` with the page's
object; ``Page.text`` does not draw annotations stored without an
appearance, so the labels that rendering draws for them are read with
``Annotation.text``, which does.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import pyropdf
from pyropdf import Caret, Color, Rect, Stamp, StampIcon, _core

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


@pytest.mark.parametrize("icon", list(StampIcon))
def test_every_stamp_draws_its_label(icon: StampIcon) -> None:
    page = pyropdf.Document().pages.new((300, 100))

    stamp = page.annotations.add(Stamp((10, 10, 290, 70), icon=icon))

    assert stamp.type is pyropdf.AnnotationType.STAMP
    assert stamp.icon == "".join(part.capitalize() for part in icon.value.split("_"))
    assert stamp.rect == Rect(10, 10, 290, 70)
    label = icon.value.replace("_", " ").upper()
    assert page.text().strip() == label


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_stamps_read_upright_and_turn_as_displayed(rotation: int) -> None:
    page = pyropdf.Document().pages.new((300, 300))
    page.rotation = rotation

    upright = page.annotations.add(Stamp((10, 10, 200, 60), icon=StampIcon.FINAL))
    turned = page.annotations.add(Stamp((240, 20, 290, 280), icon=StampIcon.DRAFT, rotate=90))
    caret = page.annotations.add(Caret((10, 250, 30, 270)))

    assert (upright.rotation, turned.rotation, caret.rotation) == (0, 90, 0)
    lines = {line.text: tuple(line.direction) for block in page.layout().blocks for line in block.lines}
    assert lines["FINAL"] == pytest.approx((1, 0), abs=1e-6)
    assert lines["DRAFT"] == pytest.approx((0, 1), abs=1e-6)
    pixmap = page.pixmap(dpi=72)
    # The caret points up: its base and apex are filled, its top corners not.
    assert _pixel(pixmap, 20, 266) == (0, 0, 255)
    red, _, blue = _pixel(pixmap, 20, 254)
    assert blue == 255
    assert red < 128
    assert _pixel(pixmap, 13, 253) == WHITE
    assert _pixel(pixmap, 27, 253) == WHITE


def test_stamps_and_carets_survive_save_and_reopen() -> None:
    page = pyropdf.Document().pages.new((300, 200))
    page.annotations.add(Stamp((10, 10, 200, 60), icon=StampIcon.TOP_SECRET, color=Color(0, 0, 0), opacity=0.5))
    page.annotations.add(Caret((250, 20, 270, 40), color=Color(1, 0, 0), contents="insert here"))

    stamp, caret = pyropdf.open(page.document.to_bytes()).pages[0].annotations

    assert stamp.icon == "TopSecret"
    assert stamp.color == Color(0, 0, 0)
    assert stamp.opacity == pytest.approx(0.5)
    assert caret.type is pyropdf.AnnotationType.CARET
    assert caret.contents == "insert here"
    assert caret.rect == Rect(250, 20, 270, 40)
    assert _pixel(pyropdf.open(page.document.to_bytes()).pages[0].pixmap(dpi=72), 260, 38) == (255, 0, 0)


def test_stamp_and_caret_specifications_validate() -> None:
    assert Stamp((0, 0, 10, 10)).icon is StampIcon.APPROVED
    assert Stamp((0, 0, 10, 10), icon=cast("Any", "sold")).icon is StampIcon.SOLD
    assert Stamp((0, 0, 10, 10), rotate=-90).rotate == 270
    with pytest.raises(ValueError, match="'stamped'"):
        Stamp((0, 0, 10, 10), icon=cast("Any", "stamped"))
    with pytest.raises(ValueError, match="multiple of 90"):
        Stamp((0, 0, 10, 10), rotate=30)
    with pytest.raises(TypeError, match="color must be a Color"):
        Caret((0, 0, 20, 20), color=cast("Any", "red"))
    with pytest.raises(pyropdf.LimitError):
        Caret((0, 0, 20, 20), contents="x" * (1024 * 1024))
    doc = pyropdf.Document()
    page = doc.pages.new((100, 100))
    location = (doc._core(), page._number, page._generation)  # noqa: SLF001
    with pytest.raises(pyropdf.PdfError, match="unknown stamp"):
        _core.annotation_add(*location, {"subtype": "Stamp", "rect": (0.0, 0.0, 9.0, 9.0), "icon": "Rubber"})
    with pytest.raises(pyropdf.PdfError, match="does not turn"):
        _core.annotation_add(*location, {"subtype": "Square", "rect": (0.0, 0.0, 9.0, 9.0), "rotate": 90})


def test_rendering_draws_missing_stamp_appearances() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Annots [4 0 R 5 0 R] >>",
                4: "<< /Type /Annot /Subtype /Stamp /Rect [10 10 200 90] /Name /Expired >>",
                5: "<< /Type /Annot /Subtype /Stamp /Rect [210 10 290 90] /Name /#53pecial >>",
            }
        )
    )

    assert [annotation.text().strip() for annotation in doc.pages[0].annotations] == ["EXPIRED", "SPECIAL"]
