"""Canvas text in an embedded OpenType face.

Ported from pylopdf ebb01d3c ``tests/test_font.py`` (the drawing cases)
and ``tests/test_font_objects.py`` (the Type0 case), with the subset faces
of ``crates/pyropdf-engine/src/fonts/testdata``. Intended differences: the
texts fit the subset faces; pyropdf writes one Type0 font per face per
document instead of one per drawing; and the program stays whole until
subsetting at save (#482), which compacts it to the drawn glyphs.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

import pyropdf
from pyropdf import FreeText, _fonts

if TYPE_CHECKING:
    from collections.abc import Callable

FONTS = Path(__file__).resolve().parent.parent / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
JAPANESE = FONTS / "NotoSansJP-subset.otf"
HEBREW = FONTS / "NotoSansHebrew-subset.ttf"
ARABIC = FONTS / "NotoSansArabic-Variable-subset.ttf"
VARIABLE_LATIN = FONTS / "variable-latin.ttf"
VARIABLE_COLOUR = FONTS / "variable-colour.ttf"
LINE = "AVA office 日本語、かな。"
ARABIC_WORD = "مرحبا"
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
    params=["colr0.ttf", "colr1.ttf", "sbix-jpeg.ttf", "cbdt-raw.ttf", "svg.ttf", "svg-cff.otf"],
    ids=["colr0", "colr1", "sbix-jpeg", "cbdt-raw", "svg", "svg-cff"],
)
def colour(request: pytest.FixtureRequest) -> pyropdf.Font:
    """Return a face of ``colour_faces.py`` whose colour glyphs a canvas cannot paint, as its docstring says."""
    return pyropdf.Font.from_file(FONTS / f"colour-{request.param}")


def _new_page(width: float = 400, height: float = 200) -> tuple[pyropdf.Document, pyropdf.Page]:
    document = pyropdf.Document()
    return document, document.pages.new((width, height))


def _glyphs(page: pyropdf.Page) -> list[pyropdf.Glyph]:
    return [glyph for run in page.content(paths=False, images=False).text_runs for glyph in run.glyphs]


def _text_without_warning(page: pyropdf.Page) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", pyropdf.PyropdfWarning)
        return page.text()


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
    assert _text_without_warning(reopened.pages[0]).strip() == text


def _arabic_word(arabic: pyropdf.Font, *, at: tuple[float, float] = (20, 60), size: float = 14) -> pyropdf.Document:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text(ARABIC_WORD, at, font=arabic, size=size)
    return document


def test_a_saved_arabic_word_has_no_unmapped_glyph(arabic: pyropdf.Font) -> None:
    # The shaper splits the letter beh into a base and a dot in one cluster.
    shaped = arabic._font.shape(ARABIC_WORD)  # noqa: SLF001
    dot = next(glyph[0] for glyph in shaped if glyph[5] == glyph[6])
    reopened = pyropdf.open(_arabic_word(arabic).to_bytes())
    page = reopened.pages[0]

    assert _text_without_warning(page).strip() == ARABIC_WORD
    assert page.text(unmapped="replacement").strip() == ARABIC_WORD
    type0 = next(
        value
        for number in reopened.objects
        if isinstance(value := reopened.objects[number], pyropdf.Dictionary) and value.get("Subtype") == "Type0"
    )
    mapped = _fonts._mapped(type0["ToUnicode"], arabic.glyph_count)  # noqa: SLF001
    drawn = _fonts._widths(type0["DescendantFonts"][0], arabic.glyph_count)  # noqa: SLF001
    assert dot in drawn
    assert set(drawn) <= set(mapped)
    assert mapped[dot] == _fonts._NO_TEXT  # noqa: SLF001


def test_a_cluster_is_one_actual_text_and_its_glyphs_stay_where_the_shaper_put_them(arabic: pyropdf.Font) -> None:
    x, y, size = 20.25, 60.0, 14.0
    document = _arabic_word(arabic, at=(x, y), size=size)
    page = document.pages[0]

    scale = size / arabic._font.units_per_em  # noqa: SLF001
    pen = x
    expected = {}
    for glyph, advance, _y_advance, x_offset, y_offset, *_ in arabic._font.shape(ARABIC_WORD):  # noqa: SLF001
        expected[glyph] = (pen + x_offset * scale, y - y_offset * scale)
        pen += advance * scale
    glyphs = _glyphs(page)
    assert {glyph.glyph_id for glyph in glyphs} == set(expected)
    for glyph in glyphs:
        assert glyph.glyph_id is not None
        assert (glyph.origin.x, glyph.origin.y) == pytest.approx(expected[glyph.glyph_id], abs=1e-2)

    # Only the letter that the face splits needs ActualText, and its glyphs lie inside.
    operators = [operation.operator for operation in page.operations]
    spans = [operation.operands[1]["ActualText"].text for operation in page.operations if operation.operator == "BDC"]
    assert spans == ["\u0628"]
    inside = page.operations[operators.index("BDC") : operators.index("EMC")]
    shown = [
        item
        for operation in inside
        if operation.operator == "TJ"
        for item in operation.operands[0]
        if isinstance(item, pyropdf.String)
    ]
    assert len(shown) == 2


def test_the_word_of_a_cluster_covers_its_glyphs(arabic: pyropdf.Font) -> None:
    x, size = 20.0, 14.0
    page = pyropdf.open(_arabic_word(arabic, at=(x, 60), size=size).to_bytes()).pages[0]

    words = page.words()
    assert [word.text for word in words] == [ARABIC_WORD]
    assert words[0].bbox.x0 == pytest.approx(x, abs=1e-2)
    assert words[0].bbox.x1 == pytest.approx(x + arabic.text_width(ARABIC_WORD, size), abs=1e-2)


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext is not installed")
def test_pdftotext_reads_a_cluster_as_its_text(arabic: pyropdf.Font) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "page.pdf"
        path.write_bytes(_arabic_word(arabic).to_bytes())
        read = subprocess.run(  # noqa: S603 - a fixed tool on a file of the test's own.
            [shutil.which("pdftotext") or "pdftotext", "-enc", "UTF-8", str(path), "-"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
    # Xpdf marks the direction of a run with embedding controls.
    assert read.translate(dict.fromkeys(map(ord, "\u202a\u202b\u202c"))).strip() == ARABIC_WORD


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


def _map_numbers(data: bytes) -> list[int]:
    """Return the object number of each ``/CIDToGIDMap`` stream that saved ``data`` holds."""
    return [int(number) for number in re.findall(rb"/CIDToGIDMap (\d+) 0 R", data)]


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"incremental": True},
        {"cross_reference": "stream"},
        {"object_streams": True},
        {"prune": True},
        {"renumber": True},
        {"linearize": True},
    ],
    ids=["full", "incremental", "cross_reference", "object_streams", "prune", "renumber", "linearize"],
)
@pytest.mark.parametrize(("face", "text"), [("hebrew", "שלום"), ("japanese", LINE)])
def test_saving_again_with_no_edit_writes_the_same_bytes(
    face: str, text: str, options: dict[str, Any], request: pytest.FixtureRequest
) -> None:
    source, _ = _new_page()
    document = pyropdf.open(source.to_bytes())
    with document.pages[0].draw() as canvas:
        canvas.text(text, (72, 100), font=request.getfixturevalue(face), size=20)

    first = document.to_bytes(**options)

    assert document.to_bytes(**options) == first
    assert document.to_bytes(**options) == first


def test_the_map_of_each_truetype_face_keeps_its_number_through_saves(
    hebrew: pyropdf.Font, arabic: pyropdf.Font
) -> None:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("שלום", (72, 100), font=hebrew, size=20)
        canvas.text(ARABIC_WORD, (72, 150), font=arabic, size=20)

    numbers = _map_numbers(document.to_bytes())

    assert len(set(numbers)) == 2
    assert _map_numbers(document.to_bytes()) == numbers


def test_undo_and_redo_return_to_the_bytes_of_a_saved_truetype_face(hebrew: pyropdf.Font) -> None:
    document, page = _new_page()
    before = document.to_bytes()
    with page.draw() as canvas:
        canvas.text("שלום", (72, 100), font=hebrew, size=20)
    drawn = document.to_bytes()

    document.history.undo()
    assert document.to_bytes() == before
    document.history.redo()
    assert document.to_bytes() == drawn


def test_an_object_added_after_a_save_does_not_take_the_number_of_the_map(hebrew: pyropdf.Font) -> None:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("שלום", (72, 100), font=hebrew, size=20)
    (saved,) = _map_numbers(document.to_bytes())

    added = document.objects.add(12345)
    data = document.to_bytes()

    (number,) = _map_numbers(data)
    assert added != saved
    assert added != number
    reopened = pyropdf.open(data)
    assert reopened.objects[added] == 12345
    assert _text_without_warning(reopened.pages[0]).strip() == "שלום"


def test_saves_in_an_open_transaction_draw_what_it_wrote(hebrew: pyropdf.Font) -> None:
    plain, page = _new_page()
    with page.draw() as canvas:
        canvas.text("שלום", (72, 100), font=hebrew, size=20)
    expected = bytes(pyropdf.open(plain.to_bytes()).pages[0].pixmap(dpi=72))

    document, page = _new_page()
    with document.transaction():
        # The face's own objects lie above the numbers of the version that
        # the transaction started from, where the map must not land.
        with page.draw() as canvas:
            canvas.text("שלום", (72, 100), font=hebrew, size=20)
        inside = document.to_bytes()
    committed = document.to_bytes()

    assert document.to_bytes() == committed
    for data in (inside, committed):
        assert bytes(pyropdf.open(data).pages[0].pixmap(dpi=72)) == expected


def test_saves_repeat_in_a_transaction_that_has_written_nothing(hebrew: pyropdf.Font) -> None:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("שלום", (72, 100), font=hebrew, size=20)
    first = document.to_bytes()

    with document.transaction():
        assert document.to_bytes() == first
        assert document.to_bytes() == first


def _size(data: bytes) -> int:
    """Return the ``/Size`` of the trailer of saved ``data``."""
    (size,) = re.findall(rb"/Size (\d+)", data)
    return int(size)


def _drop_an_added_object(document: pyropdf.Document) -> int:
    """Add an object in a transaction that raises, and return its number."""
    added = []

    def drop() -> None:
        with document.transaction():
            added.append(document.objects.add({"Dropped": True}))
            raise RuntimeError

    with pytest.raises(RuntimeError):
        drop()
    return added[0]


def _undo_an_added_object(document: pyropdf.Document) -> int:
    """Add an object, undo the addition, and return its number."""
    added = document.objects.add({"Undone": True})
    document.history.undo()
    return added


@pytest.mark.parametrize("discard", [_drop_an_added_object, _undo_an_added_object], ids=["dropped", "undone"])
@pytest.mark.parametrize(("face", "text"), [("hebrew", "שלום"), ("japanese", LINE)])
def test_a_discarded_edit_between_two_saves_leaves_the_bytes_unchanged(
    face: str, text: str, discard: Callable[[pyropdf.Document], int], request: pytest.FixtureRequest
) -> None:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text(text, (72, 100), font=request.getfixturevalue(face), size=20)
    first = document.to_bytes()

    discarded = discard(document)

    assert document.to_bytes() == first
    added = document.objects.add({"Added": True})
    assert added not in {discarded, *_map_numbers(first)}
    assert added >= _size(first)


@pytest.mark.parametrize(("face", "text"), [("hebrew", "שלום"), ("japanese", LINE)])
def test_saves_repeat_in_a_transaction_that_has_written(face: str, text: str, request: pytest.FixtureRequest) -> None:
    document, page = _new_page()
    with document.transaction():
        with page.draw() as canvas:
            canvas.text(text, (72, 100), font=request.getfixturevalue(face), size=20)
        first = document.to_bytes()

        assert document.to_bytes() == first
        assert document.to_bytes() == first
        added = document.objects.add({"Added": True})
        assert added not in _map_numbers(first)
        assert added >= _size(first)


def test_a_reference_to_a_discarded_object_stays_missing_in_a_save(hebrew: pyropdf.Font) -> None:
    document, page = _new_page()
    discarded = []

    def discard() -> None:
        with document.transaction():
            discarded.append(document.objects[document.objects.add({"A": 1})])
            raise RuntimeError

    with document.transaction():
        with page.draw() as canvas:
            canvas.text("שלום", (72, 100), font=hebrew, size=20)
        with pytest.raises(RuntimeError):
            discard()
        holder = document.objects.add({"Kept": discarded[0]})
        data = document.to_bytes()

    assert pyropdf.open(data).objects[holder]["Kept"] is None


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
    [("japanese", "日本語、かな。"), ("hebrew", "שָׁלוֹם עולם"), ("arabic", ARABIC_WORD), ("arabic_bold", "office")],
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


#: Where each glyph of a ``bitmaps-*`` face's ``"RG"`` paints at 24 points from (20, 50) on a 200 by 60 page.
RED_AT = (32, 38)
GREEN_AT = (56, 38)
RED = (255, 0, 0)
GREEN = (0, 255, 0)
MANY = "".join(chr(0xE000 + index) for index in range(300))


@pytest.fixture(params=["sbix", "cbdt"])
def bitmaps(request: pytest.FixtureRequest) -> pyropdf.Font:
    """Return a ``bitmaps-*`` face of ``colour_faces.py``, whose docstring says what its glyphs are."""
    return pyropdf.Font.from_file(FONTS / f"colour-bitmaps-{request.param}.ttf")


@pytest.fixture(params=["sbix-only", "cbdt-only"])
def bitmaps_only(request: pytest.FixtureRequest) -> pyropdf.Font:
    """Return a ``bitmaps-*`` face with no outlines."""
    return pyropdf.Font.from_file(FONTS / f"colour-bitmaps-{request.param}.ttf")


def _objects(document: pyropdf.Document) -> list[object]:
    return [document.objects[number] for number in document.objects]


def _type3_fonts(document: pyropdf.Document) -> list[pyropdf.Dictionary]:
    return [
        value
        for value in _objects(document)
        if isinstance(value, pyropdf.Dictionary) and value.get("Subtype") == "Type3"
    ]


def _glyph_images(document: pyropdf.Document) -> list[pyropdf.Stream]:
    """Return the images that the Type 3 fonts' resources name."""
    found = {}
    for font in _type3_fonts(document):
        xobjects = font["Resources"].get("XObject", {})
        for name in xobjects:
            found[xobjects[name].xref] = xobjects[name]
    return list(found.values())


def _procedures(document: pyropdf.Document) -> dict[str, bytes]:
    """Return every Type 3 glyph's procedure, by its name."""
    found = {}
    for font in _type3_fonts(document):
        procedures = font["CharProcs"]
        for name in procedures:
            assert str(name) not in found
            found[str(name)] = procedures[name].read()
    return found


def _poppler_rgb(data: bytes) -> tuple[bytes, int]:
    """Return poppler's 72 dpi colour render of the first page, and its width."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "page.pdf"
        path.write_bytes(data)
        ppm = subprocess.run(  # noqa: S603 - a fixed tool on a file of the test's own.
            [shutil.which("pdftoppm") or "pdftoppm", "-r", "72", "-singlefile", str(path)],
            check=True,
            capture_output=True,
        ).stdout
    # A binary PPM: "P6", the width and height, the maximum, then the samples.
    header = ppm.split(maxsplit=4)
    width, height = int(header[1]), int(header[2])
    return ppm[len(ppm) - 3 * width * height :], width


def test_bitmap_glyphs_draw_in_colour_in_hayro(bitmaps: pyropdf.Font) -> None:
    document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("RG", (20, 50), font=bitmaps, size=24)

    for drawn in (page, pyropdf.open(document.to_bytes()).pages[0]):
        pixmap = drawn.pixmap(dpi=72)
        assert pixmap.pixel(*RED_AT)[:3] == RED
        assert pixmap.pixel(*GREEN_AT)[:3] == GREEN
        assert pixmap.pixel(100, 38)[:3] == (255, 255, 255)


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler's pdftoppm is not installed")
def test_poppler_draws_bitmap_glyphs_in_colour(bitmaps: pyropdf.Font) -> None:
    document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("RG", (20, 50), font=bitmaps, size=24)

    samples, width = _poppler_rgb(document.to_bytes())
    for (x, y), colour in ((RED_AT, RED), (GREEN_AT, GREEN)):
        at = 3 * (y * width + x)
        assert tuple(samples[at : at + 3]) == pytest.approx(colour, abs=2)


def test_bitmap_text_reads_back_as_text_runs(bitmaps: pyropdf.Font) -> None:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("RG O R", (20, 80), font=bitmaps, size=24)

    for drawn in (page, pyropdf.open(document.to_bytes()).pages[0]):
        assert drawn.text().strip() == "RG O R"
        runs = drawn.content(paths=False, images=False).text_runs
        assert {run.font.is_type3 for run in runs} == {True, False}
        assert "".join(glyph.text or "" for run in runs for glyph in run.glyphs) == "RG O R"


@pytest.mark.parametrize("table", ["sbix", "cbdt"])
def test_an_instance_paints_its_bitmap_glyphs(table: str) -> None:
    face = pyropdf.Font.from_file(FONTS / f"variable-bitmaps-{table}.ttf", weight=700)
    assert face.weight == 700
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("RG O R", (20, 80), font=face, size=24)

    for drawn in (page, pyropdf.open(document.to_bytes()).pages[0]):
        assert drawn.text().strip() == "RG O R"
        runs = drawn.content(paths=False, images=False).text_runs
        assert {run.font.is_type3 for run in runs} == {True, False}


def test_an_em_other_than_1000_keeps_the_widths_in_the_units_of_the_font_matrix() -> None:
    face = pyropdf.Font.from_file(FONTS / "colour-bitmaps-sbix-2048.ttf")
    document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("RRG", (20, 50), font=face, size=24)

    (font,) = _type3_fonts(document)
    assert font["FontMatrix"][0] == pytest.approx(1 / 2048)
    assert list(font["Widths"]) == [2048, 2048]
    assert page.text().strip() == "RRG"
    glyphs = _glyphs(page)
    assert [glyph.origin.x for glyph in glyphs] == pytest.approx([20, 44, 68], abs=1e-3)
    pixmap = page.pixmap(dpi=72)
    assert pixmap.pixel(32, 38)[:3] == RED
    assert pixmap.pixel(80, 38)[:3] == GREEN


def test_a_type3_font_has_the_shape_that_readers_need(bitmaps: pyropdf.Font) -> None:
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("RG", (20, 80), font=bitmaps, size=24)

    (font,) = _type3_fonts(document)
    assert font["FontMatrix"][0] == pytest.approx(0.001)
    assert (font["FirstChar"], font["LastChar"]) == (0, 1)
    assert list(font["Widths"]) == [1000, 1000]
    assert list(font["FontBBox"]) == [0, 0, 1000, 1000]
    assert [str(name) for name in font["Encoding"]["Differences"]][1:] == ["g1s120", "g2s120"]
    assert _procedures(document)["g1s120"].startswith(b"1000 0 d0\n")
    assert b"<00> <0052>" in font["ToUnicode"].read()


def test_a_fill_does_not_change_a_bitmap_glyph(bitmaps: pyropdf.Font) -> None:
    _document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("RO", (20, 50), font=bitmaps, size=24, fill=pyropdf.Color(0, 0, 1))
        canvas.text("R", (120, 50), font=bitmaps, size=24, fill=None, stroke=pyropdf.Color(0, 0, 1))

    pixmap = page.pixmap(dpi=72)
    assert pixmap.pixel(*RED_AT)[:3] == RED
    assert pixmap.pixel(55, 38)[:3] == (0, 0, 255)
    assert pixmap.pixel(132, 38)[:3] == RED


def test_a_translucent_bitmap_keeps_its_soft_mask(bitmaps: pyropdf.Font) -> None:
    document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("T", (20, 50), font=bitmaps, size=7)
        canvas.text("T", (40, 50), font=bitmaps, size=24)

    (image,) = _glyph_images(document)
    assert image["SMask"]["ColorSpace"] == "DeviceGray"
    red, green, blue = page.pixmap(dpi=72).pixel(52, 38)[:3]
    assert red == 255
    assert green == pytest.approx(127, abs=3)
    assert blue == pytest.approx(127, abs=3)


def test_a_face_with_bitmaps_and_no_outlines_draws_and_reads(bitmaps_only: pyropdf.Font) -> None:
    document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("R G", (20, 50), font=bitmaps_only, size=24)
        # A glyph with no bitmap paints nothing, as an empty glyph does.
        canvas.text("N", (120, 50), font=bitmaps_only, size=24)

    assert _descriptor_programs(document) == set()
    assert {font.subtype for font in page.fonts()} == {"Type3"}
    assert page.text().split() == ["R", "G", "N"]
    glyphs = _glyphs(page)
    assert [glyph.origin.x for glyph in glyphs[:3]] == pytest.approx([20, 44, 51.2], abs=1e-3)
    procedures = _procedures(document)
    assert procedures["g7"] == b"300 0 d0"
    assert procedures["g6"] == b"1000 0 d0"
    pixmap = pyropdf.open(document.to_bytes()).pages[0].pixmap(dpi=72)
    assert pixmap.pixel(*RED_AT)[:3] == RED
    assert pixmap.pixel(132, 38)[:3] == (255, 255, 255)


def test_only_the_canvas_draws_a_face_with_no_outlines(bitmaps_only: pyropdf.Font) -> None:
    _document, page = _new_page()
    with pytest.raises(pyropdf.PdfError, match="only canvas text can draw it"):
        page.annotations.add(FreeText((20, 20, 200, 60), "N", font=bitmaps_only))


def test_a_face_with_no_outlines_or_colour_bitmaps_refuses_at_load() -> None:
    with pytest.raises(pyropdf.PdfError, match="the face has no outlines or colour bitmaps"):
        pyropdf.Font.from_file(FONTS / "colour-eblc-only.ttf")


def test_a_document_holds_one_font_per_256_colour_glyphs(bitmaps: pyropdf.Font) -> None:
    document = pyropdf.Document()
    for text in (MANY[:100], MANY[50:], MANY):
        page = document.pages.new((400, 100))
        with page.draw() as canvas:
            canvas.text(text, (10, 40), font=bitmaps, size=1)

    for held in (document, pyropdf.open(document.to_bytes())):
        counts = sorted(len(font["CharProcs"]) for font in _type3_fonts(held))
        assert counts == [44, 256]
        # Each glyph's procedure and image once, however many drawings use them.
        assert len(_procedures(held)) == 300
        assert len(_glyph_images(held)) == 300
        procedure_objects = {font["CharProcs"][name].xref for font in _type3_fonts(held) for name in font["CharProcs"]}
        assert len(procedure_objects) == 300
        assert held.pages[2].text().replace("\n", "") == MANY
    assert len(_descriptor_programs(document)) == 0


def test_a_mixed_run_keeps_its_order_and_positions(bitmaps: pyropdf.Font) -> None:
    _document, page = _new_page()
    text = "RORGO R O"
    x, y, size = 20.5, 80.0, 18.0
    with page.draw() as canvas:
        canvas.text(text, (x, y), font=bitmaps, size=size)

    glyphs = _glyphs(page)
    assert [glyph.text for glyph in glyphs] == list(text)
    for index, glyph in enumerate(glyphs):
        # Each glyph starts where the shaped advances before it end.
        assert glyph.origin.x == pytest.approx(x + bitmaps.text_width(text[:index], size), abs=1e-2)
        assert glyph.origin.y == pytest.approx(y, abs=1e-3)
    assert page.text().strip() == text


def test_a_justified_mixed_line_reaches_the_edge(bitmaps: pyropdf.Font) -> None:
    _document, page = _new_page()
    with page.draw() as canvas:
        canvas.textbox((20, 20, 380, 180), "R O G " * 12, font=bitmaps, size=18, align="justify")

    lines: dict[float, list[pyropdf.Glyph]] = {}
    for glyph in _glyphs(page):
        lines.setdefault(round(glyph.origin.y, 3), []).append(glyph)
    first = lines[min(lines)]
    assert first[0].origin.x == pytest.approx(20, abs=1e-3)
    # The last is a red or green glyph one em wide, or an outline that the face's advance ends.
    assert max(glyph.origin.x for glyph in first) + 18 == pytest.approx(380, abs=8)


def test_undo_removes_what_a_drawing_added(bitmaps: pyropdf.Font) -> None:
    document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("R", (20, 50), font=bitmaps, size=24)
    before = set(document.objects)
    with page.draw() as canvas:
        canvas.text("GO", (60, 50), font=bitmaps, size=24)
    assert set(_procedures(document)) == {"g1s120", "g2s120"}
    assert len(_glyph_images(document)) == 2
    assert len(_descriptor_programs(document)) == 1

    document.history.undo()
    assert set(document.objects) == before
    assert set(_procedures(document)) == {"g1s120"}
    assert len(_glyph_images(document)) == 1
    assert document.pages[0].text().strip() == "R"

    document.history.redo()
    assert set(_procedures(document)) == {"g1s120", "g2s120"}
    assert document.pages[0].text().split() == ["R", "GO"]

    document.history.undo()
    document.history.undo()
    assert _type3_fonts(document) == []
    page = document.pages[0]
    with page.draw() as canvas:
        canvas.text("G", (60, 50), font=bitmaps, size=24)
    (font,) = _type3_fonts(document)
    assert [str(name) for name in font["Encoding"]["Differences"]][1:] == ["g2s120"]
    assert page.text().strip() == "G"


def test_a_redo_does_not_duplicate_the_fonts_that_it_brings_back(bitmaps: pyropdf.Font) -> None:
    document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("R", (20, 50), font=bitmaps, size=24)
    document.history.undo()
    document.history.redo()
    page = document.pages[0]
    with page.draw() as canvas:
        canvas.text("R", (60, 50), font=bitmaps, size=24)

    assert len(_type3_fonts(document)) == 1
    assert len(_procedures(document)) == 1


def test_a_save_writes_only_the_glyphs_that_the_document_draws(bitmaps: pyropdf.Font) -> None:
    document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("R", (20, 50), font=bitmaps, size=24)
    colour_only = document.to_bytes()
    reopened = pyropdf.open(colour_only)
    assert set(_procedures(reopened)) == {"g1s120"}
    assert len(_glyph_images(reopened)) == 1
    # A drawing with no outlines embeds no program of the face.
    assert _descriptor_programs(reopened) == set()
    assert len(colour_only) < 4096

    with page.draw() as canvas:
        canvas.text("O", (60, 50), font=bitmaps, size=24)
    reopened = pyropdf.open(document.to_bytes())
    (program,) = _descriptor_programs(reopened)
    data = reopened.objects[program].read()
    # The compacted program keeps outlines, not the strikes.
    assert b"sbix" not in data
    assert b"CBDT" not in data
    assert len(data) < 2048
    assert reopened.pages[0].text().split() == ["R", "O"]


def test_a_later_drawing_after_a_save_adds_to_the_same_font(bitmaps: pyropdf.Font) -> None:
    document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("R", (20, 50), font=bitmaps, size=24)
    first = document.to_bytes()
    with page.draw() as canvas:
        canvas.text("G", (60, 50), font=bitmaps, size=24)
    second = pyropdf.open(document.to_bytes())

    assert pyropdf.open(first).pages[0].text().strip() == "R"
    assert second.pages[0].text().split() == ["R", "G"]
    (font,) = _type3_fonts(second)
    assert len(font["CharProcs"]) == 2


def test_a_drawing_takes_the_strike_that_its_size_needs(bitmaps: pyropdf.Font) -> None:
    document, page = _new_page(400, 100)
    with page.draw() as canvas:
        canvas.text("R", (20, 50), font=bitmaps, size=7)
        canvas.text("R", (40, 50), font=bitmaps, size=7)
        canvas.text("R", (60, 50), font=bitmaps, size=24)
        canvas.text("G", (100, 50), font=bitmaps, size=7)
    with page.draw() as canvas:
        canvas.text("R", (140, 50), font=bitmaps, size=96)

    assert set(_procedures(document)) == {"g1s32", "g1s120", "g2s120"}
    assert sorted(image["Width"] for image in _glyph_images(document)) == [32, 120, 120]
    # Both bitmaps of a glyph read back as its text, at their sizes.
    assert sorted("".join(page.text().split())) == sorted("RRRGR")
    # The image of each strike covers one em.
    pixmap = page.pixmap(dpi=72)
    assert pixmap.pixel(23, 47)[:3] == RED
    assert pixmap.pixel(70, 40)[:3] == RED


def test_a_dupe_record_draws_the_image_of_the_glyph_that_it_names() -> None:
    face = pyropdf.Font.from_file(FONTS / "colour-bitmaps-sbix.ttf")
    document, page = _new_page(200, 60)
    with page.draw() as canvas:
        canvas.text("RD", (20, 50), font=face, size=24)

    assert set(_procedures(document)) == {"g1s120", "g4s120"}
    assert len(_glyph_images(document)) == 1
    assert page.pixmap(dpi=72).pixel(56, 38)[:3] == RED
    assert page.text().strip() == "RD"


def test_the_same_png_anywhere_in_a_document_is_one_image(bitmaps: pyropdf.Font) -> None:
    document = pyropdf.Document()
    for text in ("R", "R"):
        page = document.pages.new((200, 60))
        with page.draw() as canvas:
            canvas.text(text, (20, 50), font=bitmaps, size=24)

    assert len(_procedures(document)) == 1
    assert len(_glyph_images(document)) == 1


@pytest.mark.parametrize(
    ("limits", "code"),
    [
        (pyropdf.DocumentLimits(max_total_decompressed_size=1000), "total_decompressed_size"),
        (pyropdf.DocumentLimits(max_memory_size=30_000), "memory_size"),
    ],
    ids=["decoded", "memory"],
)
def test_a_limit_refuses_a_bitmap_and_writes_nothing(
    bitmaps: pyropdf.Font, limits: pyropdf.DocumentLimits, code: str
) -> None:
    document = pyropdf.Document(limits=limits)
    page = document.pages.new((400, 100))
    numbers = set(document.objects)
    steps = document.history.steps

    with pytest.raises(pyropdf.LimitError) as raised, page.draw() as canvas:
        canvas.text(MANY[:32] + "RG", (10, 40), font=bitmaps, size=24)

    assert raised.value.code == code
    assert set(document.objects) == numbers
    assert document.history.steps == steps
    assert _type3_fonts(document) == []
    assert page.text().strip() == ""


def test_a_damaged_png_refuses_as_a_font_error_and_a_caught_refusal_writes_nothing() -> None:
    face = pyropdf.Font.from_file(FONTS / "colour-sbix-damaged.ttf")
    document, page = _new_page()
    with page.draw() as canvas:
        canvas.text("C", (20, 40), font=face, size=12)
        numbers = set(document.objects)
        with pytest.raises(pyropdf.PdfError, match="cannot draw its glyph 1"):
            canvas.text("CA", (20, 80), font=face, size=12)
        assert set(document.objects) == numbers
        canvas.text("C", (20, 120), font=face, size=12)

    assert _type3_fonts(document) == []
    assert page.text().split() == ["C", "C"]


def test_a_bitmap_glyph_measures_by_its_image(bitmaps: pyropdf.Font) -> None:
    assert bitmaps.char_bbox("R") == pytest.approx((0, 0, 1, 1))
    # A glyph with no bitmap keeps its outline's box.
    assert bitmaps.char_bbox("O") == pytest.approx((0, 0, 0.8, 0.8))
    assert bitmaps.text_width("RO", 10) == pytest.approx(17)
