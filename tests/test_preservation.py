"""The preservation harness: an edit and a save leave everything else as it was.

Each edit of ``EDITS`` is applied to each corpus document, which is then saved
in full and incrementally and opened again. Every object that the edit did
not touch must read back equal, with reals compared by the bytes they are
written with and streams by their raw data, and an untouched page must keep
its text and its rendering. A later editing operation adds its row to
``EDITS``.
"""

from __future__ import annotations

import pytest
from corpus import ASSETS, NAMES
from test_pages import build_raw_pdf
from test_preservation_operations import FAMILY_EDITS, NEW_EDITS, Edit, face

import pyropdf
from pyropdf import Array, Dictionary, Name, Operation, Real, Stream, typed

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


def prepare_rotation(doc: pyropdf.Document) -> None:
    doc.pages[0].rotation = 0


def rotate(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    page.rotation = (page.rotation + 90) % 360
    return {number(page.obj)}


def add_object(doc: pyropdf.Document) -> set[int]:
    doc.objects.add({"Preserved": Real("0.000001")})
    return set()


def catalog_entry(doc: pyropdf.Document) -> set[int]:
    doc.catalog["PageMode"] = Name("UseThumbs")
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
    doc.pages.new((211, 307))
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


def prepare_page_order(doc: pyropdf.Document) -> None:
    if len(doc.pages) == 1:
        doc.pages.new((200, 300))
    doc.pages[0].duration = 213.25


def delete_page(doc: pyropdf.Document) -> set[int]:
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
    doc.objects[number] = [Real("3.125")]
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
    touched = flattened(doc) | page_labels(doc)
    doc.pages.move(0, -1)
    return touched


def assign_operations(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    page.operations = [*page.operations, Operation("q"), Operation("w", (17,)), Operation("Q")]
    return {number(page.obj)}


def attachment_tree(doc: pyropdf.Document) -> set[int]:
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
    return touched


def add_attachment(doc: pyropdf.Document) -> set[int]:
    touched = attachment_tree(doc)
    doc.attachments.add("pyropdf-preserved", b"data", filename="preserved.txt")
    return touched


def prepare_attachment(doc: pyropdf.Document) -> None:
    add_attachment(doc)


def attachment_spec(doc: pyropdf.Document) -> Dictionary:
    entries = doc.catalog["Names"]["EmbeddedFiles"]["Names"]
    return next(entries[i + 1] for i in range(0, len(entries), 2) if entries[i] == b"pyropdf-preserved")


def write_attachment(doc: pyropdf.Document) -> set[int]:
    touched = {number(attachment_spec(doc)["EF"]["F"])}
    doc.attachments["pyropdf-preserved"].write(b"other data")
    return touched


def delete_attachment(doc: pyropdf.Document) -> set[int]:
    spec = attachment_spec(doc)
    touched = attachment_tree(doc) | {number(spec), number(spec["EF"]["F"])}
    af = doc.catalog.get("AF")
    if isinstance(af, Array) and af.xref is not None:
        touched.add(af.xref)
    del doc.attachments["pyropdf-preserved"]
    return touched


def delete_entry(doc: pyropdf.Document) -> set[int]:
    del doc.catalog["PageMode"]
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
    del typed.Catalog(doc.catalog)["PageLayout"]
    return {number(doc.catalog)}


def undo(doc: pyropdf.Document) -> set[int]:
    touched = catalog_entry(doc)
    doc.catalog["PageMode"] = Name("FullScreen")
    doc.history.undo()
    return touched


def redo(doc: pyropdf.Document) -> set[int]:
    touched = catalog_entry(doc)
    doc.history.undo()
    doc.history.redo()
    return touched


def prepare_catalog(doc: pyropdf.Document) -> None:
    doc.catalog["PageMode"] = Name("UseNone")


def prepare_layout(doc: pyropdf.Document) -> None:
    doc.catalog["PageLayout"] = Name("SinglePage")


def check_rotation(doc: pyropdf.Document) -> None:
    assert doc.pages[0].rotation == 90


def newest_data_object(doc: pyropdf.Document) -> object:
    # A stream-based save can append its xref stream after the test allocation.
    for xref in sorted(doc.objects, reverse=True):
        obj = doc.objects[xref]
        if not isinstance(obj, Stream) or obj.get("Type") != Name("XRef"):
            return obj
    pytest.fail("fixture has no data objects")


def check_object(doc: pyropdf.Document) -> None:
    obj = newest_data_object(doc)
    assert isinstance(obj, Dictionary) and obj.get("Preserved") == Real("0.000001")


def check_catalog(doc: pyropdf.Document) -> None:
    assert doc.catalog.get("PageMode") == Name("UseThumbs")


def check_new_page(doc: pyropdf.Document) -> None:
    assert tuple(doc.pages[-1].rect) == (0, 0, 211, 307)


def check_page_deleted(doc: pyropdf.Document) -> None:
    assert all(page.duration != 213.25 for page in doc.pages)


def check_page_moved(doc: pyropdf.Document) -> None:
    assert doc.pages[-1].duration == 213.25


def check_replacement(doc: pyropdf.Document) -> None:
    obj = newest_data_object(doc)
    assert isinstance(obj, Array) and plain(obj) == [plain(Real("3.125"))]


def check_stream(doc: pyropdf.Document) -> None:
    obj = newest_data_object(doc)
    assert isinstance(obj, Stream) and obj.read() == b"q 1 0 0 1 0 0 cm Q"


def check_operations(doc: pyropdf.Document) -> None:
    assert list(doc.pages[0].operations)[-3:] == [Operation("q"), Operation("w", (17,)), Operation("Q")]


def check_attachment(doc: pyropdf.Document) -> None:
    assert doc.attachments["pyropdf-preserved"].read() == b"data"


def check_rewritten_attachment(doc: pyropdf.Document) -> None:
    file = doc.attachments["pyropdf-preserved"]
    assert (file.filename, file.read(), file.size) == ("preserved.txt", b"other data", 10)


def check_attachment_deleted(doc: pyropdf.Document) -> None:
    assert "pyropdf-preserved" not in doc.attachments


def check_entry_deleted(doc: pyropdf.Document) -> None:
    assert "PageMode" not in doc.catalog


def check_array(doc: pyropdf.Document) -> None:
    obj = newest_data_object(doc)
    assert isinstance(obj, Array) and list(obj) == [Real("0.1"), 1, 3]


def check_layout(doc: pyropdf.Document) -> None:
    assert doc.catalog.get("PageLayout") == Name("OneColumn")


def check_layout_deleted(doc: pyropdf.Document) -> None:
    assert "PageLayout" not in doc.catalog


EDITS = {
    "none": Edit(apply=lambda _: set()),
    "rotate": Edit(prepare=prepare_rotation, apply=rotate, check=check_rotation),
    "add_object": Edit(apply=add_object, check=check_object),
    "catalog_entry": Edit(prepare=prepare_catalog, apply=catalog_entry, check=check_catalog),
    "new_page": Edit(apply=new_page, check=check_new_page),
    "delete_page": Edit(prepare=prepare_page_order, apply=delete_page, check=check_page_deleted),
    "replace_object": Edit(apply=replace_object, check=check_replacement),
    "rewrite_stream": Edit(apply=rewrite_stream, check=check_stream),
    "move_page": Edit(prepare=prepare_page_order, apply=move_page, check=check_page_moved),
    "assign_operations": Edit(apply=assign_operations, check=check_operations),
    "add_attachment": Edit(apply=add_attachment, check=check_attachment),
    "write_attachment": Edit(prepare=prepare_attachment, apply=write_attachment, check=check_rewritten_attachment),
    "delete_attachment": Edit(prepare=prepare_attachment, apply=delete_attachment, check=check_attachment_deleted),
    "delete_entry": Edit(prepare=prepare_catalog, apply=delete_entry, check=check_entry_deleted),
    "array_insert_delete": Edit(apply=array_insert_delete, check=check_array),
    "typed_set": Edit(prepare=prepare_layout, apply=typed_set, check=check_layout),
    "typed_delete": Edit(prepare=prepare_layout, apply=typed_delete, check=check_layout_deleted),
    "undo": Edit(prepare=prepare_catalog, apply=undo, check=check_catalog),
    "redo": Edit(prepare=prepare_catalog, apply=redo, check=check_catalog),
} | NEW_EDITS | FAMILY_EDITS


def raw_objects(doc: pyropdf.Document) -> dict[int, tuple[int, bytes]]:
    """Keep the source bytes and generation of each object, including strings and real lexemes."""
    return {xref: (generation, body) for xref, generation, body in doc._core()._objects()}  # noqa: SLF001


def page_state(doc: pyropdf.Document, index: int) -> tuple[str, bytes]:
    """Return the text and a low-resolution rendering of page ``index``."""
    page = doc.pages[index]
    return page.text(), page.pixmap(dpi=18).to_bytes()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("edit", sorted(EDITS))
@pytest.mark.parametrize("name", NAMES)
def test_an_edit_and_a_save_keep_every_untouched_object(name: str, edit: str, *, incremental: bool) -> None:
    case = EDITS[edit]
    doc = case.open(ASSETS / name)
    before = snapshot(doc)
    raw_before = raw_objects(doc)
    # A page outside the declared effect, found by object number after edits
    # move pages. Creating a field may also draw existing missing appearances.
    affected = case.affected_pages(doc)
    candidates = [index for index in range(1, len(doc.pages)) if number(doc.pages[index].obj) not in affected]
    last = candidates[-1] if candidates else None
    untouched = number(doc.pages[last].obj) if last is not None else None
    state = page_state(doc, last) if last is not None else None
    touched = case.apply(doc)
    case.check(doc)
    reopened = pyropdf.open(doc.to_bytes(incremental=incremental))
    case.check(reopened)
    after = snapshot(reopened)
    changed = [xref for xref, value in before.items() if xref not in touched and after.get(xref) != value]
    assert changed == []
    raw_after = raw_objects(reopened)
    assert [xref for xref, body in raw_before.items() if xref not in touched and raw_after.get(xref) != body] == []
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
    EDITS[edit].prepare(doc)
    before = snapshot(doc)

    touched = EDITS[edit].apply(doc)

    assert (number(root) in touched) is (edit != "write_attachment")
    after = snapshot(pyropdf.open(doc.to_bytes()))
    assert all(after.get(xref) == value for xref, value in before.items() if xref not in touched)


@pytest.mark.parametrize("edit", sorted(EDITS.keys() - {"none"}))
def test_each_edit_rejects_a_no_op(edit: str) -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 200))
    case = EDITS[edit]
    prepared = case.open(doc.to_bytes())

    with pytest.raises((AssertionError, KeyError, StopIteration)):
        case.check(prepared)

    case.apply(prepared)
    case.check(prepared)
    case.check(pyropdf.open(prepared.to_bytes()))


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("edit", sorted(NEW_EDITS | FAMILY_EDITS))
def test_added_cases_preserve_siblings_with_indirect_annotation_and_field_lists(
    edit: str, *, incremental: bool
) -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 200))
    other = doc.pages.new((200, 200))
    other.widgets.add(pyropdf.TextField("untouched", (10, 50, 100, 70)))
    doc.form.font = face()
    doc.form["untouched"].value = "office"
    doc = EDITS[edit].open(doc.to_bytes())
    form = doc.catalog["AcroForm"]
    form["Fields"] = doc.objects[doc.objects.add(list(form["Fields"]))]
    doc.catalog["AcroForm"] = doc.objects[doc.objects.add(dict(form))]
    page = doc.pages[0]
    page.obj["Annots"] = doc.objects[doc.objects.add(list(page.obj.get("Annots", [])))]
    doc = pyropdf.open(doc.to_bytes())
    before = snapshot(doc)
    state = page_state(doc, 1)

    touched = EDITS[edit].apply(doc)
    reopened = pyropdf.open(doc.to_bytes(incremental=incremental))

    EDITS[edit].check(reopened)
    after = snapshot(reopened)
    assert [xref for xref, value in before.items() if xref not in touched and after.get(xref) != value] == []
    assert reopened.form["untouched"].value == "office"
    assert page_state(reopened, 1) == state


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize(("version", "touches_catalog"), [("1.3", True), ("1.4", False)])
def test_add_annotation_elevates_catalog_version_only_below_pdf_1_4(
    version: str, *, touches_catalog: bool, incremental: bool
) -> None:
    source = synthetic().replace(b"%PDF-1.7\n", f"%PDF-{version}\n".encode(), 1)
    doc = EDITS["add_annotation"].open(source)
    before = snapshot(doc)

    touched = EDITS["add_annotation"].apply(doc)
    reopened = pyropdf.open(doc.to_bytes(incremental=incremental))

    assert (number(doc.catalog) in touched) is touches_catalog
    assert (reopened.catalog.get("Version") == Name("1.4")) is touches_catalog
    after = snapshot(reopened)
    assert [xref for xref, value in before.items() if xref not in touched and after.get(xref) != value] == []


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_flatten_tagged_widgets_preserves_sibling_structure_elements_and_parent_tree_leaves(
    *, incremental: bool
) -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 200))
    second = doc.pages.new((200, 200))
    w1 = second.widgets.add(pyropdf.TextField("kept", (10, 50, 100, 70)))
    doc.form.font = face()
    doc.form["kept"].value = "office"
    doc = EDITS["flatten_widgets"].open(doc.to_bytes())
    w0 = doc.pages[0].widgets[0]
    w1 = doc.pages[1].widgets[0]
    w0.obj["StructParent"] = 0
    w1.obj["StructParent"] = 1
    struct_root_xref = doc.objects.add({"Type": Name("StructTreeRoot"), "ParentTreeNextKey": 2})
    struct_root = doc.objects[struct_root_xref]
    elem0 = doc.objects[
        doc.objects.add(
            {
                "Type": Name("StructElem"),
                "S": Name("Form"),
                "P": struct_root,
                "K": {"Type": Name("OBJR"), "Pg": doc.pages[0].obj, "Obj": w0.obj},
            }
        )
    ]
    elem1 = doc.objects[
        doc.objects.add(
            {
                "Type": Name("StructElem"),
                "S": Name("Form"),
                "P": struct_root,
                "K": {"Type": Name("OBJR"), "Pg": doc.pages[1].obj, "Obj": w1.obj},
            }
        )
    ]
    sibling_elem = doc.objects[doc.objects.add({"Type": Name("StructElem"), "S": Name("P"), "P": struct_root})]
    leaf0 = doc.objects[doc.objects.add({"Limits": [0, 0], "Nums": [0, elem0]})]
    leaf1 = doc.objects[doc.objects.add({"Limits": [1, 1], "Nums": [1, elem1]})]
    parent_tree = doc.objects[doc.objects.add({"Kids": [leaf0, leaf1]})]
    struct_root["K"] = [elem0, elem1, sibling_elem]
    struct_root["ParentTree"] = parent_tree
    doc.catalog["StructTreeRoot"] = struct_root
    doc = pyropdf.open(doc.to_bytes())
    before = snapshot(doc)

    touched = EDITS["flatten_widgets"].apply(doc)
    reopened = pyropdf.open(doc.to_bytes(incremental=incremental))

    EDITS["flatten_widgets"].check(reopened)
    assert {struct_root_xref, number(elem0), number(leaf1)} <= touched
    assert touched.isdisjoint({number(parent_tree), number(leaf0), number(elem1), number(sibling_elem)})
    after = snapshot(reopened)
    assert [xref for xref, value in before.items() if xref not in touched and after.get(xref) != value] == []


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
    doc = EDITS[edit].open(synthetic())
    touched = EDITS[edit].apply(doc)
    data = doc.to_bytes(incremental=incremental)
    # The saved bytes are searched for the source tokens, independently of
    # the parser: a real read from bytes has no lexeme until I7 (#16).
    for token in EXACT if 3 not in touched else EXACT[:-1]:
        assert b" " + token + b" " in data.replace(b"[", b" ").replace(b"]", b" ")
    assert len(pyropdf.open(data).objects[4]) == len(EXACT) - 1
