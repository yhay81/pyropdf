"""Page imports carry the structure tree of the pages they import.

pylopdf's tests/test_structure_import.py at ebb01d3c, ported. Intended
differences:

- ``_check`` reads the elements of the parent tree as dictionaries, since
  pyropdf has no ``doc.objects``.
- a test checks that an imported link leaves its object reference behind
  with the link when it leads to a page left behind.
"""

from __future__ import annotations

import pyropdf


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


def _content(text: str) -> str:
    return f"/Para <</MCID 0>> BDC BT /F1 12 Tf 20 150 Td ({text}) Tj ET EMC"


def _tagged(
    texts: list[str],
    *,
    role_map: str = "<< /Para /P >>",
    class_map: str = "<< /Bold << /O /Layout >> >>",
    identifier: str = "(A)",
) -> bytes:
    """Return a tagged document whose page i shows texts[i] in element i under a Document element."""
    count = len(texts)
    pages = list(range(3, 3 + count))
    contents = list(range(3 + count, 3 + 2 * count))
    font = 3 + 2 * count
    root = font + 1
    document = root + 1
    elements = list(range(document + 1, document + 1 + count))
    objects: dict[int, str] = {
        1: f"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot {root} 0 R /MarkInfo << /Marked true >> >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{page} 0 R' for page in pages)}] /Count {count} >>",
        font: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, text in enumerate(texts):
        stream = _content(text)
        objects[pages[index]] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents {contents[index]} 0 R "
            f"/StructParents {index} /Resources << /Font << /F1 {font} 0 R >> >> >>"
        )
        objects[contents[index]] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        extra = f" /ID {identifier} /C /Bold" if index == count - 1 else ""
        objects[elements[index]] = (
            f"<< /Type /StructElem /S /Para /P {document} 0 R /Pg {pages[index]} 0 R /K 0{extra} >>"
        )
    nums = " ".join(f"{index} [{element} 0 R]" for index, element in enumerate(elements))
    objects[root] = (
        f"<< /Type /StructTreeRoot /K {document} 0 R /ParentTree << /Nums [{nums}] >> "
        f"/ParentTreeNextKey {count} /RoleMap {role_map} /ClassMap {class_map} "
        f"/IDTree << /Names [{identifier} {elements[-1]} 0 R] >> >>"
    )
    objects[document] = (
        f"<< /Type /StructElem /S /Document /P {root} 0 R /K [{' '.join(f'{element} 0 R' for element in elements)}] >>"
    )
    return build_raw_pdf(objects)


def _kids(element: pyropdf.Dictionary) -> list[pyropdf.Dictionary]:
    """Return an element's or the root's kids; a lone kid reads as itself."""
    kids = element["K"]
    return list(kids) if isinstance(kids, pyropdf.Array) else [kids]


def _parent_tree(root: pyropdf.Dictionary) -> dict[int, list[pyropdf.Dictionary]]:
    entries: dict[int, list[pyropdf.Dictionary]] = {}
    nodes = [root["ParentTree"]]
    while nodes:
        node = nodes.pop()
        nums = list(node.get("Nums") or [])
        for key, value in zip(nums[::2], nums[1::2], strict=True):
            entries[key] = list(value) if isinstance(value, pyropdf.Array) else [value]
        nodes.extend(node.get("Kids") or [])
    return entries


def _xrefs(elements: list[pyropdf.Dictionary]) -> list[int | None]:
    return [element.xref for element in elements]


def _check(doc: pyropdf.Document) -> None:
    """Every page's key maps its MCIDs to elements on that page."""
    root = doc.catalog["StructTreeRoot"]
    tree = _parent_tree(root)
    keys = [page.obj.get("StructParents") for page in doc.pages]
    assert len(set(keys)) == len(keys)
    for page in doc.pages:
        key = page.obj["StructParents"]
        for element in tree[key]:
            assert element["Pg"].xref == page.xref
    assert root["ParentTreeNextKey"] > max(tree)


def test_imported_pages_bring_their_elements_into_the_document_element() -> None:
    source = pyropdf.open(_tagged(["Skipped", "Imported"]))
    target = pyropdf.open(_tagged(["Own"], role_map="<< /Para /H1 >>", class_map="<< /Bold << /O /Layout /W 1 >> >>"))

    target.pages.extend(source.pages[1])

    root = target.catalog["StructTreeRoot"]
    (document,) = _kids(root)
    kids = _kids(document)
    assert len(kids) == 2
    own, imported = kids
    assert imported["P"].xref == document.xref
    assert imported["Pg"].xref == target.pages[1].xref
    # The receiving tree maps /Para to /H1, so the imported element takes the source's standard type.
    assert (own["S"], imported["S"]) == ("Para", "P")
    assert imported["ID"].text == "A+1"
    assert imported["C"] == "Bold_1"
    assert sorted(root["ClassMap"]) == ["Bold", "Bold_1"]
    names = list(root["IDTree"]["Names"])
    assert [name.text for name in names[::2]] == ["A", "A+1"]
    assert names[3].xref == imported.xref
    assert target.pages[1].text().split() == ["Imported"]
    _check(target)
    _check(pyropdf.open(target.to_bytes()))


def test_skipped_pages_leave_their_elements_behind() -> None:
    source = pyropdf.open(_tagged(["First", "Second", "Third"]))
    target = pyropdf.open(_tagged(["Own"]))

    target.pages.extend(source.pages[0:2])

    (document,) = _kids(target.catalog["StructTreeRoot"])
    assert len(_kids(document)) == 3
    saved = pyropdf.open(target.to_bytes())
    texts = [page.text().strip() for page in saved.pages]
    assert texts == ["Own", "First", "Second"]
    _check(saved)


def test_an_untagged_document_receives_the_whole_tree() -> None:
    source = pyropdf.open(_tagged(["One", "Two"]))
    target = pyropdf.Document()
    target.pages.new((200, 200))

    target.pages.extend(source.pages[1])

    root = target.catalog["StructTreeRoot"]
    (document,) = _kids(root)
    assert document["S"] == "Document"
    assert document["P"].xref == root.xref
    assert [kid["Pg"].xref for kid in _kids(document)] == [target.pages[1].xref]
    assert root["RoleMap"]["Para"] == "P"
    tree = _parent_tree(root)
    assert _xrefs(tree[target.pages[1].obj["StructParents"]]) == [_kids(document)[0].xref]


def test_untagged_sources_leave_the_tree_alone() -> None:
    target = pyropdf.open(_tagged(["Own"]))
    source = pyropdf.Document()
    source.pages.new((200, 200))
    before = {key: _xrefs(elements) for key, elements in _parent_tree(target.catalog["StructTreeRoot"]).items()}

    target.pages.extend(source.pages)

    after = _parent_tree(target.catalog["StructTreeRoot"])
    assert {key: _xrefs(elements) for key, elements in after.items()} == before
    assert len(_kids(_kids(target.catalog["StructTreeRoot"])[0])) == 1


def test_keys_move_past_the_receiving_tree() -> None:
    source = pyropdf.open(_tagged(["A", "B"]))
    target = pyropdf.open(_tagged(["X", "Y", "Z"]))

    target.pages.extend(source.pages)

    keys = [page.obj["StructParents"] for page in target.pages]
    assert keys == [0, 1, 2, 3, 4]
    assert target.catalog["StructTreeRoot"]["ParentTreeNextKey"] == 5
    _check(target)


def _linked() -> bytes:
    """Return a tagged document whose first page links to the second, with an element for the link."""
    content = _content("Linked")
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 7 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            3: (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 5 0 R /StructParents 0 "
                "/Annots [6 0 R] >>"
            ),
            4: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
            5: f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            6: "<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] /StructParent 1 /Dest [4 0 R /Fit] >>",
            7: (
                "<< /Type /StructTreeRoot /K [8 0 R] /ParentTree << /Nums [0 [9 0 R] 1 10 0 R] >> "
                "/ParentTreeNextKey 2 >>"
            ),
            8: "<< /Type /StructElem /S /Document /P 7 0 R /K [9 0 R 10 0 R] >>",
            9: "<< /Type /StructElem /S /P /P 8 0 R /Pg 3 0 R /K 0 >>",
            10: "<< /Type /StructElem /S /Link /P 8 0 R /Pg 3 0 R /K [<< /Type /OBJR /Obj 6 0 R >>] >>",
        }
    )


def test_a_link_left_behind_takes_its_element_along() -> None:
    source = pyropdf.open(_linked())
    target = pyropdf.Document()

    target.pages.extend(source.pages[0])

    (page,) = target.pages
    assert "Annots" not in page.obj
    (document,) = _kids(target.catalog["StructTreeRoot"])
    assert [str(kid["S"]) for kid in _kids(document)] == ["P"]
    _check(target)
