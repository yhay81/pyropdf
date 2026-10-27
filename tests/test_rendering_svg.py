"""page.svg: the current version of a page as SVG through hayro-svg.

Ported from the ``svg`` assertions of pylopdf's tests/test_rendering_api.py
at 036be111. pyropdf has no ``page.annotations.add`` yet, so the annotated
page is built with a Highlight whose appearance paints its rectangle red.
That the bridge converts every corpus page as hayro-svg converts the
original bytes is crates/pyropdf-engine/src/render/svg.rs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_content_model import page_with, stream_object

import pyropdf

REAL_WORLD = Path(__file__).parent / "assets" / "real_world"
CORPUS = sorted(REAL_WORLD.glob("*.pdf"))


def _page() -> pyropdf.Page:
    """Open a 200 by 100 page that shows one line of text."""
    return page_with("BT /F1 24 Tf 10 40 Td (Rendered) Tj ET")


def _annotated_page() -> pyropdf.Page:
    """Open a blank 200 by 100 page with a Highlight that paints its rectangle red."""
    return page_with(
        "",
        annots="6 0 R",
        objects={
            6: "<< /Type /Annot /Subtype /Highlight /Rect [20 20 180 80] /AP << /N 7 0 R >> >>",
            7: stream_object("1 0 0 rg 0 0 160 60 re f", "/Type /XObject /Subtype /Form /BBox [0 0 160 60]"),
        },
    )


def test_svg_draws_text_as_paths() -> None:
    svg = _page().svg()

    assert svg.startswith("<svg")
    assert 'viewBox="0 0 200 100"' in svg
    assert "<text" not in svg
    assert "<path" in svg


def test_annotations_false_leaves_annotation_appearances_out() -> None:
    page = _annotated_page()

    assert page.svg(annotations=False) != page.svg()
    assert page.svg(annotations=False) == page_with("").svg()


@pytest.mark.parametrize("value", [1, None, "yes"])
def test_annotations_must_be_a_bool(value: object) -> None:
    with pytest.raises(TypeError, match="annotations must be a bool"):
        _page().svg(annotations=value)  # ty: ignore[invalid-argument-type]


def test_max_size_bounds_the_utf8_output() -> None:
    page = _page()
    size = len(page.svg().encode())

    assert page.svg(max_size=size) == page.svg(max_size=None)
    with pytest.raises(pyropdf.LimitError, match=f"exceeds the {size - 1}-byte UTF-8 output limit") as raised:
        page.svg(max_size=size - 1)
    assert raised.value.code == "svg_output_size"


@pytest.mark.parametrize(("value", "error"), [(0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError)])
def test_max_size_is_a_positive_integer_or_none(value: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="max_size"):
        _page().svg(max_size=value)  # ty: ignore[invalid-argument-type]


def test_svg_converts_the_current_version() -> None:
    page = _page()
    before = page.svg()

    page.rotation = 90

    after = page.svg()
    assert after != before
    assert 'viewBox="0 0 100 200"' in after


def test_old_names_are_removed() -> None:
    for name in ("render_svg", "get_svg_image"):
        assert not hasattr(pyropdf.Page, name)
    assert not hasattr(pyropdf.Document, "render_page_svg")
    with pytest.raises(TypeError):
        _page().svg(None)  # ty: ignore[too-many-positional-arguments]


def test_a_deleted_page_or_a_closed_document_refuses_to_convert() -> None:
    doc = _page().document
    doc.pages.new()
    page = doc.pages[0]
    del doc.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.svg()
    page = doc.pages[0]
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        page.svg()


@pytest.mark.parametrize("path", CORPUS, ids=[path.name for path in CORPUS])
def test_every_corpus_page_converts_within_the_default_limit(path: Path) -> None:
    doc = pyropdf.open(path.read_bytes())

    for page in doc.pages:
        svg = page.svg()
        assert svg.startswith("<svg")
        assert svg.rstrip().endswith("</svg>")
