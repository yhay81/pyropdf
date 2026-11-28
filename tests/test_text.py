"""page.text() and doc.text(): the text of a page in reading order, and the text options.

Ported from pylopdf's tests/test_text_options.py at
036be111cafe77755db4bb2be17bc70a8f2f9d82, the tests that read ``text()``.
The budgets are the call's ``max_glyphs`` and ``max_size`` rather than
pylopdf's ``DocumentLimits.max_text_glyphs`` and ``max_text_size``, and a
document with an unmapped font comes from the corpus instead of pylopdf's
canvas.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from corpus import ASSETS, NAMES
from test_content import build_raw_pdf

import pyropdf


def _to_unicode(mappings: dict[int, str]) -> str:
    """Return a ToUnicode CMap that maps one-byte codes to text."""
    entries = "\n".join(f"<{code:02X}> <{text.encode('utf-16-be').hex().upper()}>" for code, text in mappings.items())
    return (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        "/CMapName /Test def 1 begincodespacerange <00> <FF> endcodespacerange "
        f"{len(mappings)} beginbfchar\n{entries}\nendbfchar endcmap "
        "CMapName currentdict /CMap defineresource pop end end"
    )


def build_text_pdf(*streams: str, to_unicode: dict[int, str] | None = None) -> bytes:
    """Build 612 by 792 pages, one per stream, that draw with Helvetica as /F1."""
    font = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
    first = 5 + len(streams)
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(len(streams)))
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} >>",
    }
    for index, stream in enumerate(streams):
        page = 3 + 2 * index
        objects[page] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page + 1} 0 R "
            f"/Resources << /Font << /F1 {first + len(streams)} 0 R >> >> >>"
        )
        objects[page + 1] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    number = first + len(streams)
    if to_unicode is None:
        objects[number] = font + " >>"
    else:
        cmap = _to_unicode(to_unicode)
        objects[number] = font + f" /ToUnicode {number + 1} 0 R >>"
        objects[number + 1] = f"<< /Length {len(cmap)} >>\nstream\n{cmap}\nendstream"
    return build_raw_pdf(objects)


@pytest.mark.spec("text-extraction", "interpret")
def test_text_follows_reading_order_with_spaces_and_line_ends() -> None:
    page = pyropdf.open(
        build_text_pdf("BT /F1 20 Tf 72 600 Td (Second) Tj ET BT /F1 20 Tf 72 700 Td (First) Tj 60 0 Td (gap) Tj ET")
    ).pages[0]

    assert page.text() == "First gap\nSecond\n"


def test_clip_keeps_the_text_inside_a_rectangle_in_reading_order() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 20 Tf 72 700 Td (Top left) Tj ET BT /F1 20 Tf 400 700 Td (Top right) Tj ET "
            "BT /F1 20 Tf 72 600 Td (Lower left) Tj ET"
        )
    ).pages[0]

    assert page.text(clip=(60, 70, 300, 200)) == "Top left\nLower left\n"
    assert page.text(clip=(380, 70, 612, 120)) == "Top right\n"
    # A glyph counts as inside when the center of its box is.
    assert page.text(clip=(60, 70, 100, 120)) == "To\n"
    assert page.text(clip=(0, 0, 10, 10)) == ""


def test_glyphs_that_the_page_or_the_clip_paths_hide_are_left_out_unless_asked_for() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 20 Tf 72 700 Td (Seen) Tj ET "
            "BT /F1 20 Tf 700 650 Td (Off page) Tj ET "
            "q 0 0 10 10 re W n BT /F1 20 Tf 72 600 Td (Masked) Tj ET Q "
            "q 3 Tr BT /F1 20 Tf 72 500 Td (Invisible) Tj ET Q"
        )
    ).pages[0]

    assert page.text() == "Seen\nInvisible\n"
    assert page.text(clipped=True) == "Seen\nOff page\nMasked\nInvisible\n"
    assert page.text() == "Seen\nInvisible\n"


def test_ligatures_and_whitespace_are_kept_unless_asked_otherwise() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 20 Tf 72 700 Td (AB) Tj ET",
            to_unicode={0x41: "ﬁ", 0x42: " x"},
        )
    ).pages[0]

    assert page.text() == "ﬁ x\n"
    assert page.text(ligatures=False) == "fi x\n"
    assert page.text(whitespace=False) == "ﬁ x\n"
    assert page.text(ligatures=False, whitespace=False) == "fi x\n"


def test_dehyphenation_joins_a_word_that_a_line_end_splits() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 12 Tf 14 TL 72 700 Td (A long exam-) Tj T* (ple follows) Tj T* (Jean-) Tj T* (Pierre stays) Tj ET"
        )
    ).pages[0]

    assert page.text() == "A long exam-\nple follows\nJean-\nPierre stays\n"
    assert page.text(dehyphenate=True) == "A long example follows\nJean-\nPierre stays\n"


def test_dehyphenation_needs_the_next_line_in_the_same_block() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 12 Tf 72 700 Td (A long exam-) Tj 0 -200 Td (ple apart) Tj ET")).pages[0]

    assert page.text(dehyphenate=True) == page.text()


def _unmapped_document() -> pyropdf.Document:
    """Return the corpus's Japanese document without its ToUnicode CMaps."""
    document = pyropdf.open(ASSETS / "mhlw-doc.pdf")
    for xref in document.objects:
        value = document.objects[xref]
        if isinstance(value, pyropdf.Dictionary) and "ToUnicode" in value:
            del value["ToUnicode"]
    return document


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        pytest.param("/Span <</ActualText (A)>> BDC BT /F1 20 Tf 72 700 Td (x) Tj ET EMC", "A\n", id="one glyph"),
        pytest.param(
            "/Span <</ActualText <FEFF00C4>>> BDC BT /F1 20 Tf 72 700 Td (x) Tj ET EMC", "\u00c4\n", id="utf-16"
        ),
        pytest.param(
            "BT /F1 20 Tf 72 700 Td (a) Tj /Span <</ActualText (BC)>> BDC (xyz) Tj EMC (d) Tj ET",
            "aBCd\n",
            id="several",
        ),
        pytest.param(
            "BT /F1 20 Tf 72 700 Td (a) Tj /Span <</ActualText (B)>> BDC (x) Tj EMC "
            "/Span <</ActualText (C)>> BDC (y) Tj EMC (d) Tj ET",
            "aBCd\n",
            id="in turn",
        ),
        pytest.param(
            "/Span <</ActualText (A)>> BDC /Span <</ActualText (B)>> BDC BT /F1 20 Tf 72 700 Td (x) Tj ET EMC EMC",
            "A\n",
            id="the outermost wins",
        ),
        pytest.param(
            "/Span <</ActualText (A)>> BDC /P BMC BT /F1 20 Tf 72 700 Td (x) Tj ET EMC "
            "BT /F1 20 Tf 83 700 Td (y) Tj ET EMC BT /F1 20 Tf 94 700 Td (z) Tj ET",
            "Az\n",
            id="an inner sequence ends without it",
        ),
        pytest.param(
            "BT /F1 20 Tf 72 700 Td (a) Tj /Span <</ActualText ()>> BDC (x) Tj EMC (b) Tj ET", "a b\n", id="empty"
        ),
        pytest.param("/Span <</ActualText (A)>> BDC EMC BT /F1 20 Tf 72 700 Td (b) Tj ET", "b\n", id="no glyph"),
        pytest.param("/Span <</MCID 0>> BDC BT /F1 20 Tf 72 700 Td (x) Tj ET EMC", "x\n", id="without it"),
        pytest.param("EMC BT /F1 20 Tf 72 700 Td (x) Tj ET EMC", "x\n", id="unbalanced"),
    ],
)
def test_actual_text_replaces_the_text_of_the_glyphs_inside(stream: str, expected: str) -> None:
    assert pyropdf.open(build_text_pdf(stream)).pages[0].text() == expected


def test_actual_text_holds_for_a_glyph_outside_the_clip_or_without_a_mapping() -> None:
    # The first glyph lies off the page, so the second one holds the text.
    stream = "/Span <</ActualText (A)>> BDC BT /F1 20 Tf 700 700 Td (x) Tj ET BT /F1 20 Tf 72 700 Td (y) Tj ET EMC"
    assert pyropdf.open(build_text_pdf(stream)).pages[0].text() == "A\n"

    # A mapping that a CMap lacks raises no warning inside the sequence.
    stream = "/Span <</ActualText (A)>> BDC BT /F1 20 Tf 72 700 Td <01> Tj ET EMC"
    with warnings.catch_warnings():
        warnings.simplefilter("error", pyropdf.PyropdfWarning)
        assert pyropdf.open(build_text_pdf(stream, to_unicode={0x41: "A"})).pages[0].text() == "A\n"


def test_actual_text_is_found_through_the_properties_of_the_resources() -> None:
    stream = "/Span /MC0 BDC BT /F1 20 Tf 72 700 Td (x) Tj ET EMC"
    data = build_text_pdf(stream).replace(
        b"/Resources << ", b"/Resources << /Properties << /MC0 << /ActualText (A) >> >> "
    )

    assert pyropdf.open(data).pages[0].text() == "A\n"


def test_a_word_of_actual_text_spans_the_glyphs_inside() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td /Span <</ActualText (AB)>> BDC (xyz) Tj EMC ET")).pages[
        0
    ]
    # Helvetica's x, y, and z are 500 units wide.
    word = page.words()[0]
    assert word.text == "AB"
    assert word.bbox.x0 == pytest.approx(72, abs=1e-2)
    assert word.bbox.x1 == pytest.approx(72 + 3 * 10, abs=1e-2)


def test_the_budget_counts_actual_text() -> None:
    stream = f"/Span <</ActualText ({'A' * 2000})>> BDC BT /F1 20 Tf 72 700 Td (x) Tj ET EMC"
    page = pyropdf.open(build_text_pdf(stream)).pages[0]

    assert page.text() == "A" * 2000 + "\n"
    with pytest.raises(pyropdf.LimitError) as refused:
        page.text(max_size=100)
    assert refused.value.code == "text_size"


def test_actual_text_false_extracts_underlying_glyphs_across_views() -> None:
    stream = "BT /F1 20 Tf 72 700 Td /Span << /ActualText (Replacement) >> BDC (Original) Tj EMC ET"
    page = pyropdf.open(build_text_pdf(stream)).pages[0]

    # Default and explicit actual_text=True substitute /ActualText.
    assert page.text() == "Replacement\n"
    assert page.text(actual_text=True) == "Replacement\n"
    assert [word.text for word in page.words(actual_text=True)] == ["Replacement"]
    assert [span.text for span in page.layout(actual_text=True).blocks[0].lines[0].spans] == ["Replacement"]
    assert [char.text for char in page.chars(actual_text=True)] == ["Replacement"]
    assert len(page.search("Replacement", actual_text=True)) == 1
    assert page.search("Original", actual_text=True) == []

    # actual_text=False exposes the underlying glyph Unicode across all views.
    assert page.text(actual_text=False) == "Original\n"
    assert [word.text for word in page.words(actual_text=False)] == ["Original"]
    assert [span.text for span in page.layout(actual_text=False).blocks[0].lines[0].spans] == ["Original"]
    assert [char.text for char in page.chars(actual_text=False)] == list("Original")
    assert len(page.search("Original", actual_text=False)) == 1
    assert page.search("Replacement", actual_text=False) == []


def test_actual_text_false_exposes_glyphs_under_empty_nested_and_annotation_actual_text() -> None:
    empty = "BT /F1 20 Tf 72 700 Td (a) Tj /Span << /ActualText () >> BDC (x) Tj EMC (b) Tj ET"
    empty_page = pyropdf.open(build_text_pdf(empty)).pages[0]
    assert empty_page.text() == "a b\n"
    assert empty_page.text(actual_text=False) == "axb\n"
    assert [word.text for word in empty_page.words(actual_text=False)] == ["axb"]
    assert [char.text for char in empty_page.chars(actual_text=False)] == ["a", "x", "b"]
    assert len(empty_page.search("axb", actual_text=False)) == 1

    nested = (
        "/Span << /ActualText (Outer) >> BDC /Span << /ActualText (Inner) >> BDC "
        "BT /F1 20 Tf 72 700 Td (Original) Tj ET EMC EMC"
    )
    nested_page = pyropdf.open(build_text_pdf(nested)).pages[0]
    assert nested_page.text() == "Outer\n"
    assert nested_page.text(actual_text=False) == "Original\n"

    unmapped = "/Span << /ActualText (A) >> BDC BT /F1 20 Tf 72 700 Td <01> Tj ET EMC"
    unmapped_page = pyropdf.open(build_text_pdf(unmapped, to_unicode={0x41: "A"})).pages[0]
    assert unmapped_page.text() == "A\n"
    with pytest.warns(pyropdf.PyropdfWarning, match="no Unicode mapping"):
        assert unmapped_page.text(actual_text=False) == ""
    assert unmapped_page.text(actual_text=False, unmapped="replacement") == "\ufffd\n"

    # Annotation appearance with /ActualText.
    ap_stream = "BT /F1 12 Tf 4 20 Td /Span << /ActualText (Replacement) >> BDC (Original) Tj EMC ET"
    annot_pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [4 0 R] >>",
            4: "<< /Type /Annot /Subtype /Stamp /Rect [10 10 150 50] /P 3 0 R /AP << /N 5 0 R >> >>",
            5: (
                f"<< /Type /XObject /Subtype /Form /BBox [0 0 140 40] "
                f"/Resources << /Font << /F1 6 0 R >> >> /Length {len(ap_stream)} >>\n"
                f"stream\n{ap_stream}\nendstream"
            ),
            6: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        }
    )
    annotation = pyropdf.open(annot_pdf).pages[0].annotations[0]
    assert annotation.text() == "Replacement\n"
    assert annotation.text(actual_text=False) == "Original\n"


def test_actual_text_validation_and_memoization_isolation() -> None:
    stream = "BT /F1 20 Tf 72 700 Td /Span << /ActualText (Replacement) >> BDC (Original) Tj EMC ET"
    document = pyropdf.open(build_text_pdf(stream))
    page = document.pages[0]

    for bad in (1, 0, None, "yes"):
        with pytest.raises(TypeError, match="actual_text must be True or False"):
            page.text(actual_text=bad)  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError, match="actual_text must be True or False"):
            page.words(actual_text=bad)  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError, match="actual_text must be True or False"):
            page.layout(actual_text=bad)  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError, match="actual_text must be True or False"):
            page.chars(actual_text=bad)  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError, match="actual_text must be True or False"):
            page.search("Original", actual_text=bad)  # ty: ignore[invalid-argument-type]

    ap_stream = "BT /F1 12 Tf 4 20 Td (Note) Tj ET"
    annot_doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [4 0 R] >>",
                4: "<< /Type /Annot /Subtype /Stamp /Rect [10 10 150 50] /P 3 0 R /AP << /N 5 0 R >> >>",
                5: (
                    f"<< /Type /XObject /Subtype /Form /BBox [0 0 140 40] "
                    f"/Resources << /Font << /F1 6 0 R >> >> /Length {len(ap_stream)} >>\n"
                    f"stream\n{ap_stream}\nendstream"
                ),
                6: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            }
        )
    )
    with pytest.raises(TypeError, match="actual_text must be True or False"):
        annot_doc.pages[0].annotations[0].text(actual_text=1)  # ty: ignore[invalid-argument-type]

    # Memoized calls with actual_text=True and actual_text=False never alias.
    hits_before, computed_before = pyropdf._core.page_result_counts(document._core())  # noqa: SLF001
    assert page.text(actual_text=True) == "Replacement\n"
    assert page.text(actual_text=False) == "Original\n"
    assert page.text(actual_text=True) == "Replacement\n"
    assert page.text(actual_text=False) == "Original\n"
    hits_text, computed_text = pyropdf._core.page_result_counts(document._core())  # noqa: SLF001
    assert computed_text == computed_before + 2
    assert hits_text == hits_before + 2

    assert [w.text for w in page.words(actual_text=True)] == ["Replacement"]
    assert [w.text for w in page.words(actual_text=False)] == ["Original"]
    assert [w.text for w in page.words(actual_text=True)] == ["Replacement"]
    assert [w.text for w in page.words(actual_text=False)] == ["Original"]
    hits_layout, computed_layout = pyropdf._core.page_result_counts(document._core())  # noqa: SLF001
    assert computed_layout == computed_text + 2
    assert hits_layout == hits_text + 2

    assert [c.text for c in page.chars(actual_text=True)] == ["Replacement"]
    assert [c.text for c in page.chars(actual_text=False)] == list("Original")
    assert [c.text for c in page.chars(actual_text=True)] == ["Replacement"]
    assert [c.text for c in page.chars(actual_text=False)] == list("Original")
    hits_chars, computed_chars = pyropdf._core.page_result_counts(document._core())  # noqa: SLF001
    assert computed_chars == computed_layout + 2
    assert hits_chars == hits_layout + 2


@pytest.mark.spec("text-extraction", "read")
def test_unmapped_glyphs_are_skipped_or_stand_for_a_placeholder() -> None:
    page = _unmapped_document().pages[0]

    with pytest.warns(pyropdf.PyropdfWarning, match="no Unicode mapping"):
        skipped = page.text()
    assert skipped.strip() == ""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        replaced = page.text(unmapped="replacement")
        glyph_ids = page.text(unmapped="glyph_id")
    assert "�" in replaced
    assert page.text(unmapped="type3_char_code") == replaced
    assert set(replaced) <= {"�", " ", "\n"}
    assert all(0 < ord(char) < 0x10000 for char in glyph_ids.replace(" ", "").replace("\n", ""))


def test_type3_glyphs_without_unicode_have_no_glyph_id() -> None:
    page = pyropdf.open(ASSETS / "pdfium-type3.pdf").pages[0]

    with pytest.warns(pyropdf.PyropdfWarning, match="no Unicode mapping"):
        assert page.text() == ""
    assert page.text(unmapped="glyph_id") == page.text(unmapped="replacement")
    assert set(page.text(unmapped="replacement")) <= {"�", " ", "\n"}


def test_type3_character_code_is_an_opt_in_fallback() -> None:
    page = pyropdf.open(ASSETS / "pdfium-type3.pdf").pages[0]
    with warnings.catch_warnings():
        warnings.simplefilter("error", pyropdf.PyropdfWarning)
        assert page.text(unmapped="type3_char_code").split() == ["A"] * 3
        assert [w.text for w in page.words(unmapped="type3_char_code")] == ["A"] * 3
        assert [c.text for c in page.chars(unmapped="type3_char_code") if not c.is_synthetic] == ["A"] * 3
    with pytest.raises(pyropdf.LimitError) as glyphs:
        page.text(unmapped="type3_char_code", max_glyphs=2)
    assert glyphs.value.code == "text_glyph_count"
    with pytest.raises(pyropdf.LimitError) as size:
        page.text(unmapped="type3_char_code", max_size=2)
    assert size.value.code == "text_size"


def test_type3_character_codes_follow_the_unicode_and_glyph_name_mappings() -> None:
    fixture = Path(__file__).resolve().parents[1] / "evidence/type3-char-codes/type3-cases.pdf"
    # Use an opaque non-Latin name in place of the custom /C1. Equal byte
    # lengths preserve the fixture's cross-reference offsets.
    page = pyropdf.open(fixture.read_bytes().replace(b"/C1", "/Ж".encode())).pages[0]
    # Fonts in order: unknown names, standard names, recognized non-Latin
    # names, valid ToUnicode, null mappings, and a malformed uni name.
    mapped = ["AB", "AB", "あА", "Bあ", "AB", "AB"]
    with pytest.warns(pyropdf.PyropdfWarning, match="no Unicode mapping"):
        assert page.text().splitlines() == ["AB", "あА", "Bあ", "AB", "A"]
    assert page.text(unmapped="type3_char_code").splitlines() == mapped
    assert [w.text for w in page.words(unmapped="type3_char_code")] == mapped
    assert "".join(c.text for c in page.chars(unmapped="type3_char_code")) == "".join(mapped)


def _type3_page(content: str, em: int) -> pyropdf.Page:
    """Open a page whose /T3 is a Type 3 font of ``em`` units, whose codes A and B advance one em."""

    def stream(data: str) -> str:
        return f"<< /Length {len(data)} >>\nstream\n{data}\nendstream"

    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>",
        3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /T3 5 0 R >> >> >>",
        4: stream(content),
        5: (
            f"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 {em} {em}] /FontMatrix [{1 / em} 0 0 {1 / em} 0 0] "
            "/CharProcs << /a 6 0 R /b 6 0 R >> /Encoding << /Type /Encoding /Differences [65 /a /b] >> "
            f"/FirstChar 65 /LastChar 66 /Widths [{em} {em}] /ToUnicode 7 0 R >>"
        ),
        6: stream(f"{em} 0 d0"),
        7: stream(_to_unicode({0x41: "A", 0x42: "B"})),
    }
    return pyropdf.open(build_raw_pdf(objects)).pages[0]


@pytest.mark.parametrize("em", [1000, 2048])
def test_type3_glyphs_read_by_the_advance_that_their_font_gives(em: int) -> None:
    # Half the font size, the estimate for a font that gives no advance, would
    # leave a gap as wide as a space between these glyphs.
    assert _type3_page("BT /T3 24 Tf 20 50 Td <4142> Tj ET", em).text().strip() == "AB"
    assert _type3_page("BT /T3 24 Tf 20 50 Td [<41> -1500 <42>] TJ ET", em).text().strip() == "A B"


def test_document_text_applies_the_options_to_every_page() -> None:
    stream = "BT /F1 20 Tf 72 700 Td (AB) Tj ET"
    document = pyropdf.open(build_text_pdf(stream, stream, to_unicode={0x41: "ﬁ", 0x42: "x"}))

    assert document.text(ligatures=False) == "fix\nfix\n"
    assert document.text(clip=(0, 0, 10, 10)) == ""
    assert document.text([1, -2, 1]) == "ﬁx\nﬁx\nﬁx\n"
    assert document.text([]) == ""


def test_document_text_joins_the_text_of_its_pages() -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (One) Tj ET", "BT /F1 20 Tf 72 700 Td (Two) Tj ET"))

    assert document.text() == "".join(page.text() for page in document.pages) == "One\nTwo\n"
    assert document.text([1, 0]) == "Two\nOne\n"
    with pytest.raises(IndexError, match="out of range"):
        document.text([2])


def test_the_glyph_budget_counts_the_glyphs_of_one_page() -> None:
    data = build_text_pdf("BT /F1 20 Tf 72 700 Td (Seen) Tj ET BT /F1 20 Tf 700 650 Td (Off) Tj ET")
    page = pyropdf.open(data).pages[0]

    # Hidden glyphs that the text leaves out are not collected.
    assert page.text(max_glyphs=4) == "Seen\n"
    assert page.text(clipped=True, max_glyphs=7) == "Seen\nOff\n"
    with pytest.raises(pyropdf.LimitError) as error:
        page.text(clipped=True, max_glyphs=6)
    assert error.value.code == "text_glyph_count"
    assert page.text(max_glyphs=None) == "Seen\n"
    # Each page of a document's text has the whole glyph budget.
    document = pyropdf.open(
        build_text_pdf("BT /F1 20 Tf 72 700 Td (Seen) Tj ET", "BT /F1 20 Tf 72 700 Td (Also) Tj ET")
    )
    assert document.text(max_glyphs=4) == "Seen\nAlso\n"


def test_the_size_budget_counts_the_text_of_the_whole_call() -> None:
    data = build_text_pdf("BT /F1 12 Tf 14 TL 72 700 Td (A long exam-) Tj T* (ple follows) Tj ET")
    document = pyropdf.open(data)
    page = document.pages[0]
    text = "A long example follows\n"

    # The assembled text fits its exact size.
    assert page.text(dehyphenate=True, max_size=len(text)) == text
    with pytest.raises(pyropdf.LimitError) as error:
        page.text(dehyphenate=True, max_size=len(text) - 1)
    assert error.value.code == "text_size"
    assert document.text(dehyphenate=True, ligatures=False, max_size=len(text)) == text
    with pytest.raises(pyropdf.LimitError) as error:
        document.text([0, 0], dehyphenate=True, max_size=2 * len(text) - 1)
    assert error.value.code == "text_size"
    assert document.text([0, 0], dehyphenate=True, max_size=2 * len(text)) == 2 * text


@pytest.mark.parametrize(
    ("options", "error", "message"),
    [
        ({"unmapped": "cid"}, ValueError, "unmapped must be"),
        ({"ruby": "inline"}, ValueError, "ruby must be"),
        ({"ruby": True}, TypeError, "ruby must be"),
        ({"ruby": 1}, TypeError, "ruby must be"),
        ({"actual_text": 1}, TypeError, "actual_text must be True or False"),
        ({"clipped": 1}, TypeError, "clipped must be True or False"),
        ({"ligatures": None}, TypeError, "ligatures must be True or False"),
        ({"whitespace": "no"}, TypeError, "whitespace must be True or False"),
        ({"dehyphenate": 0}, TypeError, "dehyphenate must be True or False"),
        ({"clip": (10, 10, 5, 20)}, ValueError, "clip must be a finite rect"),
        ({"max_glyphs": 0}, ValueError, "max_glyphs"),
        ({"max_size": -1}, ValueError, "max_size"),
    ],
)
def test_text_options_are_validated(options: dict[str, object], error: type[Exception], message: str) -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (A) Tj ET"))

    for view in (document.pages[0].text, document.text):
        with pytest.raises(error, match=message):
            view(**options)  # ty: ignore[invalid-argument-type]


def test_options_survive_a_save_and_reopen() -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 12 Tf 14 TL 72 700 Td (exam-) Tj T* (ple) Tj ET"))

    reopened = pyropdf.open(document.to_bytes())

    assert reopened.pages[0].text(dehyphenate=True) == document.pages[0].text(dehyphenate=True) == "example\n"


def test_text_reads_the_current_version_and_refuses_a_deleted_page() -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (Before) Tj ET"))
    page = document.pages[0]
    assert page.text() == "Before\n"

    page.obj["Contents"].write(b"BT /F1 20 Tf 72 700 Td (After) Tj ET")
    assert page.text() == "After\n"
    document.history.undo()
    assert page.text() == "Before\n"
    del document.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.text()


@pytest.mark.parametrize("name", NAMES)
def test_every_corpus_page_reads_within_the_default_limits(name: str) -> None:
    document = pyropdf.open(Path(ASSETS / name))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pyropdf.PyropdfWarning)
        texts = [page.text() for page in document.pages]
        assert document.text() == "".join(texts)


def test_page_words_returns_frozen_word_records_matching_layout() -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (Alpha Beta) Tj 0 -30 Td (Gamma) Tj ET"))
    page = document.pages[0]

    words = page.words()
    assert [word.text for word in words] == ["Alpha", "Beta", "Gamma"]
    for word in words:
        assert type(word) is pyropdf.Word
        assert type(word.bbox) is pyropdf.Rect
        with pytest.raises(AttributeError):
            word.text = "mutated"  # ty: ignore[invalid-assignment]
    assert [(word.block, word.line, word.index) for word in words] == [(0, 0, 0), (0, 0, 1), (0, 1, 0)]

    layout = page.layout()
    assert " ".join(word.text for word in words) == " ".join(
        line.text for block in layout.blocks for line in block.lines
    )

    del document.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.words()


def test_chunked_text_budgets_refuse_at_exact_glyph_and_size_boundaries() -> None:
    page_content = "BT /F1 12 Tf 72 700 Td (0123456789) Tj ET"
    document = pyropdf.open(build_text_pdf(page_content, page_content))

    assert document.pages[0].text(max_glyphs=10) == "0123456789\n"
    with pytest.raises(pyropdf.LimitError) as glyph_error:
        document.pages[0].text(max_glyphs=9)
    assert glyph_error.value.code == "text_glyph_count"

    assert document.text(max_glyphs=10, max_size=22) == "0123456789\n0123456789\n"
    with pytest.raises(pyropdf.LimitError) as size_error:
        document.text(max_glyphs=10, max_size=21)
    assert size_error.value.code == "text_size"


def _japanese_mixed_pdf(stream: str, *, width: int = 500, height: int = 600) -> bytes:
    """Build a page with horizontal MS-Mincho (/FH), vertical MS-Mincho (/FV), and Helvetica (/F1)."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                f"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 {width} {height}] "
                "/Resources << /Font << /F1 5 0 R /FH 6 0 R /FV 7 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
            4: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            6: "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Mincho /Encoding /90ms-RKSJ-H /DescendantFonts [8 0 R] >>",
            7: "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Mincho /Encoding /90ms-RKSJ-V /DescendantFonts [8 0 R] >>",
            8: (
                "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MS-Mincho "
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >> "
                "/FontDescriptor 9 0 R /DW 1000 >>"
            ),
            9: (
                "<< /Type /FontDescriptor /FontName /MS-Mincho /Flags 6 /FontBBox [0 -137 1000 859] "
                "/ItalicAngle 0 /Ascent 859 /Descent -140 /CapHeight 769 /StemV 78 >>"
            ),
        }
    )


def _cp932_octal(text: str) -> str:
    return "".join(f"\\{byte:03o}" for byte in text.encode("cp932"))


def test_mixed_dan_gumi_and_sidebar_extract_in_natural_reading_order() -> None:
    # Horizontal title, upper 2-column vertical tier, horizontal middle heading,
    # lower vertical tier alongside a horizontal sidebar, and horizontal footer.
    stream = "\n".join(
        [
            "BT /F1 18 Tf 40 550 Td (Title Heading) Tj ET",
            # Upper vertical tier (y=490..440 in PDF, R-to-L: 上段右 then 上段左)
            f"BT /FV 14 Tf 320 490 Td ({_cp932_octal('上段左')}) Tj ET",
            f"BT /FV 14 Tf 342 490 Td ({_cp932_octal('上段右')}) Tj ET",
            # Middle horizontal subheading
            "BT /F1 14 Tf 40 370 Td (Middle Section) Tj ET",
            # Lower band: vertical columns on the right, horizontal caption on the left
            f"BT /FV 14 Tf 320 310 Td ({_cp932_octal('下段左')}) Tj ET",
            f"BT /FV 14 Tf 342 310 Td ({_cp932_octal('下段右')}) Tj ET",
            "BT /F1 11 Tf 40 295 Td (Sidebar Note) Tj ET",
            # Horizontal footer
            "BT /F1 10 Tf 40 80 Td (Footer Line) Tj ET",
        ]
    )
    with pyropdf.open(_japanese_mixed_pdf(stream)) as doc:
        page = doc.pages[0]
        assert page.text().splitlines() == [
            "Title Heading",
            "上段右",
            "上段左",
            "Middle Section",
            "下段右",
            "下段左",
            "Sidebar Note",
            "Footer Line",
        ]
        layout_lines = [line.text for block in page.layout().blocks for line in block.lines]
        assert layout_lines == [
            "Title Heading",
            "上段右",
            "上段左",
            "Middle Section",
            "下段右",
            "下段左",
            "Sidebar Note",
            "Footer Line",
        ]


def test_embedded_canvas_vertical_multicolumn_with_horizontal_bands() -> None:
    font_path = (
        Path(__file__).resolve().parent.parent / "crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf"
    )
    font = pyropdf.Font.from_file(font_path)
    with pyropdf.Document() as doc:
        page = doc.pages.new((400, 360))
        with page.draw() as canvas:
            canvas.text("Title Heading", (40, 40), size=20)
            canvas.textbox((120, 80, 300, 145), "日本語かなのテキスト。", font=font, size=12, direction="vertical")
            canvas.text("Footer Note", (40, 310), size=10)

        assert page.text().splitlines() == ["Title Heading", "日本語かな", "のテキス", "ト。", "Footer Note"]
        blocks = [[line.text for line in block.lines] for block in page.layout().blocks]
        assert blocks == [["Title Heading"], ["日本語かな", "のテキス", "ト。"], ["Footer Note"]]


def test_horizontal_japanese_ruby_modes_and_search() -> None:
    # Base line "東京から大阪へ" at y=400 (12pt -> 12pt per glyph: x=60..144).
    # Two ruby runs directly above 東京 (x=60..84) and 大阪 (x=108..132) with slight overhang.
    stream = "\n".join(
        [
            f"BT /FH 12 Tf 60 400 Td ({_cp932_octal('東京から大阪へ')}) Tj ET",
            f"BT /FH 6 Tf 58 413 Td ({_cp932_octal('とうきょう')}) Tj ET",
            f"BT /FH 6 Tf 106 413 Td ({_cp932_octal('おおさか')}) Tj ET",
        ]
    )
    with pyropdf.open(_japanese_mixed_pdf(stream)) as doc:
        page = doc.pages[0]
        assert page.text() == "東京から大阪へ\n"
        assert doc.text() == "東京から大阪へ\n"
        assert [w.text for w in page.words()] == ["東京から大阪へ"]
        assert len(page.search("東京から大阪へ")) == 1
        assert page.search("とうきょう") == []

        assert page.text(ruby="parenthesize") == "東京(とうきょう)から大阪(おおさか)へ\n"
        assert doc.text(ruby="parenthesize") == "東京(とうきょう)から大阪(おおさか)へ\n"
        assert [line.text for block in page.layout(ruby="parenthesize").blocks for line in block.lines] == [
            "東京(とうきょう)から大阪(おおさか)へ"
        ]
        chars_text = "".join(ch.text for ch in page.chars(ruby="parenthesize"))
        assert chars_text == "東京(とうきょう)から大阪(おおさか)へ"
        assert len(page.search("とうきょう", ruby="parenthesize")) == 1

        kept_lines = page.text(ruby="keep").splitlines()
        assert kept_lines == ["とうきょう おおさか", "東京から大阪へ"]


def test_vertical_japanese_ruby_modes_including_single_kana() -> None:
    # Vertical base column "東京へ行く" at x=200, y=500 downward (14pt).
    # Multi-char ruby "とうきょう" right of 東京, and single-char ruby "い" right of 行.
    stream = "\n".join(
        [
            f"BT /FV 14 Tf 200 500 Td ({_cp932_octal('東京へ行く')}) Tj ET",
            f"BT /FV 6 Tf 215 500 Td ({_cp932_octal('とうきょう')}) Tj ET",
            f"BT /FV 6 Tf 215 458 Td ({_cp932_octal('い')}) Tj ET",
        ]
    )
    with pyropdf.open(_japanese_mixed_pdf(stream)) as doc:
        page = doc.pages[0]
        assert page.text() == "東京へ行く\n"
        assert len(page.search("東京へ行く")) == 1
        assert page.text(ruby="parenthesize") == "東京(とうきょう)へ行(い)く\n"
        assert "とうきょう" in page.text(ruby="keep")
        assert "い" in page.text(ruby="keep")
        assert "東京へ行く" in page.text(ruby="keep")


def test_document_text_accepts_actual_text_opt_out() -> None:
    stream = "/Span << /ActualText (Replaced) >> BDC BT /F1 16 Tf 72 700 Td (RawGlyphs) Tj ET EMC"
    with pyropdf.open(build_text_pdf(stream, stream)) as doc:
        assert doc.text() == "Replaced\nReplaced\n"
        assert doc.text(actual_text=False) == "RawGlyphs\nRawGlyphs\n"
        assert doc.text(actual_text=False) == "".join(page.text(actual_text=False) for page in doc.pages)


def test_parenthesized_ruby_preserves_following_word_space_and_non_superscript() -> None:
    stream = "\n".join(
        [
            f"BT /FH 12 Tf 60 400 Td ({_cp932_octal('東京')}) Tj ET",
            "BT /F1 12 Tf 100 400 Td (Station) Tj ET",
            f"BT /FH 6 Tf 58 413 Td ({_cp932_octal('とうきょう')}) Tj ET",
        ]
    )
    with pyropdf.open(_japanese_mixed_pdf(stream)) as doc:
        page = doc.pages[0]
        assert page.text(ruby="omit") == "東京 Station\n"
        assert page.text(ruby="parenthesize") == "東京(とうきょう) Station\n"
        assert [w.text for w in page.words(ruby="parenthesize")] == ["東京(とうきょう)", "Station"]
        assert len(page.search("東京(とうきょう) Station", ruby="parenthesize")) == 1
        layout = page.layout(ruby="parenthesize")
        ruby_spans = [
            span for block in layout.blocks for line in block.lines for span in line.spans if "とうきょう" in span.text
        ]
        assert len(ruby_spans) == 1
        assert ruby_spans[0].is_superscript is False
