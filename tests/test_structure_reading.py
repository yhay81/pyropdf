"""Hand-inspected structure order, page-local identifiers and PDF 2.0 nesting."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pytest
from test_content import build_raw_pdf

import pyropdf

_ASSETS = Path(__file__).parent / "assets" / "pdfua"


def _stream(data: str, extra: str = "") -> str:
    return f"<< /Length {len(data)} {extra} >>\nstream\n{data}\nendstream"


def _tagged() -> bytes:
    # On disk: Page 1 paints A, WHO, an artifact and B; page 2 paints C.
    # In the logical tree: C, WHO, B, A, one external stream and an annotation.
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Version /2.0 /Pages 2 0 R /StructTreeRoot 8 0 R /MarkInfo << /Marked true >> >>",
            2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] /Resources "
            "<< /Font << /F1 7 0 R >> /Properties << /Abbr << /MCID 2 /E (World Health Organization) >> >> >> >>",
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /Annots [15 0 R] /StructParents 0 >>",
            4: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R /StructParents 1 >>",
            5: _stream(
                "BT /F1 10 Tf 10 180 Td /P << /MCID 0 >> BDC (A) Tj EMC "
                "0 -20 Td /Span /Abbr BDC (WHO) Tj EMC "
                "0 -20 Td /Artifact BMC (HEADER) Tj EMC "
                "0 -20 Td /P << /MCID 1 >> BDC (B) Tj EMC ET"
            ),
            6: _stream("BT /F1 10 Tf 10 180 Td /P << /MCID 0 >> BDC (C) Tj EMC ET"),
            7: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            8: "<< /Type /StructTreeRoot /K 9 0 R /RoleMap << /Editorial /P >> /Namespaces [16 0 R] >>",
            9: "<< /Type /StructElem /S /Document /NS 16 0 R /P 8 0 R /K [10 0 R 13 0 R] >>",
            10: "<< /Type /StructElem /S /Editorial /P 9 0 R /Pg 3 0 R /K "
            "[<< /Type /MCR /Pg 4 0 R /MCID 0 >> 2 11 0 R 0] >>",
            11: "<< /Type /StructElem /S /Span /P 10 0 R /Pg 3 0 R /ActualText (Beta) /E (Expanded Beta) /K 1 >>",
            13: "<< /Type /StructElem /S /Annot /P 9 0 R /Pg 3 0 R /NS 16 0 R /K "
            "[<< /Type /MCR /Stm 14 0 R /MCID 0 >> << /Type /OBJR /Obj 15 0 R >>] >>",
            14: _stream("/Span << /MCID 0 >> BDC EMC", "/Type /XObject /Subtype /Form /BBox [0 0 10 10]"),
            15: "<< /Type /Annot /Subtype /Text /Rect [1 2 3 4] /Contents (annotation) >>",
            16: "<< /Type /Namespace /NS (http://iso.org/pdf2/ssn) >>",
        }
    )


@pytest.mark.spec("logical-structure", "interpret")
@pytest.mark.spec("tagged-pdf", "interpret")
def test_structure_orders_mixed_kids_across_pages_and_separates_form_ids() -> None:
    doc = pyropdf.open(_tagged())
    parts = doc.structure.reading_order()
    assert [part.text for part in parts] == ["C", "WHO", "Beta", "A", None, "annotation"]
    assert [part.element.standard_role for part in parts] == ["P", "P", "Span", "P", "Annot", "Annot"]
    assert [part.element.role for part in parts[:2]] == ["Editorial", "Editorial"]
    assert parts[0].item == pyropdf.MarkedContentReference(page=1, mcid=0)
    assert parts[3].item == pyropdf.MarkedContentReference(page=0, mcid=0)
    assert parts[4].item == pyropdf.MarkedContentReference(page=0, mcid=0, stream_xref=14)
    assert parts[5].item == pyropdf.ObjectReference(page=0, xref=15)
    assert "HEADER" in doc.pages[0].text()
    assert pyropdf.Document().structure.reading_order() == ()


@pytest.mark.spec("accessibility", "interpret")
def test_expansions_from_element_and_named_properties_and_empty_replacement() -> None:
    doc = pyropdf.open(_tagged())
    assert [part.text for part in doc.structure.reading_order(expand_abbreviations=True)][:4] == [
        "C",
        "World Health Organization",
        "Expanded Beta",
        "A",
    ]
    span = doc.structure[0].children[0].children[0]
    span.expanded = ""
    assert doc.structure.reading_order(expand_abbreviations=True)[2].text == ""
    assert doc.structure.reading_order()[2].text == "Beta"
    doc.structure[0].actual_text = "entire document"
    assert [part.text for part in doc.structure.reading_order()] == ["entire document"]


@pytest.mark.parametrize(
    ("file", "description"),
    [
        ("pdfua-1.pdf", "A blue rectangle crossed by a red line"),
        ("pdfua-2.pdf", "Water level rose at each of three readings; the final level was three times the first."),
        (
            "pdfua-2-pdfa-4.pdf",
            "Water level rose at each of three readings; the final level was three times the first.",
        ),
    ],
)
@pytest.mark.spec("tagged-pdf", "interpret")
def test_independently_authored_figure_is_read_once(file: str, description: str) -> None:
    doc = pyropdf.open(_ASSETS / file)
    parts = doc.structure.reading_order()
    assert len(parts) == 1
    assert parts[0].text == description
    assert parts[0].element.role == "Figure"


@pytest.mark.spec("structure-children", "read")
@pytest.mark.spec("structure-children", "interpret")
def test_annex_l_counts_siblings_and_distinguishes_namespaces() -> None:
    doc = pyropdf.open(_tagged())
    tree = doc.structure
    root = tree[0]
    ns = root.obj["NS"]
    table = root.children.add("Table")
    table.obj["NS"] = ns
    for role in ["THead", "THead", "TBody", "TR", "Span"]:
        table.children.add(role).obj["NS"] = ns
    rules = {(r.parent_xref, r.child_role): r for r in tree.child_rules()}
    assert (rules[None, "Document"].rule, rules[None, "Document"].allowed) == ("exactly one", True)
    assert rules[table.xref, "THead"].count == 2
    assert (rules[table.xref, "THead"].rule, rules[table.xref, "THead"].allowed) == ("optional", False)
    assert rules[table.xref, "TBody"].allowed
    assert rules[table.xref, "TR"].allowed
    assert rules[table.xref, "Span"].allowed is False
    assert rules[root.xref, "Editorial"].allowed is None
    assert rules[root.xref, "Editorial"].rule == "other namespace"
    assert tree.obj is not None
    tree.obj["K"] = table.obj
    missing = next(row for row in tree.child_rules() if row.parent_xref is None and row.child_role == "Document")
    assert (missing.count, missing.allowed) == (0, False)


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("structure-children", "preserve")
def test_mixed_structure_references_and_unrelated_namespace_survive_save(*, incremental: bool) -> None:
    original = _tagged()
    doc = pyropdf.open(original)
    doc.structure[0].children[0].children[0].expanded = "BETA"
    saved = doc.to_bytes(incremental=incremental)
    if incremental:
        assert saved.startswith(original)
    with pikepdf.Pdf.open(io.BytesIO(saved)) as other:
        document = other.Root.StructTreeRoot.K
        editorial, annot = document.K
        assert str(document.NS.NS) == "http://iso.org/pdf2/ssn"
        assert editorial.K[0].Pg.objgen == other.pages[1].obj.objgen
        assert (int(editorial.K[0].MCID), int(editorial.K[1]), int(editorial.K[3])) == (0, 2, 0)
        assert str(editorial.K[2].E) == "BETA"
        assert str(annot.K[0].Stm.Subtype) == "/Form"
        assert annot.K[1].Obj.objgen == other.pages[0].Annots[0].objgen
    reopened = pyropdf.open(saved)
    assert [item.text for item in reopened.structure.reading_order(expand_abbreviations=True)][:4] == [
        "C",
        "World Health Organization",
        "BETA",
        "A",
    ]


@pytest.mark.parametrize("bad_child", [-1, True, None, {"Type": pyropdf.Name("MCR"), "MCID": -5}])
@pytest.mark.spec("logical-structure", "interpret")
@pytest.mark.spec("structure-children", "read")
def test_malformed_children_fail_without_partial_reading_order(bad_child: object) -> None:
    doc = pyropdf.open(_tagged())
    doc.structure[0].children[0].obj["K"] = [0, bad_child]
    with pytest.raises(pyropdf.MalformedPdfError, match="child"):
        doc.structure.reading_order()
    with pytest.raises(pyropdf.MalformedPdfError, match="child"):
        doc.structure.child_rules()


def test_cycles_and_duplicate_page_identifiers_are_reported() -> None:
    doc = pyropdf.open(_tagged())
    doc.structure[0].children[0].obj["K"] = doc.structure[0].obj
    with pytest.raises(pyropdf.MalformedPdfError, match="cycle"):
        doc.structure.reading_order()
    doc = pyropdf.open(_tagged())
    stream = doc.pages[0].obj["Contents"]
    stream.write(stream.read() + b" /P << /MCID 0 >> BDC EMC")
    with pytest.raises(pyropdf.MalformedPdfError, match="duplicate"):
        doc.structure.reading_order()


@pytest.mark.spec("structure-children", "interpret")
def test_numbered_headings_wrapped_roles_required_ruby_and_caption_position() -> None:
    doc = pyropdf.open(_tagged())
    document = doc.structure[0]
    ns = document.obj["NS"]

    def add(parent: pyropdf.StructureElement, role: str) -> pyropdf.StructureElement:
        child = parent.children.add(role)
        child.obj["NS"] = ns
        return child

    p = add(document, "P")
    add(p, "Table")
    add(document, "H7")
    add(document, "Hn")
    section = add(document, "Sect")
    part = add(add(section, "Part"), "Part")
    add(part, "Document")
    figure = add(section, "Figure")
    add(figure, "P")
    add(figure, "Caption")
    add(figure, "P")
    ruby = add(p, "Ruby")
    valid = add(p, "Ruby")
    for role in ("RB", "RP", "RT", "RP"):
        add(valid, role)
    link = add(document, "Link")
    add(link, "Span")
    add(link, "Table")
    rules = {(r.parent_xref, r.child_role): r for r in doc.structure.child_rules()}
    assert (rules[link.xref, "Table"].rule, rules[link.xref, "Table"].allowed) == ("grouping only", False)
    assert rules[p.xref, "Table"].allowed
    assert rules[document.xref, "H7"].allowed
    assert rules[document.xref, "Hn"].allowed is None
    assert rules[part.xref, "Document"].allowed is False
    assert rules[figure.xref, "Caption"].allowed is False
    assert (rules[ruby.xref, "RB"].count, rules[ruby.xref, "RT"].allowed) == (0, False)
    assert (rules[valid.xref, "RP"].count, rules[valid.xref, "RP"].allowed) == (2, True)
    # A later PDF 2.0 descendant still activates the root rule.
    del document.obj["NS"]
    root_document = [r for r in doc.structure.child_rules() if r.parent_xref is None and r.child_role == "Document"]
    assert {(r.count, r.allowed) for r in root_document} == {(1, None), (0, False)}


@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.spec("tagged-pdf", "interpret")
def test_published_correct_and_incorrect_marked_content_order(*, invalid: bool) -> None:
    name = "UA1_Tpdf-G4_F02.pdf" if invalid else "UA1_Tpdf-G4_02.pdf"
    doc = pyropdf.open(_ASSETS.parent / "tagged-reading" / name)
    words = [(r.text or "").strip().split()[0] for r in doc.structure.reading_order()]
    expected = ["This", "Second", "Third", "Fourth", "Fifth"]
    assert words == (["FAIL", *reversed(expected), *reversed(expected)] if invalid else expected * 2)
