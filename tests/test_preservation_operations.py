"""Canvas, annotation, and form cases shared by the save-and-reopen harnesses.

Preparation runs before the preservation baseline, so edits and removals
exercise existing objects. Allowed writes are named before each operation,
independently of differences observed afterwards.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import Color, Dictionary, Font, Square, TextField

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


def nothing(doc: pyropdf.Document) -> None:
    """Leave a case that needs no preparation or additional assertions alone."""


def no_pages(_doc: pyropdf.Document) -> set[int]:
    return set()


@dataclass(frozen=True, slots=True, kw_only=True)
class Edit:
    """An operation, optional setup, and assertions to repeat after reopening."""

    apply: Callable[[pyropdf.Document], set[int]]
    prepare: Callable[[pyropdf.Document], None] = nothing
    check: Callable[[pyropdf.Document], None] = nothing
    affected_pages: Callable[[pyropdf.Document], set[int]] = no_pages

    def baseline(self, source: bytes | Path) -> bytes:
        """Finish setup, including save-time font subsets, before comparison."""
        data = source.read_bytes() if isinstance(source, Path) else source
        if self.prepare is nothing:
            return data
        with pyropdf.open(data) as doc:
            self.prepare(doc)
            return doc.to_bytes(incremental=True)

    def open(self, source: bytes | Path) -> pyropdf.Document:
        """Open the exact input bytes against which this edit is compared."""
        return pyropdf.open(self.baseline(source))


MARKER = "pyropdf-preservation"
FIELD = "pyropdf_preservation"
DRAWN = "AVA office"


def number(value: Dictionary) -> int:
    assert value.xref is not None
    return value.xref


def first_page(doc: pyropdf.Document) -> set[int]:
    return {number(doc.pages[0].obj)}


@cache
def face() -> Font:
    """Use the bundled subset face, including its font-subset settling at save."""
    root = Path(__file__).resolve().parent.parent
    return Font.from_file(root / "crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf")


def canvas_path(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    with page.draw() as canvas:
        canvas.rect((10, 10, 30, 30), fill=Color(0.25, 0.5, 0.75))
    return {number(page.obj)}


def canvas_text(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    with page.draw() as canvas:
        canvas.text(DRAWN, (10, 40), font=face(), size=10)
    return {number(page.obj)}


def canvas_image(doc: pyropdf.Document) -> set[int]:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\x40\x80\xc0"))
        + chunk(b"IEND", b"")
    )
    page = doc.pages[0]
    with page.draw() as canvas:
        canvas.image((10, 10, 30, 30), png)
    return {number(page.obj)}


def check_path(doc: pyropdf.Document) -> None:
    assert any(tuple(path.bbox) == pytest.approx((10, 10, 30, 30), abs=0.001) for path in doc.pages[0].paths())


def check_text(doc: pyropdf.Document) -> None:
    assert DRAWN in doc.pages[0].text(unmapped="replacement")


def check_image(doc: pyropdf.Document) -> None:
    assert any(image.pixmap().pixel(0, 0) == (64, 128, 192) for image in doc.pages[0].images())


def pdf_version(doc: pyropdf.Document) -> tuple[int, int]:
    """Return the effective PDF version from the file header and catalog ``/Version``."""
    header = doc.to_bytes()[:8].removeprefix(b"%PDF-").decode("ascii")
    major, minor = header.split(".")
    effective = (int(major), int(minor))
    declared = doc.catalog.get("Version")
    if isinstance(declared, pyropdf.Name):
        parts = str(declared).split(".")
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            effective = max(effective, (int(parts[0]), int(parts[1])))
    return effective


def add_annotation(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    touched = page_annotations(page)
    # Square interior_color (/IC) and annotation.name (/NM) require PDF 1.4,
    # elevating /Catalog /Version only on older inputs (#797).
    if pdf_version(doc) < (1, 4):
        touched.add(number(doc.catalog))
    annotation = page.annotations.add(Square((10, 10, 30, 30), interior_color=Color(0.25, 0.5, 0.75), contents=MARKER))
    annotation.name = MARKER
    return touched


def page_annotations(page: pyropdf.Page) -> set[int]:
    """Include an annotation list stored in its own indirect array."""
    touched = {number(page.obj)}
    items = page.obj.get("Annots")
    if isinstance(items, pyropdf.Array) and items.xref is not None:
        touched.add(items.xref)
    return touched


def prepare_annotation(doc: pyropdf.Document) -> None:
    assert all(annotation.name != MARKER for annotation in doc.pages[0].annotations)
    add_annotation(doc)


def annotation(doc: pyropdf.Document) -> pyropdf.Annotation:
    return next(item for item in doc.pages[0].annotations if item.name == MARKER)


def edit_annotation(doc: pyropdf.Document) -> set[int]:
    item = annotation(doc)
    touched = {item.xref, number(item.obj["AP"]["N"])}
    with doc.transaction():
        item.contents = MARKER + " edited"
        item.interior_color = Color(0.75, 0.5, 0.25)
    return touched


def delete_annotation(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    item = annotation(doc)
    touched = page_annotations(page) | {item.xref, number(item.obj["AP"]["N"])}
    del page.annotations[next(index for index, entry in enumerate(page.annotations) if entry == item)]
    return touched


def check_annotation(doc: pyropdf.Document) -> None:
    assert annotation(doc).contents == MARKER
    assert pdf_version(doc) >= (1, 4)


def check_edited_annotation(doc: pyropdf.Document) -> None:
    item = annotation(doc)
    assert item.contents == MARKER + " edited"
    assert item.interior_color == Color(0.75, 0.5, 0.25)


def check_removed_annotation(doc: pyropdf.Document) -> None:
    assert all(item.name != MARKER for item in doc.pages[0].annotations)


def appearance_objects(obj: Dictionary) -> set[int]:
    """Name appearance dictionaries and streams, without their shared resources."""
    found: set[int] = set()
    pending = [obj.get("AP")]
    while pending:
        item = pending.pop()
        if not isinstance(item, Dictionary):
            continue
        if item.xref is not None:
            if item.xref in found:
                continue
            found.add(item.xref)
        if not isinstance(item, pyropdf.Stream):
            pending.extend(item.values())
    return found


def _has_objr_for(elem: Dictionary, target: Dictionary) -> bool:
    """Return whether ``elem`` is a structure element whose ``/K`` holds an ``/OBJR`` to ``target``."""
    if not isinstance(elem.get("S"), pyropdf.Name):
        return False
    kids = elem.get("K")
    entries = kids if isinstance(kids, pyropdf.Array) else [kids]
    return any(
        isinstance(entry, Dictionary) and entry.get("Type") == pyropdf.Name("OBJR") and entry.get("Obj") == target
        for entry in entries
    )


def _parent_tree_entries(parent_tree: object) -> dict[int, Dictionary]:
    """Collect integer ``/ParentTree`` keys that name indirect structure elements."""
    entries: dict[int, Dictionary] = {}
    visited: set[int] = set()
    pending = [parent_tree]
    while pending:
        node = pending.pop()
        if not isinstance(node, Dictionary):
            continue
        if node.xref is not None:
            if node.xref in visited:
                continue
            visited.add(node.xref)
        kids = node.get("Kids")
        if isinstance(kids, pyropdf.Array):
            pending.extend(kids)
        nums = node.get("Nums")
        if isinstance(nums, pyropdf.Array):
            for index in range(0, len(nums) - 1, 2):
                key = nums[index]
                value = nums[index + 1]
                if isinstance(key, int) and isinstance(value, Dictionary) and value.xref is not None:
                    entries[key] = value
    return entries


def _right_spine(parent_tree: Dictionary) -> tuple[list[Dictionary], Dictionary]:
    """Walk ``/ParentTree``'s rightmost ``/Kids`` chain and return ``(ancestors, leaf)``."""
    spine: list[Dictionary] = []
    leaf = parent_tree
    for _ in range(64):
        kids = leaf.get("Kids")
        if not isinstance(kids, pyropdf.Array) or not kids or not isinstance(kids[-1], Dictionary):
            return spine, leaf
        spine.append(leaf)
        leaf = kids[-1]
    pytest.fail("the fixture's ParentTree is cyclic or too deep")


def _split_spine_objects(spine: list[Dictionary], leaf: Dictionary, pairs: int) -> set[int]:
    """Name ``/ParentTree`` objects rewritten when appending overflows a 64-pair leaf."""
    touched: set[int] = set()
    if (not spine or pairs > 64 or "Limits" not in leaf) and leaf.xref is not None:
        touched.add(leaf.xref)
    splitting = True
    for index in range(len(spine) - 1, -1, -1):
        ancestor = spine[index]
        if not splitting:
            if (index > 0 or "Limits" in ancestor) and ancestor.xref is not None:
                touched.add(ancestor.xref)
            continue
        if ancestor.xref is not None:
            touched.add(ancestor.xref)
        kids = ancestor.get("Kids")
        if isinstance(kids, pyropdf.Array) and len(kids) + 1 <= 64:
            if kids.xref is not None:
                touched.add(kids.xref)
            splitting = False
        elif index == 0:
            splitting = False
    return touched


def _parent_tree_append_objects(doc: pyropdf.Document, struct_root: Dictionary) -> set[int]:
    """Name the ``/StructTreeRoot`` and ``/ParentTree`` right-spine objects touched by one appended key."""
    touched = {struct_root.xref if struct_root.xref is not None else number(doc.catalog)}
    parent_tree = struct_root.get("ParentTree")
    if not isinstance(parent_tree, Dictionary):
        return touched
    spine, leaf = _right_spine(parent_tree)
    nums = leaf.get("Nums")
    pairs = len(nums) // 2 if isinstance(nums, pyropdf.Array) else 0
    if pairs >= 64:
        return touched | _split_spine_objects(spine, leaf, pairs)
    if leaf.xref is not None:
        touched.add(leaf.xref)
    if isinstance(nums, pyropdf.Array) and nums.xref is not None:
        touched.add(nums.xref)
    touched.update(
        ancestor.xref
        for index, ancestor in enumerate(spine)
        if (index > 0 or "Limits" in ancestor) and ancestor.xref is not None
    )
    return touched


def tagged_flatten_objects(doc: pyropdf.Document, items: Iterable[Dictionary]) -> set[int]:
    """Name structure-tree objects rewritten when flattening tagged annotations or widgets (#800)."""
    struct_root = doc.catalog.get("StructTreeRoot")
    if not isinstance(struct_root, Dictionary):
        return set()
    tagged_candidates = [obj for obj in items if isinstance(obj.get("StructParent"), int) and obj["StructParent"] >= 0]
    if not tagged_candidates:
        return set()
    parent_map = _parent_tree_entries(struct_root.get("ParentTree"))
    elements = {
        number(parent_map[obj["StructParent"]])
        for obj in tagged_candidates
        if obj["StructParent"] in parent_map and _has_objr_for(parent_map[obj["StructParent"]], obj)
    }
    if not elements:
        return set()
    return elements | _parent_tree_append_objects(doc, struct_root)


def flatten_annotations(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    touched = page_annotations(page) | tagged_flatten_objects(doc, (item.obj for item in page.annotations))
    for item in page.annotations:
        touched.add(item.xref)
        touched.update(appearance_objects(item.obj))
        popup = item.obj.get("Popup")
        if isinstance(popup, Dictionary) and popup.xref is not None:
            touched.add(popup.xref)
    page.flatten(widgets=False)
    return touched


def check_flattened_annotations(doc: pyropdf.Document) -> None:
    assert not doc.pages[0].annotations
    assert any(
        isinstance(path.fill, Color) and path.fill.rgb == pytest.approx((0.25, 0.5, 0.75), abs=0.005)
        for path in doc.pages[0].paths()
    )


def form_containers(doc: pyropdf.Document, *, membership: bool = True) -> set[int]:
    """Name the catalog/form containers that field-list mutations can rewrite."""
    form = doc.catalog.get("AcroForm")
    touched = {form.xref} if isinstance(form, Dictionary) and form.xref is not None else {number(doc.catalog)}
    if membership and isinstance(form, Dictionary):
        for key in ("Fields", "CO"):
            items = form.get(key)
            if isinstance(items, pyropdf.Array) and items.xref is not None:
                touched.add(items.xref)
    return touched


def removal_containers(doc: pyropdf.Document) -> set[int]:
    touched = form_containers(doc)
    if len(doc.form) == 1:
        touched.add(number(doc.catalog))
    return touched


def missing_appearances(doc: pyropdf.Document) -> list[pyropdf.Widget]:
    """Widgets that the documented form-fill contract may draw as a side effect.

    Existing nonempty appearances stay outside the allowed writes. Button
    selection can update every widget of a group that lacks a selected state.
    """
    missing = []
    for field in doc.form.values():
        buttons = field.type in {pyropdf.FieldType.CHECKBOX, pyropdf.FieldType.RADIO}
        if not buttons and field.type not in {
            pyropdf.FieldType.TEXT,
            pyropdf.FieldType.COMBOBOX,
            pyropdf.FieldType.LISTBOX,
        }:
            continue
        widgets = field.widgets
        absent = []
        for widget in widgets:
            appearances = widget.obj.get("AP")
            normal = appearances.get("N") if isinstance(appearances, Dictionary) else None
            selected = normal.get(widget.obj.get("AS", "Off")) if buttons and isinstance(normal, Dictionary) else normal
            if not isinstance(selected, pyropdf.Stream) or not selected.read(decode=False):
                absent.append(widget)
        missing.extend(widgets if buttons and absent else absent)
    return missing


def add_field_pages(doc: pyropdf.Document) -> set[int]:
    return first_page(doc) | {number(widget.page.obj) for widget in missing_appearances(doc)}


def add_field(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    touched = form_containers(doc) | page_annotations(page)
    touched.update(widget.xref for widget in missing_appearances(doc))
    page.widgets.add(TextField(FIELD, (10, 50, 100, 70), label=MARKER))
    doc.form.font = face()
    doc.form[FIELD].value = "AVA"
    return touched


def prepare_field(doc: pyropdf.Document) -> None:
    assert FIELD not in doc.form
    add_field(doc)


def fill_field(doc: pyropdf.Document) -> set[int]:
    field = doc.form[FIELD]
    touched = form_containers(doc, membership=False) | {field.xref}
    # The reopened fixture has no registered runtime face. Filling installs
    # one and replaces the appearance; the old embedded resources stay intact.
    for widget in field.widgets:
        touched.add(widget.xref)
        appearance = widget.obj["AP"]["N"]
        touched.add(number(appearance))
    doc.form.font = face()
    doc.form[FIELD].value = DRAWN
    return touched


def delete_field(doc: pyropdf.Document) -> set[int]:
    field = doc.form[FIELD]
    touched = removal_containers(doc) | {field.xref}
    for widget in field.widgets:
        touched.update(page_annotations(widget.page) | {widget.xref, number(widget.obj["AP"]["N"])})
    del doc.form[FIELD]
    return touched


def delete_widget(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    field = doc.form[FIELD]
    widget = field.widgets[0]
    touched = (
        removal_containers(doc) | page_annotations(page) | {field.xref, widget.xref, number(widget.obj["AP"]["N"])}
    )
    del page.widgets[next(index for index, entry in enumerate(page.widgets) if entry == widget)]
    return touched


def check_field(doc: pyropdf.Document) -> None:
    assert doc.form[FIELD].value == "AVA"
    assert "AVA" in doc.pages[0].text(unmapped="replacement")


def check_filled_field(doc: pyropdf.Document) -> None:
    assert doc.form[FIELD].value == DRAWN
    check_text(doc)


def check_removed_field(doc: pyropdf.Document) -> None:
    assert FIELD not in doc.form
    assert all(widget.field is None or widget.field.name != FIELD for widget in doc.pages[0].widgets)


def flatten_widgets(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    touched = (
        form_containers(doc)
        | page_annotations(page)
        | tagged_flatten_objects(doc, (widget.obj for widget in page.widgets))
    )
    for widget in page.widgets:
        touched.update(appearance_objects(widget.obj))
        node = widget.obj
        for _ in range(64):
            touched.add(number(node))
            kids = node.get("Kids")
            if isinstance(kids, pyropdf.Array) and kids.xref is not None:
                touched.add(kids.xref)
            parent = node.get("Parent")
            if not isinstance(parent, Dictionary):
                break
            node = parent
        else:
            pytest.fail("the fixture's field parents are cyclic or too deep")
    form = doc.catalog["AcroForm"]
    for key in ("Fields", "CO"):
        items = form.get(key)
        if isinstance(items, pyropdf.Array) and items.xref is not None:
            touched.add(items.xref)
    # Removing the last field also removes /AcroForm from the catalog.
    if all(all(widget.page == page for widget in field.widgets) for field in doc.form.values()):
        touched.add(number(doc.catalog))
    page.flatten(annotations=False)
    return touched


def check_flattened_widgets(doc: pyropdf.Document) -> None:
    assert not doc.pages[0].widgets
    assert FIELD not in doc.form
    assert "AVA" in doc.pages[0].text(unmapped="replacement")


NEW_EDITS = {
    "canvas_path": Edit(apply=canvas_path, check=check_path),
    "canvas_text": Edit(apply=canvas_text, check=check_text),
    "canvas_image": Edit(apply=canvas_image, check=check_image),
    "add_annotation": Edit(apply=add_annotation, check=check_annotation),
    "edit_annotation": Edit(prepare=prepare_annotation, apply=edit_annotation, check=check_edited_annotation),
    "delete_annotation": Edit(prepare=prepare_annotation, apply=delete_annotation, check=check_removed_annotation),
    "flatten_annotations": Edit(
        prepare=prepare_annotation, apply=flatten_annotations, check=check_flattened_annotations
    ),
    "add_field": Edit(apply=add_field, check=check_field),
    "fill_field": Edit(prepare=prepare_field, apply=fill_field, check=check_filled_field),
    "delete_field": Edit(prepare=prepare_field, apply=delete_field, check=check_removed_field),
    "delete_widget": Edit(prepare=prepare_field, apply=delete_widget, check=check_removed_field),
    "flatten_widgets": Edit(prepare=prepare_field, apply=flatten_widgets, check=check_flattened_widgets),
}

# Every added operation intentionally changes the first page's content or
# appearances, including field fills that do not rewrite its dictionary.
NEW_EDITS = {
    name: Edit(
        apply=case.apply,
        prepare=case.prepare,
        check=case.check,
        affected_pages=add_field_pages if name == "add_field" else first_page,
    )
    for name, case in NEW_EDITS.items()
}


def prepare_viewer_preferences(doc: pyropdf.Document) -> None:
    # Keep the existing PDF/UA display-title setting, and force the indirect
    # replacement path without disturbing the original document's other objects.
    doc.viewer_preferences = pyropdf.ViewerPreferences(display_title=True, hide_toolbar=False)
    prefs = doc.catalog["ViewerPreferences"]
    if not prefs.is_indirect:
        doc.catalog["ViewerPreferences"] = doc.objects[doc.objects.add(dict(prefs))]


def edit_viewer_preferences(doc: pyropdf.Document) -> set[int]:
    prefs = doc.catalog["ViewerPreferences"]
    doc.viewer_preferences = pyropdf.ViewerPreferences(display_title=True, hide_toolbar=True)
    assert doc.catalog["ViewerPreferences"] == prefs
    return {number(prefs)}


def check_viewer_preferences(doc: pyropdf.Document) -> None:
    assert doc.viewer_preferences == pyropdf.ViewerPreferences(display_title=True, hide_toolbar=True)
    assert doc.catalog["ViewerPreferences"].is_indirect


def prepare_structure(doc: pyropdf.Document) -> None:
    # A tagged corpus document may already have a single Document element.
    # Place the test container inside it to preserve its root and reading order.
    parent = doc.structure[0] if doc.structure else doc.structure.add("Document")
    container = parent.children.add(pyropdf.TagRole.DIV, title=MARKER)
    container.children.add(pyropdf.TagRole.PARAGRAPH, title="first")
    container.children.add(pyropdf.TagRole.PARAGRAPH, title="second")
    role_map = doc.structure.role_map
    assert "PreservationRole" not in role_map
    role_map["PreservationRole"] = "P"
    root = doc.structure.obj
    assert root is not None
    roles = root["RoleMap"]
    if not roles.is_indirect:
        root["RoleMap"] = doc.objects[doc.objects.add(dict(roles))]


def edit_structure(doc: pyropdf.Document) -> set[int]:
    container = doc.structure[0].children[-1]
    root = doc.structure.obj
    assert root is not None
    touched = {number(root["RoleMap"]), container.xref, container.children[0].xref}
    doc.structure.role_map.update(PreservationRole="Span")
    elem = container.children[0]
    elem.role = "PreservationRole"
    elem.actual_text = MARKER
    container.children.move(0, 1)
    return touched


def check_structure(doc: pyropdf.Document) -> None:
    container = doc.structure[0].children[-1]
    assert container.title == MARKER
    assert [child.title for child in container.children] == ["second", "first"]
    elem = container.children[-1]
    assert elem.parent == container
    assert elem.role == "PreservationRole"
    assert elem.standard_role == "Span"
    assert elem.actual_text == MARKER


def prepare_actions(doc: pyropdf.Document) -> None:
    # Two holders share the indirect action. Removing the page trigger must
    # keep the action alive while the catalog still opens the same destination.
    page = doc.pages[0]
    dest = [page.obj, pyropdf.Name("Fit")]
    action = doc.objects[doc.objects.add({"S": pyropdf.Name("GoTo"), "D": dest})]
    doc.catalog["OpenAction"] = action
    page.obj["AA"] = doc.objects[doc.objects.add({"O": action})]


def edit_actions(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    shared = doc.catalog["OpenAction"]
    touched = {page.xref, number(page.obj["AA"]), number(shared), number(doc.catalog)}
    del page.triggers[pyropdf.PageTrigger.OPEN]
    assert doc.catalog["OpenAction"] == shared
    assert doc.open_action == pyropdf.Destination(0)
    doc.open_action = pyropdf.Destination(0, zoom=1.25)
    assert number(shared) not in doc.objects
    return touched


def check_actions(doc: pyropdf.Document) -> None:
    assert doc.open_action == pyropdf.Destination(0, zoom=1.25)
    assert "AA" not in doc.pages[0].obj


def prepare_article(doc: pyropdf.Document) -> None:
    thread = doc.threads.add(
        [(0, (10, 10, 30, 30)), (0, (40, 40, 60, 60))], created="D:20250101", modified="D:20250102"
    )
    thread.obj["I"] = doc.objects[doc.objects.add(dict(thread.obj["I"]))]
    page = doc.pages[0]
    page.obj["B"] = doc.objects[doc.objects.add(list(page.obj["B"]))]


def edit_article(doc: pyropdf.Document) -> set[int]:
    thread = doc.threads[-1]
    first, second = thread.beads
    touched = {thread.xref, number(thread.obj["I"]), first.xref, second.xref, number(doc.pages[0].obj["B"])}
    thread.modified = "D:20250103"
    thread.beads.move(0, 1)
    thread.beads[0].rect = (45, 45, 75, 75)
    return touched


def check_article(doc: pyropdf.Document) -> None:
    thread = doc.threads[-1]
    assert (thread.created, thread.modified) == ("D:20250101", "D:20250103")
    first, second = thread.beads
    assert tuple(first.rect) == pytest.approx((45, 45, 75, 75))
    assert tuple(second.rect) == pytest.approx((10, 10, 30, 30))
    assert thread.obj["F"] == first.obj
    assert first.obj["T"] == thread.obj
    assert second.obj.get("T", thread.obj) == thread.obj
    assert first.obj["N"] == first.obj["V"] == second.obj
    assert second.obj["N"] == second.obj["V"] == first.obj
    beads = [item.xref for item in doc.pages[0].obj["B"] if item.xref in (first.xref, second.xref)]
    assert beads == [first.xref, second.xref]


def prepare_collection(doc: pyropdf.Document) -> None:
    doc.collection = pyropdf.Collection(view=pyropdf.CollectionView.DETAILS)


def edit_collection(doc: pyropdf.Document) -> set[int]:
    previous = number(doc.catalog["Collection"])
    doc.collection = pyropdf.Collection(view=pyropdf.CollectionView.TILE)
    assert previous not in doc.objects
    return {number(doc.catalog), previous}


def check_collection(doc: pyropdf.Document) -> None:
    assert doc.collection == pyropdf.Collection(view=pyropdf.CollectionView.TILE)
    assert doc.catalog["Collection"].is_indirect


def import_form_data(doc: pyropdf.Document) -> set[int]:
    field = doc.form[FIELD]
    touched = form_containers(doc, membership=False) | {field.xref}
    for widget in field.widgets:
        touched.update((widget.xref, number(widget.obj["AP"]["N"])))
    doc.form.font = face()
    # Exercise both public parsers, then their common atomic field/appearance
    # update. Each operation must make its own observable change.
    with pyropdf.Document() as source:
        source.pages.new((200, 200))
        source.pages[0].widgets.add(TextField(FIELD, (10, 50, 100, 70), value="office"))
        payload = source.form.to_fdf()
    assert doc.form.import_fdf(payload) == [FIELD]
    assert field.value == "office"
    xml = (
        '<xfdf xmlns="http://ns.adobe.com/xfdf/"><fields>'
        f'<field name="{FIELD}"><value>{DRAWN}</value></field></fields></xfdf>'
    )
    assert doc.form.import_xfdf(xml) == [FIELD]
    return touched


FAMILY_EDITS = {
    "viewer_preferences": Edit(
        prepare=prepare_viewer_preferences, apply=edit_viewer_preferences, check=check_viewer_preferences
    ),
    "structure_role_map": Edit(prepare=prepare_structure, apply=edit_structure, check=check_structure),
    "shared_action_trigger": Edit(prepare=prepare_actions, apply=edit_actions, check=check_actions),
    "article_beads": Edit(prepare=prepare_article, apply=edit_article, check=check_article),
    "collection": Edit(prepare=prepare_collection, apply=edit_collection, check=check_collection),
    "import_form_data": Edit(
        prepare=prepare_field, apply=import_form_data, check=check_filled_field, affected_pages=first_page
    ),
}


def prepare_named_pages(doc: pyropdf.Document) -> None:
    doc.named_pages = {MARKER: 0, "sibling": 0}


def edit_named_pages(doc: pyropdf.Document) -> set[int]:
    names = doc.catalog["Names"]
    touched = {number(names) if names.is_indirect else number(doc.catalog), number(names["Pages"])}
    del doc.named_pages[MARKER]
    doc.named_pages.update({MARKER + "-renamed": 0})
    return touched


def check_named_pages(doc: pyropdf.Document) -> None:
    assert dict(doc.named_pages) == {MARKER + "-renamed": doc.pages[0], "sibling": doc.pages[0]}


def prepare_outline(doc: pyropdf.Document) -> None:
    doc.outline = [
        pyropdf.OutlineEntry(1, MARKER, pyropdf.Destination(0), is_open=True),
        pyropdf.OutlineEntry(2, "child", pyropdf.Destination(0)),
    ]


def edit_outline(doc: pyropdf.Document) -> set[int]:
    parent = doc.outline[0]
    root = doc.catalog["Outlines"]
    touched = {number(root), parent.xref}
    if pdf_version(doc) < (1, 4):
        touched.add(number(doc.catalog))
    parent.title = MARKER + "-edited"
    parent.is_open = False
    parent.italic = True
    return touched


def check_outline(doc: pyropdf.Document) -> None:
    parent = doc.outline[0]
    assert parent.title == MARKER + "-edited"
    assert parent.italic
    assert not parent.is_open
    assert parent.obj["Count"] == -1
    assert doc.catalog["Outlines"]["Count"] == 1
    assert parent.obj["First"]["Title"].text == "child"


def prepare_page_labels(doc: pyropdf.Document) -> None:
    doc.page_labels = [pyropdf.PageLabelRange(0, prefix="before")]


def edit_page_labels(doc: pyropdf.Document) -> set[int]:
    root = doc.catalog["PageLabels"]
    touched = {number(doc.catalog)}
    if root.is_indirect:
        touched.add(number(root))
    touched.update(entry.xref for entry in root["Nums"] if isinstance(entry, Dictionary) and entry.xref is not None)
    doc.page_labels = [pyropdf.PageLabelRange(0, prefix=MARKER)]
    return touched


def check_page_labels(doc: pyropdf.Document) -> None:
    assert doc.page_labels == [pyropdf.PageLabelRange(0, prefix=MARKER)]
    assert doc.pages[0].label == MARKER + "1"


def restore_history(doc: pyropdf.Document) -> set[int]:
    with pyropdf.open(doc.to_bytes(incremental=True)) as other:
        with other.transaction("presentation"):
            other.pages[0].duration = 17.125
            other.page_layout = pyropdf.PageLayout.ONE_COLUMN
        other.page_layout = pyropdf.PageLayout.TWO_COLUMN_LEFT
        other.history.undo()
        history = other.history.save()
    doc.history.restore(history)
    assert doc.history.can_redo
    assert doc.history.can_undo
    return {number(doc.catalog), doc.pages[0].xref}


def check_history(doc: pyropdf.Document) -> None:
    assert doc.pages[0].duration == 17.125
    assert doc.page_layout is pyropdf.PageLayout.ONE_COLUMN


def prepare_scripts(doc: pyropdf.Document) -> None:
    doc.scripts = {MARKER: "void 0"}
    doc.triggers = {pyropdf.DocumentTrigger.CLOSE: "void 1"}


def remove_scripts(doc: pyropdf.Document) -> set[int]:
    catalog = doc.catalog
    names = catalog["Names"]
    tree = names["JavaScript"]
    touched = {number(catalog), number(tree)}
    for item in (names, tree["Names"][1], catalog["AA"], catalog["AA"]["DC"]):
        if item.is_indirect:
            touched.add(number(item))
    del doc.scripts[MARKER]
    doc.triggers.clear()
    return touched


def check_scripts(doc: pyropdf.Document) -> None:
    assert MARKER not in doc.scripts
    assert pyropdf.DocumentTrigger.CLOSE not in doc.triggers
    assert "AA" not in doc.catalog


def prepare_structure_membership(doc: pyropdf.Document) -> None:
    parent = doc.structure[0] if doc.structure else doc.structure.add("Document")
    container = parent.children.add(pyropdf.TagRole.DIV, title=MARKER)
    container.children.add(pyropdf.TagRole.PARAGRAPH, title="old")
    container.children.add(pyropdf.TagRole.DIV, title="new")


def change_structure_membership(doc: pyropdf.Document) -> set[int]:
    container = doc.structure[0].children[-1]
    old, new = container.children
    touched = {container.xref, old.xref, new.xref}
    old.move(parent=new)
    assert new.children[0].xref == old.xref
    new.children.add(pyropdf.TagRole.PARAGRAPH, title="surviving")
    del new.children[0]
    assert old.xref not in doc.objects
    return touched


def check_structure_membership(doc: pyropdf.Document) -> None:
    container = doc.structure[0].children[-1]
    assert [c.title for c in container.children] == ["new"]
    assert [c.title for c in container.children[0].children] == ["surviving"]
    assert container.children[0].children[0].parent == container.children[0]


LINK_URI = "https://example.invalid/pyropdf-preservation-link"


def prepare_links(doc: pyropdf.Document) -> None:
    assert all(link.target != LINK_URI for link in doc.pages[0].links)
    doc.pages[0].links.add((5, 5, 20, 20), LINK_URI)


def change_links(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    link = page.links[-1]
    xref = link.xref
    touched = page_annotations(page) | {xref}
    link.rect = (6, 7, 21, 23)
    link.target = LINK_URI + "/edited"
    assert tuple(link.rect) == pytest.approx((6, 7, 21, 23))
    assert link.target == LINK_URI + "/edited"
    del page.links[-1]
    assert all(item.xref != xref for item in page.obj.get("Annots", ()))
    return touched


def check_links(doc: pyropdf.Document) -> None:
    assert all(link.target not in (LINK_URI, LINK_URI + "/edited") for link in doc.pages[0].links)


def prepare_page_boxes(doc: pyropdf.Document) -> None:
    doc.pages.new((200, 300))


def edit_page_boxes(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[-1]
    page.media_box = (0, 0, 240, 320)
    page.crop_box = (10, 20, 180, 280)
    page.art_box = (5, 6, 70, 80)
    page.bleed_box = (1, 2, 130, 140)
    page.trim_box = (2, 3, 120, 130)
    return {page.xref}


def check_page_boxes(doc: pyropdf.Document) -> None:
    page = doc.pages[-1]
    assert tuple(page.rect) == (0, 0, 170, 260)
    assert tuple(page.art_box) == (5, 6, 70, 80)
    assert tuple(page.bleed_box) == (1, 2, 130, 140)
    assert tuple(page.trim_box) == (2, 3, 120, 130)


def last_page(doc: pyropdf.Document) -> set[int]:
    return {doc.pages[-1].xref}


FAMILY_EDITS.update(
    named_pages=Edit(prepare=prepare_named_pages, apply=edit_named_pages, check=check_named_pages),
    outline=Edit(prepare=prepare_outline, apply=edit_outline, check=check_outline),
    page_labels=Edit(prepare=prepare_page_labels, apply=edit_page_labels, check=check_page_labels),
    history_restore=Edit(apply=restore_history, check=check_history),
    document_scripts=Edit(prepare=prepare_scripts, apply=remove_scripts, check=check_scripts),
    structure_membership=Edit(
        prepare=prepare_structure_membership, apply=change_structure_membership, check=check_structure_membership
    ),
    links=Edit(prepare=prepare_links, apply=change_links, check=check_links, affected_pages=first_page),
    page_boxes=Edit(
        prepare=prepare_page_boxes, apply=edit_page_boxes, check=check_page_boxes, affected_pages=last_page
    ),
)


def indirect_numbers(*values: object) -> set[int]:
    return {
        item.xref for item in values if isinstance(item, (Dictionary, pyropdf.Array)) and item.xref is not None
    }


def prepare_widget_events(doc: pyropdf.Document) -> None:
    prepare_field(doc)
    field = doc.form[FIELD]
    field.scripts[pyropdf.ScriptTrigger.FORMAT] = "event.value = event.value;"
    widget = field.widgets[0]
    widget.triggers[pyropdf.AnnotationTrigger.FOCUS] = pyropdf.NamedAction("NextPage")


def edit_widget_events(doc: pyropdf.Document) -> set[int]:
    field = doc.form[FIELD]
    widget = field.widgets[0]
    aa = widget.obj["AA"]
    touched = {widget.xref, field.xref} | indirect_numbers(aa, aa["Fo"], field.obj["AA"]["F"])
    del widget.triggers[pyropdf.AnnotationTrigger.FOCUS]
    assert field.scripts[pyropdf.ScriptTrigger.FORMAT] == "event.value = event.value;"
    del field.scripts[pyropdf.ScriptTrigger.FORMAT]
    widget.triggers[pyropdf.AnnotationTrigger.ACTIVATE] = LINK_URI
    return touched


def check_widget_events(doc: pyropdf.Document) -> None:
    field = doc.form[FIELD]
    assert dict(field.widgets[0].triggers) == {pyropdf.AnnotationTrigger.ACTIVATE: LINK_URI}
    assert not field.scripts
    assert field.value == "AVA"


def prepare_replies(doc: pyropdf.Document) -> None:
    page = doc.pages[0]
    for text in ("question", "reply"):
        item = page.annotations.add(pyropdf.Note((10, 10), contents=text))
        item.name = MARKER + text


def edit_replies(doc: pyropdf.Document) -> set[int]:
    page = doc.pages[0]
    question, reply = page.annotations[-2:]
    touched = page_annotations(page) | {question.xref, reply.xref}
    reply.in_reply_to = question
    question.popup_rect = (24, 25, 64, 65)
    question.is_open = True
    return touched


def check_replies(doc: pyropdf.Document) -> None:
    page = doc.pages[0]
    question, reply = page.annotations[-2:]
    assert question.name == MARKER + "question" and reply.name == MARKER + "reply"
    assert reply.in_reply_to == question
    assert question.is_open and question.popup_rect == pyropdf.Rect(24, 25, 64, 65)
    popup = question.obj["Popup"]
    assert popup["Parent"] == question.obj
    assert [entry.xref for entry in page.obj["Annots"]].count(popup.xref) == 1


def prepare_embedded_note(doc: pyropdf.Document) -> None:
    item = doc.pages[0].annotations.add(pyropdf.FileAttachment((10, 10), b"before", filename="source.txt"))
    item.name = MARKER


def edit_embedded_note(doc: pyropdf.Document) -> set[int]:
    item = annotation(doc)
    file = item.file
    assert file is not None
    spec = item.obj["FS"]
    stream = spec["EF"]["F"]
    touched = {item.xref} | indirect_numbers(spec, stream, stream.get("Params"))
    file.write(b"changed bytes")
    file.filename = "changed.txt"
    file.description = MARKER
    file.created = "D:20250101000000Z"
    del file.modified
    return touched


def check_embedded_note(doc: pyropdf.Document) -> None:
    file = annotation(doc).file
    assert file is not None
    assert (file.filename, file.description, file.read()) == ("changed.txt", MARKER, b"changed bytes")
    assert file.created == "D:20250101000000Z" and file.modified is None


def prepare_collection_item(doc: pyropdf.Document) -> None:
    attachment = doc.attachments.add(MARKER, b"embedded", filename="notes.txt")
    attachment.collection_item = {"Project": "before"}


def edit_collection_item(doc: pyropdf.Document) -> set[int]:
    entries = doc.catalog["Names"]["EmbeddedFiles"]["Names"]
    spec = next(entries[i + 1] for i in range(0, len(entries), 2) if entries[i] == MARKER.encode())
    previous = number(spec["CI"])
    doc.attachments[MARKER].collection_item = {"Project": MARKER, "Count": 7}
    assert previous not in doc.objects
    return {number(spec), previous}


def check_collection_item(doc: pyropdf.Document) -> None:
    item = doc.attachments[MARKER]
    assert item.collection_item == {"Project": MARKER, "Count": 7}
    assert (item.filename, item.read()) == ("notes.txt", b"embedded")


def prepare_bead_membership(doc: pyropdf.Document) -> None:
    doc.pages.new((181, 211))
    doc.threads.add([(0, (5, 6, 15, 16)), (0, (20, 21, 30, 31))], title=MARKER)
    page = doc.pages[0]
    page.obj["B"] = doc.objects[doc.objects.add(list(page.obj["B"]))]


def edit_bead_membership(doc: pyropdf.Document) -> set[int]:
    page, other = doc.pages[0], doc.pages[-1]
    thread = doc.threads[-1]
    first, second = thread.beads
    touched = {page.xref, other.xref, thread.xref, first.xref, second.xref} | indirect_numbers(page.obj["B"])
    second.page = other
    thread.beads.add(other, (41, 42, 51, 52))
    gone = first.xref
    del thread.beads[0]
    assert gone not in doc.objects
    return touched


def check_bead_membership(doc: pyropdf.Document) -> None:
    thread = doc.threads[-1]
    assert thread.title == MARKER
    first, second = thread.beads
    other = doc.pages[-1]
    assert first.page == second.page == other
    assert (first.rect, second.rect) == (pyropdf.Rect(20, 21, 30, 31), pyropdf.Rect(41, 42, 51, 52))
    assert thread.obj["F"] == first.obj and first.obj["T"] == thread.obj
    assert first.obj["N"] == first.obj["V"] == second.obj
    assert second.obj["N"] == second.obj["V"] == first.obj
    assert list(other.obj["B"]) == [first.obj, second.obj]
    assert all(bead.get("T") != thread.obj for bead in doc.pages[0].obj.get("B", ()))


def prepare_metadata(doc: pyropdf.Document) -> None:
    if doc.xmp is None:
        from test_metadata import XMP  # noqa: PLC0415 - use the corpus's minimal valid XMP packet.

        doc.xmp = XMP
    doc.metadata["title"] = MARKER + " before"


def edit_metadata(doc: pyropdf.Document) -> set[int]:
    touched = indirect_numbers(doc.trailer.get("Info"), doc.catalog.get("Metadata"))
    doc.metadata["title"] = MARKER
    return touched


def check_metadata(doc: pyropdf.Document) -> None:
    assert doc.metadata["title"] == MARKER
    assert doc.trailer["Info"]["Title"].text == MARKER
    assert doc.xmp and f">{MARKER}<" in doc.xmp


def prepare_catalog_records(doc: pyropdf.Document) -> None:
    doc.legal_attestation = pyropdf.LegalAttestation(attestation="before")
    doc.requirements = (pyropdf.DocumentRequirement(pyropdf.RequirementType.ENABLE_JAVASCRIPTS),)


def edit_catalog_records(doc: pyropdf.Document) -> set[int]:
    catalog = doc.catalog
    req = catalog["Requirements"]
    touched = {number(catalog)} | indirect_numbers(catalog["Legal"], req, *req)
    del doc.requirements
    doc.legal_attestation = pyropdf.LegalAttestation(attestation=MARKER)
    return touched


def check_catalog_records(doc: pyropdf.Document) -> None:
    assert doc.requirements == () and "Requirements" not in doc.catalog
    assert doc.legal_attestation == pyropdf.LegalAttestation(attestation=MARKER)


def prepare_signature(doc: pyropdf.Document) -> None:
    doc.prepare_signature(FIELD)
    doc.form[FIELD].lock = pyropdf.FieldLock()


def edit_signature(doc: pyropdf.Document) -> set[int]:
    field = doc.form[FIELD]
    touched = {field.xref} | indirect_numbers(field.obj.get("SV"), field.obj["Lock"])
    assert doc.prepare_signature(FIELD, field_lock=pyropdf.FieldLock(), reason=MARKER) == field.xref
    return touched


def check_signature(doc: pyropdf.Document) -> None:
    field = doc.form[FIELD]
    assert field.lock == pyropdf.FieldLock()
    assert [item.text for item in field.obj["SV"]["Reasons"]] == [MARKER]
    assert field.widgets[0].page == doc.pages[0] and field.value is None


def edit_security_store(doc: pyropdf.Document) -> set[int]:
    # A DER OCSPResponse containing the protocol's unauthorized response status.
    ocsp = bytes.fromhex("30030a0106")
    key = pyropdf.vri_key(b"archived signature")
    info = pyropdf.ValidationInfo(ocsps=(ocsp,))
    doc.security_store = pyropdf.SecurityStore(ocsps=(ocsp,), vri={key: info})
    return {number(doc.catalog)}


def check_security_store(doc: pyropdf.Document) -> None:
    dss = doc.catalog["DSS"]
    key = pyropdf.vri_key(b"archived signature")
    stream = dss["OCSPs"][0]
    assert stream.xref and stream == dss["VRI"][key]["OCSP"][0]
    assert stream.read() == bytes.fromhex("30030a0106")
    assert doc.security_store and doc.security_store.vri[key].ocsps == doc.security_store.ocsps


FAMILY_EDITS.update(
    action_triggers=Edit(prepare=prepare_widget_events, apply=edit_widget_events, check=check_widget_events),
    annotation_relations=Edit(prepare=prepare_replies, apply=edit_replies, check=check_replies, affected_pages=first_page),
    file_specification=Edit(prepare=prepare_embedded_note, apply=edit_embedded_note, check=check_embedded_note),
    collection_item=Edit(prepare=prepare_collection_item, apply=edit_collection_item, check=check_collection_item),
    article_membership=Edit(prepare=prepare_bead_membership, apply=edit_bead_membership, check=check_bead_membership),
    document_metadata=Edit(prepare=prepare_metadata, apply=edit_metadata, check=check_metadata),
    catalog_records=Edit(prepare=prepare_catalog_records, apply=edit_catalog_records, check=check_catalog_records),
    signature_field=Edit(prepare=prepare_signature, apply=edit_signature, check=check_signature),
    security_store=Edit(apply=edit_security_store, check=check_security_store),
)
