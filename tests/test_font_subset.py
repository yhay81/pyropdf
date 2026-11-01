"""Font subsetting: every page draws and reads as it did, and the file shrinks."""

from __future__ import annotations

import re

import pytest
from corpus import ASSETS, NAMES

import pyropdf
from pyropdf import _core, _fonts

#: The render tolerance of one 8-bit sample.
TOLERANCE = 2
#: The resolution of the comparison renders: low, so that the corpus renders fast.
DPI = 36


def _open(name: str) -> pyropdf.Document:
    return pyropdf.open((ASSETS / name).read_bytes())


def _pages(doc: pyropdf.Document) -> list[tuple[bytes, str]]:
    return [(bytes(page.pixmap(dpi=DPI)), page.text(unmapped="glyph_id")) for page in doc.pages]


def _assert_same(before: list[tuple[bytes, str]], after: list[tuple[bytes, str]]) -> None:
    assert len(after) == len(before)
    for at, ((pixels, text), (new_pixels, new_text)) in enumerate(zip(before, after, strict=True)):
        assert new_text == text, f"page {at}"
        assert len(new_pixels) == len(pixels), f"page {at}"
        differing = sum(abs(a - b) > TOLERANCE for a, b in zip(pixels, new_pixels, strict=True))
        assert differing == 0, f"page {at}: {differing} samples differ"


@pytest.mark.parametrize("name", NAMES)
def test_every_corpus_page_draws_and_reads_the_same_after_subsetting(name: str) -> None:
    doc = _open(name)
    before = _pages(doc)

    doc.subset_fonts()

    _assert_same(before, _pages(pyropdf.open(doc.to_bytes())))


@pytest.mark.parametrize("name", NAMES)
def test_every_readable_program_subset_regardless_of_size_draws_the_same(name: str) -> None:
    # The pass that subset_fonts skips when a subset does not shrink still draws the same.
    doc = _open(name)
    before = _pages(doc)
    programs = _fonts._programs(doc)  # noqa: SLF001 - the subsetter's own steps.
    fonts = [font for program in programs.values() for font in program.fonts]
    used = iter(_fonts._glyphs(doc, fonts))  # noqa: SLF001
    for program in programs.values():
        keep = sorted({glyph for _ in program.fonts for glyph in next(used)})
        if _fonts._UNKNOWN_GLYPHS in keep:  # noqa: SLF001 - the subsetter's safety marker.
            continue
        subset = _core.subset_font_program(program.stream.read(), program.format, keep)
        assert subset is not None
        program.stream.write(subset[0])

    _assert_same(before, _pages(pyropdf.open(doc.to_bytes())))


def test_fully_embedded_programs_shrink_to_the_glyphs_drawn() -> None:
    doc = _open("mhlw-doc.pdf")
    size = len(doc.to_bytes())

    [font] = doc.subset_fonts()

    assert font.kept < font.glyphs
    assert font.size_after < font.size_before
    assert re.fullmatch(r"[A-Z]{6}\+YuMincho-Regular", font.name)
    stream = doc.objects[font.xref]
    assert stream["Length1"] == font.size_after == len(stream.read())
    assert len(doc.to_bytes()) < size


def test_subsetting_tags_the_font_names_once() -> None:
    doc = _open("senate-expenditures.pdf")
    names = {font.name for font in doc.subset_fonts()}

    assert names
    assert all(re.fullmatch(r"[A-Z]{6}\+[^+]+", name) for name in names)
    # A second pass finds nothing more to drop and keeps the names.
    assert doc.subset_fonts() == ()


def test_subsetting_is_one_step_that_undo_reverts() -> None:
    doc = _open("mhlw-doc.pdf")
    original = doc.to_bytes()

    doc.subset_fonts()

    assert doc.history.steps[-1] == "subset fonts"
    doc.history.undo()
    assert doc.to_bytes() == original


def test_programs_that_the_form_resources_reach_stay_whole() -> None:
    doc = _open("bill-hr815.pdf")
    form = doc.catalog["AcroForm"]
    default = form["DR"]["Font"]
    descriptors = {
        descriptor.xref
        for key in default
        for descriptor in _fonts._descriptors(default[key])  # noqa: SLF001
    }
    programs = _fonts._programs(doc)  # noqa: SLF001

    assert descriptors
    assert not any(
        descriptor.xref in descriptors for program in programs.values() for descriptor in program.descriptors
    )


def test_collecting_with_every_layer_on_leaves_no_trace() -> None:
    doc = _open("pdfium-ocg-invisible.pdf")
    config = doc.catalog["OCProperties"]["D"]
    before = dict(config)

    doc.subset_fonts()

    assert dict(doc.catalog["OCProperties"]["D"]) == before
    assert doc.history.steps[-1:] in ((), ("subset fonts",))


def test_an_unknown_program_format_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown font program format"):
        _core.subset_font_program(b"", "type1", [])


def test_a_program_the_subsetter_does_not_read_is_left_alone() -> None:
    assert _core.subset_font_program(b"not a font", "sfnt", [1]) is None
    assert _core.subset_font_program(b"not a font", "cff", [1]) is None
