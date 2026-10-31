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


@pytest.mark.filterwarnings("ignore::pyropdf.PyropdfWarning")
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


@pytest.mark.filterwarnings("ignore::pyropdf.PyropdfWarning")
def test_undecodable_page_content_keeps_its_fonts_whole() -> None:
    doc = _exclusive_program()
    broken = doc.objects.add_stream(b"BT /F1 12 Tf <1000> Tj ET", filter=None)
    doc.objects[broken]["Filter"] = pyropdf.Name("JBIG2Decode")
    page = doc.pages[0].obj
    page["Contents"] = [page["Contents"], doc.objects[broken]]
    before = hashlib.sha256(doc.objects[PROGRAM].read()).digest()

    doc.subset_fonts()

    assert hashlib.sha256(doc.objects[PROGRAM].read()).digest() == before


@pytest.mark.filterwarnings("ignore::pyropdf.PyropdfWarning")
def test_nested_undecodable_form_keeps_its_fonts_whole() -> None:
    doc = _exclusive_program()
    page = doc.pages[0].obj
    broken = doc.objects.add_stream(
        b"BT /F1 12 Tf <1000> Tj ET",
        {"Type": pyropdf.Name("XObject"), "Subtype": pyropdf.Name("Form"), "BBox": [0, 0, 10, 10]},
        filter=None,
    )
    doc.objects[broken]["Filter"] = pyropdf.Name("JBIG2Decode")
    outer = doc.objects.add_stream(
        b"q /Inner Do Q",
        {
            "Type": pyropdf.Name("XObject"),
            "Subtype": pyropdf.Name("Form"),
            "BBox": [0, 0, 10, 10],
            "Resources": {"Font": page["Resources"]["Font"], "XObject": {"Inner": doc.objects[broken]}},
        },
        filter=None,
    )
    page["Resources"]["XObject"] = {"Outer": doc.objects[outer]}
    _add_text_stream(doc, b"q /Outer Do Q")
    before = hashlib.sha256(doc.objects[PROGRAM].read()).digest()

    doc.subset_fonts()

    assert hashlib.sha256(doc.objects[PROGRAM].read()).digest() == before


@pytest.mark.filterwarnings("ignore::pyropdf.PyropdfWarning")
def test_undecodable_page_does_not_keep_other_pages_fonts_whole() -> None:
    doc = _exclusive_program()
    page = doc.pages[0].obj
    resources = dict(page["Resources"])
    page_fonts = dict(resources["Font"])
    del page_fonts["F2"]
    resources["Font"] = page_fonts
    page["Resources"] = resources
    other_program = doc.objects[SIMPLE_FONT]["FontDescriptor"]["FontFile2"]
    other_xref = other_program.xref
    assert other_xref is not None
    before = len(other_program.read())
    broken = doc.objects.add_stream(b"BT /F1 12 Tf <1000> Tj ET", filter=None)
    doc.objects[broken]["Filter"] = pyropdf.Name("JBIG2Decode")
    page["Contents"] = [page["Contents"], doc.objects[broken]]

    doc.subset_fonts()

    assert len(doc.objects[other_xref].read()) < before


@pytest.mark.filterwarnings("ignore::pyropdf.PyropdfWarning")
def test_shared_undecodable_form_keeps_each_pages_fonts_whole() -> None:
    doc = _exclusive_program()
    other_program = doc.objects[SIMPLE_FONT]["FontDescriptor"]["FontFile2"]
    other_xref = other_program.xref
    assert other_xref is not None
    broken = doc.objects.add_stream(
        b"BT <1000> Tj ET",
        {"Type": pyropdf.Name("XObject"), "Subtype": pyropdf.Name("Form"), "BBox": [0, 0, 10, 10]},
        filter=None,
    )
    doc.objects[broken]["Filter"] = pyropdf.Name("JBIG2Decode")
    for page, font_name in zip(doc.pages, ("F1", "F2"), strict=True):
        resources = dict(page.obj["Resources"])
        resources["Font"] = {font_name: resources["Font"][font_name]}
        resources["XObject"] = {"Broken": doc.objects[broken]}
        page.obj["Resources"] = resources
    before = [hashlib.sha256(doc.objects[xref].read()).digest() for xref in (PROGRAM, other_xref)]

    doc.subset_fonts()

    assert [hashlib.sha256(doc.objects[xref].read()).digest() for xref in (PROGRAM, other_xref)] == before


def test_content_decode_limit_refuses_instead_of_subsetting() -> None:
    source = _exclusive_program()
    data = source.to_bytes()
    doc = pyropdf.open(data, limits=pyropdf.DocumentLimits(max_page_content_size=16))
    with pytest.raises(pyropdf.LimitError) as error:
        doc.subset_fonts()
    assert error.value.code == "page_content_size"
    assert doc.objects[PROGRAM].read() == source.objects[PROGRAM].read()
