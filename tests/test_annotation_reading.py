"""Rendering and reading the appearance of one annotation: ``Annotation.pixmap`` and ``Annotation.text``."""

from __future__ import annotations

import pytest
from test_annotation_appearances import build_raw_pdf

import pyropdf
from pyropdf import AnnotationFlags, AnnotationType, Color, LimitError, Square, StaleObjectError


def _form(bbox: str, content: str, resources: str = "") -> str:
    dictionary = f"<< /Type /XObject /Subtype /Form /BBox [{bbox}] {resources} /Length {len(content)} >>"
    return f"{dictionary}\nstream\n{content}\nendstream"


_FONT = "/Resources << /Font << /F1 3 0 R >> >>"
_PAGE = "1 0 0 rg 0 0 300 300 re f BT /F1 12 Tf 0 0 0 rg 20 270 Td (Page body) Tj ET"


def _document() -> pyropdf.Document:
    """Return a red page that says "Page body", with a FreeText, a Stamp, and a blue Square over the FreeText.

    Then a Square and a FreeText without appearances, and a Stamp stored in ``/Annots`` itself.
    """
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 3 0 R >> >> "
        "/Contents 5 0 R /Annots [6 0 R 8 0 R 10 0 R 12 0 R 13 0 R "
        "<< /Type /Annot /Subtype /Stamp /Rect [200 20 280 60] /AP << /N 14 0 R >> >>] >>",
        5: f"<< /Length {len(_PAGE)} >>\nstream\n{_PAGE}\nendstream",
        6: "<< /Type /Annot /Subtype /FreeText /Rect [50 50 150 100] /Contents (not drawn) /AP << /N 7 0 R >> >>",
        7: _form("0 0 100 50", "0 1 0 rg 0 0 100 50 re f BT /F1 12 Tf 0 0 0 rg 5 20 Td (Hello FreeText) Tj ET", _FONT),
        8: "<< /Type /Annot /Subtype /Stamp /Rect [160 160 260 210] /AP << /N 9 0 R >> >>",
        9: _form("0 0 100 50", "1 1 0 rg 0 0 100 50 re f BT /F1 12 Tf 0 0 0 rg 5 20 Td (APPROVED) Tj ET", _FONT),
        10: "<< /Type /Annot /Subtype /Square /Rect [40 40 160 110] /AP << /N 11 0 R >> >>",
        11: _form("0 0 120 70", "0 0 1 rg 0 0 120 70 re f"),
        12: "<< /Type /Annot /Subtype /Square /Rect [20 200 80 240] /IC [0 0 1] /C [0 0 1] >>",
        13: "<< /Type /Annot /Subtype /FreeText /Rect [100 200 140 240] /Contents (no appearance) >>",
        14: _form("0 0 80 40", "0 1 1 rg 0 0 80 40 re f BT /F1 12 Tf 0 0 0 rg 5 15 Td (DIRECT) Tj ET", _FONT),
    }
    return pyropdf.open(build_raw_pdf(objects))


def _pixels(pixmap: pyropdf.Pixmap) -> set[tuple[int, ...]]:
    data, step = bytes(pixmap), pixmap.channels
    return {tuple(data[at : at + 3]) for at in range(0, len(data), step)}


def _at(pixmap: pyropdf.Pixmap, x: int, y: int) -> tuple[int, ...]:
    offset = y * pixmap.stride + x * pixmap.channels
    return tuple(bytes(pixmap)[offset : offset + 3])


def test_a_free_text_yields_its_text_and_pixmap_alone() -> None:
    doc = _document()
    page = doc.pages[0]
    free_text = page.annotations[0]
    assert free_text.type is AnnotationType.FREE_TEXT

    pixmap = free_text.pixmap()

    assert (pixmap.width, pixmap.height) == (100, 50)
    assert _at(pixmap, 90, 5) == (0, 255, 0)
    # Neither the red page nor the blue Square drawn over it shows.
    assert (255, 0, 0) not in _pixels(pixmap)
    assert (0, 0, 255) not in _pixels(pixmap)
    assert free_text.text().strip() == "Hello FreeText"
    assert "Page body" in page.text()


def test_a_stamp_yields_its_text_and_pixmap_alone() -> None:
    stamp = _document().pages[0].annotations[1]
    assert stamp.type is AnnotationType.STAMP

    pixmap = stamp.pixmap(dpi=144)

    assert (pixmap.width, pixmap.height) == (200, 100)
    assert _at(pixmap, 190, 10) == (255, 255, 0)
    assert stamp.text().strip() == "APPROVED"


def test_a_direct_annotation_is_read_by_its_position() -> None:
    stamp = _document().pages[0].annotations[5]
    assert stamp.xref == 0

    assert _at(stamp.pixmap(), 75, 5) == (0, 255, 255)
    assert stamp.text().strip() == "DIRECT"


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_the_pixmap_follows_page_rotation(rotation: int) -> None:
    doc = _document()
    page = doc.pages[0]
    page.rotation = rotation
    free_text = page.annotations[0]

    pixmap = free_text.pixmap()

    rect = free_text.rect
    assert (pixmap.width, pixmap.height) == (round(rect.width), round(rect.height))
    upright = (pixmap.width, pixmap.height) if rotation == 180 else (pixmap.height, pixmap.width)
    assert upright == (100, 50)
    assert (0, 255, 0) in _pixels(pixmap)
    assert (255, 0, 0) not in _pixels(pixmap)
    assert free_text.text().strip() == "Hello FreeText"


def test_an_absent_appearance_is_synthesized_or_empty() -> None:
    page = _document().pages[0]
    square, free_text = page.annotations[3], page.annotations[4]

    assert (0, 0, 255) in _pixels(square.pixmap())
    assert square.text() == ""
    # A render synthesizes no free text appearance: only the background remains.
    assert _pixels(free_text.pixmap()) == {(255, 255, 255)}
    assert free_text.text() == ""


def test_a_hidden_annotation_renders_as_the_background() -> None:
    stamp = _document().pages[0].annotations[1]
    stamp.flags = AnnotationFlags.HIDDEN

    assert _pixels(stamp.pixmap()) == {(255, 255, 255)}
    assert _pixels(stamp.pixmap(alpha=True)) == {(0, 0, 0)}
    assert stamp.text() == ""


def test_the_options_of_page_pixmap_apply() -> None:
    stamp = _document().pages[0].annotations[1]

    gray = stamp.pixmap(colorspace="gray")
    assert gray.channels == 1
    assert stamp.pixmap(alpha=True).alpha
    assert _at(stamp.pixmap(matrix=(2, 0, 0, 2)), 190, 10) == (255, 255, 0)
    with pytest.raises(ValueError, match="dpi or matrix"):
        stamp.pixmap(dpi=72, matrix=(1, 0, 0, 1))


def test_current_version_edits_are_read() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    square = page.annotations.add(Square((20, 20, 120, 80), color=Color(0, 0, 1), interior_color=Color(0, 0, 1)))
    assert (0, 0, 255) in _pixels(square.pixmap())

    square.interior_color = Color(1, 0, 0)
    assert (255, 0, 0) in _pixels(square.pixmap())

    with doc.transaction():
        square.interior_color = Color(0, 1, 0)
        square.rect = (10, 10, 60, 40)
        pixmap = square.pixmap()
        assert (pixmap.width, pixmap.height) == (50, 30)
        assert (0, 255, 0) in _pixels(pixmap)
    assert (0, 255, 0) in _pixels(square.pixmap())


def test_reading_an_annotation_changes_nothing() -> None:
    doc = _document()
    page = doc.pages[0]
    before = doc.to_bytes()
    steps = doc.history.steps

    for annotation in page.annotations:
        annotation.pixmap()
        annotation.text()
    with doc.transaction():
        page.annotations[1].pixmap()
        page.annotations[1].text()

    assert doc.to_bytes() == before
    assert doc.history.steps == steps
    assert len(page.annotations) == 6
    assert "Page body" in page.text()


def test_limits_and_stale_handles_raise() -> None:
    doc = _document()
    page = doc.pages[0]
    stamp = page.annotations[1]

    with pytest.raises(ValueError, match="exceeds"):
        stamp.pixmap(dpi=72 * 1000)
    with pytest.raises(LimitError) as raised:
        stamp.text(max_glyphs=2)
    assert raised.value.code == "text_glyph_count"

    del page.annotations[1]
    with pytest.raises(StaleObjectError):
        stamp.pixmap()
    with pytest.raises(StaleObjectError):
        stamp.text()
