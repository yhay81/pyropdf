"""Subsetting the embedded fonts of existing documents.

Ported from pylopdf ebb01d3c ``tests/test_font_subsetting.py``. Intended
differences: ``Document.subset_fonts`` reports one ``SubsetFont`` per
program instead of a ``FontSubsetResult``, and keeps glyph IDs, emptying the
glyphs that no page draws, so an explicit ``CIDToGIDMap`` stays as it is
and the programs keep their tables: a subset is under half the program
where pylopdf's is under a fifth.
pyropdf compacts glyph IDs only for the faces that it embeds itself, at
save (``tests/test_canvas_embedded.py``). The cases that
``tests/test_font_subset.py`` already covers are not repeated.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from corpus import ASSETS

import pyropdf

# In mhlw-doc.pdf one full YuMincho program (object 152, 733,908 bytes)
# serves a Type0 font (object 5, `/F1`) and a simple TrueType font (object 12,
# `/F2`).
TYPE0_FONT = 5
SIMPLE_FONT = 12
PROGRAM = 152
PROGRAM_SIZE = 733_908


def _mhlw() -> pyropdf.Document:
    return pyropdf.open((ASSETS / "mhlw-doc.pdf").read_bytes())


def _exclusive_program() -> pyropdf.Document:
    """Give the simple font a copy of the program, so the Type0 font has it alone."""
    doc = _mhlw()
    data = doc.objects[PROGRAM].read()
    copy = doc.objects.add_stream(data, {"Length1": len(data)})
    doc.objects[SIMPLE_FONT]["FontDescriptor"]["FontFile2"] = doc.objects[copy]
    return pyropdf.open(doc.to_bytes())


def _signature(doc: pyropdf.Document) -> list[tuple[str, bytes]]:
    return [(page.text(), bytes(page.pixmap(dpi=72))) for page in doc.pages]


def _add_text_stream(doc: pyropdf.Document, content: bytes) -> None:
    """Append a content stream to the first page."""
    page = doc.pages[0].obj
    added = doc.objects.add_stream(content, filter=None)
    page["Contents"] = [page["Contents"], doc.objects[added]]


def test_subsetting_keeps_text_and_rendering_and_shrinks_the_file() -> None:
    doc = _exclusive_program()
    expected = _signature(doc)
    before = len(doc.to_bytes())

    report = doc.subset_fonts()

    # The Type0 font's program and the simple font's copy.
    assert len(report) == 2
    assert all(font.size_before == PROGRAM_SIZE for font in report)
    # pylopdf's krilla subsetter renumbers glyphs and drops tables, to under
    # a fifth; keeping glyph IDs and tables leaves under a half.
    assert sum(font.size_after for font in report) < 2 * PROGRAM_SIZE // 2
    assert _signature(doc) == expected
    saved = doc.to_bytes()
    assert len(saved) < before * 2 // 3
    assert _signature(pyropdf.open(saved)) == expected


def test_subsetting_a_subset_again_changes_nothing() -> None:
    doc = _exclusive_program()
    doc.subset_fonts()

    assert pyropdf.open(doc.to_bytes()).subset_fonts() == ()


def test_a_program_shared_by_a_cid_font_and_a_simple_font_is_subset() -> None:
    doc = _mhlw()
    expected = _signature(doc)
    before = len(doc.to_bytes())

    [font] = doc.subset_fonts()

    assert font.size_after < PROGRAM_SIZE // 2
    saved = doc.to_bytes()
    assert len(saved) < before // 2
    assert _signature(pyropdf.open(saved)) == expected


def test_a_simple_font_held_directly_keeps_its_program_whole() -> None:
    doc = _mhlw()
    resources = doc.pages[0].obj["Resources"]["Font"]
    # Copy the simple font into the resource dictionary, so no object holds it.
    resources["F2"] = dict(doc.objects[SIMPLE_FONT].items())
    doc = pyropdf.open(doc.to_bytes())

    assert doc.subset_fonts() == ()


def test_a_font_in_the_form_default_resources_is_left_alone() -> None:
    doc = _exclusive_program()
    font = doc.objects[TYPE0_FONT]
    doc.catalog["AcroForm"] = {"Fields": [], "DR": {"Font": {"F1": font}}}
    before = doc.objects[PROGRAM].read()

    # Only the simple font's copy of the program is subset.
    assert len(doc.subset_fonts()) == 1

    assert doc.objects[PROGRAM].read() == before


def test_a_form_draws_with_the_font_it_inherits() -> None:
    doc = _exclusive_program()
    # CID 0x1000 is a glyph that no page draws.
    form = doc.objects.add_stream(
        b"BT 72 500 Td <1000> Tj ET",
        {"Type": pyropdf.Name("XObject"), "Subtype": pyropdf.Name("Form"), "BBox": [0, 0, 600, 800]},
        filter=None,
    )
    resources = doc.pages[0].obj["Resources"]
    resources["XObject"] = {"Inherits": doc.objects[form]}
    _add_text_stream(doc, b"q /F1 40 Tf /Inherits Do Q")
    doc = pyropdf.open(doc.to_bytes())
    expected = _signature(doc)

    assert len(doc.subset_fonts()) == 2

    assert _signature(doc) == expected


@pytest.mark.filterwarnings("ignore::pyropdf.PyropdfWarning")
@pytest.mark.xfail(strict=True, reason="subset_fonts subsets past an undecodable stream (yhay81/pyropdf#486)")
def test_a_stream_that_does_not_decode_keeps_its_fonts_whole() -> None:
    doc = _exclusive_program()
    # A content stream behind an image filter does not decode.
    broken = doc.objects.add_stream(
        b"BT <1000> Tj ET",
        {"Type": pyropdf.Name("XObject"), "Subtype": pyropdf.Name("Form"), "BBox": [0, 0, 10, 10]},
        filter=None,
    )
    doc.objects[broken]["Filter"] = pyropdf.Name("JBIG2Decode")
    doc.pages[0].obj["Resources"]["XObject"] = {"Broken": doc.objects[broken]}
    _add_text_stream(doc, b"q /F1 12 Tf /Broken Do Q")
    before = hashlib.sha256(doc.objects[PROGRAM].read()).hexdigest()

    doc.subset_fonts()

    # Digests, not the program's bytes: pytest diffs two unequal byte
    # strings of 733,908 bytes for minutes before it reports the failure.
    assert hashlib.sha256(doc.objects[PROGRAM].read()).hexdigest() == before


def test_an_explicit_cid_to_gid_map_keeps_its_glyph_ids() -> None:
    doc = _exclusive_program()
    cid_font = doc.objects[TYPE0_FONT]["DescendantFonts"][0]
    identity = b"".join(cid.to_bytes(2, "big") for cid in range(20_000))
    cid_font["CIDToGIDMap"] = doc.objects[doc.objects.add_stream(identity)]
    doc = pyropdf.open(doc.to_bytes())
    expected = _signature(doc)

    assert len(doc.subset_fonts()) == 2

    assert _signature(doc) == expected
    kept = doc.objects[TYPE0_FONT]["DescendantFonts"][0]["CIDToGIDMap"].read()
    assert kept == identity


def test_subsetting_is_saved_incrementally() -> None:
    original = _exclusive_program().to_bytes()
    doc = pyropdf.open(original)
    expected = _signature(doc)

    doc.subset_fonts()
    saved = doc.to_bytes(incremental=True)

    assert saved.startswith(original)
    assert _signature(pyropdf.open(saved)) == expected


def test_a_document_without_embedded_cid_fonts_considers_nothing() -> None:
    doc = pyropdf.open((ASSETS / "usrguide.pdf").read_bytes())

    assert doc.subset_fonts() == ()


def test_pikepdf_reads_a_subset_document() -> None:
    pikepdf = pytest.importorskip("pikepdf", reason="interop group (pikepdf) is not installed")
    doc = _exclusive_program()
    doc.subset_fonts()

    with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
        assert pdf.check_pdf_syntax() == []
