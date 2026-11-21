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
        assert doc.structure.obj is not None
        doc.structure.obj["Namespaces"].extend([custom, middle])
        section = _tag(root, "Sect", standard)
        _tag(section, "H", standard)
        box = _tag(_tag(section, "Div", standard), "NonStruct", standard)
        _tag(box, "ListTitle", custom)
        _tag(section, "Legacy", custom)
        _tag(section, "Unknown", custom)
        _tag(section, "Loop", custom)
        xref, box_xref = section.xref, box.xref
        result = doc.to_bytes(incremental=incremental)
        if incremental:
            assert result.startswith(source)
    with pdf.open(result) as doc:
        rows = _rules(doc)
        assert (rows[xref, "H"].count, rows[xref, "H"].allowed) == (2, False)
        assert rows[box_xref, "H"].allowed is False
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


@pytest.mark.spec("structure-children", "create")
@pytest.mark.spec("structure-children", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_checked_transaction_refuses_a_second_heading_through_the_mapped_role(*, incremental: bool) -> None:
    source = _tagged()
    with pdf.open(source) as doc:
        root = doc.structure[0]
        standard = root.obj["NS"]
        custom = doc.objects[doc.objects.add({"Type": pdf.Name("Namespace"), "NS": "urn:publishing"})]
        custom["RoleMapNS"] = {"Headline": [pdf.Name("H"), standard]}
        with doc.structure.edit():
            section = _tag(root, "Sect", standard)
            _tag(section, "Headline", custom)
        before = doc.to_bytes(incremental=incremental)
        with pytest.raises(ValueError, match="2 H violates optional"), doc.structure.edit():
            _tag(_tag(section, "Div", standard), "H", standard)
        assert doc.to_bytes(incremental=incremental) == before
        section_xref = section.xref
    with pdf.open(before) as reopened:
        row = _rules(reopened)[section_xref, "H"]
        assert (row.count, row.allowed) == (1, True)
    with pikepdf.open(io.BytesIO(before)) as independent:
        parent = independent.Root.StructTreeRoot.K.K[-1]
        only = parent.K[0] if isinstance(parent.K, pikepdf.Array) else parent.K
        assert str(only.S) == "/Headline"
        assert str(only.NS.RoleMapNS.Headline[0]) == "/H"


@pytest.mark.spec("structure-children", "interpret")
def test_namespace_bounds_diagnostics_and_independent_producer() -> None:
    # Generate the source with a separate PDF implementation. The same 32-hop
    # path is valid from the second namespace, and one hop too long from the first.
    with pikepdf.open(io.BytesIO(_tagged())) as independent:
        root = independent.Root.StructTreeRoot
        parent = root.K
        standard = parent.NS
        spaces = [
            independent.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Namespace, NS=f"urn:alternate:{i}"))
            for i in range(33)
        ]
        targets = [*spaces[1:], standard]
        for ns, target in zip(spaces, targets, strict=True):
            ns.RoleMapNS = pikepdf.Dictionary(H=pikepdf.Array([pikepdf.Name.H, target]))
        root.Namespaces.extend(spaces)
        heading = independent.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.StructElem, S=pikepdf.Name.H, P=parent, NS=spaces[1])
        )
        parent.K.append(heading)
        saved = io.BytesIO()
        independent.save(saved)
    with pdf.open(saved.getvalue()) as doc:
        root = doc.structure[0]
        child = root.children[-1]
        assert _rules(doc)[root.xref, "H"].allowed is True
        assert doc.structure.obj is not None
        all_namespaces = doc.structure.obj["Namespaces"]
        child.obj["NS"] = all_namespaces[1]
        with pytest.raises(pdf.LimitError, match="32 namespace"):
            doc.structure.child_rules()
        child.obj["NS"] = {"NS": "urn:direct"}
        assert _rules(doc)[root.xref, "H"].rule == "invalid namespace reference"
        child.obj["NS"] = doc.objects[doc.objects.add({"Type": pdf.Name("Namespace")})]
        assert _rules(doc)[root.xref, "H"].rule == "missing namespace identifier"


@pytest.mark.spec("structure-children", "interpret")
def test_text_graphics_and_opaque_form_paint_in_the_same_marked_content() -> None:
    with pdf.open(_tagged()) as doc:
        root = doc.structure[0]
        ns = root.obj["NS"]
        page = doc.pages[0].obj
        page["Resources"] = {"XObject": {"Nested": doc.objects[14]}}
        stream = page["Contents"]
        stream.write(
            stream.read()
            + b" /Span << /MCID 4 >> BDC BT (A) Tj ET /Nested Do EMC"
            + b" /Span << /MCID 5 >> BDC BT (B) Tj ET 2 2 9 9 re S EMC"
            + b" /Span << /MCID 6 /ActualText (C) >> BDC /Nested Do EMC"
            + b" /Span << /MCID 7 >> BDC /Artifact BMC 2 2 9 9 re f EMC BT (D) Tj ET EMC"
        )
        owners = {}
        for mcid in (4, 5, 6, 7):
            rt = _tag(root, "RT", ns)
            rt.obj["Pg"] = page
            rt.obj["K"] = mcid
            owners[mcid] = rt.xref
        external = _tag(root, "WP", ns)
        external.obj["K"] = {"Type": pdf.Name("MCR"), "Pg": page, "Stm": doc.objects[14], "MCID": 0}
        rows = _rules(doc)
        assert [rows[owners[mcid], "content item"].allowed for mcid in (4, 5, 6, 7)] == [None, False, True, True]
        assert rows[external.xref, "content item"].allowed is None


@pytest.mark.spec("structure-children", "interpret")
def test_caption_cannot_be_between_two_content_items_and_declarations_are_typed() -> None:
    with pdf.open(_tagged()) as doc:
        root = doc.structure[0]
        ns = root.obj["NS"]
        figure = _tag(root, "Figure", ns)
        caption = _tag(figure, "Caption", ns)
        figure.obj["Pg"] = doc.pages[0].obj
        figure.obj["K"] = [0, caption.obj, 1]
        row = _rules(doc)[figure.xref, "Caption"]
        assert (row.rule, row.allowed) == ("single first or last caption", False)
        with pytest.raises(ValueError, match="positive object numbers"):
            doc.structure.child_rules(grouping_usage={False: True})
