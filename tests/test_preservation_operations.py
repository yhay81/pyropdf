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


def prepare_related_files(doc: pyropdf.Document) -> None:
    doc.attachments.add("pyropdf-related-owner", b"owner", filename="drawing.dat")


def set_related_files(doc: pyropdf.Document) -> set[int]:
    from pyropdf._attachments import _lookup  # noqa: PLC0415 - identify the pre-existing object allowed to change.

    found = _lookup(doc, "pyropdf-related-owner")
    assert found is not None
    touched = {number(found.filespec)}
    stream = doc.objects[doc.objects.add_stream(b"legend", {"Type": pyropdf.Name("EmbeddedFile")})]
    owner = doc.attachments["pyropdf-related-owner"]
    owner.related_files = {"F": [pyropdf.RelatedFile(filename="legend.dat", stream=stream)]}
    return touched


def check_related_files(doc: pyropdf.Document) -> None:
    owner = doc.attachments["pyropdf-related-owner"]
    assert owner.read() == b"owner"
    (item,) = owner.related_files["F"]
    assert item.filename == b"legend.dat"
    assert item.stream.read() == b"legend"


NEW_EDITS = {
    "related_files": Edit(prepare=prepare_related_files, apply=set_related_files, check=check_related_files),
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
