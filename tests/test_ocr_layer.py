"""The invisible text layer that ``Page.apply_ocr(words=...)`` adds.

Ported from pylopdf 036be111 ``tests/test_ocr_layer.py``. The tests check
both sides of the contract: nothing is drawn, while extraction and search
see the text. The differences:

- pylopdf turns its CJK fallback fonts off with ``doc.fallback_fonts``,
  which pyropdf does not have; the test checks instead that the layer's
  font embeds no program.
- pylopdf draws its source text with ``page.draw``; these tests write
  content streams that show Helvetica.
- The tests of undo, of a stale page, of a taken resource name, and of
  content that leaves its graphics state changed are pyropdf's own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from test_content import build_raw_pdf
from test_text import build_text_pdf

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


def ocr_words(entries: Iterable[tuple[float, float, float, float, str]]) -> Iterator[pyropdf.OcrWord]:
    """Yield an OCR record for each ``(x0, y0, x1, y1, text)`` entry, lazily, as another engine would."""
    for x0, y0, x1, y1, text in entries:
        yield pyropdf.OcrWord(text=text, bbox=pyropdf.Rect(x0, y0, x1, y1), confidence=1.0)


def _blank_page_doc() -> pyropdf.Document:
    document = pyropdf.Document()
    document.pages.new((300, 200))
    return document


def _hello_doc() -> pyropdf.Document:
    """Return a 612 by 792 page that shows "Hello" near its top."""
    return pyropdf.open(build_text_pdf("BT /F1 12 Tf 72 700 Td (Hello) Tj ET"))


def _rotate_box_clockwise(
    box: tuple[float, float, float, float],
    width: float,
    height: float,
    rotation: int,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    if rotation == 90:
        return (height - y1, x0, height - y0, x1)
    if rotation == 180:
        return (width - x1, height - y1, width - x0, height - y0)
    if rotation == 270:
        return (y0, width - x1, y1, width - x0)
    return box


def test_ocr_layer_is_extractable_and_searchable() -> None:
    page = _blank_page_doc().pages[0]

    page.apply_ocr(
        words=ocr_words([(50, 50, 150, 70, "Hello"), (50, 80, 170, 100, "日本語テキスト")]),
        skip_existing=False,
    )

    text = page.text()
    assert "Hello" in text
    assert "日本語テキスト" in text
    hits = page.search("日本語")
    assert hits
    # The match is near the requested box; the reference font need not fill it exactly.
    assert abs(hits[0].x0 - 50) < 5
    assert 70 < hits[0].y0 < 105
    assert 70 < hits[0].y1 < 110


def test_ocr_layer_is_invisible() -> None:
    page = _blank_page_doc().pages[0]

    page.apply_ocr(words=ocr_words([(50, 50, 150, 70, "Invisible")]), skip_existing=False)

    samples = bytes(page.pixmap(background=(255, 255, 255), alpha=True))
    assert all(samples[i] == 255 for i in range(0, len(samples), 4))


def test_ocr_layer_embeds_no_font_program() -> None:
    page = _blank_page_doc().pages[0]

    page.apply_ocr(words=ocr_words([(50, 50, 200, 75, "帳票スキャン")]), skip_existing=False)

    font = page.obj["Resources"]["Font"]["PyroOCR"]
    assert (font["Subtype"], font["Encoding"]) == ("Type0", "Identity-H")
    descriptor = font["DescendantFonts"][0]["FontDescriptor"]
    assert not {"FontFile", "FontFile2", "FontFile3"} & set(descriptor)
    assert "帳票スキャン" in page.text()


def test_ocr_layer_survives_save_roundtrip() -> None:
    document = _blank_page_doc()
    document.pages[0].apply_ocr(
        words=ocr_words([(50, 50, 150, 70, "Persistent"), (50, 80, 150, 100, "残存")]), skip_existing=False
    )

    reopened = pyropdf.open(document.to_bytes())

    text = reopened.pages[0].text()
    assert "Persistent" in text
    assert "残存" in text
    assert reopened.pages[0].search("残存")


def test_ocr_layer_round_trips_extracted_words() -> None:
    words = pyropdf.open(build_text_pdf("BT /F1 12 Tf 72 692 Td (Roundtrip works) Tj ET")).pages[0].words()
    assert words
    document = _blank_page_doc()

    document.pages[0].apply_ocr(words=ocr_words([(*word.bbox, word.text) for word in words]), skip_existing=False)

    assert {word.text for word in document.pages[0].words()} == {"Roundtrip", "works"}


def test_ocr_layer_on_rotated_page_uses_display_coordinates() -> None:
    document = pyropdf.Document()
    page = document.pages.new((100, 200))
    page.rotation = 90  # The display is 200 by 100.

    page.apply_ocr(words=ocr_words([(120, 30, 180, 50, "回転ページ")]), skip_existing=False)

    hits = page.search("回転ページ")
    assert hits
    assert abs(hits[0].x0 - 120) < 5


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_ocr_layer_orients_sideways_baselines(rotation: pyropdf.OcrRotation) -> None:
    page = _blank_page_doc().pages[0]

    page.apply_ocr(words=ocr_words([(80, 30, 105, 150, "Vertical box")]), rotation=rotation, skip_existing=False)

    hits = page.search("Vertical box")
    assert hits
    assert hits[0].height > hits[0].width
    assert tuple(hits[0]) == pytest.approx((80, 30, 105, 150), abs=0.1)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_ocr_layer_orders_rotated_columns_logically(rotation: pyropdf.OcrRotation) -> None:
    logical_width = 400.0
    logical_height = 300.0
    logical_words = [
        ((20.0, 10.0, 380.0, 35.0), "Spanning heading"),
        ((40.0, 60.0, 150.0, 80.0), "Left one"),
        ((40.0, 100.0, 150.0, 120.0), "Left two"),
        ((40.0, 140.0, 150.0, 160.0), "Left three"),
        ((240.0, 60.0, 350.0, 80.0), "Right one"),
        ((240.0, 100.0, 350.0, 120.0), "Right two"),
        ((240.0, 140.0, 350.0, 160.0), "Right three"),
        ((20.0, 260.0, 380.0, 285.0), "Spanning footer"),
    ]
    display_rotation = (360 - rotation) % 360
    physical_words = [
        (_rotate_box_clockwise(box, logical_width, logical_height, display_rotation), text)
        for box, text in logical_words
    ]
    page_size = (logical_height, logical_width) if display_rotation in (90, 270) else (logical_width, logical_height)
    page = pyropdf.Document().pages.new(page_size)
    # Geometry, not the order of the words, decides the reading order.
    scrambled = physical_words[4:] + physical_words[:4]

    page.apply_ocr(words=ocr_words([(*box, text) for box, text in scrambled]), rotation=rotation, skip_existing=False)

    assert page.text().splitlines() == [text for _, text in logical_words]
    for box, text in physical_words:
        hits = page.search(text)
        assert len(hits) == 1
        assert tuple(hits[0]) == pytest.approx(box, abs=0.1)


def test_ocr_layer_without_words_changes_nothing() -> None:
    document = _blank_page_doc()
    before = document.to_bytes()

    assert document.pages[0].apply_ocr(words=[], skip_existing=False) == []
    assert document.pages[0].apply_ocr(words=ocr_words([(10, 10, 50, 20, "")]), skip_existing=False) == []
    assert document.to_bytes() == before


def test_ocr_layer_returns_the_words_with_text() -> None:
    document = _blank_page_doc()
    words = list(ocr_words([(10, 10, 50, 20, ""), (50, 50, 150, 70, "Kept")]))

    assert document.pages[0].apply_ocr(words=words, skip_existing=False) == words[1:]


def test_ocr_layer_validates_words_and_options() -> None:
    document = _blank_page_doc()
    before = document.to_bytes()
    word = pyropdf.OcrWord(text="Text", bbox=pyropdf.Rect(10, 10, 50, 20), confidence=0.9)
    page = document.pages[0]

    with pytest.raises(pyropdf.OcrError, match="rotation"):
        page.apply_ocr(words=[word], rotation=45)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="OcrWord records"):
        page.apply_ocr(words=[(10, 10, 50, 20, "Text")])  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="word bbox"):
        page.apply_ocr(words=[pyropdf.OcrWord(text="Text", bbox=(10, 10, 50), confidence=1.0)])  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="word text"):
        page.apply_ocr(words=[pyropdf.OcrWord(text=b"Text", bbox=(10, 10, 50, 20), confidence=1.0)])  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="skip_existing"):
        page.apply_ocr(words=[word], skip_existing=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="clip"):
        page.apply_ocr(words=[word], clip=(10, 10, 5, 20))
    for options in ({"engine": object()}, {"dpi": 150}, {"tile_size": 1024}, {"overlap": 64}, {"min_confidence": 0.9}):
        with pytest.raises(TypeError, match="apply to recognition, not to words"):
            page.apply_ocr(words=[word], **options)  # ty: ignore[invalid-argument-type]
    assert document.to_bytes() == before


def test_ocr_layer_from_words_skips_pages_with_text_by_default() -> None:
    document = _hello_doc()
    before = document.to_bytes()
    word = pyropdf.OcrWord(text="Scanned", bbox=pyropdf.Rect(300, 300, 400, 320), confidence=1.0)

    assert document.pages[0].apply_ocr(words=[word]) == []
    assert document.to_bytes() == before
    # Only text that meets the clip counts; "Hello" is near the top of the page.
    assert document.pages[0].apply_ocr(words=[word], clip=(250, 250, 450, 350)) == [word]
    assert "Scanned" in document.pages[0].text()


def test_ocr_layer_bounds_generator_words_before_full_materialization() -> None:
    document = _hello_doc()
    before = document.to_bytes()
    consumed = 0

    def words() -> Iterator[tuple[int, int, int, int, str]]:
        nonlocal consumed
        for index in range(5000):
            consumed += 1
            yield (10, 10, 50, 20, f"word-{index}")

    with pytest.raises(ValueError, match="4096 entries with text"):
        document.pages[0].apply_ocr(words=ocr_words(words()), skip_existing=False)
    assert consumed == 4097
    assert document.to_bytes() == before


def test_ocr_layer_bounds_utf8_text_before_mutation() -> None:
    document = _hello_doc()
    before = document.to_bytes()

    with pytest.raises(ValueError, match="1048576 UTF-8 bytes"):
        document.pages[0].apply_ocr(words=ocr_words([(10, 10, 50, 20, "é" * (512 * 1024 + 1))]), skip_existing=False)
    assert document.to_bytes() == before


def test_ocr_layer_bounds_distinct_cids_before_mutation() -> None:
    document = _hello_doc()
    before = document.to_bytes()
    text = "".join(chr(codepoint) for codepoint in range(1, 0x11000) if not 0xD800 <= codepoint <= 0xDFFF)
    text = text[:65_535]

    with pytest.raises(pyropdf.PdfError, match="65,534 per call"):
        document.pages[0].apply_ocr(words=ocr_words([(10, 10, 290, 20, text)]), skip_existing=False)
    assert document.to_bytes() == before


def test_one_undo_removes_the_layer() -> None:
    document = _blank_page_doc()
    before = document.to_bytes()

    document.pages[0].apply_ocr(words=ocr_words([(50, 50, 150, 70, "Undone")]), skip_existing=False)
    assert "Undone" in document.pages[0].text()
    document.history.undo()

    assert document.pages[0].text() == ""
    assert document.to_bytes() == before


def test_a_deleted_page_refuses_a_layer() -> None:
    document = _blank_page_doc()
    page = document.pages[0]
    del document.pages[0]

    with pytest.raises(pyropdf.StalePageError):
        page.apply_ocr(words=ocr_words([(50, 50, 150, 70, "Stale")]), skip_existing=False)


def test_the_layer_keeps_existing_content_and_its_graphics_state() -> None:
    """Existing content that scales the page and leaves it scaled does not move the layer."""
    stream = "2 0 0 2 0 0 cm BT /PyroOCR 6 Tf 10 300 Td (Kept) Tj ET"
    document = pyropdf.open(
        build_raw_pdf({
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 612 792]"
            " /Resources << /Font << /PyroOCR 3 0 R >> >> >>",
            3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            4: "<< /Type /Page /Parent 2 0 R /Contents [5 0 R] >>",
            5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        })
    )  # fmt: skip
    page = document.pages[0]

    page.apply_ocr(words=ocr_words([(300, 300, 400, 320, "Layer")]), skip_existing=False)

    # The inherited font keeps its name, and the layer's font takes a free one.
    fonts = page.obj["Resources"]["Font"]
    assert set(fonts) == {"PyroOCR", "PyroOCR1"}
    assert fonts["PyroOCR"]["BaseFont"] == "Helvetica"
    assert [word.text for word in page.words()] == ["Kept", "Layer"]
    (hit,) = page.search("Layer")
    assert tuple(hit) == pytest.approx((300, 300, 400, 320), abs=0.1)
    reopened = pyropdf.open(document.to_bytes()).pages[0]
    assert [word.text for word in reopened.words()] == ["Kept", "Layer"]
