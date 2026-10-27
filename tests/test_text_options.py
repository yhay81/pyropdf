"""The character and box options of the text records: clipping, ligatures, dehyphenation, and exact boxes.

Ported from the character and box tests of pylopdf 036be111
``tests/test_text_options.py``; the options of ``page.text`` are tested in
``test_text.py``, and search belongs to #100.
"""

from __future__ import annotations

import pytest
from test_text import build_text_pdf

import pyropdf


def test_words_inside_a_clip_rectangle() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 20 Tf 72 700 Td (Top left) Tj ET BT /F1 20 Tf 400 700 Td (Top right) Tj ET "
            "BT /F1 20 Tf 72 600 Td (Lower left) Tj ET"
        )
    ).pages[0]

    assert [word.text for word in page.words(clip=(380, 70, 612, 120))] == ["Top", "right"]


def test_spans_that_the_page_or_the_clip_paths_hide_are_clipped() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 20 Tf 72 700 Td (Seen) Tj ET "
            "BT /F1 20 Tf 700 650 Td (Off page) Tj ET "
            "q 0 0 10 10 re W n BT /F1 20 Tf 72 600 Td (Masked) Tj ET Q "
            "q 3 Tr BT /F1 20 Tf 72 500 Td (Invisible) Tj ET Q"
        )
    ).pages[0]

    spans = [span for block in page.layout(clipped=True).blocks for line in block.lines for span in line.spans]
    assert {span.text: span.is_clipped for span in spans} == {
        "Seen": False,
        "Off page": True,
        "Masked": True,
        "Invisible": False,
    }
    assert [word.text for word in page.words()] == ["Seen", "Invisible"]


def test_an_expanded_ligature_is_still_one_char() -> None:
    page = pyropdf.open(
        build_text_pdf("BT /F1 20 Tf 72 700 Td (AB) Tj ET", to_unicode={0x41: "ﬁ", 0x42: "\xa0x"})
    ).pages[0]

    chars = page.layout(chars=True, ligatures=False).blocks[0].lines[0].spans[0].chars

    assert [char.text for char in chars] == ["fi", "\xa0x"]
    assert [char.text for char in page.layout(chars=True).blocks[0].lines[0].spans[0].chars] == ["ﬁ", "\xa0x"]


def test_dehyphenation_joins_a_word_on_its_first_line() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 12 Tf 14 TL 72 700 Td (A long exam-) Tj T* (ple follows) Tj T* (Jean-) Tj T* (Pierre stays) Tj ET"
        )
    ).pages[0]

    words = page.words(dehyphenate=True)
    assert [word.text for word in words] == ["A", "long", "example", "follows", "Jean-", "Pierre", "stays"]
    example = words[2]
    first, second = page.words()[2], page.words()[3]
    assert (example.block, example.line, example.index) == (first.block, first.line, first.index)

    lines = page.layout(dehyphenate=True).blocks[0].lines
    # The lines keep their glyphs; the joined word belongs to the first line.
    assert [line.text for line in lines] == ["A long example", "follows", "Jean-", "Pierre stays"]
    assert [span.text for line in lines for span in line.spans] == [
        "A long exam",
        "ple follows",
        "Jean-",
        "Pierre stays",
    ]
    # The word's box spans "exam", without its hyphen, and "ple" on the next line.
    exam = pyropdf.Rect(first.bbox.x0, first.bbox.y0, lines[0].bbox.x1, first.bbox.y1)
    assert example.bbox == exam | second.bbox


def test_exact_boxes_come_from_the_glyph_outlines() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (Ax) Tj ET")).pages[0]

    loose = page.layout(chars=True).blocks[0].lines[0].spans[0].chars
    exact = page.layout(chars=True, exact_bboxes=True).blocks[0].lines[0].spans[0].chars

    runs = page.content(paths=False, images=False).text_runs
    assert [char.bbox for char in exact] == [glyph.bbox for glyph in runs[0].glyphs]
    for tight, wide in zip(exact, loose, strict=True):
        assert tight.bbox.height < wide.bbox.height
        x0, y0, x1, y1 = wide.bbox
        assert tight.bbox in pyropdf.Rect(x0 - 0.5, y0, x1 + 0.5, y1)
    assert page.words(exact_bboxes=True)[0].bbox == exact[0].bbox | exact[1].bbox
    # The reading order and the words do not depend on the box option.
    assert [word.text for word in page.words(exact_bboxes=True)] == [word.text for word in page.words()]


def test_synthetic_spaces_are_chars_that_join_to_the_span_text() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (A) Tj 20 0 Td (gap) Tj ET")).pages[0]

    (span,) = page.layout(chars=True).blocks[0].lines[0].spans
    assert span.text == "A gap"
    assert "".join(char.text for char in span.chars) == span.text
    space = span.chars[1]
    assert space.is_synthetic
    assert [char.is_synthetic for char in span.chars] == [False, True, False, False, False]
    assert space.bbox.x0 == pytest.approx(span.chars[0].bbox.x1)
    assert space.bbox.x1 == pytest.approx(span.chars[2].bbox.x0)
    assert space.origin.x == pytest.approx(span.chars[0].bbox.x1)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"exact_bboxes": 1}, "exact_bboxes must be True or False"),
        ({"styles": None}, "styles must be True or False"),
        ({"chars": "yes"}, "chars must be True or False"),
    ],
)
def test_record_options_are_validated(options: dict[str, object], message: str) -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (A) Tj ET")).pages[0]

    with pytest.raises(TypeError, match=message):
        page.layout(**options)  # ty: ignore[invalid-argument-type]
