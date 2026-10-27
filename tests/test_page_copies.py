"""Copies of pages within a document carry annotations, widgets, and structure of their own.

pylopdf's tests/test_page_copies.py at ebb01d3c, ported. Intended
differences:

- the widget case builds its form as a raw PDF and reads the fields through
  the catalog, since pyropdf has no ``page.widgets`` or ``doc.form`` yet
  (yhay81/pyropdf#156).
"""

from __future__ import annotations

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
