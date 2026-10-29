"""Page boxes: reading them inherited and clipped, and setting them on one page."""

from __future__ import annotations

import pytest
from test_pages import build_raw_pdf

import pyropdf
from pyropdf import Rect

CONTENT = "BT /F1 12 Tf 72 700 Td (Hello) Tj ET"


def boxes_pdf(page: str = "", tree: str = "/MediaBox [0 0 612 792]") -> bytes:
    """Return one page showing ``Hello`` at (72, 700), with ``tree`` on its parent and ``page`` on the page."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: f"<< /Type /Pages /Kids [4 0 R] /Count 1 {tree} >>",
            3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            4: f"<< /Type /Page /Parent 2 0 R {page} /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
            5: f"<< /Length {len(CONTENT)} >>\nstream\n{CONTENT}\nendstream",
        }
    )


def _boxes(page: pyropdf.Page) -> list[Rect]:
    return [page.media_box, page.crop_box, page.bleed_box, page.trim_box, page.art_box]


def test_boxes_default_to_the_media_box_and_then_the_crop_box() -> None:
    page = pyropdf.open(boxes_pdf("/CropBox [10 20 600 780]")).pages[0]
    assert page.media_box == Rect(-10, -12, 602, 780)
    assert page.crop_box == Rect(0, 0, 590, 760)
    assert page.bleed_box == page.trim_box == page.art_box == page.crop_box


def test_boxes_are_inherited_and_clipped_to_the_media_box() -> None:
    page = pyropdf.open(
        boxes_pdf(
            "/TrimBox [-50 -50 300 300] /ArtBox [100 100 200 200]", "/MediaBox [0 0 400 400] /CropBox [0 0 500 500]"
        )
    ).pages[0]
    assert page.media_box == page.crop_box == Rect(0, 0, 400, 400)
    assert page.trim_box == Rect(0, 100, 300, 400)
    assert page.art_box == Rect(100, 200, 200, 300)


def test_a_page_without_a_media_box_has_the_a4_page() -> None:
    page = pyropdf.open(boxes_pdf(tree="")).pages[0]
    assert page.media_box == page.crop_box == Rect(0, 0, 595, 842)


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, Rect(0, 692, 100, 792)),
        (90, Rect(0, 0, 100, 100)),
        (180, Rect(512, 0, 612, 100)),
        (270, Rect(692, 512, 792, 612)),
    ],
)
def test_boxes_are_in_rotated_display_space(rotation: int, expected: Rect) -> None:
    page = pyropdf.open(boxes_pdf(f"/Rotate {rotation} /ArtBox [0 0 100 100]")).pages[0]
    assert page.art_box == expected
    assert page.media_box == page.rect


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("attribute", ["crop_box", "bleed_box", "trim_box", "art_box", "media_box"])
def test_a_set_box_reads_back(rotation: int, attribute: str) -> None:
    document = pyropdf.open(boxes_pdf(f"/Rotate {rotation}"))
    page = document.pages[0]
    target = Rect(10, 20, 300, 400)
    setattr(page, attribute, target)
    if attribute in {"crop_box", "media_box"}:
        # The display space moved to the new visible page.
        assert getattr(page, attribute).width == pytest.approx(target.width)
        assert getattr(page, attribute).height == pytest.approx(target.height)
    else:
        assert getattr(page, attribute) == target
    saved = pyropdf.open(document.to_bytes()).pages[0]
    assert _boxes(saved) == _boxes(page)


def test_setting_a_box_writes_only_that_page() -> None:
    document = pyropdf.open(boxes_pdf())
    page = document.pages[0]
    parent = page.obj["Parent"]
    page.trim_box = (10, 10, 100, 100)
    assert "TrimBox" in page.obj
    assert "CropBox" not in page.obj
    assert "MediaBox" not in page.obj
    assert "TrimBox" not in parent
    assert list(page.obj["TrimBox"]) == [10, 692, 100, 782]


def test_a_box_that_leaves_the_media_box_is_refused() -> None:
    document = pyropdf.open(boxes_pdf())
    page = document.pages[0]
    before = document.to_bytes()
    with pytest.raises(ValueError, match="leaves the MediaBox"):
        page.crop_box = (-1, 0, 100, 100)
    with pytest.raises(ValueError, match="empty"):
        page.art_box = (10, 10, 10, 100)
    with pytest.raises(pyropdf.PdfError, match="finite"):
        page.bleed_box = (0, 0, float("nan"), 100)
    assert document.to_bytes() == before


def test_a_media_box_that_would_not_hold_the_other_boxes_is_refused() -> None:
    document = pyropdf.open(boxes_pdf("/TrimBox [100 100 500 500]"))
    page = document.pages[0]
    with pytest.raises(ValueError, match="does not hold"):
        page.media_box = (0, 0, 300, 300)
    page.trim_box = (0, 592, 200, 792)
    page.media_box = (0, 492, 300, 792)
    assert page.media_box == Rect(0, 0, 300, 300)
    assert page.trim_box == Rect(0, 100, 200, 300)


def test_rendering_and_text_follow_a_changed_crop_box() -> None:
    page = pyropdf.open(boxes_pdf()).pages[0]
    (before,) = page.words()
    page.crop_box = (50, 50, 350, 250)
    assert page.rect == Rect(0, 0, 300, 200)
    pixmap = page.pixmap(dpi=72)
    assert (pixmap.width, pixmap.height) == (300, 200)
    (after,) = page.words()
    assert after.text == "Hello"
    assert after.bbox.x0 == pytest.approx(before.bbox.x0 - 50)
    assert after.bbox.y0 == pytest.approx(before.bbox.y0 - 50)


def test_a_stale_page_raises() -> None:
    document = pyropdf.open(boxes_pdf())
    page = document.pages[0]
    del document.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        _ = page.crop_box
    with pytest.raises(pyropdf.StalePageError):
        page.crop_box = (0, 0, 10, 10)
