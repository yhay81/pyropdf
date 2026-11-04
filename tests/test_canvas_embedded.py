"""Canvas text in an embedded OpenType face.

Ported from pylopdf ebb01d3c ``tests/test_font.py`` (the drawing cases)
and ``tests/test_font_objects.py`` (the Type0 case), with the subset faces
of ``crates/pyropdf-engine/src/fonts/testdata``. Intended differences: the
texts fit the subset faces; pyropdf writes one Type0 font per face per
document instead of one per drawing; and the program stays whole until
subsetting at save (#482), which compacts it to the drawn glyphs.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

import pyropdf
from pyropdf import _fonts

FONTS = Path(__file__).resolve().parent.parent / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
JAPANESE = FONTS / "NotoSansJP-subset.otf"
HEBREW = FONTS / "NotoSansHebrew-subset.ttf"
ARABIC = FONTS / "NotoSansArabic-Variable-subset.ttf"
VARIABLE_LATIN = FONTS / "variable-latin.ttf"
VARIABLE_COLOUR = FONTS / "variable-colour.ttf"
LINE = "AVA office 日本語、かな。"
#: The refusal of a colour glyph with no outline, for one character.
NO_OUTLINE = r"cannot draw U\+{:04X} \('{}'\): its glyph is a colour glyph with no outline"


@pytest.fixture(scope="module")
def japanese() -> pyropdf.Font:
    return pyropdf.Font.from_file(JAPANESE)


@pytest.fixture(scope="module")
def hebrew() -> pyropdf.Font:
    return pyropdf.Font.from_file(HEBREW)


@pytest.fixture(scope="module")
def arabic() -> pyropdf.Font:
    return pyropdf.Font.from_file(ARABIC)


@pytest.fixture(scope="module")
def arabic_bold() -> pyropdf.Font:
    return pyropdf.Font.from_file(ARABIC, weight=700)


@pytest.fixture(
    params=["colr0.ttf", "colr1.ttf", "sbix.ttf", "cbdt.ttf", "svg.ttf", "svg-cff.otf"],
    ids=["colr0", "colr1", "sbix", "cbdt", "svg", "svg-cff"],
)
def colour(request: pytest.FixtureRequest) -> pyropdf.Font:
    """Return a face of ``colour_faces.py``, whose docstring says what its glyphs are."""
    return pyropdf.Font.from_file(FONTS / f"colour-{request.param}")


def _new_page(width: float = 400, height: float = 200) -> tuple[pyropdf.Document, pyropdf.Page]:
    document = pyropdf.Document()
    return document, document.pages.new((width, height))


def _glyphs(page: pyropdf.Page) -> list[pyropdf.Glyph]:
    return [glyph for run in page.content(paths=False, images=False).text_runs for glyph in run.glyphs]


def _descriptor_programs(document: pyropdf.Document) -> set[int]:
    found = set()
    for number in document.objects:
        value = document.objects[number]
        if isinstance(value, pyropdf.Dictionary) and value.get("Type") == "FontDescriptor":
            for key in ("FontFile2", "FontFile3"):
                program = value.get(key)
                if isinstance(program, pyropdf.Stream) and program.xref is not None:
                    found.add(program.xref)
    return found


def test_text_width_equals_the_drawn_advance(japanese: pyropdf.Font) -> None:
    document, page = _new_page()
    x, y, size = 30.25, 80.0, 13.5

    with page.draw() as canvas:
        canvas.text(LINE, (x, y), font=japanese, size=size)

    glyphs = _glyphs(page)
    texts = [glyph.text or "" for glyph in glyphs]
    assert "".join(texts) == LINE
    assert glyphs[0].origin.x == pytest.approx(x, abs=1e-3)
    # A prefix ends where the glyph after it begins, kerning included.
    prefix = "AVA office 日本語、"
    offsets = [0]
    for text in texts:
        offsets.append(offsets[-1] + len(text))
    following = glyphs[offsets.index(len(prefix))]
    assert following.origin.x == pytest.approx(x + japanese.text_width(prefix, size), abs=1e-2)
    last = glyphs[-1]
    assert last.advance is not None
    assert last.origin.x + last.advance == pytest.approx(x + japanese.text_width(LINE, size), abs=1e-2)
    reopened = pyropdf.open(document.to_bytes())
    assert reopened.pages[0].text().strip() == LINE


def test_text_width_places_a_right_aligned_textbox_line(japanese: pyropdf.Font) -> None:
    _document, page = _new_page()
    rect = (20, 20, 380, 120)

    with page.draw() as canvas:
        canvas.textbox(rect, "日本語AVA", font=japanese, size=16, align="right")

    glyphs = _glyphs(page)
    last = glyphs[-1]
    assert last.advance is not None
    assert glyphs[0].origin.x == pytest.approx(rect[2] - japanese.text_width("日本語AVA", 16), abs=1e-2)
    assert last.origin.x + last.advance == pytest.approx(rect[2], abs=1e-2)


def test_a_justified_textbox_widens_the_spaces_of_wrapped_lines(japanese: pyropdf.Font) -> None:
    _document, page = _new_page()
    rect = (20, 20, 160, 180)

    with page.draw() as canvas:
        fit = canvas.textbox(rect, "AVA office fit it AVA tot oil fit", font=japanese, size=12, align="justify")

    assert fit.spare_height > 0
    first = page.layout().blocks[0].lines[0]
    assert first.bbox.x0 == pytest.approx(rect[0], abs=1e-2)
    assert first.bbox.x1 == pytest.approx(rect[2], abs=0.5)


@pytest.mark.parametrize(
    ("font", "text"),
    [("japanese", LINE), ("hebrew", "שָׁלוֹם עולם"), ("arabic", "مرحبا"), ("arabic", "عالم مرحبا")],
)
def test_drawn_text_extracts_in_logical_order(font: str, text: str, request: pytest.FixtureRequest) -> None:
    face = request.getfixturevalue(font)
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text(text, (20, 60), font=face, size=14)

    reopened = pyropdf.open(document.to_bytes())
    # A dot that the Arabic face draws apart from its letter maps to no text.
    with pytest.warns(pyropdf.PyropdfWarning) if font == "arabic" else _no_warning():
        assert reopened.pages[0].text().strip() == text


class _no_warning:  # noqa: N801 - a context manager used like a function.
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


def test_the_font_is_a_type0_font_with_identity_h(hebrew: pyropdf.Font) -> None:
    source, page = _new_page()
    with page.draw() as canvas:
        canvas.text("שלום", (72, 100), font=hebrew, size=20)
    document = pyropdf.open(source.to_bytes())

    (font,) = document.pages[0].fonts()
    assert (font.subtype, font.encoding, font.program) == ("Type0", "Identity-H", "ttf")
    program = document.font_program(font)
    assert program is not None
    # The save subsets the program to the drawn glyphs and tags its name.
    assert len(program.data) < len(hebrew.program() or b"")
    assert font.subset
    assert font.base_font == f"{font.base_font[:6]}+{hebrew._font.postscript_name}"  # noqa: SLF001


def test_one_face_on_100_pages_embeds_one_program(japanese: pyropdf.Font, hebrew: pyropdf.Font) -> None:
    document = pyropdf.Document()
    for index in range(100):
        page = document.pages.new((300, 100))
        with page.draw() as canvas:
            canvas.text(f"AVA {index % 3 + 1} 日本語", (10, 40), font=japanese, size=12)
            canvas.text("שלום", (10, 80), font=hebrew, size=12)

    reopened = pyropdf.open(document.to_bytes())
    assert len(_descriptor_programs(reopened)) == 2
    fonts = {font.xref for page in reopened.pages for font in page.fonts()}
    assert len(fonts) == 2
    assert reopened.pages[99].text().split("\n")[0] == "AVA 1 日本語"


# krilla's saved KiB with dedupe=True, for 100 stamps of the same and of
# varying text: evidence/embedded-text/README.md, through pylopdf 0.16.0.
KRILLA_100_STAMPS_KIB = {
    "hebrew": (29.6, 82.8),
    "latin": (32.3, 154.5),
    "japanese": (29.9, 68.3),
}
STAMPS = {"hebrew": "שלום עולם", "latin": "Hello, office ffi", "japanese": "日本語のテキスト。"}


@pytest.mark.parametrize("varying", [False, True], ids=["same", "varying"])
@pytest.mark.parametrize("face", ["hebrew", "latin", "japanese"])
def test_100_stamps_save_no_larger_than_krilla(face: str, *, varying: bool, request: pytest.FixtureRequest) -> None:
    # The save subsets the face to its drawn glyphs, so the subset test faces
    # stand in for the full faces that krilla embedded.
    font = request.getfixturevalue("hebrew" if face == "hebrew" else "japanese")
    letters = STAMPS[face].replace(" ", "")
    document = pyropdf.Document()
    for index in range(100):
        text = letters[index % len(letters)] + letters[index * 7 % len(letters)] if varying else STAMPS[face]
        with document.pages.new((612, 792)).draw() as canvas:
            canvas.text(text, (72, 72), font=font, size=24)

    saved = document.to_bytes(dedupe=True, prune=True)
    assert len(saved) / 1024 <= KRILLA_100_STAMPS_KIB[face][varying]
    assert len(_descriptor_programs(pyropdf.open(saved))) == 1


def test_one_drawing_is_one_undo_step(japanese: pyropdf.Font) -> None:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("AVA", (20, 40), font=japanese, size=12)
    with page.draw() as canvas:
        canvas.text("日本語", (20, 80), font=japanese, size=12)
    assert page.text().split() == ["AVA", "日本語"]

    document.history.undo()
    page = document.pages[0]
    assert page.text().split() == ["AVA"]
    (font,) = page.fonts()
    descendant = font._font["DescendantFonts"][0]  # noqa: SLF001
    assert len(_fonts._widths(descendant, japanese.glyph_count)) == 2  # noqa: SLF001 - "A" and "V".
    document.history.undo()
    assert document.pages[0].text().strip() == ""
    assert _descriptor_programs(document) == set()


def test_two_weights_of_one_face_make_two_programs_that_read_back() -> None:
    document, page = _new_page()
    bold = pyropdf.Font.from_file(ARABIC, weight=700)
    light = pyropdf.Font.from_file(ARABIC, weight=200)
    with page.draw() as canvas:
        canvas.text("office", (20, 40), font=bold, size=14)
        canvas.text("tilt", (20, 80), font=light, size=14)
    assert len(_descriptor_programs(document)) == 2
    assert page.text().split() == ["office", "tilt"]

    reopened = pyropdf.open(document.to_bytes())
    assert len(_descriptor_programs(reopened)) == 2
    assert reopened.pages[0].text().split() == ["office", "tilt"]
    names = {font._font["BaseFont"] for font in reopened.pages[0].fonts()}  # noqa: SLF001
    assert len(names) == 2
    assert all(name.endswith(("_700wght", "_200wght")) for name in names), names


def test_the_same_weight_shares_a_program() -> None:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("office", (20, 40), font=pyropdf.Font.from_file(ARABIC, weight=700), size=14)
        canvas.text("tilt", (20, 80), font=pyropdf.Font.from_file(ARABIC, weight=700), size=14)
    assert len(_descriptor_programs(document)) == 1


def test_the_descriptor_describes_the_instance() -> None:
    _document, page = _new_page()
    font = pyropdf.Font.from_file(VARIABLE_LATIN, weight=900)
    with page.draw() as canvas:
        canvas.text("AV", (20, 40), font=font, size=14)
    (used,) = page.fonts()
    descendant = used._font["DescendantFonts"][0]  # noqa: SLF001
    descriptor = descendant["FontDescriptor"]
    assert used._font["BaseFont"] == descriptor["FontName"] == "VariableLatin_900wght"  # noqa: SLF001
    assert list(descriptor["FontBBox"]) == [100, 0, 1000, 700]
    assert descriptor["Ascent"] == 850
    assert descriptor["CapHeight"] == 800
    assert page.text().strip() == "AV"
    # The kerning of the instance reaches the drawing: V begins 800 units on.
    glyphs = _glyphs(page)
    assert glyphs[1].origin.x - glyphs[0].origin.x == pytest.approx(14 * 0.8, abs=1e-3)


def test_a_save_subsets_an_instance() -> None:
    document, page = _new_page()
    font = pyropdf.Font.from_file(ARABIC, weight=700)
    with page.draw() as canvas:
        canvas.text("tilt", (20, 40), font=font, size=14)
    whole = cast("bytes", font.program())
    data = document.to_bytes()

    reopened = pyropdf.open(data)
    (used,) = reopened.pages[0].fonts()
    program = reopened.font_program(used)
    assert program is not None
    assert len(program.data) < len(whole) / 3
    assert reopened.pages[0].text().strip() == "tilt"
    # A save leaves the page's drawing as it was before.
    assert bytes(reopened.pages[0].pixmap(dpi=72)) == bytes(page.pixmap(dpi=72))
    assert bytes(page.pixmap(dpi=72)) != bytes(_drawn_at_default("tilt").pixmap(dpi=72))


def _drawn_at_default(text: str) -> pyropdf.Page:
    _document, page = _new_page()
    with page.draw() as canvas:
        canvas.text(text, (20, 40), font=pyropdf.Font.from_file(ARABIC), size=14)
    return page


def test_undo_removes_a_drawing_of_an_instance_and_its_glyphs() -> None:
    font = pyropdf.Font.from_file(ARABIC, weight=700)
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("tilt", (20, 40), font=font, size=12)
    with page.draw() as canvas:
        canvas.text("office", (20, 80), font=font, size=12)
    assert page.text().split() == ["tilt", "office"]
    (used,) = page.fonts()
    assert len(_fonts._widths(used._font["DescendantFonts"][0], font.glyph_count)) == 7  # noqa: SLF001

    document.history.undo()
    page = document.pages[0]
    assert page.text().split() == ["tilt"]
    (used,) = page.fonts()
    assert len(_fonts._widths(used._font["DescendantFonts"][0], font.glyph_count)) == 3  # noqa: SLF001 - t, i, l.
    document.history.undo()
    assert document.pages[0].text().strip() == ""
    assert _descriptor_programs(document) == set()


def test_a_colour_glyph_with_no_outline_refuses_at_an_instance() -> None:
    font = pyropdf.Font.from_file(VARIABLE_COLOUR, weight=700)
    document, page = _new_page()
    with pytest.raises(pyropdf.PdfError, match=NO_OUTLINE.format(ord("A"), "A")), page.draw() as canvas:
        canvas.text("A", (20, 40), font=font, size=12)
    assert _descriptor_programs(document) == set()
    with page.draw() as canvas:
        canvas.text("B", (20, 40), font=font, size=12)
    assert page.text().strip() == "B"


def test_drawing_after_a_save_keeps_the_earlier_content(japanese: pyropdf.Font, hebrew: pyropdf.Font) -> None:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("AVA 日本", (20, 40), font=japanese, size=12)
    first = document.to_bytes()
    with page.draw() as canvas:
        canvas.text("office かな", (20, 80), font=japanese, size=12)
        canvas.text("שלום", (20, 120), font=hebrew, size=12)
    second = pyropdf.open(document.to_bytes())

    assert pyropdf.open(first).pages[0].text().split() == ["AVA", "日本"]
    assert second.pages[0].text().split() == ["AVA", "日本", "office", "かな", "שלום"]
    assert len({font.xref for font in second.pages[0].fonts()}) == 2


def test_an_exception_discards_the_drawing_and_its_glyphs(japanese: pyropdf.Font) -> None:
    document, page = _new_page()

    def fail() -> None:
        with page.draw() as canvas:
            canvas.text("AVA", (20, 40), font=japanese, size=12)
            raise RuntimeError

    with pytest.raises(RuntimeError):
        fail()
    assert _descriptor_programs(document) == set()
    with page.draw() as canvas:
        canvas.text("日本", (20, 40), font=japanese, size=12)
    assert page.text().strip() == "日本"


def test_drawing_names_the_missing_character(japanese: pyropdf.Font) -> None:
    _document, page = _new_page()
    with page.draw() as canvas, pytest.raises(pyropdf.PdfError, match=r"no glyph for U\+1F980 \('🦀'\)"):
        canvas.text("🦀", (10, 40), font=japanese)


@pytest.mark.parametrize("character", ["A", "D"], ids=["empty", "two-points"])
def test_a_colour_glyph_with_no_outline_refuses_and_writes_nothing(colour: pyropdf.Font, character: str) -> None:
    document, page = _new_page()
    steps = document.history.steps

    def draw() -> None:
        with page.draw() as canvas:
            canvas.text("C", (20, 40), font=colour, size=12)
            canvas.text(f"C\nC{character}", (20, 80), font=colour, size=12)

    with pytest.raises(pyropdf.PdfError, match=NO_OUTLINE.format(ord(character), character)):
        draw()

    assert document.history.steps == steps
    assert page.text().strip() == ""
    assert page.fonts() == []
    assert _descriptor_programs(document) == set()


def test_a_refusal_caught_in_the_block_leaves_the_rest_of_the_canvas(colour: pyropdf.Font) -> None:
    document, page = _new_page()

    with page.draw() as canvas:
        canvas.text("C", (20, 40), font=colour, size=12)
        with pytest.raises(pyropdf.PdfError, match="colour glyph with no outline"):
            canvas.text("A", (20, 80), font=colour, size=12)
        canvas.text("B", (20, 120), font=colour, size=12)

    assert page.text().split() == ["C", "B"]
    assert pyropdf.open(document.to_bytes()).pages[0].text().split() == ["C", "B"]


def test_a_space_or_empty_glyph_that_no_colour_table_covers_draws(colour: pyropdf.Font) -> None:
    document, page = _new_page()

    with page.draw() as canvas:
        canvas.text("C C", (20, 40), font=colour, size=12)
        canvas.text("E", (20, 80), font=colour, size=12)

    assert page.text().split("\n")[:2] == ["C C", "E"]
    assert pyropdf.open(document.to_bytes()).pages[0].text().split("\n")[:2] == ["C C", "E"]


def test_a_space_that_a_colour_table_covers_asks_for_no_ink(colour: pyropdf.Font) -> None:
    _document, page = _new_page()

    with page.draw() as canvas:
        canvas.text("C\u00a0C", (20, 40), font=colour, size=12)

    assert page.text().strip().replace("\u00a0", " ") == "C C"


def test_a_colour_glyph_with_a_fallback_outline_draws_it(colour: pyropdf.Font) -> None:
    document, page = _new_page(100, 100)
    assert page.pixmap(dpi=72).color_count() == 1

    with page.draw() as canvas:
        canvas.text("B", (10, 60), font=colour, size=48)

    assert page.text().strip() == "B"
    assert page.pixmap(dpi=72).color_count() > 1
    saved = pyropdf.open(document.to_bytes()).pages[0]
    assert saved.text().strip() == "B"
    assert bytes(saved.pixmap(dpi=72)) == bytes(page.pixmap(dpi=72))


def test_a_textbox_refuses_as_text_does(colour: pyropdf.Font) -> None:
    document, page = _new_page()
    steps = document.history.steps

    with pytest.raises(pyropdf.PdfError, match=NO_OUTLINE.format(0x41, "A")), page.draw() as canvas:
        canvas.textbox((20, 20, 380, 120), "C A", font=colour, size=12, align="justify")

    assert document.history.steps == steps
    assert page.text().strip() == ""
    assert _descriptor_programs(document) == set()


def test_a_textbox_draws_a_fallback_outline(colour: pyropdf.Font) -> None:
    _document, page = _new_page()

    with page.draw() as canvas:
        canvas.textbox((20, 20, 380, 120), "B C", font=colour, size=12)

    assert page.text().strip() == "B C"


def test_a_textbox_that_does_not_fit_draws_nothing_to_refuse(colour: pyropdf.Font) -> None:
    _document, page = _new_page()

    with page.draw() as canvas:
        fit = canvas.textbox((20, 20, 380, 21), "A", font=colour, size=12)

    assert fit.spare_height < 0
    assert page.text().strip() == ""


def test_a_colour_glyph_with_no_outline_still_measures(colour: pyropdf.Font) -> None:
    assert colour.has_glyph("A")
    assert colour.text_width("A", 10) == pytest.approx(10)
    assert colour.char_widths("A", 10) == pytest.approx((10,))
    assert colour.text_height("A", 10, width=100) > 0
    box = colour.char_bbox("A")
    assert (box.x0, box.y0, box.x1, box.y1) == (0, 0, 0, 0)


def test_a_monochrome_bitmap_table_is_not_a_colour_table() -> None:
    face = pyropdf.Font.from_file(FONTS / "colour-eblc.ttf")
    _document, page = _new_page()

    with page.draw() as canvas:
        canvas.text("A", (20, 40), font=face, size=12)

    assert page.text().strip() == "A"


def test_a_face_with_no_outlines_refuses_at_load() -> None:
    with pytest.raises(pyropdf.PdfError, match="the face has no outlines"):
        pyropdf.Font.from_file(FONTS / "colour-bitmap-only.ttf")


def test_stroked_and_rotated_embedded_text_extracts(japanese: pyropdf.Font) -> None:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("日本語", (100, 100), font=japanese, size=12, fill=None, stroke=pyropdf.Color(1, 0, 0), rotate=90)
    assert pyropdf.open(document.to_bytes()).pages[0].text().strip() == "日本語"


def test_one_font_draws_on_several_documents_and_threads(japanese: pyropdf.Font) -> None:
    def draw(index: int) -> tuple[float, str]:
        _document, page = _new_page()
        text = f"日本{index % 3 + 1}"
        with page.draw() as canvas:
            canvas.text(text, (10, 40), font=japanese, size=12)
        return japanese.text_width(text, 12), page.text().strip()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(draw, range(8)))

    assert [text for _, text in results] == [f"日本{index % 3 + 1}" for index in range(8)]


def test_hayro_renders_the_glyphs(hebrew: pyropdf.Font) -> None:
    _document, page = _new_page(200, 60)
    assert page.pixmap(dpi=72).color_count() == 1
    with page.draw() as canvas:
        canvas.text("שלום", (20, 40), font=hebrew, size=24)
    assert page.pixmap(dpi=72).color_count((20, 15, 90, 45)) > 1


def _ink_box(gray: bytes, width: int) -> tuple[int, int, int, int]:
    """Return the box of the samples darker than mid-gray, in pixels."""
    dark = [at for at, sample in enumerate(gray) if sample < 128]
    xs = [at % width for at in dark]
    ys = [at // width for at in dark]
    return min(xs), min(ys), max(xs), max(ys)


def _poppler_gray(data: bytes) -> tuple[bytes, int]:
    """Return poppler's 72 dpi gray render of the first page, and its width."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "page.pdf"
        path.write_bytes(data)
        pgm = subprocess.run(  # noqa: S603 - a fixed tool on a file of the test's own.
            [shutil.which("pdftoppm") or "pdftoppm", "-gray", "-r", "72", "-singlefile", str(path)],
            check=True,
            capture_output=True,
        ).stdout
    # A binary PGM: "P5", the width and height, the maximum, then the samples.
    header = pgm.split(maxsplit=4)
    width, height = int(header[1]), int(header[2])
    return pgm[len(pgm) - width * height :], width


def test_a_non_identity_cid_keyed_cff_draws_the_same_before_and_after_a_save(japanese: pyropdf.Font) -> None:
    # The fixture's CFF keeps the source's CIDs, which are not its glyph IDs;
    # loading makes them the identity, and the save's compacted program maps
    # its new glyphs back to those CIDs through its charset.
    document, page = _new_page(300, 120)
    with page.draw() as canvas:
        canvas.text("日本語、かな。", (20, 40), font=japanese, size=24)
    before = bytes(page.pixmap(dpi=72))
    first = document.to_bytes()

    saved = pyropdf.open(first)
    (font,) = saved.pages[0].fonts()
    assert font.program == "cff"
    assert saved.pages[0].text().strip() == "日本語、かな。"
    assert bytes(saved.pages[0].pixmap(dpi=72)) == before

    with page.draw() as canvas:
        canvas.text("AVA 123", (20, 90), font=japanese, size=24)
    again = pyropdf.open(document.to_bytes())
    assert again.pages[0].text().split() == ["日本語、かな。", "AVA", "123"]
    assert bytes(again.pages[0].pixmap(dpi=72)) == bytes(page.pixmap(dpi=72))
    assert bytes(pyropdf.open(first).pages[0].pixmap(dpi=72)) == before


@pytest.mark.parametrize("size", [9, 12, 16, 24])
def test_a_cff_face_hints_the_same_before_and_after_a_save(japanese: pyropdf.Font, size: int) -> None:
    # The save compacts the program, which keeps its hints and hint values.
    document, page = _new_page(300, 120)
    with page.draw() as canvas:
        canvas.text("日本語、かな。", (20, 40), font=japanese, size=size)
        canvas.text("AVA 123", (20, 90), font=japanese, size=size)
    before = bytes(page.pixmap(dpi=72, hinting=True))
    assert before != bytes(page.pixmap(dpi=72))

    saved = pyropdf.open(document.to_bytes())
    (font,) = saved.pages[0].fonts()
    assert font.program == "cff"
    assert bytes(saved.pages[0].pixmap(dpi=72, hinting=True)) == before


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler's pdftoppm is not installed")
@pytest.mark.parametrize(
    ("font", "text"),
    [("japanese", "日本語、かな。"), ("hebrew", "שָׁלוֹם עולם"), ("arabic_bold", "office")],
)
def test_poppler_draws_the_saved_subset_where_hayro_does(font: str, text: str, request: pytest.FixtureRequest) -> None:
    document, page = _new_page(300, 80)
    with page.draw() as canvas:
        canvas.text(text, (20, 40), font=request.getfixturevalue(font), size=24)
    data = document.to_bytes()

    poppler, width = _poppler_gray(data)
    hayro = pyropdf.open(data).pages[0].pixmap(dpi=72).to_gray()
    assert hayro.width == width
    poppler_box = _ink_box(poppler, width)
    hayro_box = _ink_box(bytes(hayro), width)
    assert all(abs(a - b) <= 2 for a, b in zip(poppler_box, hayro_box, strict=True)), (poppler_box, hayro_box)
