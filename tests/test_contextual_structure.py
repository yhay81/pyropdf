"""Contextual parent-child rules in the PDF 2.0 standard structure namespace."""

from __future__ import annotations

import io

import pikepdf
import pytest
from test_structure_reading import _tagged

import pyropdf as pdf


def _tag(parent: pdf.StructureElement, role: str, namespace: pdf.Dictionary) -> pdf.StructureElement:
    child = parent.children.add(role)
    child.obj["NS"] = namespace
    return child


def _rules(doc: pdf.Document, **kwargs: dict[int, bool]) -> dict[tuple[int | None, str], pdf.StructureChildRule]:
    return {(r.parent_xref, r.child_role): r for r in doc.structure.child_rules(**kwargs)}


@pytest.mark.spec("structure-children", "interpret")
@pytest.mark.parametrize("incremental", [False, True])
def test_mapped_and_transparent_elements_share_the_effective_count(*, incremental: bool) -> None:
    source = _tagged()
    with pdf.open(source) as doc:
        root = doc.structure[0]
        standard = root.obj["NS"]
        custom = doc.objects[doc.objects.add({"Type": pdf.Name("Namespace"), "NS": "urn:editorial"})]
        middle = doc.objects[doc.objects.add({"Type": pdf.Name("Namespace"), "NS": "urn:layout"})]
        custom["RoleMapNS"] = {
            "ListTitle": [pdf.Name("Heading"), middle],
            "Legacy": pdf.Name("P"),
            "Loop": [pdf.Name("Again"), middle],
        }
        middle["RoleMapNS"] = {"Heading": [pdf.Name("H"), standard], "Again": [pdf.Name("Loop"), custom]}
        doc.structure.obj["Namespaces"].extend([custom, middle])
        section = _tag(root, "Sect", standard)
        _tag(section, "H", standard)
        box = _tag(_tag(section, "Div", standard), "NonStruct", standard)
        _tag(box, "ListTitle", custom)
        _tag(section, "Legacy", custom)
        _tag(section, "Unknown", custom)
        _tag(section, "Loop", custom)
        xref = section.xref
        result = doc.to_bytes(incremental=incremental)
        if incremental:
            assert result.startswith(source)
    with pdf.open(result) as doc:
        rows = _rules(doc)
        assert (rows[xref, "H"].count, rows[xref, "H"].allowed) == (2, False)
        assert rows[box.xref, "H"].allowed is False
        assert rows[xref, "P"].rule == "other namespace"
        assert rows[xref, "P"].allowed is None
        assert rows[xref, "Unknown"].rule == "unmapped namespace role"
        assert rows[xref, "Loop"].rule == "cyclic namespace role map"
        assert rows[xref, "Loop"].allowed is None
    with pikepdf.open(io.BytesIO(result)) as independent:
        editorial, layout = independent.Root.StructTreeRoot.Namespaces[-2:]
        assert layout.RoleMapNS.Heading[1].NS == "http://iso.org/pdf2/ssn"
        assert editorial.RoleMapNS.ListTitle[1].objgen == layout.objgen


@pytest.mark.spec("structure-children", "interpret")
def test_grouping_usage_distinguishes_evidence_from_intention() -> None:
    with pdf.open(_tagged()) as doc:
        root = doc.structure[0]
        ns = root.obj["NS"]
        ambiguous = _tag(root, "Link", ns)
        _tag(ambiguous, "Table", ns)
        grouping = _tag(root, "Link", ns)
        _tag(grouping, "Sect", ns)
        _tag(grouping, "Table", ns)
        inline = _tag(root, "Link", ns)
        _tag(inline, "Ruby", ns)
        _tag(inline, "Table", ns)
        content = _tag(root, "Annot", ns)
        _tag(content, "Table", ns)
        content.obj["K"].append(0)
        rows = _rules(doc)
        assert rows[ambiguous.xref, "Table"].allowed is None
        assert rows[grouping.xref, "Table"].allowed is True
        assert rows[inline.xref, "Table"].allowed is False
        assert rows[content.xref, "Table"].allowed is False
        rows = _rules(doc, grouping_usage={ambiguous.xref: True, inline.xref: True, grouping.xref: False})
        assert rows[ambiguous.xref, "Table"].allowed is True
        assert rows[inline.xref, "Table"].allowed is False
        assert rows[grouping.xref, "Table"].allowed is False
        with pytest.raises(ValueError, match="outside"):
            doc.structure.child_rules(grouping_usage={100_000: True})


@pytest.mark.spec("structure-children", "interpret")
@pytest.mark.parametrize("incremental", [False, True])
def test_text_paint_and_warichu_assembly(*, incremental: bool) -> None:
    with pdf.open(_tagged()) as doc:
        root = doc.structure[0]
        ns = root.obj["NS"]
        page = doc.pages[0].obj
        stream = page["Contents"]
        stream.write(stream.read() + b" /Span << /MCID 4 >> BDC 0 0 15 15 re f EMC ")
        p = _tag(root, "P", ns)
        tagged = _tag(p, "Warichu", ns)
        for role in ("WP", "WT", "WP"):
            _tag(tagged, role, ns)
        bad = _tag(p, "Warichu", ns)
        for role in ("WP", "WP", "WT"):
            _tag(bad, role, ns)
        plain = _tag(p, "Warichu", ns)
        plain.obj["Pg"] = page
        plain.obj["K"] = 0
        owners = {}
        for role in ("RB", "RT", "RP", "WT", "WP"):
            for mcid in (0, 4, 33):
                child = _tag(p, role, ns)
                child.obj["Pg"] = page
                child.obj["K"] = mcid
                owners[(role, mcid)] = child.xref
        result = doc.to_bytes(incremental=incremental)
        owner_ids = tagged.xref, bad.xref, plain.xref
    with pdf.open(result) as reopened:
        rows = _rules(reopened)
        good, wrong, direct = owner_ids
        assert (rows[good, "WP"].count, rows[good, "WP"].allowed) == (2, True)
        assert rows[wrong, "WT"].allowed is False
        assert rows[direct, "content item"].allowed is True
        assert (direct, "WT") not in rows
        for (role, mcid), number in owners.items():
            row = rows[number, "content item"]
            assert row.rule == f"text content of {role}"
            assert row.allowed is {0: True, 4: False, 33: None}[mcid]
