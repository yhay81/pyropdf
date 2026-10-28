"""doc.flatten and page.flatten: annotation and widget appearances drawn into page content.

Ported from pylopdf's ``tests/test_flatten.py`` at
ebb01d3c4c742046e8ce7fd72957cd15a2933321. Intended differences:

- the fixture leaves out the free text and stamp annotations, which wait
  for #168.
- a flag that is no bool raises "must be a bool", as pyropdf's other flags do.
- tests cover ``page.flatten``, which pylopdf lacks, and that a flattening
  is one undo step.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from test_annotation_appearances import build_raw_pdf

import pyropdf
from pyropdf import (
    AnnotationFlags,
    CheckBox,
    Color,
    ComboBox,
    Highlight,
    Ink,
    LineAnnotation,
    Note,
    Square,
    TextField,
)


def _differing(before: pyropdf.Pixmap, after: pyropdf.Pixmap, tolerance: int = 8) -> int:
    """Count the samples that differ by more than ``tolerance``."""
    assert (before.width, before.height) == (after.width, after.height)
    return sum(abs(a - b) > tolerance for a, b in zip(bytes(before), bytes(after), strict=True))


def _annotated() -> pyropdf.Document:
    doc = pyropdf.Document()
    page = doc.pages.new((400, 400))
    page.annotations.add(Square((20, 20, 120, 80), interior_color=Color(0, 0, 1)))
    page.annotations.add(LineAnnotation((20, 100), (180, 140), color=Color(0, 0.5, 0)))
    page.annotations.add(Ink([[(200, 20), (260, 60), (320, 20)]]))
    page.annotations.add(Highlight((20, 160, 180, 180)))
    page.annotations.add(Note((20, 200), contents="note")).popup_rect = (40, 200, 140, 260)
    page.links.add((300, 300, 380, 320), "https://example.test")
    page.widgets.add(TextField("name", (20, 300, 180, 324), value="Ada Lovelace"))
    page.widgets.add(CheckBox("agree", (200, 300, 216, 316), checked=True))
    page.widgets.add(ComboBox("level", (20, 340, 180, 364), choices=["low", "high"], value="high"))
    return doc


def test_flattening_keeps_what_rendering_shows() -> None:
    doc = _annotated()
    page = doc.pages[0]
    before = page.pixmap(dpi=72)

    doc.flatten()

    assert [annotation.type for annotation in page.annotations] == []
    assert len(page.links) == 1
    assert [item["Subtype"] for item in page.obj["Annots"]] == ["Link"]
    assert len(page.widgets) == 0
    assert len(doc.form) == 0
    assert "AcroForm" not in doc.catalog
    assert _differing(before, page.pixmap(dpi=72)) < 40
    assert "Ada Lovelace" in page.text()

    reopened = pyropdf.open(doc.to_bytes())
    assert _differing(before, reopened.pages[0].pixmap(dpi=72)) < 40
    assert "Ada Lovelace" in reopened.pages[0].text()


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_flattening_follows_page_rotation(rotation: int) -> None:
    doc = _annotated()
    page = doc.pages[0]
    page.rotation = rotation
    before = page.pixmap(dpi=72)

    doc.flatten()

    assert _differing(before, page.pixmap(dpi=72)) < 40


def test_annotations_and_widgets_flatten_apart() -> None:
    doc = _annotated()
    doc.flatten(annotations=False)
    page = doc.pages[0]
    assert len(page.annotations) == 5
    assert len(page.widgets) == 0
    assert "AcroForm" not in doc.catalog

    doc = _annotated()
    doc.flatten(widgets=False)
    page = doc.pages[0]
    assert len(page.annotations) == 0
    assert len(page.widgets) == 3
    assert doc.form["name"].value == "Ada Lovelace"
    assert [item["Subtype"] for item in page.obj["Annots"]] == ["Link", "Widget", "Widget", "Widget"]

    doc = _annotated()
    before = doc.to_bytes()
    doc.flatten(annotations=False, widgets=False)
    assert doc.to_bytes() == before


def test_hidden_annotations_are_removed_undrawn() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    square = page.annotations.add(Square((20, 20, 120, 120), interior_color=Color(1, 0, 0)))
    square.flags = AnnotationFlags.HIDDEN

    doc.flatten()

    assert "Annots" not in page.obj
    assert set(bytes(page.pixmap(dpi=72))) == {255}


def test_missing_appearances_are_drawn_as_rendering_draws_them() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [4 0 R 5 0 R] >>",
                4: "<< /Type /Annot /Subtype /Square /Rect [10 10 90 90] /C [1 0 0] /IC [0 0 1] >>",
                5: "<< /Type /Annot /Subtype /Square /Rect [110 10 190 90] /C [0 1 0] >>",
            }
        )
    )
    page = doc.pages[0]
    before = page.pixmap(dpi=72)
    blank = pyropdf.Document()
    assert _differing(before, blank.pages.new((200, 100)).pixmap(dpi=72)) > 1000

    doc.flatten()

    assert "Annots" not in page.obj
    assert _differing(before, page.pixmap(dpi=72)) == 0


def test_appearance_boxes_and_matrices_map_onto_the_rectangle() -> None:
    # The appearance's BBox is off the origin and turned by its Matrix; the
    # flattened drawing lands on /Rect as ISO 32000-2 §12.5.5 maps it, which
    # MuPDF and pdf.js follow.
    stream = b"0 0 1 rg 10 20 40 10 re f 1 0 0 rg 10 20 10 10 re f"
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [4 0 R] >>",
                4: "<< /Type /Annot /Subtype /Square /Rect [50 50 70 130] /AP << /N 5 0 R >> >>",
                5: (
                    f"<< /Type /XObject /Subtype /Form /BBox [10 20 50 30] /Matrix [0 1 -1 0 0 0] "
                    f"/Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream"
                ),
            }
        )
    )
    page = doc.pages[0]

    doc.flatten()

    pixmap = page.pixmap(dpi=72)
    samples = bytes(pixmap)

    def pixel(x: int, y: int) -> bytes:
        offset = y * pixmap.stride + x * pixmap.channels
        return samples[offset : offset + 3]

    # The turned box fills /Rect, 20 wide and 80 tall, red at its bottom.
    assert pixel(60, 80) == b"\x00\x00\xff"
    assert pixel(60, 140) == b"\xff\x00\x00"
    assert pixel(45, 100) == pixel(75, 100) == pixel(60, 65) == pixel(60, 155) == b"\xff\xff\xff"


def test_flattening_draws_shared_annotations_on_every_page() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots 5 0 R >>",
                4: "<< /Type /Page /Parent 2 0 R /Annots 5 0 R >>",
                5: "[6 0 R]",
                6: "<< /Type /Annot /Subtype /Square /Rect [10 10 90 90] /C [1 0 0] /IC [1 0 0] >>",
            }
        )
    )
    before = [page.pixmap(dpi=72) for page in doc.pages]

    doc.flatten()

    for page, pixmap in zip(doc.pages, before, strict=True):
        assert "Annots" not in page.obj
        assert _differing(pixmap, page.pixmap(dpi=72)) == 0


def test_drawing_stops_where_rendering_stops() -> None:
    # hayro stops reading /Annots at an entry that is no dictionary, so the
    # square after it is not drawn before flattening, and not after it.
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [5 0 R 4 0 R] >>",
                4: "<< /Type /Annot /Subtype /Square /Rect [10 10 90 90] /C [1 0 0] /IC [1 0 0] >>",
                5: "42",
            }
        )
    )
    page = doc.pages[0]
    before = page.pixmap(dpi=72)

    doc.flatten()

    assert _differing(before, page.pixmap(dpi=72)) == 0
    assert list(page.obj["Annots"]) == [42]


def test_flatten_takes_booleans() -> None:
    doc = pyropdf.Document()
    with pytest.raises(TypeError, match="annotations must be a bool"):
        doc.flatten(annotations=cast("Any", 1))
    with pytest.raises(TypeError, match="widgets must be a bool"):
        doc.pages.new().flatten(widgets=cast("Any", 1))
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        doc.flatten()


def test_a_page_flattens_alone_and_its_widgets_leave_their_fields() -> None:
    doc = _annotated()
    second = doc.pages.new((400, 400))
    second.annotations.add(Square((20, 20, 120, 80), interior_color=Color(1, 0, 0)))
    second.widgets.add(TextField("other", (20, 300, 180, 324), value="Grace"))
    # A second widget of the name field, which stays with it.
    second.widgets.add(TextField("name", (20, 340, 180, 364)))
    first = doc.pages[0]
    before = first.pixmap(dpi=72)

    first.flatten()

    assert _differing(before, first.pixmap(dpi=72)) < 40
    assert [item["Subtype"] for item in first.obj["Annots"]] == ["Link"]
    assert sorted(doc.form) == ["name", "other"]
    assert doc.form["name"].widgets == [second.widgets[1]]
    assert doc.form["name"].value == "Ada Lovelace"
    assert len(second.annotations) == 1
    assert len(second.widgets) == 2
    extra = doc.pages.new()
    doc.history.undo()
    with pytest.raises(pyropdf.StalePageError):
        extra.flatten()


def test_a_flattening_is_one_step() -> None:
    doc = _annotated()
    page = doc.pages[0]

    doc.flatten()
    doc.history.undo()

    assert len(page.annotations) == 5
    assert len(page.widgets) == 3
    assert doc.form["name"].value == "Ada Lovelace"


def test_flattening_a_page_takes_a_widget_with_a_generation_out_of_its_field() -> None:
    from test_field_creation import _generation_pdf  # noqa: PLC0415 - shared fixture.

    doc = pyropdf.open(_generation_pdf())

    doc.pages[0].flatten()

    assert len(doc.pages[0].widgets) == 0
    assert "name" not in doc.form


def test_flattenings_in_a_transaction_roll_back_together() -> None:
    doc = _annotated()
    before = doc.to_bytes()

    def flatten_and_fail() -> None:
        with doc.transaction("flatten"):
            doc.pages[0].flatten()
            assert len(doc.pages[0].widgets) == 0
            message = "rollback"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        flatten_and_fail()
    assert doc.to_bytes() == before
    assert doc.form["name"].value == "Ada Lovelace"


def test_flattening_observes_cancellation() -> None:
    token = pyropdf.CancellationToken()
    doc = pyropdf.open(_annotated().to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token))
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.flatten()
    assert caught.value.code == "cancelled"
