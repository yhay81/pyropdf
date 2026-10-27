"""``Page.search``: the case-insensitive matches of a text on a page.

Ported from pylopdf 036be111 ``tests/test_layout.py``, lines 319 to 421,
and the search half of ``test_vertical_cjk_writing_mode_and_search``. The
quads of matches are in ``tests/test_text_quads.py``. The differences:

- pylopdf draws its pages with ``page.draw``; these tests write content
  streams.
- pylopdf selects pages with ``doc.pages.select``; pyropdf deletes one.
- pylopdf turns its CJK fallback fonts off with ``doc.fallback_fonts``,
  which pyropdf does not have; text from a CMap needs no font program.
- ``Page.search`` also takes the ``max_glyphs`` and ``max_size`` budgets of
  the other text views.
"""

from __future__ import annotations

import pytest
from corpus import ASSETS, NAMES
from test_content import build_raw_pdf
from test_text import build_text_pdf

import pyropdf


@pytest.fixture
def usrguide() -> pyropdf.Page:
    return pyropdf.open(ASSETS / "usrguide.pdf").pages[0]


def build_cjk_pdf(encoding: str = "90ms-RKSJ-H") -> bytes:
    """Build a page that shows "こんにちは日本語" with a non-embedded CJK font through ``encoding``."""
    text = "".join(f"\\{byte:03o}" for byte in "こんにちは日本語".encode("cp932"))
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET"
    return build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> >>",
        3: f"<< /Type /Font /Subtype /Type0 /BaseFont /MS-Mincho /Encoding /{encoding} /DescendantFonts [6 0 R] >>",
        4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
        5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        6: "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MS-Mincho"
        " /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >> /FontDescriptor 7 0 R /DW 1000 >>",
        7: "<< /Type /FontDescriptor /FontName /MS-Mincho /Flags 6 /FontBBox [0 -137 1000 859]"
        " /ItalicAngle 0 /Ascent 859 /Descent -140 /CapHeight 769 /StemV 78 >>",
    })  # fmt: skip


def test_document_level_get_page_text_modes() -> None:
    document = pyropdf.open(
        build_text_pdf("BT /F1 12 Tf 72 700 Td (Alpha beta) Tj ET", "BT /F1 12 Tf 72 700 Td (Gamma) Tj ET")
    )

    assert [word.text for word in document.pages[0].words()] == ["Alpha", "beta"]
    assert document.pages[1].words()[0].text == "Gamma"


def test_search_for_basic(usrguide: pyropdf.Page) -> None:
    hits = usrguide.search("authors")

    assert len(hits) >= 1
    rect = hits[0]
    assert isinstance(rect, pyropdf.Rect)
    assert rect.x0 < rect.x1
    assert rect.y0 < rect.y1


def test_search_for_is_case_insensitive(usrguide: pyropdf.Page) -> None:
    assert usrguide.search("AUTHORS") == usrguide.search("authors")


def test_search_for_multiword(usrguide: pyropdf.Page) -> None:
    """A space of the text matches a space inferred from a gap."""
    hits = usrguide.search("for authors")

    assert len(hits) == 1
    assert hits[0].x0 <= usrguide.search("for")[0].x1


def test_search_for_no_match(usrguide: pyropdf.Page) -> None:
    assert usrguide.search("zzzz-not-in-document") == []


@pytest.mark.parametrize(("text", "error"), [("", ValueError), (b"a", TypeError)])
def test_search_for_validates_its_text(usrguide: pyropdf.Page, text: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="text must be"):
        usrguide.search(text)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_search_for_validates_hit_budget(usrguide: pyropdf.Page, value: object) -> None:
    with pytest.raises((TypeError, ValueError), match="max_hits"):
        usrguide.search("authors", max_hits=value)  # ty: ignore[invalid-argument-type]


def test_search_for_bounds_utf8_input_before_core_copy(usrguide: pyropdf.Page) -> None:
    assert usrguide.search("é" * 2048) == []

    with pytest.raises(pyropdf.LimitError) as caught:
        usrguide.search("é" * 2049)

    assert caught.value.code == "search_input_size"


def test_search_for_bounds_hits_without_returning_partial_results() -> None:
    page = pyropdf.open(build_text_pdf(f"BT /F1 0.2 Tf 10 50 Td ({'a' * 4097}) Tj ET")).pages[0]

    with pytest.raises(pyropdf.LimitError) as caught:
        page.search("a")

    assert caught.value.code == "search_hit_count"
    assert len(page.search("a", max_hits=4097)) == 4097
    assert len(page.search("a", max_hits=None)) == 4097


def test_search_counts_against_the_text_budgets(usrguide: pyropdf.Page) -> None:
    with pytest.raises(pyropdf.LimitError) as caught:
        usrguide.search("authors", max_glyphs=10)

    assert caught.value.code == "text_glyph_count"


def test_search_for_cjk() -> None:
    hits = pyropdf.open(ASSETS / "mhlw-doc.pdf").pages[0].search("裁判例")

    assert len(hits) >= 3
    for rect in hits:
        assert rect.x0 < rect.x1
        assert rect.y0 < rect.y1


def test_search_for_nonembedded_cjk() -> None:
    """Search non-embedded CJK text whose Unicode comes from the CMap."""
    assert len(pyropdf.open(build_cjk_pdf()).pages[0].search("日本語")) == 1


def test_search_for_vertical_cjk() -> None:
    page = pyropdf.open(build_cjk_pdf("90ms-RKSJ-V")).pages[0]

    assert page.text() == "こんにちは日本語\n"
    assert len(page.search("日本語")) == 1


def test_a_match_spans_the_lines_that_dehyphenation_joins() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 12 Tf 14 TL 72 700 Td (exam-) Tj T* (ple) Tj ET")).pages[0]

    assert page.search("example") == []
    (rect,) = page.search("example", dehyphenate=True)
    first, second = page.search("exam") + page.search("ple")
    assert rect == first | second


def test_search_keeps_to_a_clip() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (one) Tj 300 0 Td (one) Tj ET")).pages[0]

    (rect,) = page.search("one", clip=(0, 0, 200, 792))

    assert rect.x1 < 200
    assert len(page.search("one")) == 2


def test_layout_reflects_edits() -> None:
    document = pyropdf.open(
        build_text_pdf("BT /F1 12 Tf 72 700 Td (First page) Tj ET", "BT /F1 12 Tf 72 700 Td (Second page) Tj ET")
    )

    del document.pages[0]

    assert [word.text for word in document.pages[0].words()] == ["Second", "page"]
    assert len(document.pages[0].search("second")) == 1


def test_search_reflects_an_edit_to_the_content() -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 12 Tf 72 700 Td (Before) Tj ET"))
    page = document.pages[0]
    assert "Before" in page.text()
    assert page.search("After") == []

    page.obj["Contents"].write(b"BT /F1 12 Tf 72 700 Td (Before) Tj 0 -20 Td (After) Tj ET")

    assert "After" in page.text()
    assert page.search("After") == pyropdf.open(document.to_bytes()).pages[0].search("After")
    assert len(page.search("After")) == 1


def test_a_deleted_page_refuses_a_search() -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 12 Tf 72 700 Td (Alpha) Tj ET"))
    page = document.pages[0]
    del document.pages[0]

    with pytest.raises(pyropdf.StalePageError):
        page.search("alpha")


@pytest.mark.filterwarnings("ignore::pyropdf.PyropdfWarning")
@pytest.mark.parametrize("name", NAMES)
def test_search_finds_every_word_of_a_corpus_page_at_its_box(name: str) -> None:
    page = pyropdf.open((ASSETS / name).read_bytes()).pages[0]
    for word in page.words()[:40]:
        assert word.bbox in page.search(word.text, max_hits=None), word.text
