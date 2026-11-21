"""Author ISO 32000 structure attributes and reciprocal object references across revisions."""

from __future__ import annotations

import io

import pikepdf
import pytest
from test_pages import numbered
from test_structure_reading import _tagged

import pyropdf


@pytest.mark.spec("logical-structure", "create")
@pytest.mark.spec("tagged-pdf", "create")
@pytest.mark.parametrize("incremental", [False, True])
def test_classes_and_user_properties_preserve_other_owners_and_revisions(*, incremental: bool) -> None:
    source = numbered(2)
    with pyropdf.open(source) as doc:
        doc.catalog["XXRetained"] = "original"
        element = doc.structure.add("Document").children.add("Table")
        tree = doc.structure
        tree.set_class_attributes("Grid", "Layout", {"Placement": "Block"})
        tree.set_class_attributes("Grid", "Table", {"RowSpan": 2})
        tree.set_class_attributes("Grid", "Table", {"ColSpan": 3})
        element.add_class("Grid")
        element.add_class("Grid")
        shared = doc.objects[doc.objects.add({"O": pyropdf.Name("Layout"), "StartIndent": 1})]
        doc.catalog["XXShared"] = shared
        element.obj["A"] = [shared, 2]
        element.set_attributes("Layout", {"TextAlign": "Center"})
        element.set_user_property("Price", 7, formatted="seven", hidden=True)
        element.set_user_property("City", "東京")
        element.set_user_property("Price", 9, formatted="nine")
        assert doc.catalog["XXShared"]["StartIndent"] == 1
        assert "TextAlign" not in shared
        result = doc.to_bytes(incremental=incremental)
    assert result.startswith(source) is incremental
    with pikepdf.open(io.BytesIO(result)) as pdf:
        root = pdf.Root.StructTreeRoot
        element = root.K[0].K[0]
        assert pdf.Root.XXRetained == "original"
        assert pdf.Root.MarkInfo.UserProperties
        assert pikepdf.Name.Grid == element.C
        layout, table = root.ClassMap.Grid
        assert layout.Placement == pikepdf.Name.Block
        assert (table.RowSpan, table.ColSpan) == (2, 3)
        layout, revision, props = element.A
        assert (layout.O, revision, layout.TextAlign) == (pikepdf.Name.Layout, 2, pikepdf.Name.Center)
        assert "/TextAlign" not in pdf.Root.XXShared
        price, city = props.P
        assert (str(price.N), price.V, str(price.F), bool(price.H)) == ("Price", 9, "nine", False)
        assert (str(city.N), str(city.V)) == ("City", "東京")


def _annot(doc: pyropdf.Document, page: int, label: str) -> pyropdf.Dictionary:
    p = doc.pages[page]
    entry = doc.objects[
        doc.objects.add(
            {
                "Type": pyropdf.Name("Annot"),
                "Subtype": pyropdf.Name("Text"),
                "P": p.obj,
                "Rect": [0, 0, 12, 12],
                "Contents": label,
            }
        )
    ]
    p.obj["Annots"] = [*p.obj.get("Annots", []), entry]
    return entry


@pytest.mark.spec("logical-structure", "create")
@pytest.mark.spec("structure-children", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_object_reference_retarget_keeps_parent_tree_and_identity_across_pages(*, incremental: bool) -> None:
    source = numbered(3)
    with pyropdf.open(source) as doc:
        parent = doc.structure.add("Document")
        elem = parent.children.add("Annot")
        first = _annot(doc, 0, "first")
        last = _annot(doc, 2, "last")
        ref = elem.set_object_reference(first, page=0)
        assert ref.page == 0
        assert ref.xref == first.xref
        key = first["StructParent"]
        root = doc.structure.obj
        assert root is not None
        root["XXRetained"] = b"keep"
        assert last.xref is not None
        elem.set_object_reference(last, page=2, replace=first)
        assert "StructParent" not in first
        assert last["StructParent"] == key
        doc.pages.move(2, 0)
        assert elem.items == (pyropdf.ObjectReference(xref=last.xref, page=0),)
        del doc.pages[1]
        assert elem.items == (pyropdf.ObjectReference(xref=last.xref, page=0),)
        result = doc.to_bytes(incremental=incremental)
    assert result.startswith(source) is incremental
    with pikepdf.open(io.BytesIO(result)) as pdf:
        root = pdf.Root.StructTreeRoot
        assert bytes(root.XXRetained) == b"keep"
        element = root.K[0].K[0]
        objr = element.K[0]
        assert objr.Type == pikepdf.Name.OBJR
        assert objr.Pg.objgen == pdf.pages[0].objgen
        assert objr.Obj.objgen == pdf.pages[0].Annots[0].objgen
        number, owner = root.ParentTree.Nums
        assert number == objr.Obj.StructParent
        assert owner.objgen == element.objgen


@pytest.mark.spec("structure-children", "create", "edit")
def test_checked_structure_edit_rolls_back_an_invalid_second_table_head_and_reparent() -> None:
    with pyropdf.open(_tagged()) as doc:
        root = doc.structure[0]
        ns = root.obj["NS"]["NS"].text
        with doc.structure.edit():
            table = root.children.add("Table", namespace=ns)
            head = table.children.add("THead", namespace=ns)
        before = doc.to_bytes()
        with pytest.raises(ValueError, match=r"Table with 2 THead"), doc.structure.edit():
            table.children.add("THead", namespace=ns)
        with pytest.raises(ValueError, match="violates"), doc.structure.edit():
            head.move(parent=root)
        assert doc.to_bytes() == before


def test_invalid_attribute_or_reference_rolls_back_without_losing_unrelated_data() -> None:
    with pyropdf.open(numbered(2)) as doc, pyropdf.open(numbered(1)) as other:
        element = doc.structure.add("Annot")
        first = _annot(doc, 0, "first")
        second = _annot(doc, 1, "second")
        element.set_object_reference(first, page=0)
        doc.catalog["XXRetained"] = 42
        before = doc.to_bytes()
        with pytest.raises(ValueError, match="undefined"):
            element.add_class("Missing")
        with pytest.raises(ValueError, match="positive integer"):
            element.set_attributes("Table", {"RowSpan": 0})
        with pytest.raises(ValueError, match="not a /Layout"):
            element.set_attributes("Layout", {"RowSpan": 1})
        with pytest.raises(ValueError, match="belong"):
            element.set_object_reference(second, page=0)
        with pytest.raises(ValueError, match="structure parent"):
            element.set_object_reference(first, page=0)
        with pytest.raises(ValueError, match="of this document"):
            element.set_object_reference(other.pages[0].obj, page=0)
        assert doc.to_bytes() == before
        root = doc.structure.obj
        assert root is not None
        root["ParentTree"]["Nums"][1] = doc.structure.add("Div").obj
        before = doc.to_bytes()
        with pytest.raises(pyropdf.MalformedPdfError, match="does not map"):
            element.set_object_reference(second, replace=first, page=1)
        assert doc.to_bytes() == before


def test_last_matching_owner_wins_and_attribute_array_values_survive_independent_reopen() -> None:
    with pyropdf.open(numbered(1)) as doc:
        cell = doc.structure.add("TH")
        first = {"O": pyropdf.Name("Layout"), "TextAlign": pyropdf.Name("End")}
        last = {"O": pyropdf.Name("Layout"), "TextAlign": pyropdf.Name("Start"), "XXRetained": 42}
        cell.obj["A"] = [first, last]
        cell.set_attributes("Layout", {"TextAlign": "Center", "Color": [0.1, 0.2, 0.3]})
        cell.set_attributes("Table", {"Headers": [b"row1", b"col1"], "Scope": "Both"})
        with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
            before, after, table = pdf.Root.StructTreeRoot.K[0].A
            assert before.TextAlign == pikepdf.Name.End
            assert after.TextAlign == pikepdf.Name.Center
            assert after.XXRetained == 42
            assert [float(n) for n in after.Color] == [0.1, 0.2, 0.3]
            assert (list(map(bytes, table.Headers)), table.Scope) == ([b"row1", b"col1"], pikepdf.Name.Both)


def test_new_checked_namespace_is_registered_once_and_rejects_malformed_uris_atomically() -> None:
    uri = "http://iso.org/pdf2/ssn"
    with pyropdf.open(numbered(1)) as doc:
        with doc.structure.edit() as tree:
            root = tree.add("Document", namespace=uri)
            root.children.add("Sect", namespace=uri)
        with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
            (ns,) = pdf.Root.StructTreeRoot.Namespaces
            parent = pdf.Root.StructTreeRoot.K[0]
            assert str(ns.NS) == uri
            assert ns.objgen == parent.NS.objgen == parent.K[0].NS.objgen
            assert pdf.trailer.ID
        original = doc.to_bytes()
        with pytest.raises(ValueError, match="absolute URI"):
            root.children.add("P", namespace="relative name")
        assert doc.to_bytes() == original


def test_namespace_attribute_owners_and_standard_export_classes_keep_distinct_values() -> None:
    with pyropdf.open(numbered(1)) as doc:
        elem = doc.structure.add("Document", namespace="http://iso.org/pdf2/ssn")
        tree = doc.structure
        tree.set_class_attributes("Export", "HTML-5.00", {"colspan": 2})
        elem.add_class("Export")
        for uri, value in [("urn:owner:a", 5), ("urn:owner:b", 8), ("urn:owner:a", 7)]:
            elem.set_attributes("NSO", {"Count": value}, namespace=uri)
        with pytest.raises(ValueError, match="namespace URI"):
            elem.set_attributes("NSO", {"Count": 0})
        with pytest.raises(ValueError, match="owner and namespace"):
            elem.set_attributes("HTML-5.00", {"O": pyropdf.Name("Table")})
        with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
            root = pdf.Root.StructTreeRoot
            assert str(root.ClassMap.Export.O) == "/HTML-5.00"
            assert root.ClassMap.Export.colspan == 2
            assert [(str(a.NS.NS), a.Count) for a in root.K[0].A] == [("urn:owner:a", 7), ("urn:owner:b", 8)]
            assert len(root.Namespaces) == 3


@pytest.mark.parametrize("incremental", [False, True])
def test_a_whole_form_reference_names_the_drawn_stream_and_surviving_page(*, incremental: bool) -> None:
    source = numbered(2)
    with pyropdf.open(source) as doc:
        page = doc.pages[1]
        form = doc.objects[
            doc.objects.add_stream(
                b"0 0 1 1 re f",
                {
                    "Type": pyropdf.Name("XObject"),
                    "Subtype": pyropdf.Name("Form"),
                    "BBox": [0, 0, 1, 1],
                    "Resources": {},
                },
            )
        ]
        page.obj["Resources"] = {"XObject": {"Tile": form}}
        page.obj["Contents"] = doc.objects[doc.objects.add_stream(b"/Tile Do")]
        element = doc.structure.add("Figure", page=page, alt="One tile")
        element.set_object_reference(form, page=page)
        del doc.pages[0]
        saved = doc.to_bytes(incremental=incremental)
    assert saved.startswith(source) is incremental
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        only = pdf.pages[0]
        root = pdf.Root.StructTreeRoot
        elem = root.K[0]
        ref = elem.K[0]
        assert ref.Pg.objgen == only.objgen
        assert ref.Obj.objgen == only.Resources.XObject.Tile.objgen
        assert only.Contents.read_bytes() == b"/Tile Do"
        assert ref.Obj.read_bytes() == b"0 0 1 1 re f"
        assert root.ParentTree.Nums[0] == ref.Obj.StructParent
        assert root.ParentTree.Nums[1].objgen == elem.objgen
