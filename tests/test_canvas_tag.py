"""``Canvas.tag``: maintaining the structure tree of a page's document (DESIGN.md section 7).

Ported from pylopdf's tests/test_canvas_tag.py at ebb01d3c. The intended
differences:

- pyropdf has no ``create()`` builder, so the tagged document that a
  drawing joins is one with a hand-built ``Document`` element and a first
  drawing of pyropdf's, and the text is in a Standard 14 font;
- the canvas writes an untagged drawing into the page's content, so the
  test of a drawing without tags reads the page's last content stream;
- a tag scope inside an opacity scope raises ``ValueError`` when it starts,
  where pylopdf raises ``PdfError`` at the commit.
"""

from __future__ import annotations

import pytest

import pyropdf
from pyropdf import Color, ListNumbering, Name, TableHeaderScope, TagRole

RED = Color(1, 0, 0)


def _structure(doc: pyropdf.Document) -> pyropdf.Dictionary:
    root = doc.catalog["StructTreeRoot"]
    assert isinstance(root, pyropdf.Dictionary)
    return root


def _parent_tree(root: pyropdf.Dictionary) -> dict[int, list[int]]:
    """Return the parent tree as keys to the object numbers of their elements."""
    entries: dict[int, list[int]] = {}
    nodes = [root["ParentTree"]]
    while nodes:
        node = nodes.pop()
        nums = list(node.get("Nums") or [])
        for key, value in zip(nums[::2], nums[1::2], strict=True):
            entries[key] = [element.xref for element in value] if isinstance(value, pyropdf.Array) else [value.xref]
        nodes.extend(node.get("Kids") or [])
    return entries


def _check_consistency(doc: pyropdf.Document) -> None:
    """Every MCID that the parent tree maps names an element whose kids hold that MCID."""
    root = _structure(doc)
    for key, owners in _parent_tree(root).items():
        for mcid, owner in enumerate(owners):
            kids = doc.objects[owner]["K"]
            kids = list(kids) if isinstance(kids, pyropdf.Array) else [kids]
            if mcid in kids:
                # Page content names its MCIDs directly; the page holds the key.
                continue
            matching = [kid for kid in kids if isinstance(kid, pyropdf.Dictionary) and kid.get("MCID") == mcid]
            assert matching, (key, mcid, owner)
            assert matching[0]["Stm"]["StructParents"] == key


def _roles(kids: pyropdf.Array) -> list[str]:
    return [str(kid["S"]) for kid in kids if isinstance(kid, pyropdf.Dictionary) and "S" in kid]


def _with_document_element(doc: pyropdf.Document, kids: object) -> int:
    """Give ``doc`` a structure tree whose root holds one ``Document`` element; return its number."""
    document = doc.objects.add({"Type": Name("StructElem"), "S": Name("Document"), "K": []})
    root = doc.objects.add(
        {"Type": Name("StructTreeRoot"), "K": doc.objects[document] if kids is None else [doc.objects[document]]}
    )
    doc.objects[root]["ParentTree"] = doc.objects[doc.objects.add({"Nums": []})]
    doc.objects[document]["P"] = doc.objects[root]
    doc.catalog["StructTreeRoot"] = doc.objects[root]
    return document


def test_tags_on_an_untagged_document_create_its_structure_tree() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    with page.draw() as canvas:
        with canvas.tag(TagRole.HEADING_1):
            canvas.text("Title", (20, 40), size=18)
        with canvas.tag(TagRole.PARAGRAPH, lang="en-US"):
            canvas.text("Hello", (20, 80))
        with canvas.tag(TagRole.FIGURE, alt="A red square"):
            canvas.rect((20, 100, 60, 140), fill=RED)

    root = _structure(doc)
    assert _roles(root["K"]) == ["H1", "P", "Figure"]
    heading, paragraph, figure = root["K"]
    assert paragraph["Lang"].text == "en-US"
    assert figure["Alt"].text == "A red square"
    assert heading["P"].xref == root.xref
    assert heading["Pg"].xref == page.xref
    form = heading["K"][0]["Stm"]
    assert form["StructParents"] == 0
    assert root["ParentTreeNextKey"] == 1
    assert _parent_tree(root) == {0: [heading.xref, paragraph.xref, figure.xref]}
    assert doc.catalog["MarkInfo"]["Marked"] is True
    content = form.read()
    assert b"/H1 <</MCID 0>> BDC" in content
    assert b"/Figure <</MCID 2>> BDC" in content
    assert page.text().split() == ["Title", "Hello"]
    _check_consistency(doc)

    reopened = pyropdf.open(doc.to_bytes())
    assert _roles(_structure(reopened)["K"]) == ["H1", "P", "Figure"]
    _check_consistency(reopened)


def test_kids_keep_the_order_of_drawing_and_nesting() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    with page.draw() as canvas, canvas.tag(TagRole.PARAGRAPH):
        canvas.text("Before", (20, 40))
        with canvas.tag(TagRole.SPAN, actual_text="Middle"):
            canvas.text("Middle", (20, 60))
        canvas.text("After", (20, 80))

    (paragraph,) = _structure(doc)["K"]
    kids = list(paragraph["K"])
    assert [kid.get("MCID") if kid.get("Type") == "MCR" else str(kid["S"]) for kid in kids] == [0, "Span", 2]
    assert kids[1]["P"].xref == paragraph.xref
    assert kids[1]["ActualText"].text == "Middle"
    _check_consistency(doc)


def test_lists_and_tables_carry_their_attributes() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    with page.draw() as canvas:
        with canvas.tag(TagRole.LIST, numbering=ListNumbering.DECIMAL), canvas.tag(TagRole.LIST_ITEM):
            with canvas.tag(TagRole.LABEL):
                canvas.text("1.", (20, 40))
            with canvas.tag(TagRole.LIST_BODY):
                canvas.text("First", (40, 40))
        with canvas.tag(TagRole.TABLE), canvas.tag(TagRole.TABLE_ROW):
            with canvas.tag(TagRole.TABLE_HEADER, scope=TableHeaderScope.ROW):
                canvas.text("Name", (20, 100))
            with canvas.tag(TagRole.TABLE_DATA):
                canvas.text("Ada", (100, 100))

    lists, table = _structure(doc)["K"]
    assert (lists["A"]["O"], lists["A"]["ListNumbering"]) == ("List", "Decimal")
    header = table["K"][0]["K"][0]
    assert (str(header["S"]), header["A"]["O"], header["A"]["Scope"]) == ("TH", "Table", "Row")
    _check_consistency(doc)


def test_tag_arguments_are_validated() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    with page.draw() as canvas:
        with pytest.raises(ValueError, match="TagRole"):
            canvas.tag("Paragraph")
        with pytest.raises(TypeError):
            canvas.tag(TagRole.FIGURE, alt=1)  # ty: ignore[invalid-argument-type]
        with pytest.raises(ValueError, match="BCP 47"):
            canvas.tag(TagRole.PARAGRAPH, lang="en_US")
        with pytest.raises(ValueError, match="only TABLE_HEADER"):
            canvas.tag(TagRole.PARAGRAPH, scope=TableHeaderScope.ROW)
        with pytest.raises(ValueError, match="ListNumbering"):
            canvas.tag(TagRole.LIST, numbering="Hex")
        with canvas.tag("P"), pytest.raises(ValueError, match="artifact"), canvas.tag(TagRole.ARTIFACT):
            canvas.tag(TagRole.SPAN).__enter__()


def test_tags_join_a_tagged_documents_document_element() -> None:
    first = pyropdf.Document()
    first.pages.new((300, 300))
    _with_document_element(first, [])
    with first.pages[0].draw() as canvas, canvas.tag(TagRole.PARAGRAPH):
        canvas.text("Existing", (20, 40))
    doc = pyropdf.open(first.to_bytes())
    root = _structure(doc)
    before = _parent_tree(root)
    (document,) = root["K"]
    assert document["S"] == "Document"
    existing = len(document["K"])

    with doc.pages[0].draw() as canvas:
        with canvas.tag(TagRole.PARAGRAPH):
            canvas.text("Added", (20, 80))
        canvas.rect((10, 10, 20, 20), fill=RED)

    root = _structure(doc)
    (document,) = root["K"]
    assert len(document["K"]) == existing + 1
    added = document["K"][-1]
    assert (str(added["S"]), added["P"].xref) == ("P", document.xref)
    tree = _parent_tree(root)
    new_keys = set(tree) - set(before)
    assert len(new_keys) == 1
    assert root["ParentTreeNextKey"] == max(tree) + 1
    form = added["K"][0]["Stm"]
    assert form["StructParents"] == next(iter(new_keys))
    content = form.read()
    # Drawing outside every tag scope of a tagged document is an artifact.
    assert b"/Artifact BMC" in content
    assert doc.pages[0].text().split() == ["Existing", "Added"]
    _check_consistency(doc)
    _check_consistency(pyropdf.open(doc.to_bytes()))


def test_a_new_key_follows_every_key_of_a_parent_tree_with_kids() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    _with_document_element(doc, [])
    root = _structure(doc)
    leaf = doc.objects.add({"Limits": [0, 4], "Nums": [4, doc.objects[doc.objects.add({})]]})
    root["ParentTree"] = doc.objects[doc.objects.add({"Kids": [doc.objects[leaf]]})]
    root["ParentTreeNextKey"] = 2

    with page.draw() as canvas, canvas.tag(TagRole.PARAGRAPH):
        canvas.text("Next", (20, 40))

    assert list(doc.objects[leaf]["Nums"])[2] == 5
    assert list(doc.objects[leaf]["Limits"]) == [0, 5]
    assert root["ParentTreeNextKey"] == 6
    (document,) = root["K"]
    assert document["K"][0]["K"][0]["Stm"]["StructParents"] == 5


def test_untagged_documents_get_no_artifacts() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    with page.draw() as canvas:
        canvas.text("Plain", (20, 40))

    assert "StructTreeRoot" not in doc.catalog
    content = page.obj["Contents"][-1].read()
    assert b"BMC" not in content
    assert b"BDC" not in content


def test_artifact_scopes_mark_without_elements() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    with page.draw() as canvas:
        with canvas.tag(TagRole.ARTIFACT):
            canvas.text("Page 1", (20, 280))
        with canvas.tag(TagRole.PARAGRAPH):
            canvas.text("Body", (20, 40))

    root = _structure(doc)
    assert _roles(root["K"]) == ["P"]
    form = root["K"][0]["K"][0]["Stm"]
    content = form.read()
    assert content.index(b"/Artifact BMC") < content.index(b"/P <</MCID 0>> BDC")


def test_opacity_inside_a_tag_is_drawn_inside_its_content() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    with page.draw() as canvas, canvas.tag(TagRole.FIGURE, alt="Faded"), canvas.opacity(0.5):
        canvas.rect((20, 20, 80, 80), fill=RED)

    (figure,) = _structure(doc)["K"]
    form = figure["K"][0]["Stm"]
    content = form.read()
    start = content.index(b"/Figure <</MCID 0>> BDC")
    assert start < content.index(b"Do", start) < content.index(b"EMC", start)
    _check_consistency(doc)


def test_scopes_inside_a_tag_close_its_marked_content() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    with page.draw() as canvas, canvas.tag(TagRole.PARAGRAPH):
        canvas.text("Plain", (20, 40))
        with canvas.transform(pyropdf.Matrix(1, 0, 0, 1, 0, 20)):
            canvas.text("Moved", (20, 40))

    (paragraph,) = _structure(doc)["K"]
    assert [kid["MCID"] for kid in paragraph["K"]] == [0, 1]
    content = paragraph["K"][0]["Stm"].read()
    assert content.index(b"EMC") < content.index(b"cm\n/P <</MCID 1>> BDC") < content.rindex(b"EMC\nQ")
    assert page.text().split() == ["Plain", "Moved"]
    _check_consistency(doc)


def test_tags_inside_opacity_scopes_are_refused() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    with page.draw() as canvas, canvas.opacity(0.5):
        with pytest.raises(ValueError, match="opacity"), canvas.tag(TagRole.PARAGRAPH):
            pass
        canvas.text("x", (20, 40))
    assert "StructTreeRoot" not in doc.catalog


def test_a_lone_document_element_reference_takes_the_new_elements() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 300))
    document = _with_document_element(doc, None)

    with page.draw() as canvas, canvas.tag(TagRole.PARAGRAPH):
        canvas.text("Inside", (20, 40))

    root_dict = _structure(doc)
    assert root_dict["K"].xref == document
    (paragraph,) = doc.objects[document]["K"]
    assert (str(paragraph["S"]), paragraph["P"].xref) == ("P", document)
    _check_consistency(doc)
