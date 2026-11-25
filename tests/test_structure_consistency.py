"""Page operations on the tagged corpus, ported from pylopdf ebb01d3c.

The frozen parent-tree oracle covers MCID and OBJR ownership, parent links and
key uniqueness. The three consistency harness validators check both save modes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from corpus import ASSETS
from test_consistency import QPDF, defects, pikepdf_problems, qpdf_findings, saved_qpdf_findings

import pyropdf

if TYPE_CHECKING:
    from pathlib import Path

TAGGED = ("f1040.pdf", "mhlw-doc.pdf")


def _as_list(value: object) -> list[object]:
    if isinstance(value, pyropdf.Array):
        return list(value)
    return [] if value is None else [value]


def _parent_tree(root: pyropdf.Dictionary) -> dict[int, object]:
    entries: dict[int, object] = {}
    nodes: list[pyropdf.Dictionary] = [root["ParentTree"]]
    seen: set[int] = set()
    while nodes:
        node = nodes.pop()
        if node.xref is not None:
            assert node.xref not in seen
            seen.add(node.xref)
        nums = _as_list(node.get("Nums"))
        for key, value in zip(nums[::2], nums[1::2], strict=True):
            assert isinstance(key, int)
            assert key not in entries
            entries[key] = value
        nodes.extend(kid for kid in _as_list(node.get("Kids")) if isinstance(kid, pyropdf.Dictionary))
    return entries


def _check_parents(root: pyropdf.Dictionary) -> None:
    """Every element's /P is the element or root that lists it."""
    stack: list[tuple[pyropdf.Dictionary, pyropdf.Dictionary]] = [
        (kid, root) for kid in _as_list(root.get("K")) if isinstance(kid, pyropdf.Dictionary)
    ]
    seen: set[int] = set()
    while stack:
        element, parent = stack.pop()
        if element.get("Type") in {"MCR", "OBJR"} or "S" not in element:
            continue
        if element.xref is not None:
            if element.xref in seen:
                continue
            seen.add(element.xref)
            assert element["P"].xref == parent.xref
        stack.extend((kid, element) for kid in _as_list(element.get("K")) if isinstance(kid, pyropdf.Dictionary))


def _check_page(page: pyropdf.Page, tree: dict[int, object]) -> list[int]:
    """Check a page's and its annotations' keys and return them."""
    keys: list[int] = []
    key = page.obj.get("StructParents")
    if key is not None:
        keys.append(key)
        for mcid, owner in enumerate(_as_list(tree[key])):
            if not isinstance(owner, pyropdf.Dictionary):
                continue
            assert owner["Pg"].xref == page.xref, (page.index, mcid)
            kids = _as_list(owner.get("K"))
            assert mcid in kids or any(
                isinstance(kid, pyropdf.Dictionary) and kid.get("MCID") == mcid for kid in kids
            ), (page.index, mcid)
    for annotation in _as_list(page.obj.get("Annots")):
        if not isinstance(annotation, pyropdf.Dictionary) or "StructParent" not in annotation:
            continue
        keys.append(annotation["StructParent"])
        owner = tree[annotation["StructParent"]]
        if isinstance(owner, pyropdf.Dictionary):
            references = [
                kid
                for kid in _as_list(owner.get("K"))
                if isinstance(kid, pyropdf.Dictionary) and kid.get("Type") == "OBJR"
            ]
            assert any(reference["Obj"].xref == annotation.xref for reference in references)
    return keys


def _check(doc: pyropdf.Document) -> None:
    root = doc.catalog["StructTreeRoot"]
    assert isinstance(root, pyropdf.Dictionary)
    tree = _parent_tree(root)
    assert root.get("ParentTreeNextKey", 0) > max(tree, default=-1)
    _check_parents(root)
    keys = [key for page in doc.pages for key in _check_page(page, tree)]
    assert len(keys) == len(set(keys)), "two pages or annotations share a parent-tree key"


def _delete_first(doc: pyropdf.Document) -> None:
    del doc.pages[0]


def _tag(doc: pyropdf.Document) -> None:
    with doc.pages[0].draw() as canvas, canvas.tag(pyropdf.TagRole.PARAGRAPH):
        canvas.text("Added", (36, 36))


@pytest.mark.parametrize(("name", "other"), [TAGGED, TAGGED[::-1]])
@pytest.mark.parametrize(
    "operation",
    ["delete", "select", "move", "copy", "copy_all", "import_self", "import_other", "import_one", "new", "tag"],
)
def test_page_operations_keep_tagged_documents_consistent(
    name: str, other: str, operation: str, tmp_path: Path
) -> None:
    path = ASSETS / name
    doc = pyropdf.open(path)
    _check(doc)
    operations = {
        "delete": lambda: _delete_first(doc),
        "select": lambda: doc.pages.select([len(doc.pages) - 1, 0]),
        "move": lambda: doc.pages.move(0, len(doc.pages) - 1),
        "copy": lambda: doc.pages.insert(0, doc.pages[-1]),
        "copy_all": lambda: doc.pages.extend(list(doc.pages)),
        "import_self": lambda: doc.pages.extend(pyropdf.open(doc.to_bytes()).pages),
        "import_other": lambda: doc.pages.extend(pyropdf.open(ASSETS / other).pages),
        "import_one": lambda: doc.pages.insert(1, pyropdf.open(ASSETS / TAGGED[0]).pages[1]),
        "new": lambda: doc.pages.new((100, 100)),
        "tag": lambda: _tag(doc),
    }
    operations[operation]()
    _check(doc)
    original_defects = defects(path.read_bytes())
    original_qpdf = qpdf_findings(path) if QPDF else set()
    _, original_pikepdf = pikepdf_problems(path)
    for incremental in (False, True):
        data = doc.to_bytes(incremental=incremental)
        _check(pyropdf.open(data))
        assert not defects(data) - original_defects
        saved = tmp_path / "saved.pdf"
        saved.write_bytes(data)
        if QPDF:
            assert not saved_qpdf_findings(saved) - original_qpdf
        count, problems = pikepdf_problems(saved)
        assert count == len(doc.pages)
        assert not problems - original_pikepdf
