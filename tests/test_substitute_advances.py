"""The advances of fonts that a document does not embed (#656).

hayro draws such a font in a substitute and stretches each glyph to the width
that the PDF gives it. hayro 0.8 stretched the outline, and hayro 0.9 stretches
through the glyph's transform. Text reads the PDF's widths on either, and only
the ink keeps the stretch. ``evidence/phase1-parity`` reads the pages of the
fixture with frozen pylopdf too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from test_content import build_raw_pdf

import pyropdf

#: The pages of PAGES in one file, which the differential tests against
#: frozen pylopdf read too (evidence/phase1-parity).
FIXTURE = Path(__file__).resolve().parent / "assets" / "text" / "nonembedded-latin.pdf"
#: The bytes of a font program that no parser reads.
DAMAGED = "not a font program"
#: The entries that the stream of each key of a font descriptor requires.
DAMAGED_ENTRIES = {
    "FontFile": f"/Length1 {len(DAMAGED)} /Length2 0 /Length3 0",
    "FontFile2": f"/Length1 {len(DAMAGED)}",
    "FontFile3": "/Subtype /OpenType",
}


@dataclass(frozen=True)
class _Page:
    """A page that shows a content stream in ``/F1``, a font that the file does not embed."""

    content: str
    #: The font's subtype and BaseFont.
    subtype: str = "Type1"
    name: str = "Verdana"
    #: The width of every code in ``/Widths``, or in ``/W`` for a Type0 font.
    width: int = 700
    #: The widths of some characters that differ from ``width``.
    widths: dict[str, int] = field(default_factory=dict)
    #: The descriptor's key and the bytes of a program that does not load.
    damaged: str | None = None


#: The text of the pages that read as three words.
HELLO = "BT /F1 20 Tf 20 50 Td (Hello big world) Tj ET"
#: The same text in the two-byte codes of the CMap of a Type0 font's page.
HELLO_UCS2 = f"BT /F1 20 Tf 20 50 Td <{'Hello big world'.encode('utf-16-be').hex().upper()}> Tj ET"
PAGES = (
    # The page of #656, with widths above those of the substitute Helvetica.
    _Page(HELLO),
    # Widths below them, which the stretch shrinks into gaps inside a word.
    _Page(HELLO, subtype="TrueType", name="Georgia", width=300),
    # Neither glyph's transform is a uniform scale, or along the page.
    _Page("BT /F1 20 Tf 150 Tz 20 50 Td (Wide letters) Tj ET"),
    _Page("BT /F1 20 Tf 0 1 -1 0 100 10 Tm (Up) Tj ET"),
    # A Standard 14 font, whose stretch reads the font's metrics, not /Widths.
    _Page(HELLO, name="Helvetica", width=300),
    # A CID font, whose stretch reads /W.
    _Page(HELLO_UCS2, subtype="Type0", name="MS-Gothic"),
    # Glyphs that the widths stretch to nothing keep their stand-in advances.
    _Page("BT /F1 20 Tf 20 50 Td (Hi there) Tj ET", widths={"i": 0}),
    # A stroke completes a fill of the same glyphs.
    _Page("BT /F1 20 Tf 2 Tr 20 50 Td (Hello) Tj ET"),
    # Programs that hayro does not read, which leave it the same substitutes.
    _Page(HELLO, damaged="FontFile"),
    _Page(HELLO, subtype="TrueType", damaged="FontFile2"),
    _Page(HELLO, subtype="TrueType", damaged="FontFile3"),
    _Page(HELLO_UCS2, subtype="Type0", name="MS-Gothic", damaged="FontFile2"),
    # A glyph stretched to nothing keeps the direction of the text around it.
    _Page("BT /F1 20 Tf 0 1 -1 0 100 10 Tm (Hi all) Tj ET", widths={"i": 0}),
)


def _font_objects(first: int, page: _Page) -> dict[int, str]:
    """Return the font of ``page``, numbered from ``first``, with its descriptor and any others."""
    descriptor = (
        f"<< /Type /FontDescriptor /FontName /{page.name} /Flags 32 /FontBBox [0 -200 1000 800] "
        f"/ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 "
        f"{f'/{page.damaged} {first + 3} 0 R' if page.damaged else ''} >>"
    )
    objects = {first + 1: descriptor}
    if page.damaged:
        entries = DAMAGED_ENTRIES[page.damaged]
        objects[first + 3] = f"<< /Length {len(DAMAGED)} {entries} >>\nstream\n{DAMAGED}\nendstream"
    if page.subtype == "Type0":
        return objects | {
            first: (
                f"<< /Type /Font /Subtype /Type0 /BaseFont /{page.name} /Encoding /UniJIS-UCS2-H "
                f"/DescendantFonts [{first + 2} 0 R] >>"
            ),
            first + 2: (
                f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{page.name} "
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 2 >> "
                f"/FontDescriptor {first + 1} 0 R /DW 1000 /W [0 8000 {page.width}] >>"
            ),
        }
    widths = " ".join(str(page.widths.get(chr(code), page.width)) for code in range(32, 127))
    return objects | {
        first: (
            f"<< /Type /Font /Subtype /{page.subtype} /BaseFont /{page.name} /Encoding /WinAnsiEncoding "
            f"/FirstChar 32 /LastChar 126 /Widths [{widths}] /FontDescriptor {first + 1} 0 R >>"
        ),
    }


def nonembedded_latin_pdf(*pages: _Page) -> bytes:
    """Build one 300 by 100 point page for each of ``pages``."""
    objects: dict[int, str | bytes] = {1: "<< /Type /Catalog /Pages 2 0 R >>"}
    kids = []
    for at, page in enumerate(pages):
        number = 3 + 6 * at
        objects[number] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] "
            f"/Resources << /Font << /F1 {number + 2} 0 R >> >> /Contents {number + 1} 0 R >>"
        )
        objects[number + 1] = f"<< /Length {len(page.content)} >>\nstream\n{page.content}\nendstream"
        objects |= _font_objects(number + 2, page)
        kids.append(f"{number} 0 R")
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    return build_raw_pdf(objects)


def _open(index: int = 0) -> pyropdf.Page:
    """Open the page of PAGES at ``index`` on its own."""
    return pyropdf.open(nonembedded_latin_pdf(PAGES[index])).pages[0]


def _runs(page: pyropdf.Page) -> list[pyropdf.TextRun]:
    return [item for item in page.content(paths=False, images=False) if isinstance(item, pyropdf.TextRun)]


def test_the_fixture_holds_the_pages() -> None:
    assert FIXTURE.read_bytes() == nonembedded_latin_pdf(*PAGES)
    with pyropdf.open(FIXTURE) as doc:
        assert len(doc.pages) == len(PAGES)
        assert all(page.text().strip() for page in doc.pages)


@pytest.mark.parametrize(
    ("index", "boxes"),
    [
        (0, {"Hello": (20, 34, 90, 54), "big": (104, 34, 146, 54), "world": (160, 34, 230, 54)}),
        # A narrow font's words stay whole, with the gaps that its spaces leave.
        (1, {"Hello": (20, 34, 50, 54), "big": (56, 34, 74, 54), "world": (80, 34, 110, 54)}),
        (2, {"Wide": (20, 34, 104, 54), "letters": (125, 34, 272, 54)}),
        (3, {"Up": (84, 62, 104, 90)}),
        # Programs that hayro does not read leave the same boxes as naming none.
        (8, {"Hello": (20, 34, 90, 54), "big": (104, 34, 146, 54), "world": (160, 34, 230, 54)}),
        (9, {"Hello": (20, 34, 90, 54), "big": (104, 34, 146, 54), "world": (160, 34, 230, 54)}),
        (10, {"Hello": (20, 34, 90, 54), "big": (104, 34, 146, 54), "world": (160, 34, 230, 54)}),
        # A glyph stretched to nothing stays in its word, along the rotated text.
        (12, {"Hi": (84, 66, 104, 90), "all": (84, 20, 104, 62)}),
    ],
)
def test_words_and_search_read_the_boxes_of_frozen_pylopdf(index: int, boxes: dict[str, tuple[int, ...]]) -> None:
    page = _open(index)
    words = {word.text: tuple(word.bbox) for word in page.words()}
    assert list(words) == list(boxes)
    for text, box in boxes.items():
        assert words[text] == pytest.approx(box)
        assert [tuple(rect) for rect in page.search(text, max_hits=None)] == [pytest.approx(box)]


def test_a_layout_boxes_each_char_with_the_pdf_width() -> None:
    def chars(*, exact_bboxes: bool) -> list[pyropdf.Char]:
        layout = _open().layout(chars=True, exact_bboxes=exact_bboxes)
        return [char for span in layout.blocks[0].lines[0].spans for char in span.chars]

    boxed = chars(exact_bboxes=False)
    assert [char.text for char in boxed] == list("Hello big world")
    assert [char.bbox.x0 for char in boxed] == pytest.approx([20 + 14 * at for at in range(15)])
    assert [char.bbox.x1 for char in boxed] == pytest.approx([34 + 14 * at for at in range(15)])
    # A glyph without ink keeps the box of its advance among exact boxes.
    space = chars(exact_bboxes=True)[5]
    assert (space.text, space.bbox.x0, space.bbox.x1) == (" ", pytest.approx(90), pytest.approx(104))


def test_the_advances_of_text_runs_are_the_pdf_widths() -> None:
    (run,) = _runs(_open())
    assert [glyph.advance for glyph in run.glyphs] == pytest.approx([14.0] * 15)
    # Another writing direction turns the advance, and it keeps its length.
    (rotated,) = _runs(_open(3))
    assert [glyph.advance for glyph in rotated.glyphs] == pytest.approx([14.0] * 2)
    # hayro's horizontal scaling lengthens it, as the width's own scaling does.
    (wide,) = _runs(_open(2))
    assert [glyph.advance for glyph in wide.glyphs] == pytest.approx([21.0] * len(wide.glyphs))


def test_the_ink_keeps_the_stretch_that_a_rendering_draws() -> None:
    page = _open()
    # The boxes of frozen pylopdf, whose outlines hold the stretch.
    assert [tuple(word.bbox) for word in page.words(exact_bboxes=True)] == [
        pytest.approx((21.493, 35.64, 89.119, 50.3), abs=1e-3),
        pytest.approx((105.46, 35.64, 144.565, 54.4), abs=1e-3),
        pytest.approx((160.271, 35.64, 228.565, 50.3), abs=1e-3),
    ]
    (run,) = _runs(page)
    # An l of 222 units in the substitute draws 700 wide: the matrix says so,
    # and the ink box measures the outline through it.
    letter = run.glyphs[2]
    assert letter.text == "l"
    assert letter.matrix.a == pytest.approx(20 * 700 / 222, abs=1e-3)
    assert letter.bbox is not None
    assert (letter.bbox.x0, letter.bbox.x1) == pytest.approx((52.225, 57.775), abs=1e-3)
    assert tuple(run.bbox) == pytest.approx((21.493, 35.64, 228.565, 54.4), abs=1e-3)


def test_a_font_that_widths_stretch_to_nothing_keeps_its_stand_in_advance() -> None:
    page = _open(6)
    assert [(word.text, tuple(word.bbox)) for word in page.words()] == [
        ("Hi", pytest.approx((20, 34, 44, 54))),
        ("there", pytest.approx((48, 34, 118, 54))),
    ]
    (run,) = _runs(page)
    assert run.glyphs[1].text == "i"
    assert run.glyphs[1].advance == 0


def test_actual_text_spans_the_pdf_widths_of_its_glyphs() -> None:
    content = "BT /Span << /ActualText (Whole) >> BDC /F1 20 Tf 20 50 Td (Parts) Tj EMC ET"
    page = pyropdf.open(nonembedded_latin_pdf(_Page(content))).pages[0]
    assert [(word.text, tuple(word.bbox)) for word in page.words()] == [("Whole", pytest.approx((20, 34, 90, 54)))]


def test_a_stroke_that_completes_a_fill_keeps_the_pdf_widths() -> None:
    page = _open(7)
    assert [tuple(word.bbox) for word in page.words()] == [pytest.approx((20, 34, 90, 54))]
    (run,) = _runs(page)
    assert run.render_mode == pyropdf.TextRenderMode.FILL_STROKE
    assert [glyph.advance for glyph in run.glyphs] == pytest.approx([14.0] * 5)
