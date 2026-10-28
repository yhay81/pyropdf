"""The dictionary, JSON, HTML, and XML serializations of a page's layout.

Ported from pylopdf 036be111 ``tests/test_layout_serializations.py`` and,
from ``tests/test_layout.py``, ``test_dict_structure``,
``test_span_dictionaries_hold_pymupdf_color_alpha_and_char_flags``, and
``test_dict_reports_transformed_baseline_direction``. The font-metric cases
use an existing embedded TrueType program, a non-embedded CJK fixture,
and an invisible OCR font, without configuring Japanese fallback fonts.
"""

from __future__ import annotations

import html.parser
import json
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest
from corpus import ASSETS
from test_content import build_raw_pdf
from test_extraction_records import build_painted_text_pdf
from test_search import build_cjk_pdf
from test_text import build_text_pdf

import pyropdf


class _Collector(html.parser.HTMLParser):
    """Collect the elements, styles, and text of an HTML fragment."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, str]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs).get("style") or ""))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _xml(text: str) -> ET.Element:
    """Parse pyropdf's own XML output, which holds no entity declarations."""
    return ET.fromstring(text)  # noqa: S314 - the input is pyropdf's own output.


def _parse(fragment: str) -> _Collector:
    collector = _Collector()
    collector.feed(fragment)
    collector.close()
    return collector


def test_dict_structure() -> None:
    page = pyropdf.open(ASSETS / "usrguide.pdf").pages[0]

    d = page.layout().to_dict()

    assert d["width"] == pytest.approx(page.rect.width, abs=1.0)
    assert d["height"] == pytest.approx(page.rect.height, abs=1.0)
    block = d["blocks"][0]
    assert block["type"] == 0
    assert len(block["bbox"]) == 4
    line = block["lines"][0]
    assert line["wmode"] == 0
    assert line["dir"] == pytest.approx((1.0, 0.0))
    span = line["spans"][0]
    assert set(span) == {
        "bbox",
        "origin",
        "size",
        "font",
        "flags",
        "char_flags",
        "color",
        "alpha",
        "ascender",
        "descender",
        "text",
    }
    assert span["size"] > 1.0
    all_text = "".join(span["text"] for b in d["blocks"] for line in b["lines"] for span in line["spans"])
    assert "authors" in all_text


def test_span_dictionaries_hold_pymupdf_color_alpha_and_char_flags() -> None:
    page = pyropdf.open(build_painted_text_pdf()).pages[0]

    spans = {
        span["text"]: span
        for block in page.layout(clipped=True).to_dict()["blocks"]
        for line in block["lines"]
        for span in line["spans"]
    }

    assert (spans["Red"]["color"], spans["Red"]["alpha"], spans["Red"]["char_flags"]) == (0xFF0000, 255, 16)
    assert (spans["Half"]["color"], spans["Half"]["alpha"]) == (0x0000FF, 128)
    assert spans["Outline"]["char_flags"] == 32
    assert spans["Both"]["char_flags"] == 48
    assert spans["Hidden"]["char_flags"] == 0
    assert spans["Clipped"]["char_flags"] == 16 | 64
    assert (spans["Red"]["ascender"], spans["Red"]["descender"]) == (0.8, -0.2)


def test_span_flags_mark_a_superscript_and_char_flags_its_decorations() -> None:
    page = pyropdf.open(
        build_text_pdf(
            "BT /F1 20 Tf 72 700 Td (x) Tj 10 8 Td /F1 10 Tf (2) Tj ET 72 698 m 90 698 l S 72 704 m 90 704 l S"
        )
    ).pages[0]

    spans = [span for line in page.layout(styles=True).to_dict()["blocks"][0]["lines"] for span in line["spans"]]

    assert [(span["text"], span["flags"] & 1) for span in spans] == [("x", 0), ("2", 1)]
    assert spans[0]["char_flags"] & 3 == 3


def test_spans_take_the_ascender_and_descender_of_an_opentype_program() -> None:
    page = pyropdf.open(ASSETS / "nics-background-checks-2015-11.pdf").pages[0]
    layout = page.layout(chars=True)
    span = next(s for b in layout.blocks for line in b.lines for s in line.spans if s.font == "ArialMT")
    # The embedded ArialMT's head has 2048 units per em, and its hhea
    # ascender and descender are 1854 and -434, independently read from it.
    ascender, descender = 1854 / 2048, -434 / 2048
    assert (span.ascender, span.descender) == (ascender, descender)
    assert ascender - descender > 1
    char = span.chars[0]
    assert char.bbox.y0 == pytest.approx(char.origin.y - span.size * ascender)
    assert char.bbox.y1 == pytest.approx(char.origin.y - span.size * descender)
    serialized = next(
        s for b in layout.to_dict()["blocks"] for line in b["lines"] for s in line["spans"] if s["font"] == "ArialMT"
    )
    assert (serialized["ascender"], serialized["descender"]) == (ascender, descender)


def test_a_substitute_for_a_font_that_is_not_embedded_lends_no_metrics() -> None:
    # This CID font has no FontFile. Hayro supplies a substitute for its
    # glyphs, while the extraction metrics retain the documented defaults.
    page = pyropdf.open(build_cjk_pdf()).pages[0]
    (span,) = page.layout().blocks[0].lines[0].spans
    assert span.font == "MS-Mincho"
    assert (span.ascender, span.descender) == (0.8, -0.2)
    serialized = page.layout().to_dict()["blocks"][0]["lines"][0]["spans"][0]
    assert (serialized["ascender"], serialized["descender"]) == (0.8, -0.2)


def test_dict_reports_transformed_baseline_direction() -> None:
    """Assemble a rotated baseline without mistaking it for vertical WMode."""
    stream = "BT /F1 24 Tf 0 1 -1 0 100 100 Tm (Vertical) Tj ET"
    pdf = build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 300 300] /Resources << /Font << /F1 3 0 R >> >> >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
        5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    })  # fmt: skip
    page = pyropdf.open(pdf).pages[0]
    assert page.text() == "Vertical\n"
    assert [word.text for word in page.words()] == ["Vertical"]
    assert len(page.search("Vertical")) == 1

    line = page.layout().to_dict()["blocks"][0]["lines"][0]
    direction_x, direction_y = line["dir"]
    assert direction_x == pytest.approx(0.0, abs=1e-9)
    assert abs(direction_y) == pytest.approx(1.0)
    assert line["wmode"] == 0
    assert line["spans"][0]["text"] == "Vertical"


def test_an_ocr_substitute_font_lends_no_metrics() -> None:
    """The OCR layer's font is not embedded, so its spans take the default metrics."""
    source = pyropdf.Document()
    source.pages.new((360, 200)).apply_ocr(
        words=[pyropdf.OcrWord(text="Hello 1", bbox=pyropdf.Rect(20, 40, 200, 70), confidence=0.9)]
    )
    page = pyropdf.open(source.to_bytes()).pages[0]

    (span,) = page.layout().blocks[0].lines[0].spans

    assert span.font == "PyroOCR-Gothic"
    assert (span.ascender, span.descender) == (0.8, -0.2)
    # The search box is the box that the OCR word was placed in.
    assert page.search("Hello 1") == [pytest.approx(pyropdf.Rect(20, 40, 200, 70), abs=0.01)]


def test_html_places_one_paragraph_per_line_with_its_spans() -> None:
    page = pyropdf.open(build_painted_text_pdf()).pages[0]
    layout = page.layout()

    parsed = _parse(layout.to_html())

    (page_tag, page_style), *rest = parsed.tags
    assert page_tag == "div"
    assert "width:612pt;height:792pt" in page_style
    paragraphs = [style for tag, style in rest if tag == "p"]
    spans = [style for tag, style in rest if tag == "span"]
    lines = [line for block in layout.blocks for line in block.lines]
    assert len(paragraphs) == len(lines)
    assert len(spans) == sum(len(line.spans) for line in lines)
    assert "".join(parsed.text).split() == page.text().split()
    by_text = dict(zip((span.text for line in lines for span in line.spans), spans, strict=True))
    assert "color:#ff0000" in by_text["Red"]
    assert "opacity:0.502" in by_text["Half"]
    assert "opacity:0" in by_text["Hidden"]
    first = lines[0]
    assert f"left:{first.bbox.x0:g}pt;top:{first.bbox.y0:g}pt" in paragraphs[0]


def test_html_and_xml_escape_text() -> None:
    page = pyropdf.open(build_text_pdf(r"BT /F1 20 Tf 72 700 Td (<a> & \"b\") Tj ET")).pages[0]

    fragment = page.layout().to_html()
    root = _xml(page.layout(chars=True).to_xml())

    assert "&lt;a&gt; &amp; " in fragment
    assert "".join(_parse(fragment).text).strip() == '<a> & "b"'
    assert "".join(char.get("c", "") for char in root.iter("char")) == '<a> & "b"'


@pytest.mark.parametrize(
    ("font", "quoted"),
    [
        ('A";position:fixed;/*', '"A\\22 ;position:fixed;/*"'),
        ("A\\B\nC\rD\fE", '"A\\5c B\\a C\\d D\\c E"'),
    ],
)
def test_html_quotes_font_names_inside_css_strings(font: str, quoted: str) -> None:
    layout = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (Text) Tj ET")).pages[0].layout()
    (block,) = layout.blocks
    (line,) = block.lines
    (span,) = line.spans
    changed = replace(layout, blocks=(replace(block, lines=(replace(line, spans=(replace(span, font=font),)),)),))

    styles = [style for tag, style in _parse(changed.to_html()).tags if tag == "span"]

    # HTML entities have already been decoded. Quotes, backslashes, and
    # line breaks must still be escaped for the CSS parser at this point.
    assert len(styles) == 1
    assert styles[0].startswith(f"font-family:{quoted},sans-serif;font-size:20pt;")


def test_html_turns_rotated_lines_along_their_baseline() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 20 Tf 0 1 -1 0 300 100 Tm (Up) Tj ET")).pages[0]

    (line,) = page.layout().blocks[0].lines
    (_, (_, paragraph), _) = _parse(page.layout().to_html()).tags

    assert "transform:rotate(-90deg)" in paragraph
    assert f"left:{line.quad.ul.x:g}pt;top:{line.quad.ul.y:g}pt" in paragraph


def test_xml_lists_blocks_lines_fonts_and_chars() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (A) Tj 20 0 Td (gap) Tj ET")).pages[0]
    layout = page.layout(chars=True)

    root = _xml(layout.to_xml())

    assert root.tag == "page"
    assert (root.get("width"), root.get("height")) == ("612", "792")
    (block,) = root.findall("block")
    (line,) = block.findall("line")
    assert (line.get("wmode"), line.get("dir")) == ("0", "1 0")
    (font,) = line.findall("font")
    assert (font.get("size"), font.get("color")) == ("20", "#000000")
    chars = font.findall("char")
    assert [char.get("c") for char in chars] == list("A gap")
    assert [char.get("synthetic") for char in chars] == [None, "1", None, None, None]
    span = layout.blocks[0].lines[0].spans[0]
    assert chars[0].get("bbox") == " ".join(f"{value:.3f}".rstrip("0").rstrip(".") for value in span.chars[0].bbox)
    # Without characters, each font holds its span's text.
    (plain,) = _xml(page.layout().to_xml()).iter("font")
    assert plain.text == "A gap"


def test_a_layout_serializes_as_json_through_its_dictionary() -> None:
    layout = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (Json) Tj ET")).pages[0].layout(chars=True)

    data = json.loads(json.dumps(layout.to_dict()))

    assert data["blocks"][0]["lines"][0]["spans"][0]["chars"][0]["c"] == "J"


def test_an_empty_page_serializes_to_empty_containers() -> None:
    layout = pyropdf.Document().pages.new((100, 50)).layout()

    assert layout.to_html() == '<div style="position:relative;width:100pt;height:50pt">\n</div>\n'
    assert _xml(layout.to_xml()).attrib == {"width": "100", "height": "50"}
    assert layout.to_dict() == {"width": 100, "height": 50, "blocks": []}
