"""Caret annotations: creation, settled appearances, edits, and round trips (issue 421)."""

from __future__ import annotations

import io

import pikepdf
import pytest

import pyropdf
from pyropdf import AnnotationType, Caret, Color

RECT = (50.0, 20.0, 70.0, 40.0)


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


def _page(rotation: int = 0) -> pyropdf.Page:
    return pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Rotate {rotation} >>",
            }
        )
    ).pages[0]


def _colored(page: pyropdf.Page, x: float, y: float, channel: int) -> bool:
    """Return whether the display pixel at ``(x, y)`` is mostly ``channel``."""
    pixel = page.pixmap(background=(255, 255, 255)).pixel(int(x), int(y))
    return pixel[channel] > 200 and all(pixel[other] < 120 for other in range(3) if other != channel)


def _white(page: pyropdf.Page, x: float, y: float) -> bool:
    return all(value > 240 for value in page.pixmap(background=(255, 255, 255)).pixel(int(x), int(y)))


def _points_up(page: pyropdf.Page, rect: tuple[float, float, float, float], channel: int = 2) -> bool:
    """Return whether a caret in display ``rect`` points up: wide at the bottom, narrow at the top."""
    x0, y0, x1, y1 = rect
    width, height = x1 - x0, y1 - y0
    return (
        _colored(page, x0 + width / 2, y0 + height / 2, channel)
        and _colored(page, x0 + width * 0.15, y1 - height * 0.05, channel)
        and _colored(page, x1 - width * 0.15, y1 - height * 0.05, channel)
        and _white(page, x0 + 1, y0 + 1)
        and _white(page, x1 - 1, y0 + 1)
    )


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_caret_points_up_on_the_display(rotation: int) -> None:
    page = _page(rotation)

    caret = page.annotations.add(Caret(RECT, contents="insert here"))

    assert caret.type is AnnotationType.CARET
    assert caret.rect == RECT
    assert (caret.color, caret.contents, caret.rotation) == (Color(0, 0, 1), "insert here", 0)
    assert _points_up(page, RECT)
    data = page.document.to_bytes()
    reopened = pyropdf.open(data).pages[0]
    (again,) = reopened.annotations
    assert (again.type, again.rect, again.rotation) == (AnnotationType.CARET, RECT, 0)
    assert _points_up(reopened, RECT)
    # pikepdf reads what was saved through its own parser.
    with pikepdf.open(io.BytesIO(data)) as pdf:
        (annotation,) = pdf.pages[0].Annots
        assert annotation.Subtype == "/Caret"
        assert isinstance(annotation.AP.N, pikepdf.Stream)
        assert annotation.AP.N.read_bytes().endswith(b"\nf\n")
        assert int(annotation.get("/Rotate", 0)) == rotation


def test_edits_in_one_transaction_settle_once_and_undo_restores() -> None:
    page = _page(90)
    caret = page.annotations.add(Caret(RECT))
    doc = page.document
    objects = doc.objects
    count, steps = len(objects), len(doc.history.steps)
    before = bytes(page.pixmap())
    moved = (20.0, 120.0, 50.0, 140.0)

    with doc.transaction("restyle"):
        caret.color = Color(1, 0, 0)
        caret.rect = moved
        caret.opacity = 0.9
        caret.rotation = 0

    assert len(objects) == count + 1
    assert len(doc.history.steps) == steps + 1
    assert caret.rect == moved
    assert _points_up(page, moved, channel=0)
    assert _white(page, *RECT[:2])
    doc.history.undo()
    assert caret.rect == RECT
    assert bytes(page.pixmap()) == before


def test_turning_a_caret_turns_its_point() -> None:
    page = _page()
    caret = page.annotations.add(Caret((40, 20, 80, 60)))

    caret.rotation = 90

    # Turned clockwise on the display, the caret points right.
    assert _colored(page, 76, 40, 2)
    assert _colored(page, 42, 26, 2)
    assert _colored(page, 42, 54, 2)
    assert _white(page, 78, 22)
    assert _white(page, 78, 58)
    reopened = pyropdf.open(page.document.to_bytes()).pages[0]
    assert reopened.annotations[0].rotation == 90
    assert bytes(reopened.pixmap()) == bytes(page.pixmap())


def test_a_caret_without_an_appearance_is_drawn() -> None:
    page = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [4 0 R] >>",
                4: "<< /Type /Annot /Subtype /Caret /Rect [50 60 70 80] /C [0 0 1] >>",
            }
        )
    ).pages[0]

    assert _points_up(page, RECT)


def test_caret_specifications_are_validated() -> None:
    with pytest.raises(ValueError, match="opacity"):
        Caret(RECT, opacity=0)
    with pytest.raises(TypeError):
        Caret(RECT, color=(0, 0, 1))  # ty: ignore[invalid-argument-type]
    with pytest.raises(pyropdf.PdfError, match="finite"):
        Caret((0, 0, float("inf"), 10))
