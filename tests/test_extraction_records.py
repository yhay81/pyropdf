"""The records of ``Page.words`` and ``Page.layout``: words, blocks, lines, spans, and chars.

Ported from pylopdf 036be111 ``tests/test_extraction_records.py`` and the
record tests of ``tests/test_layout.py``, lines 61 to 320. ``to_dict``
belongs to #100; the page content and links have their own tests; and the
tests that draw with ``page.draw`` or OCR are not ported.
"""

from __future__ import annotations

import dataclasses

import pytest
from corpus import ASSETS
from test_content import build_raw_pdf
from test_text import build_text_pdf

import pyropdf


@pytest.fixture
def usrguide() -> pyropdf.Page:
    return pyropdf.open(ASSETS / "usrguide.pdf").pages[0]


def _hello() -> pyropdf.Page:
    return pyropdf.open(build_text_pdf("BT /F1 12 Tf 72 700 Td (Hello world) Tj ET")).pages[0]


def test_words_are_word_records() -> None:
    words = _hello().words()

    assert [word.text for word in words] == ["Hello", "world"]
    first = words[0]
    assert isinstance(first, pyropdf.Word)
    assert not isinstance(first, tuple)
    assert (first.block, first.line, first.index) == (0, 0, 0)
    assert words[1].index == 1
    assert isinstance(first.bbox, pyropdf.Rect)
    assert first.bbox.x0 < first.bbox.x1 < words[1].bbox.x0
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.text = "Changed"  # ty: ignore[invalid-assignment]


def test_layout_records_nest_blocks_lines_spans_and_chars() -> None:
    page = _hello()

    layout = page.layout()
    (block,) = layout.blocks
    (line,) = block.lines

    assert (layout.width, layout.height) == (612, 792)
    assert isinstance(block, pyropdf.Block)
    assert block.text == line.text == "Hello world"
    assert line.direction == pyropdf.Point(1, 0)
    assert not line.is_vertical
    assert "".join(span.text for span in line.spans).replace(" ", "") == "Helloworld"
    assert all(span.chars == () for span in line.spans)
    chars = [char for span in page.layout(chars=True).blocks[0].lines[0].spans for char in span.chars]
    assert "".join(char.text for char in chars) == "Hello world"
    assert all(isinstance(char, pyropdf.Char) for char in chars)


def test_words_have_positions_and_numbering(usrguide: pyropdf.Page) -> None:
    words = usrguide.words()
    assert len(words) > 100
    page_rect = usrguide.rect
    for word in words:
        x0, y0, x1, y1 = word.bbox
        assert x0 < x1
        assert y0 < y1
        assert -1 <= x0 <= page_rect.x1 + 1
        assert -1 <= y0 <= page_rect.y1 + 1
        assert word.text
        assert word.block >= 0
        assert word.line >= 0
        assert word.index >= 0
    assert "authors" in [word.text for word in words]


def test_words_numbering_is_consistent(usrguide: pyropdf.Page) -> None:
    """Keep word numbers consecutive per line and line numbers monotonic."""
    seen: dict[tuple[int, int], int] = {}
    for word in usrguide.words():
        key = (word.block, word.line)
        expected = seen.get(key, 0)
        assert word.index == expected
        seen[key] = expected + 1


def test_blocks_structure(usrguide: pyropdf.Page) -> None:
    layout = usrguide.layout()
    blocks = layout.blocks
    assert 1 < len(blocks) < 30
    assert all(isinstance(block, pyropdf.Block) for block in blocks)
    assert all(block.text == "\n".join(line.text for line in block.lines) for block in blocks)
    joined = "\n".join(b.text for b in blocks)
    assert "for authors" in joined
    assert "Introduction" in joined
    assert layout.width == pytest.approx(usrguide.rect.width, abs=1.0)
    assert layout.height == pytest.approx(usrguide.rect.height, abs=1.0)
    line = blocks[0].lines[0]
    assert not line.is_vertical
    assert line.direction == pytest.approx((1.0, 0.0))
    assert line.spans[0].size > 1.0


def test_text_records_hold_checked_geometry_and_compare_as_constructed(usrguide: pyropdf.Page) -> None:
    """The fast record paths give the values that the public constructors would."""
    layout = usrguide.layout(chars=True)
    spans = [span for block in layout.blocks for line in block.lines for span in line.spans]
    chars = [char for span in spans for char in span.chars]
    boxes = [word.bbox for word in usrguide.words()] + [span.bbox for span in spans] + [char.bbox for char in chars]
    points = [span.origin for span in spans] + [char.origin for char in chars]

    assert chars
    assert {type(box) for box in boxes} == {pyropdf.Rect}
    assert {type(point) for point in points} == {pyropdf.Point}
    assert boxes == [pyropdf.Rect(*box) for box in boxes]
    assert points == [pyropdf.Point(*point) for point in points]
    rebuilt = [
        pyropdf.Char(text=char.text, bbox=char.bbox, origin=char.origin, is_synthetic=char.is_synthetic)
        for char in chars
    ]
    assert rebuilt == chars
    assert {hash(char) for char in rebuilt} == {hash(char) for char in chars}
    assert repr(rebuilt[0]) == repr(chars[0])
    assert [span.text for span in usrguide.layout().blocks[0].lines[0].spans] == [
        span.text for span in layout.blocks[0].lines[0].spans
    ]


def build_painted_text_pdf() -> bytes:
    """Build one page of text lines that are painted differently, one per line."""
    stream = (
        "BT /F1 20 Tf 1 0 0 rg 72 700 Td (Red) Tj ET "
        "q /Half gs BT /F1 20 Tf 0 0 1 rg 72 650 Td (Half) Tj ET Q "
        "q BT /F1 20 Tf 1 Tr 0 0.5 0 RG 72 600 Td (Outline) Tj ET Q "
        "q BT /F1 20 Tf 2 Tr 0 0 0 rg 1 0 0 RG 72 550 Td (Both) Tj ET Q "
        "q BT /F1 20 Tf 3 Tr 72 500 Td (Hidden) Tj ET Q "
        "q 0 0 10 10 re W n BT /F1 20 Tf 72 450 Td (Clipped) Tj ET Q "
        "q 60 390 200 40 re W n BT /F1 20 Tf 72 400 Td (Inside) Tj ET Q "
        "BT /F1 20 Tf /Pattern cs /Shade scn 72 350 Td (Pattern) Tj ET"
    )
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
                "/Resources << /Font << /F1 5 0 R >> /ExtGState << /Half << /ca 0.5 >> >> "
                "/Pattern << /Shade 6 0 R >> >> >>"
            ),
            4: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            6: (
                "<< /PatternType 2 /Shading << /ShadingType 2 /ColorSpace /DeviceRGB /Coords [72 350 300 350] "
                "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >> >>"
            ),
        }
    )


def test_spans_report_their_paint_render_mode_and_clipping() -> None:
    page = pyropdf.open(build_painted_text_pdf()).pages[0]

    spans = {
        span.text: span for block in page.layout(clipped=True).blocks for line in block.lines for span in line.spans
    }

    assert spans["Red"].color == pyropdf.Color(1.0, 0.0, 0.0, 1.0)
    # hayro keeps colors and opacities as bytes.
    assert spans["Half"].color is not None
    assert spans["Half"].color.rgb == (0.0, 0.0, 1.0)
    assert spans["Half"].color.alpha == pytest.approx(128 / 255)
    assert spans["Outline"].color is not None
    assert spans["Outline"].color.rgb == pytest.approx((0.0, 128 / 255, 0.0))
    assert spans["Both"].color == pyropdf.Color(0.0, 0.0, 0.0, 1.0)
    assert spans["Hidden"].color is None
    assert spans["Pattern"].color is None
    assert {name: span.render_mode for name, span in spans.items()} == {
        "Red": pyropdf.TextRenderMode.FILL,
        "Half": pyropdf.TextRenderMode.FILL,
        "Outline": pyropdf.TextRenderMode.STROKE,
        "Both": pyropdf.TextRenderMode.FILL_STROKE,
        "Hidden": pyropdf.TextRenderMode.INVISIBLE,
        "Clipped": pyropdf.TextRenderMode.FILL,
        "Inside": pyropdf.TextRenderMode.FILL,
        "Pattern": pyropdf.TextRenderMode.FILL,
    }
    assert {name for name, span in spans.items() if span.is_clipped} == {"Clipped"}
    # The render modes agree with what page.content() reports.
    runs = {run.text: run for run in page.content(paths=False, images=False).text_runs}
    assert {name: runs[name].render_mode for name in spans} == {name: span.render_mode for name, span in spans.items()}
    # By default the clipped span is left out.
    assert "Clipped" not in page.text()
    # A standard font that is not embedded has no program, so no metrics or flags of one.
    red = spans["Red"]
    assert (red.ascender, red.descender) == (0.8, -0.2)


def test_spans_split_where_the_paint_changes() -> None:
    stream = "BT /F1 20 Tf 72 700 Td 1 0 0 rg (Re) Tj 0 0 1 rg (d) Tj 1 Tr (x) Tj ET"
    page = pyropdf.open(build_text_pdf(stream)).pages[0]

    (line,) = page.layout().blocks[0].lines

    assert [(span.text, span.color, span.render_mode.value) for span in line.spans] == [
        ("Re", pyropdf.Color(1.0, 0.0, 0.0), "fill"),
        ("d", pyropdf.Color(0.0, 0.0, 1.0), "fill"),
        ("x", pyropdf.Color(0.0, 0.0, 0.0), "stroke"),
    ]
    assert line.text == "Redx"
    assert [word.text for word in page.words()] == ["Redx"]


def test_glyph_boxes_span_at_least_one_em() -> None:
    page = pyropdf.open(build_painted_text_pdf()).pages[0]

    char = page.layout(chars=True).blocks[0].lines[0].spans[0].chars[0]

    assert char.bbox.y1 - char.bbox.y0 == pytest.approx(20)
    assert char.bbox.y0 == pytest.approx(char.origin.y - 16)


def test_the_records_count_against_the_text_budget() -> None:
    page = _hello()
    size = len(page.text().encode())

    # The layout's strings count on top of the glyphs, so a budget that
    # holds the text refuses the layout, and the refusal leaves nothing.
    assert page.text(max_size=size) == "Hello world\n"
    with pytest.raises(pyropdf.LimitError) as error:
        page.layout(chars=True, max_size=size)
    assert error.value.code == "text_size"
    with pytest.raises(pyropdf.LimitError) as error:
        page.words(max_glyphs=10)
    assert error.value.code == "text_glyph_count"
    assert [word.text for word in page.words(max_glyphs=11)] == ["Hello", "world"]
    assert page.layout(chars=True, max_size=None).blocks[0].text == "Hello world"


def test_records_read_the_current_version_and_refuse_a_deleted_page() -> None:
    document = pyropdf.open(build_text_pdf("BT /F1 12 Tf 72 700 Td (Old) Tj ET"))
    page = document.pages[0]
    assert [word.text for word in page.words()] == ["Old"]

    page.obj["Contents"].write(b"BT /F1 12 Tf 72 700 Td (New) Tj ET")
    assert [word.text for word in page.words()] == ["New"]
    document.history.undo()
    assert page.layout().blocks[0].text == "Old"
    del document.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.layout()
