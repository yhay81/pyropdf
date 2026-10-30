"""Removing a page's rotation while keeping what it displays (issue 429)."""

from __future__ import annotations

import pytest

import pyropdf
from pyropdf import AnnotationType, Rect

# Content that no turn of the page maps onto itself.
CONTENT = (
    "1 0 0 rg 30 20 50 40 re f "
    "0 0 1 rg 200 120 70 60 re f "
    "0 0.6 0 rg 120 100 m 160 100 l 120 170 l f "
    "0.5 0.5 0 rg 250 15 20 10 re f"
)
# An appearance whose top differs from its bottom and its left from its right.
APPEARANCE = "1 0 1 rg 0 0 40 8 re f 0 1 1 rg 0 8 10 12 re f"
WIDGET_APPEARANCE = "0 0.5 1 rg 0 0 80 10 re f 1 0.5 0 rg 60 10 20 20 re f"


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


def _stream(data: str, extra: str = "") -> str:
    return f"<< /Length {len(data)} {extra}>>\nstream\n{data}\nendstream"


def build(rotation: int, *, inherit: bool = False) -> bytes:
    """Return a page with a nonzero crop box, content, a square, a link, an ink line, a note, and a widget."""
    rotate = "" if inherit else f"/Rotate {rotation}"
    parent_rotate = f"/Rotate {rotation}" if inherit else ""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [8 0 R] >> >>",
            2: f"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 200] {parent_rotate} >>",
            3: f"<< /Type /Page /Parent 2 0 R /CropBox [20 10 280 190] {rotate} /TrimBox [25 15 275 185] "
            "/Contents 4 0 R /Annots [5 0 R 7 0 R 8 0 R 10 0 R "
            "<< /Type /Annot /Subtype /Text /F 16 /Rect [60 150 80 170] >>] >>",
            4: _stream(CONTENT),
            5: "<< /Type /Annot /Subtype /Square /Rect [100 50 140 70] /RD [1 2 3 4] /AP << /N 6 0 R >> >>",
            6: _stream(APPEARANCE, "/Type /XObject /Subtype /Form /BBox [0 0 40 20] "),
            7: "<< /Type /Annot /Subtype /Link /Rect [150 30 190 50] /QuadPoints [150 50 190 50 150 30 190 30] "
            "/Border [0 0 0] >>",
            8: "<< /Type /Annot /Subtype /Widget /FT /Tx /T (name) /Rect [40 120 120 150] /MK << /R 90 >> "
            "/AP << /N 9 0 R >> >>",
            9: _stream(WIDGET_APPEARANCE, "/Type /XObject /Subtype /Form /BBox [0 0 80 30] "),
            10: "<< /Type /Annot /Subtype /Line /Rect [200 40 260 90] /L [205 45 255 85] >>",
        }
    )


def _render(page: pyropdf.Page) -> bytes:
    return bytes(page.pixmap(background=(255, 255, 255)))


def _layout(page: pyropdf.Page) -> tuple:
    """Return the display geometry that removing the rotation keeps; an upright note moves on purpose."""
    turning = [annotation for annotation in page.annotations if annotation.type != AnnotationType.TEXT]
    return (
        page.rect,
        page.media_box,
        page.crop_box,
        page.trim_box,
        [annotation.rect for annotation in turning],
        [annotation.vertices for annotation in turning],
        [link.rect for link in page.links],
        [widget.rect for widget in page.widgets],
    )


def _note(page: pyropdf.Page) -> pyropdf.Annotation:
    return next(annotation for annotation in page.annotations if annotation.type == AnnotationType.TEXT)


@pytest.mark.parametrize("inherit", [False, True])
@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_removal_keeps_the_display(rotation: int, *, inherit: bool) -> None:
    doc = pyropdf.open(build(rotation, inherit=inherit))
    page = doc.pages[0]
    before, layout = _render(page), _layout(page)
    assert page.rotation == rotation

    page.remove_rotation()

    assert page.rotation == 0
    assert _render(page) == before
    assert _layout(page) == layout
    reopened = pyropdf.open(doc.to_bytes()).pages[0]
    assert reopened.rotation == 0
    assert _render(reopened) == before
    assert _layout(reopened) == layout
    assert reopened.widgets[0].rotation == (90 - rotation) % 360


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_an_upright_note_stays_at_its_corner(rotation: int) -> None:
    page = pyropdf.open(build(rotation)).pages[0]
    shown = _note(page).rect
    # The corner that /Rect's upper-left corner is displayed at.
    corner = {90: (shown.x1, shown.y0), 180: (shown.x1, shown.y1), 270: (shown.x0, shown.y1)}[rotation]

    page.remove_rotation()

    assert _note(page).rect == Rect(corner[0], corner[1], corner[0] + 20, corner[1] + 20)


def test_undo_restores_the_rotation() -> None:
    data = build(90)
    doc = pyropdf.open(data)
    page = doc.pages[0]
    steps = len(doc.history.steps)

    page.remove_rotation()
    assert len(doc.history.steps) == steps + 1
    doc.history.undo()

    assert page.rotation == 90
    assert doc.to_bytes() == pyropdf.open(data).to_bytes()


def test_a_page_without_rotation_is_left_as_it_is() -> None:
    doc = pyropdf.open(build(0))
    before = doc.to_bytes()

    doc.pages[0].remove_rotation()

    assert doc.to_bytes() == before


def _two_pages(second_annots: str) -> bytes:
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R 7 0 R] /Count 2 /MediaBox [0 0 300 200] >>",
            3: "<< /Type /Page /Parent 2 0 R /Rotate 90 /Contents 4 0 R /Annots [5 0 R] >>",
            4: _stream(CONTENT),
            5: "<< /Type /Annot /Subtype /Square /Rect [100 50 140 70] /AP << /N 6 0 R >> >>",
            6: _stream(APPEARANCE, "/Type /XObject /Subtype /Form /BBox [0 0 40 20] "),
            7: f"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Annots [{second_annots}] >>",
            8: "<< /Type /Annot /Subtype /Square /Rect [10 10 50 30] /AP << /N 6 0 R >> >>",
        }
    )


def test_shared_content_and_appearances_keep_their_meaning() -> None:
    doc = pyropdf.open(_two_pages("8 0 R"))
    first, second = doc.pages
    before = (_render(first), _render(second))

    first.remove_rotation()

    assert (_render(first), _render(second)) == before
    reopened = pyropdf.open(doc.to_bytes())
    assert tuple(_render(page) for page in reopened.pages) == before


def test_an_annotation_that_another_page_lists_is_refused() -> None:
    doc = pyropdf.open(_two_pages("5 0 R"))
    before, steps = doc.to_bytes(), doc.history.steps

    with pytest.raises(ValueError, match="another page"):
        doc.pages[0].remove_rotation()

    assert doc.to_bytes() == before
    assert doc.history.steps == steps
    assert doc.pages[0].rotation == 90


def test_a_stale_page_raises() -> None:
    doc = pyropdf.open(build(90))
    page = doc.pages[0]
    del doc.pages[0]

    with pytest.raises(pyropdf.StalePageError):
        page.remove_rotation()
