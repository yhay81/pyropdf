"""The preservation harness: an edit and a save leave everything else as it was.

Each edit of ``EDITS`` is applied to each corpus document, which is then saved
in full and incrementally and opened again. Every object that the edit did
not touch must read back equal, with reals compared by the bytes they are
written with and streams by their raw data, and an untouched page must keep
its text and its rendering. A later editing operation adds its row to
``EDITS``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from corpus import ASSETS, NAMES
from test_pages import build_raw_pdf

import pyropdf
from pyropdf import Array, Dictionary, Name, Operation, Real, Stream, typed

if TYPE_CHECKING:
    from collections.abc import Callable

#: Plain values are compared through this, which nests no deeper.
_MAX_DEPTH = 64


def plain(value: object, depth: int = 0) -> object:
    """Return ``value`` as plain data: indirect objects inside it as references, reals as exact decimals."""
    assert depth < _MAX_DEPTH
    if isinstance(value, (Dictionary, Array)) and depth and value.xref is not None:
        return ("ref", value.xref)
    if isinstance(value, Stream):
        entries = {key: plain(item, depth + 1) for key, item in value.items()}
        return ("stream", entries, value.read(decode=False, max_size=None))
    if isinstance(value, Dictionary):
        return {key: plain(item, depth + 1) for key, item in value.items()}
    if isinstance(value, Array):
        return [plain(item, depth + 1) for item in value]
    if isinstance(value, Real):
        # The exact decimal of the real's lexeme, or of its float when it
        # has none; test_exact_reals_keep_their_tokens_through_every_edit
        # checks digits beyond a float in the saved bytes.
        return ("real", value.to_decimal())
    return (type(value).__name__, value)


def number(value: Dictionary) -> int:
    """Return the object number of the indirect ``value``."""
    assert value.xref is not None
    return value.xref


def snapshot(doc: pyropdf.Document) -> dict[int, object]:
    """Return every indirect object of ``doc`` as plain data, by number."""
    return {number: plain(doc.objects[number]) for number in doc.objects}


def rotate(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    page.rotation = (page.rotation + 90) % 360
    return {number(page.obj)}


def add_object(doc: pyropdf.Document) -> set[int]:
    doc.objects.add({"Preserved": Real("0.000001")})
    return set()


def catalog_entry(doc: pyropdf.Document) -> set[int]:
    doc.catalog["PageMode"] = Name("UseNone")
    return {number(doc.catalog)}


def flattened(doc: pyropdf.Document) -> set[int]:
    """Return the page-tree objects that writing the page list rewrites."""
    # Writing the page list flattens the page tree: it writes the root and
    # every page that is not already the root's kid (#272 narrows what those
    # pages keep).
    root = doc.catalog["Pages"]
    return {number(root)} | {number(page.obj) for page in doc.pages if page.obj.get("Parent") != root}


def new_page(doc: pyropdf.Document) -> set[int]:
    touched = flattened(doc)
    doc.pages.new((200, 300))
    return touched


def _targets(value: object) -> set[int]:
    """Return the object numbers that ``value``, as :func:`plain` returns it, refers to."""
    found: set[int] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, tuple) and item[:1] == ("ref",):
            found.add(item[1])
        elif isinstance(item, tuple) and item[:1] == ("stream",):
            pending.append(item[1])
    return found


def _pruned(before: object, after: object, depth: int = 0) -> bool:
    """Return whether ``after``, as :func:`plain` returns it, is ``before`` with entries removed.

    Tree limits may change with the pairs and kids they bound.
    """
    assert depth < _MAX_DEPTH
    if before == after:
        return True
    if isinstance(before, dict) and isinstance(after, dict):
        return after.keys() <= before.keys() and all(
            key == "Limits" or _pruned(before[key], value, depth + 1) for key, value in after.items()
        )
    if isinstance(before, list) and isinstance(after, list):
        items = iter(before)
        return all(any(_pruned(item, kept, depth + 1) for item in items) for kept in after)
    if isinstance(before, tuple) and isinstance(after, tuple) and before[:1] == after[:1] == ("stream",):
        return before[2] == after[2] and _pruned(before[1], after[1], depth + 1)
    return False


def _labels(doc: pyropdf.Document) -> set[int]:
    """Return the page-label tree's objects, which a page's removal renumbers."""
    numbers: set[int] = set()
    pending = [doc.catalog.get("PageLabels")]
    while pending:
        node = pending.pop()
        if not isinstance(node, Dictionary):
            continue
        if node.xref is not None:
            numbers.add(node.xref)
        kids = node.get("Kids")
        if isinstance(kids, Array):
            pending.extend(kids)
    return numbers


def delete_page(doc: pyropdf.Document) -> set[int]:
    if len(doc.pages) == 1:
        doc.pages.new((200, 300))
    page = number(doc.pages[0].obj)
    touched = flattened(doc) | {page} | _labels(doc)
    before = snapshot(doc)
    del doc.pages[0]
    after = snapshot(doc)
    # The delete frees what only the deleted page reached, and its rules
    # remove the navigation to the page and the structure content on it.
    gone = before.keys() - after.keys()
    pruned = {xref for xref, value in before.items() if xref in after and _pruned(value, after[xref])}
    changed = {xref for xref in pruned if before[xref] != after[xref]}
    # What the rules leave unreferenced, a save leaves out.
    held = set().union(*map(_targets, before.values()))
    kept = set().union(*map(_targets, after.values()))
    return touched | gone | changed | (held - kept)


def replace_object(doc: pyropdf.Document) -> set[int]:
    number = doc.objects.add([1, 2])
    doc.objects[number] = [Real("3.14159265358979323846264338327950288")]
    return set()


def rewrite_stream(doc: pyropdf.Document) -> set[int]:
    number = doc.objects.add_stream(b"q Q")
    doc.objects[number].write(b"q 1 0 0 1 0 0 cm Q")
    return set()


def page_labels(doc: pyropdf.Document) -> set[int]:
    """Return the objects of the page label tree, which moving a page rewrites."""
    # A structural edit writes the tree as one leaf whose ranges keep each
    # page's label (#180), or the catalog when the tree is a direct value.
    tree = doc.catalog.get("PageLabels")
    if not isinstance(tree, Dictionary):
        return set()
    found = {number(doc.catalog)} if tree.xref is None else set()
    pending = [tree]
    while pending:
        node = pending.pop()
        if node.xref is not None:
            found.add(node.xref)
        kids = node.get("Kids")
        if isinstance(kids, Array):
            pending.extend(kid for kid in kids if isinstance(kid, Dictionary))
        nums = node.get("Nums")
        if isinstance(nums, Array):
            found.update(label.xref for label in nums if isinstance(label, Dictionary) and label.xref is not None)
    return found


def move_page(doc: pyropdf.Document) -> set[int]:
    if len(doc.pages) == 1:
        doc.pages.new((200, 300))
    touched = flattened(doc) | page_labels(doc)
    doc.pages.move(0, -1)
    return touched


def assign_operations(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    page.operations = [*page.operations, Operation("q"), Operation("Q")]
    return {number(page.obj)}


def add_attachment(doc: pyropdf.Document) -> set[int]:
    # Adding a name writes the catalog, for its /Version where the file is
    # older than PDF 1.4, its name dictionary, and its EmbeddedFiles root.
    touched = {number(doc.catalog)}
    names = doc.catalog.get("Names")
    if names is not None:
        if names.xref is not None:
            touched.add(names.xref)
        files = names.get("EmbeddedFiles")
        if files is not None and files.xref is not None:
            touched.add(files.xref)
    doc.attachments.add("pyropdf-preserved", b"data", filename="preserved.txt")
    return touched


def write_attachment(doc: pyropdf.Document) -> set[int]:
    touched = add_attachment(doc)
    doc.attachments["pyropdf-preserved"].write(b"other data")
    return touched


def delete_attachment(doc: pyropdf.Document) -> set[int]:
    touched = add_attachment(doc)
    del doc.attachments["pyropdf-preserved"]
    return touched


def delete_entry(doc: pyropdf.Document) -> set[int]:
    doc.catalog["Preserved"] = Name("Gone")
    del doc.catalog["Preserved"]
    return {number(doc.catalog)}


def array_insert_delete(doc: pyropdf.Document) -> set[int]:
    array = doc.objects[doc.objects.add([1, 2, 3])]
    array.insert(0, Real("0.1"))
    del array[2]
    return set()


def typed_set(doc: pyropdf.Document) -> set[int]:
    typed.Catalog(doc.catalog)["PageLayout"] = Name("OneColumn")
    return {number(doc.catalog)}


def typed_delete(doc: pyropdf.Document) -> set[int]:
    typed_set(doc)
    del typed.Catalog(doc.catalog)["PageLayout"]
    return {number(doc.catalog)}


def undo(doc: pyropdf.Document) -> set[int]:
    catalog_entry(doc)
    doc.history.undo()
    return set()


def redo(doc: pyropdf.Document) -> set[int]:
    touched = catalog_entry(doc)
    doc.history.undo()
    doc.history.redo()
    return touched


#: Each edit, which returns the object numbers it touched among those that existed before it.
EDITS: dict[str, Callable[[pyropdf.Document], set[int]]] = {
    "none": lambda _: set(),
    "rotate": rotate,
    "add_object": add_object,
    "catalog_entry": catalog_entry,
    "new_page": new_page,
    "delete_page": delete_page,
    "replace_object": replace_object,
    "rewrite_stream": rewrite_stream,
    "move_page": move_page,
    "assign_operations": assign_operations,
    "add_attachment": add_attachment,
    "write_attachment": write_attachment,
    "delete_attachment": delete_attachment,
    "delete_entry": delete_entry,
    "array_insert_delete": array_insert_delete,
    "typed_set": typed_set,
    "typed_delete": typed_delete,
    "undo": undo,
    "redo": redo,
}


def page_state(doc: pyropdf.Document, index: int) -> tuple[str, bytes]:
    """Return the text and a low-resolution rendering of page ``index``."""
    page = doc.pages[index]
    return page.text(), page.pixmap(dpi=18).to_bytes()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("edit", sorted(EDITS))
@pytest.mark.parametrize("name", NAMES)
def test_an_edit_and_a_save_keep_every_untouched_object(name: str, edit: str, *, incremental: bool) -> None:
    doc = pyropdf.open(ASSETS / name)
    before = snapshot(doc)
    # The last page, which no edit of the table touches unless it is the
    # first; it is found again by its object number, since edits move pages.
    last = len(doc.pages) - 1
    untouched = number(doc.pages[last].obj)
    state = page_state(doc, last) if last else None
    touched = EDITS[edit](doc)
    reopened = pyropdf.open(doc.to_bytes(incremental=incremental))
    after = snapshot(reopened)
    changed = [xref for xref, value in before.items() if xref not in touched and after.get(xref) != value]
    assert changed == []
    if state is not None:
        index = next(at for at, page in enumerate(reopened.pages) if number(page.obj) == untouched)
        assert page_state(reopened, index) == state


@pytest.mark.parametrize("edit", ["add_attachment", "write_attachment", "delete_attachment"])
@pytest.mark.parametrize("indirect_names", [False, True])
def test_attachment_edits_record_an_indirect_tree_root(edit: str, *, indirect_names: bool) -> None:
    doc = pyropdf.open(synthetic())
    doc.attachments.add("existing", b"kept")
    entries = list(doc.catalog["Names"]["EmbeddedFiles"]["Names"])
    root = doc.objects[doc.objects.add({"Names": entries})]
    names = {"EmbeddedFiles": root}
    doc.catalog["Names"] = doc.objects[doc.objects.add(names)] if indirect_names else names
    before = snapshot(doc)

    touched = EDITS[edit](doc)

    assert number(root) in touched
    after = snapshot(pyropdf.open(doc.to_bytes()))
    assert all(after.get(xref) == value for xref, value in before.items() if xref not in touched)


#: Reals that a float cannot hold exactly, as the synthetic document writes them.
EXACT = (
    b"35.68123456789012345678",
    b"0.000000000000000000000000000001",
    b"123456789012345678901234567890.5",
    b"-.5",
    b"-0.30000000000000004",
    b"99999999999999999999.9",
    b"792.000000000000000001",
)


def synthetic() -> bytes:
    """Return a one-page document whose catalog and page hold the reals of ``EXACT``."""
    reals = " ".join(token.decode() for token in EXACT[:-1])
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Exact 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1000 {EXACT[-1].decode()}] >>",
            4: f"[{reals}]",
        }
    )


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("edit", sorted(EDITS))
def test_exact_reals_keep_their_tokens_through_every_edit(edit: str, *, incremental: bool) -> None:
    doc = pyropdf.open(synthetic())
    touched = EDITS[edit](doc)
    data = doc.to_bytes(incremental=incremental)
    # The saved bytes are searched for the source tokens, independently of
    # the parser: a real read from bytes has no lexeme until I7 (#16).
    for token in EXACT if 3 not in touched else EXACT[:-1]:
        assert b" " + token + b" " in data.replace(b"[", b" ").replace(b"]", b" ")
    assert len(pyropdf.open(data).objects[4]) == len(EXACT) - 1
