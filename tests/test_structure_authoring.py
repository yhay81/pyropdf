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


@pytest.mark.spec("logical-structure", "create")
@pytest.mark.parametrize("incremental", [False, True])
def test_attribute_class_requires_an_attribute_owner_and_preserves_private_streams(*, incremental: bool) -> None:
    source = numbered(1)
    with pyropdf.open(source) as doc:
        element = doc.structure.add("P")
        root = doc.structure.obj
        assert root is not None
        private = doc.objects[doc.objects.add_stream(b"encoded attributes", {"O": pyropdf.Name("Private")})]
        root["ClassMap"] = {
            "BadNumber": 42,
            "BadOwner": [{"O": pyropdf.Name("Layout")}, {"O": "wrong type"}],
            "Missing": {},
            "Private": [private, {"O": pyropdf.Name("Layout")}],
        }
        element.add_class("Private")
        for invalid in ("BadNumber", "BadOwner", "Missing"):
            before = doc.to_bytes(incremental=True)
            with pytest.raises(pyropdf.MalformedPdfError, match="attribute definition"):
                element.add_class(invalid)
            assert doc.to_bytes(incremental=True) == before
        result = doc.to_bytes(incremental=incremental)
        root["ClassMap"] = private
        before = doc.to_bytes(incremental=True)
        with pytest.raises(pyropdf.MalformedPdfError, match="ClassMap"):
            element.add_class("O")
        assert doc.to_bytes(incremental=True) == before
    with pikepdf.open(io.BytesIO(result)) as pdf:
        tree = pdf.Root.StructTreeRoot
        assert str(tree.K[0].C) == "/Private"
        assert tree.ClassMap.Private[0].read_bytes() == b"encoded attributes"
        assert str(tree.ClassMap.Private[0].O) == "/Private"


@pytest.mark.spec("tagged-pdf", "create")
@pytest.mark.spec("structure-children", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_whole_xobject_page_requires_a_drawing_in_its_effective_resources(*, incremental: bool) -> None:
    source = numbered(2)
    with pyropdf.open(source) as doc:
        objects = doc.objects
        attrs = {"Type": pyropdf.Name("XObject"), "Subtype": pyropdf.Name("Form"), "BBox": [0, 0, 4, 4]}
        tile = objects[objects.add_stream(b"0 0 4 4 re f", attrs)]
        unused = objects[objects.add_stream(b"", attrs)]
        outer = objects[objects.add_stream(b"/Nested Do", {**attrs, "Resources": {"XObject": {"Nested": tile}}})]
        # Both pages inherit the resource dictionary. Only page two invokes the outer Form.
        doc.catalog["Pages"]["Resources"] = {"XObject": {"Outer": outer, "Unused": unused}}
        blank, drawn = doc.pages
        for page in (blank, drawn):
            if "Resources" in page.obj:
                del page.obj["Resources"]
        blank.obj["Contents"] = objects[objects.add_stream(b"")]
        drawn.obj["Contents"] = objects[objects.add_stream(b"/Outer Do")]
        element = doc.structure.add("Figure", page=drawn)
        before = doc.to_bytes(incremental=True)
        for target, wrong_page in ((tile, blank), (unused, drawn)):
            with pytest.raises(ValueError, match="specified page"):
                element.set_object_reference(target, page=wrong_page)
            assert doc.to_bytes(incremental=True) == before
        element.set_object_reference(tile, page=drawn)
        before = doc.to_bytes(incremental=True)
        with pytest.raises(ValueError, match="specified page"):
            element.set_object_reference(unused, page=drawn, replace=tile)
        assert doc.to_bytes(incremental=True) == before
        result = doc.to_bytes(incremental=incremental)
    assert result.startswith(source) is incremental
    with pikepdf.open(io.BytesIO(result)) as pdf:
        ref = pdf.Root.StructTreeRoot.K[0].K[0]
        assert ref.Pg.objgen == pdf.pages[1].objgen
        assert ref.Obj.objgen == pdf.pages[1].Resources.XObject.Outer.Resources.XObject.Nested.objgen
        assert ref.Obj.StructParent == pdf.Root.StructTreeRoot.ParentTree.Nums[0]


@pytest.mark.parametrize("incremental", [False, True])
def test_image_object_reference_can_use_a_nested_annotation_appearance(*, incremental: bool) -> None:
    source = numbered(2)
    with pyropdf.open(source) as doc:
        objects = doc.objects
        image = objects[
            objects.add_stream(
                b"\x7f",
                {
                    "Type": pyropdf.Name("XObject"),
                    "Subtype": pyropdf.Name("Image"),
                    "Width": 1,
                    "Height": 1,
                    "BitsPerComponent": 8,
                    "ColorSpace": pyropdf.Name("DeviceGray"),
                },
            )
        ]
        appearance = objects[
            objects.add_stream(
                b"/Image Do",
                {
                    "Subtype": pyropdf.Name("Form"),
                    "BBox": [0, 0, 1, 1],
                    "Resources": {"XObject": {"Image": image}},
                },
            )
        ]
        annotation = _annot(doc, 1, "Hidden")
        annotation["F"] = 2
        annotation["AP"] = {"N": {"Shown": appearance}}
        annotation["AS"] = pyropdf.Name("Shown")
        element = doc.structure.add("Figure", page=1)
        with pytest.raises(ValueError, match="specified page"):
            element.set_object_reference(image, page=0)
        element.set_object_reference(image, page=1)
        result = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(result)) as pdf:
        ref = pdf.Root.StructTreeRoot.K[0].K[0]
        assert ref.Pg.objgen == pdf.pages[1].objgen
        assert ref.Obj.objgen == pdf.pages[1].Annots[-1].AP.N.Shown.Resources.XObject.Image.objgen


def test_whole_xobject_lookup_shares_the_edits_decode_budget() -> None:
    with pyropdf.open(numbered(1)) as source:
        form = {"Subtype": pyropdf.Name("Form"), "BBox": [0, 0, 1, 1]}
        tile = source.objects[source.objects.add_stream(b"", form)]
        outer = source.objects[
            source.objects.add_stream(b"q Q " * 1024 + b"/Tile Do", {**form, "Resources": {"XObject": {"Tile": tile}}})
        ]
        page = source.pages[0]
        page.obj["Resources"] = {"XObject": {"Outer": outer}}
        page.obj["Contents"] = source.objects[source.objects.add_stream(b"/Outer Do")]
        data = source.to_bytes()
    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_decompressed_size=1024)) as doc:
        element = doc.structure.add("Figure", page=0)
        target = doc.pages[0].obj["Resources"]["XObject"]["Outer"]["Resources"]["XObject"]["Tile"]
        position = doc.history.position
        saved = doc.to_bytes(incremental=True)
        with pytest.raises(pyropdf.LimitError) as caught:
            element.set_object_reference(target, page=0)
        assert caught.value.code == "decompressed_size"
        assert doc.history.position == position
        assert doc.to_bytes(incremental=True) == saved
        assert "StructParent" not in target


@pytest.mark.spec("structure-children", "create")
@pytest.mark.spec("structure-children", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_shared_xobject_has_one_parent_key_and_one_reference_per_drawn_page(*, incremental: bool) -> None:
    source = numbered(2)
    with pyropdf.open(source) as doc:
        objects = doc.objects
        attrs = {"Type": pyropdf.Name("XObject"), "Subtype": pyropdf.Name("Form"), "BBox": [0, 0, 2, 2]}
        shared = objects[objects.add_stream(b"0 0 1 1 re f", attrs)]
        other = objects[objects.add_stream(b"0 0 2 2 re f", attrs)]
        for page in doc.pages:
            page.obj["Resources"] = {"XObject": {"Tile": shared, "Other": other}}
            page.obj["Contents"] = objects[objects.add_stream(b"/Tile Do /Tile Do /Other Do")]
        parent = doc.structure.add("Figure", page=0)
        foreign = doc.structure.add("Figure", page=1)
        parent.set_object_reference(shared, page=0)
        with pytest.raises(ValueError, match="structure parent"):
            foreign.set_object_reference(shared, page=1)
        first_key = shared["StructParent"]
        parent.set_object_reference(shared, page=1)
        before = doc.to_bytes(incremental=True)
        with pytest.raises(ValueError, match="specified page"):
            parent.set_object_reference(shared, page=1)
        assert doc.to_bytes(incremental=True) == before
        with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as pdf:
            root = pdf.Root.StructTreeRoot
            refs = root.K[0].K
            assert [ref.Pg.objgen for ref in refs] == [page.obj.objgen for page in pdf.pages]
            assert refs[0].Obj.objgen == refs[1].Obj.objgen
            assert len(root.ParentTree.Nums) == 2
            assert refs[0].Obj.StructParent == root.ParentTree.Nums[0] == first_key
        parent.set_object_reference(other, page=1, replace=shared)
        assert shared["StructParent"] == first_key
        assert other["StructParent"] != first_key
        saved = doc.to_bytes(incremental=incremental)
        del doc.pages[0]
        after_delete = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        root = pdf.Root.StructTreeRoot
        refs = root.K[0].K
        assert [ref.Pg.objgen for ref in refs] == [page.obj.objgen for page in pdf.pages]
        assert refs[0].Obj.objgen == pdf.pages[0].Resources.XObject.Tile.objgen
        assert refs[1].Obj.objgen == pdf.pages[1].Resources.XObject.Other.objgen
        keys = list(root.ParentTree.Nums)
        assert [refs[0].Obj.StructParent, refs[1].Obj.StructParent] == keys[::2]
        assert all(owner.objgen == root.K[0].objgen for owner in keys[1::2])
    with pikepdf.open(io.BytesIO(after_delete)) as pdf:
        (ref,) = pdf.Root.StructTreeRoot.K[0].K
        assert ref.Pg.objgen == pdf.pages[0].objgen
        assert ref.Obj.objgen == pdf.pages[0].Resources.XObject.Other.objgen
