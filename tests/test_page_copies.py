"""Copies of pages within a document carry annotations, widgets, and structure of their own.

pylopdf's tests/test_page_copies.py at ebb01d3c, ported. Intended
differences:

- the widget case builds its form as a raw PDF and reads the fields through
  the catalog, since pyropdf has no ``page.widgets`` or ``doc.form`` yet
  (yhay81/pyropdf#156).
"""

from __future__ import annotations

import pytest

import pyropdf
from pyropdf import Color, Destination, Note, Square


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _kids(element: pyropdf.Dictionary) -> list[pyropdf.Dictionary]:
    kids = element["K"]
    return list(kids) if isinstance(kids, pyropdf.Array) else [kids]


def test_annotations_become_objects_of_the_copy() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    square = page.annotations.add(Square((20, 20, 80, 80), color=Color(0, 0, 1), contents="Original"))
    note = page.annotations.add(Note((150, 40), contents="Note"))
    note.popup_rect = (150, 100, 250, 160)
    page.links.add((20, 200, 100, 220), Destination(0, point=(10, 10)))
    del square, note

    doc.pages.insert(1, doc.pages[0])

    original, copy = doc.pages
    assert len(copy.annotations) == len(original.annotations) == 2
    for old, new in zip(original.annotations, copy.annotations, strict=True):
        assert new.xref != old.xref
        assert new.obj["P"].xref == copy.xref
        assert (new.type, new.rect) == (old.type, old.rect)
    popup = copy.annotations[1].obj["Popup"]
    assert popup.xref != original.annotations[1].obj["Popup"].xref
    assert popup["Parent"].xref == copy.annotations[1].xref
    assert copy.links[0].target == original.links[0].target
    assert copy.links[0].xref != original.links[0].xref

    copy.annotations[0].contents = "Edited"
    assert original.annotations[0].contents == "Original"


def _form() -> bytes:
    widget = "/Type /Annot /Subtype /Widget /P 3 0 R"
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R 5 0 R 6 0 R] >> >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Annots [4 0 R 5 0 R 7 0 R 8 0 R] >>",
            4: f"<< {widget} /FT /Tx /T (name) /V (Ada) /Rect [20 20 200 44] >>",
            5: f"<< {widget} /FT /Btn /T (agree) /V /Off /AS /Off /Rect [20 60 36 76] >>",
            6: "<< /FT /Btn /Ff 32768 /T (plan) /V /Basic /Kids [7 0 R 8 0 R] >>",
            7: f"<< {widget} /Parent 6 0 R /AS /Basic /Rect [20 90 36 106] >>",
            8: f"<< {widget} /Parent 6 0 R /AS /Off /Rect [50 90 66 106] >>",
        }
    )


def _fields(doc: pyropdf.Document) -> dict[str, pyropdf.Dictionary]:
    return {field["T"].text: field for field in doc.catalog["AcroForm"]["Fields"]}


def _widgets(page: pyropdf.Page) -> list[pyropdf.Dictionary]:
    return [annotation for annotation in page.obj["Annots"] if annotation["Subtype"] == "Widget"]


def test_widgets_join_their_fields() -> None:
    doc = pyropdf.open(_form())

    doc.pages.extend(doc.pages[0])

    fields = _fields(doc)
    assert sorted(fields) == ["agree", "name", "plan"]
    assert [len(fields[name]["Kids"]) for name in ("name", "agree", "plan")] == [2, 2, 4]
    assert fields["name"]["V"].text == "Ada"
    original, copy = doc.pages
    assert {widget.xref for widget in _widgets(copy)}.isdisjoint(widget.xref for widget in _widgets(original))
    assert all(widget["P"].xref == copy.xref for widget in _widgets(copy))
    for widget in _widgets(copy):
        assert "T" not in widget
        assert widget["Parent"]["T"].text in fields

    reopened = pyropdf.open(doc.to_bytes())
    fields = _fields(reopened)
    assert sorted(fields) == ["agree", "name", "plan"]
    assert {kid["Parent"].xref for kid in fields["name"]["Kids"]} == {fields["name"].xref}
    assert [len(_widgets(page)) for page in reopened.pages] == [4, 4]


def _tagged() -> bytes:
    content = (
        "/P <</MCID 0>> BDC BT /F1 12 Tf 20 150 Td (Text) Tj ET EMC "
        "/Figure <</MCID 1>> BDC 0 0 1 rg 20 20 40 40 re f EMC"
    )
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 6 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /StructParents 0 "
                "/Annots [11 0 R] /Resources << /Font << /F1 5 0 R >> >> >>"
            ),
            4: f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            6: (
                "<< /Type /StructTreeRoot /K 7 0 R /ParentTree << /Nums [0 [9 0 R 10 0 R] 1 12 0 R] >> "
                "/ParentTreeNextKey 2 >>"
            ),
            7: "<< /Type /StructElem /S /Document /P 6 0 R /K [8 0 R] >>",
            8: "<< /Type /StructElem /S /Sect /P 7 0 R /K [9 0 R 10 0 R 12 0 R] >>",
            9: "<< /Type /StructElem /S /P /P 8 0 R /Pg 3 0 R /K 0 /ID (para) >>",
            10: "<< /Type /StructElem /S /Figure /P 8 0 R /Pg 3 0 R /K [1] /Alt (Square) >>",
            11: (
                "<< /Type /Annot /Subtype /Link /Rect [20 100 80 120] /StructParent 1 /P 3 0 R "
                "/A << /S /URI /URI (https://example.test/) >> >>"
            ),
            12: ("<< /Type /StructElem /S /Link /P 8 0 R /Pg 3 0 R /K [<< /Type /OBJR /Obj 11 0 R /Pg 3 0 R >>] >>"),
        }
    )


def _parent_tree(root: pyropdf.Dictionary) -> dict[int, list[pyropdf.Dictionary]]:
    """Return the parent tree as keys to lists of elements; a lone element reads as a list of one."""
    nums = list(root["ParentTree"]["Nums"])
    return {
        key: list(value) if isinstance(value, pyropdf.Array) else [value]
        for key, value in zip(nums[::2], nums[1::2], strict=True)
    }


def test_tagged_copies_get_elements_and_keys_of_their_own() -> None:
    doc = pyropdf.open(_tagged())

    doc.pages.extend(doc.pages[0])

    original, copy = doc.pages
    root = doc.catalog["StructTreeRoot"]
    (document,) = _kids(root)
    sections = _kids(document)
    assert [str(section["S"]) for section in sections] == ["Sect", "Sect"]
    copied = sections[1]
    assert copied.xref != sections[0].xref
    assert [str(kid["S"]) for kid in _kids(copied)] == ["P", "Figure", "Link"]
    paragraph, figure, link = _kids(copied)
    assert (paragraph["Pg"].xref, figure["Pg"].xref) == (copy.xref, copy.xref)
    assert "ID" not in paragraph
    assert figure["Alt"].text == "Square"
    assert link["K"][0]["Obj"].xref == copy.links[0].xref

    tree = _parent_tree(root)
    page_key = copy.obj["StructParents"]
    assert page_key not in (0, 1)
    assert [element.xref for element in tree[page_key]] == [paragraph.xref, figure.xref]
    link_key = copy.links[0].obj["StructParent"]
    assert [element.xref for element in tree[link_key]] == [link.xref]
    assert root["ParentTreeNextKey"] == max(tree) + 1
    assert original.obj["StructParents"] == 0
    assert copy.text().split() == ["Text"]

    reopened = pyropdf.open(doc.to_bytes())
    tree = _parent_tree(reopened.catalog["StructTreeRoot"])
    for page in reopened.pages:
        for element in tree[page.obj["StructParents"]]:
            assert element["Pg"].xref == page.xref


def test_untagged_copies_keep_no_structure() -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 200))

    doc.pages.insert(0, doc.pages[0])

    assert "StructTreeRoot" not in doc.catalog
    assert "StructParents" not in doc.pages[0].obj


def test_a_copy_undoes_in_one_step() -> None:
    doc = pyropdf.open(_tagged())
    before = doc.to_bytes()

    doc.pages.extend([doc.pages[0], doc.pages[0]])

    assert len(doc.pages) == 3
    doc.history.undo()
    assert len(doc.pages) == 1
    assert doc.to_bytes() == before


def _minimal_tagged(extra: dict[int, str] | None = None) -> dict[int, str]:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 6 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /StructParents 0 >>",
        6: "<< /Type /StructTreeRoot /K 7 0 R /ParentTree 8 0 R >>",
        7: "<< /Type /StructElem /S /P /P 6 0 R /Pg 3 0 R /K 0 >>",
        8: "<< /Nums [0 [7 0 R]] /Limits [0 0] >>",
    }
    objects.update(extra or {})
    return objects


def test_copy_preserves_parent_tree_pairs_behind_an_alias() -> None:
    doc = pyropdf.open(build_raw_pdf(_minimal_tagged({8: "9 0 R", 9: "<< /Nums [0 [7 0 R]] >>"})))
    doc.pages.extend(doc.pages[0])
    tree = _parent_tree(doc.catalog["StructTreeRoot"])
    assert 0 in tree
    assert tree[0][0].xref == 7
    assert doc.pages[1].obj["StructParents"] in tree


def test_copy_keeps_content_ids_in_an_indirect_k_array() -> None:
    doc = pyropdf.open(
        build_raw_pdf(_minimal_tagged({7: "<< /Type /StructElem /S /P /P 6 0 R /Pg 3 0 R /K 9 0 R >>", 9: "[0]"}))
    )
    doc.pages.extend(doc.pages[0])
    tree = _parent_tree(doc.catalog["StructTreeRoot"])
    copied = tree[doc.pages[1].obj["StructParents"]][0]
    assert list(copied["K"]) == [0]


def test_copy_updates_existing_parent_tree_limits() -> None:
    doc = pyropdf.open(build_raw_pdf(_minimal_tagged()))
    doc.pages.extend(doc.pages[0])
    root = doc.catalog["StructTreeRoot"]
    assert list(root["ParentTree"]["Limits"]) == [0, doc.pages[1].obj["StructParents"]]


@pytest.mark.parametrize("kind", ["Page", "Pages", "Catalog"])
def test_copy_refuses_parent_tree_aliases_of_other_identities(kind: str) -> None:
    doc = pyropdf.open(build_raw_pdf(_minimal_tagged({8: f"<< /Type /{kind} /Nums [0 [7 0 R]] >>"})))
    before = doc.to_bytes()
    with pytest.raises(pyropdf.MalformedPdfError):
        doc.pages.extend(doc.pages[0])
    assert doc.to_bytes() == before


def test_copy_refuses_excessive_structure_ancestry_without_partial_writes() -> None:
    objects = _minimal_tagged(
        {6: "<< /Type /StructTreeRoot /K 10 0 R /ParentTree 8 0 R >>", 8: "<< /Nums [0 [266 0 R]] >>"}
    )
    for number in range(10, 267):
        parent = 6 if number == 10 else number - 1
        kid = "0" if number == 266 else f"{number + 1} 0 R"
        objects[number] = f"<< /Type /StructElem /S /P /P {parent} 0 R /Pg 3 0 R /K {kid} >>"
    doc = pyropdf.open(build_raw_pdf(objects))
    before = doc.to_bytes()
    with doc.transaction("Catch a failed copy"):
        with pytest.raises(pyropdf.LimitError) as caught:
            doc.pages.extend(doc.pages[0])
        assert caught.value.code == "structure_tree_count"
    assert doc.to_bytes() == before


def test_copied_widgets_keep_only_widget_triggers_from_indirect_actions() -> None:
    doc = pyropdf.open(_form())
    actions = doc.objects.add(
        {"K": {"S": pyropdf.Name("JavaScript"), "JS": "field"}, "E": {"S": pyropdf.Name("JavaScript"), "JS": "widget"}}
    )
    doc.objects[4]["AA"] = doc.objects[actions]
    doc.pages.extend(doc.pages[0])
    copied = _widgets(doc.pages[1])[0]
    assert set(copied["AA"]) == {"E"}
    assert set(copied["Parent"]["AA"]) == {"K"}


def test_copy_does_not_retarget_annotation_references_of_another_generation() -> None:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [4 0 R 5 0 R] >>",
        4: "<< /Type /Annot /Subtype /Text /Rect [1 1 5 5] /Popup 5 1 R >>",
        5: "<< /Type /Annot /Subtype /Popup /Rect [1 1 5 5] /Parent 4 0 R >>",
    }
    doc = pyropdf.open(build_raw_pdf(objects))
    doc.pages.extend(doc.pages[0])
    copied = doc.pages[1].obj["Annots"][0].xref
    reopened = pyropdf.open(doc.to_bytes())
    raw = next(body for number, _, body in reopened._core()._objects() if number == copied)  # noqa: SLF001
    assert b"/Popup 5 1 R" in raw


@pytest.mark.parametrize("entry", ["Kids", "Fields"])
def test_copy_preserves_field_members_behind_an_array_alias(entry: str) -> None:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [4 0 R] >>",
        4: "<< /Type /Annot /Subtype /Widget /Parent 5 0 R /Rect [1 1 5 5] >>",
        5: "<< /FT /Tx /T (field) /Kids 6 0 R >>",
        6: "7 0 R",
        7: "[4 0 R]",
    }
    if entry == "Fields":
        objects[1] = "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields 6 0 R >> >>"
        objects[4] = "<< /Type /Annot /Subtype /Widget /FT /Tx /T (field) /Rect [1 1 5 5] >>"
        del objects[5]
    doc = pyropdf.open(build_raw_pdf(objects))
    doc.pages.extend(doc.pages[0])
    field = doc.catalog["AcroForm"]["Fields"][0]
    assert field["T"] == b"field"
    assert len(field["Kids"]) == 2
    assert field["Kids"][0].xref == 4


def test_copy_keeps_a_single_indirect_structure_child() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            _minimal_tagged(
                {
                    6: "<< /Type /StructTreeRoot /K 10 0 R /ParentTree 8 0 R >>",
                    7: "<< /Type /StructElem /S /P /P 10 0 R /Pg 3 0 R /K 0 >>",
                    10: "<< /Type /StructElem /S /Sect /P 6 0 R /K 7 0 R >>",
                }
            )
        )
    )
    doc.pages.extend(doc.pages[0])
    section = _kids(doc.catalog["StructTreeRoot"])[1]
    assert len(_kids(section)) == 1
    assert _kids(section)[0]["Pg"].xref == doc.pages[1].xref


def test_copy_follows_structure_owner_aliases() -> None:
    doc = pyropdf.open(
        build_raw_pdf(_minimal_tagged({7: "10 0 R", 10: "<< /Type /StructElem /S /P /P 6 0 R /Pg 3 0 R /K 0 >>"}))
    )
    doc.pages.extend(doc.pages[0])
    copy = doc.pages[1]
    owner = _parent_tree(doc.catalog["StructTreeRoot"])[copy.obj["StructParents"]][0]
    assert owner.xref != 10
    assert owner["Pg"].xref == copy.xref
    assert list(owner["K"]) == [0]


@pytest.mark.parametrize("alias", [False, True])
def test_copy_gives_direct_and_aliased_annotations_their_own_objects(*, alias: bool) -> None:
    annotation = "<< /Type /Annot /Subtype /Text /Rect [1 1 5 5] /Contents (original) >>"
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [{'4 0 R' if alias else annotation}] >>",
    }
    if alias:
        objects.update({4: "5 0 R", 5: annotation})
    doc = pyropdf.open(build_raw_pdf(objects))
    doc.pages.extend(doc.pages[0])
    copied = doc.pages[1].obj["Annots"][0]
    assert copied.xref is not None
    if alias:
        assert copied.xref not in (4, 5)
    copied["Contents"] = "edited"
    assert doc.pages[0].obj["Annots"][0]["Contents"] == b"original"


def test_copy_refuses_a_direct_merged_widget_atomically() -> None:
    widget = "<< /Type /Annot /Subtype /Widget /FT /Tx /T (name) /V (Ada) /Rect [1 1 5 5] >>"
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: f"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [{widget}] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [{widget}] >>",
            }
        )
    )
    before = doc.to_bytes()
    with pytest.raises(pyropdf.MalformedPdfError):
        doc.pages.extend(doc.pages[0])
    assert doc.to_bytes() == before


def test_copy_replaces_an_aliased_merged_field_instead_of_adding_a_duplicate() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [4 0 R] >>",
                4: "5 0 R",
                5: "<< /Type /Annot /Subtype /Widget /FT /Tx /T (name) /V (Ada) /Rect [1 1 5 5] >>",
            }
        )
    )
    doc.pages.extend(doc.pages[0])
    fields = doc.catalog["AcroForm"]["Fields"]
    assert len(fields) == 1
    assert fields[0]["T"] == b"name"
    assert len(fields[0]["Kids"]) == 2


@pytest.mark.parametrize("key", ["Kids", "Nums"])
def test_copy_extends_parent_tree_arrays_through_aliases(key: str) -> None:
    extra = {
        8: f"<< /{key} 9 0 R /Limits [0 0] >>",
        9: "10 0 R",
        10: "[11 0 R]" if key == "Kids" else "[0 [7 0 R]]",
        11: "<< /Nums [0 [7 0 R]] /Limits [0 0] >>",
    }
    doc = pyropdf.open(build_raw_pdf(_minimal_tagged(extra)))
    doc.pages.extend(doc.pages[0])
    tree = doc.catalog["StructTreeRoot"]["ParentTree"]
    assert list(tree["Limits"]) == [0, 1]
    if key == "Kids":
        assert len(tree["Kids"]) == 2
        assert next(iter(tree["Kids"][0]["Nums"])) == 0
        assert next(iter(tree["Kids"][1]["Nums"])) == 1
    else:
        assert list(tree["Nums"])[::2] == [0, 1]


def test_copy_joins_a_direct_child_widget_to_its_existing_field() -> None:
    widget = "<< /Type /Annot /Subtype /Widget /Parent 5 0 R /Rect [1 1 5 5] >>"
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [{widget}] >>",
                5: f"<< /FT /Tx /T (name) /V (Ada) /Kids [{widget}] >>",
            }
        )
    )
    doc.pages.extend(doc.pages[0])
    copied = doc.pages[1].obj["Annots"][0]
    assert copied.xref is not None
    assert copied["Parent"].xref == 5
    assert len(doc.objects[5]["Kids"]) == 2
