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

import pytest

import pyropdf
from pyropdf import _fonts

FONTS = Path(__file__).resolve().parent.parent / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
JAPANESE = FONTS / "NotoSansJP-subset.otf"
HEBREW = FONTS / "NotoSansHebrew-subset.ttf"
ARABIC = FONTS / "NotoSansArabic-Variable-subset.ttf"
LINE = "AVA office 日本語、かな。"


@pytest.fixture(scope="module")
def japanese() -> pyropdf.Font:
    return pyropdf.Font.from_file(JAPANESE)


@pytest.fixture(scope="module")
def hebrew() -> pyropdf.Font:
    return pyropdf.Font.from_file(HEBREW)


@pytest.fixture(scope="module")
def arabic() -> pyropdf.Font:
    return pyropdf.Font.from_file(ARABIC)


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


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler's pdftoppm is not installed")
@pytest.mark.parametrize(("font", "text"), [("japanese", "日本語、かな。"), ("hebrew", "שָׁלוֹם עולם")])
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
