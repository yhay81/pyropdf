"""Write structure attributes and reciprocal object references in the caller's transaction."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from pyropdf import _core, typed
from pyropdf._errors import MalformedPdfError
from pyropdf._objects import Array, Dictionary, Name, Stream, String, _encode, _name_text
from pyropdf._structure import (
    ObjectReference,
    _discover_tree_nodes,
    _ensure_root,
    _resolve_page_arg,
    _validate_role_name,
    _validate_text_value,
)
from pyropdf._xobjects import _resources, _sub

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from pyropdf._document import Document
    from pyropdf._page import Page
    from pyropdf._structure import StructureElement

# Standard attribute owners and the key groups in ISO 32000-2, 14.8.5.
_LIST = frozenset(("ListNumbering", "ContinuedList", "ContinuedFrom"))
_PRINT = frozenset(("Role", "Checked", "Desc"))
_TABLE = frozenset(("RowSpan", "ColSpan", "Headers", "Scope", "Summary", "Short"))
_ARTIFACT = frozenset(("Type", "Subtype"))
_SPECIAL = _LIST | _PRINT | _TABLE | _ARTIFACT | {"O", "NS", "P", "NoteType", "Contents", "checked"}
_15 = frozenset(
    [
        "BackgroundColor",
        "BorderColor",
        "BorderStyle",
        "BorderThickness",
        "Padding",
        "Color",
        "TBorderStyle",
        "TPadding",
        "TextDecorationColor",
        "TextDecorationThickness",
        "RubyAlign",
        "RubyPosition",
        "GlyphOrientationVertical",
        "Scope",
    ]
)
_16 = frozenset(("ColumnCount", "ColumnGap", "ColumnWidths"))
_20 = frozenset(("TextPosition", "ContinuedList", "ContinuedFrom", "Short")) | _ARTIFACT


_EXPORT_14 = frozenset(("XML-1.00", "HTML-3.20", "HTML-4.01", "OEB-1.00", "RTF-1.05", "CSS-1", "CSS-2"))
_EXPORT_20 = frozenset(("HTML-5.00", "CSS-3", "RDFa-1.10", "ARIA-1.1"))


def _reserve(doc: Document, count: int) -> None:
    """Charge materialized container members to the surrounding edit."""
    _core.read_operation(doc._core(), lambda budget: budget.charge("memory_size", 160 * (count + 1)))  # noqa: SLF001


def _fields(doc: Document, obj: Dictionary) -> dict[str, object]:
    _reserve(doc, len(obj))
    return dict(obj)


def _entries(doc: Document, value: object) -> list[object]:
    if isinstance(value, Array):
        _reserve(doc, len(value))
        return list(value)
    return [] if value is None else [value]


def _is_dictionary(value: object) -> bool:
    return isinstance(value, Dictionary) and not isinstance(value, Stream)


def namespace_object(doc: Document, uri: str) -> Dictionary:
    """Reuse a registered namespace by URI, or create and register its indirect dictionary."""
    from pyropdf._measurement import _require_measurement_version  # noqa: PLC0415

    _validate_text_value(uri, "namespace URI")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*:[^\s]+", uri):
        msg = "namespace must be an absolute URI"
        raise ValueError(msg)
    root = _ensure_root(doc)
    _require_measurement_version(doc, doc.catalog, (2, 0))
    existing = root.get("Namespaces")
    if existing is not None and not isinstance(existing, Array):
        msg = "structure /Namespaces is not an array"
        raise MalformedPdfError(msg)
    entries = _entries(doc, existing)
    for ns in entries:
        if not isinstance(ns, Dictionary) or not ns.is_indirect or not isinstance(name := ns.get("NS"), String):
            msg = "invalid registered structure namespace"
            raise MalformedPdfError(msg)
        if name.text == uri:
            return ns
    new = cast("Dictionary", doc.objects[doc.objects.add({"Type": Name("Namespace"), "NS": uri})])
    root["Namespaces"] = [*entries, new]
    return new


def _same_owner(value: object, owner: str, namespace: Dictionary | None) -> bool:
    if not isinstance(value, Dictionary) or isinstance(value, Stream) or value.get("O") != owner:
        return False
    if namespace is None:
        return True
    declared = value.get("NS")
    return isinstance(declared, Dictionary) and declared.get("NS") == namespace.get("NS")


def _attribute(
    doc: Document, holder: Dictionary, key: str, owner: str, namespace: Dictionary | None = None
) -> Dictionary:
    """Detach the selected owner while retaining other owners, revision numbers and shared aliases."""
    previous = holder.get(key)
    entries = _entries(doc, previous)
    position = next((i for i in range(len(entries) - 1, -1, -1) if _same_owner(entries[i], owner, namespace)), None)
    # The last object for one owner takes precedence (PDF Association correction 289).
    old = cast("Dictionary", entries[position]) if position is not None else None
    fields: dict[str, object] = {"O": Name(owner)} if old is None else _fields(doc, old)
    if namespace is not None:
        fields["NS"] = namespace
    attr = cast("Dictionary", doc.objects[doc.objects.add(fields)])
    if position is None:
        entries.append(attr)
    else:
        entries[position] = attr
    holder[key] = entries[0] if len(entries) == 1 else entries
    candidates = [obj.xref for obj in (old, previous) if isinstance(obj, (Array, Dictionary)) and obj.xref is not None]
    if candidates:
        _core.object_free_unreferenced(doc._core(), candidates)  # noqa: SLF001
    return attr


def set_attributes(  # noqa: PLR0913 - the owner and namespace identify this entry on the holder.
    doc: Document,
    holder: Dictionary,
    key: str,
    owner: str,
    values: Mapping[str, object],
    *,
    namespace: str | None = None,
) -> None:
    """Use the Arlington descriptor for value types and keep owner-specific edition gates."""
    from pyropdf._measurement import _require_measurement_version  # noqa: PLC0415

    _reserve(doc, len(values))
    if (owner == "NSO") != (namespace is not None):
        msg = "the NSO owner requires a namespace URI and other owners do not take one"
        raise ValueError(msg)
    if not values or any(name in {"O", "NS"} for name in values):
        msg = "supply attribute entries with separate owner and namespace arguments"
        raise ValueError(msg)
    if namespace is not None or owner in _EXPORT_14 or owner in _EXPORT_20:
        ns = namespace_object(doc, namespace) if namespace is not None else None
        _require_measurement_version(doc, doc.catalog, (1, 4) if owner in _EXPORT_14 else (2, 0))
        attr = _attribute(doc, holder, key, owner, ns)
        for name, value in values.items():
            attr[_validate_role_name(name, label="attribute key")] = value
        return
    minimum = _attribute_edition(owner, values)
    _require_measurement_version(doc, doc.catalog, minimum)
    dictionary = _attribute(doc, holder, key, owner)
    view = typed.StructureAttributesDict(dictionary)
    for name, value in values.items():
        if value is None:
            msg = f"/{name} needs an attribute value"
            raise TypeError(msg)
        if name in {"RowSpan", "ColSpan", "ColumnCount"} and (type(value) is not int or value < 1):
            msg = f"/{name} must be a positive integer"
            raise ValueError(msg)
        written = value
        if isinstance(value, (list, tuple)) and name != "BBox":
            # Array-valued Arlington setters accept live array handles. Stage the
            # sequence in the same transaction before applying its descriptor.
            _reserve(doc, len(value))
            dictionary[name] = value
            written = dictionary[name]
        setattr(view, name, written)


def _attribute_edition(owner: str, values: Mapping[str, object]) -> tuple[int, int]:
    groups = {"List": _LIST, "Table": _TABLE, "PrintField": _PRINT, "Artifact": _ARTIFACT | {"BBox"}}
    if owner != "Layout" and owner not in groups:
        msg = "owner must be Layout, List, Table, PrintField or Artifact"
        raise ValueError(msg)
    invalid = _SPECIAL.intersection(values) if owner == "Layout" else values.keys() - groups[owner]
    if invalid:
        msg = f"/{min(invalid)} is not a /{owner} attribute"
        raise ValueError(msg)
    minimum = (1, 4)
    if _15.intersection(values):
        minimum = (1, 5)
    if _16.intersection(values):
        minimum = (1, 6)
    if owner == "PrintField" or "Summary" in values:
        minimum = (1, 7)
    if owner == "Artifact" or _20.intersection(values) or values.get("ListNumbering") in ("Unordered", "Description"):
        minimum = (2, 0)
    return minimum


def class_attributes(doc: Document, name: str, owner: str, values: Mapping[str, object], namespace: str | None) -> None:
    name = _validate_role_name(name, label="class name")
    root = _ensure_root(doc)
    classes = root.get("ClassMap")
    if classes is not None and not _is_dictionary(classes):
        msg = "structure /ClassMap is not a dictionary"
        raise MalformedPdfError(msg)
    # A producer can share the original dictionary elsewhere in the document.
    root["ClassMap"] = _fields(doc, cast("Dictionary", classes)) if classes is not None else {}
    set_attributes(doc, root["ClassMap"], name, owner, values, namespace=namespace)


def add_class(element: StructureElement, name: str) -> None:
    name = _validate_role_name(name, label="class name")
    doc = element.document
    classes = _ensure_root(doc).get("ClassMap")
    if classes is not None and not _is_dictionary(classes):
        msg = "structure /ClassMap is not a dictionary"
        raise MalformedPdfError(msg)
    if not isinstance(classes, Dictionary) or name not in classes:
        msg = f"undefined structure attribute class /{name}"
        raise ValueError(msg)
    definition = classes[name]
    attributes = definition if isinstance(definition, Array) else (definition,)
    if any(not isinstance(attr, Dictionary) or not isinstance(attr.get("O"), Name) for attr in attributes):
        msg = f"structure class /{name} has no valid attribute definition"
        raise MalformedPdfError(msg)
    holder = element.obj
    entries = _entries(doc, holder.get("C"))
    if Name(name) not in entries:
        entries.append(Name(name))
        holder["C"] = entries[0] if len(entries) == 1 else entries


def _matching_properties(entries: list[object], name: str) -> list[int]:
    matches = []
    for i, entry in enumerate(entries):
        if not _is_dictionary(entry) or not isinstance(prop_name := cast("Dictionary", entry).get("N"), String):
            msg = "a user property has no text name"
            raise MalformedPdfError(msg)
        if prop_name.text == name:
            matches.append(i)
    if len(matches) > 1:
        msg = f"duplicate user property {name!r}"
        raise MalformedPdfError(msg)
    return matches


def user_property(element: StructureElement, name: str, value: object, formatted: str | None, *, hidden: bool) -> None:
    from pyropdf._metadata import _require_version  # noqa: PLC0415

    doc = element.document
    _validate_text_value(name, "property name")
    if not name or type(hidden) is not bool:
        msg = "a user property needs a nonempty name and a boolean hidden flag"
        raise ValueError(msg)
    if formatted is not None:
        _validate_text_value(formatted, "formatted value")
    _require_version(doc, doc.catalog, (1, 6))
    attributes = _attribute(doc, element.obj, "A", "UserProperties")
    old = attributes.get("P")
    if old is not None and not isinstance(old, Array):
        msg = "the user properties /P entry is not an array"
        raise MalformedPdfError(msg)
    entries = _entries(doc, old)
    matches = _matching_properties(entries, name)
    fields = _fields(doc, cast("Dictionary", entries[matches[0]])) if matches else {}
    fields.update({"N": name, "V": value, "H": hidden})
    if formatted is None:
        fields.pop("F", None)
    else:
        fields["F"] = formatted
    if matches:
        entries[matches[0]] = fields
    else:
        entries.append(fields)
    attributes["P"] = entries
    mark = doc.catalog.get("MarkInfo")
    if mark is not None and not _is_dictionary(mark):
        msg = "/MarkInfo is not a dictionary"
        raise MalformedPdfError(msg)
    doc.catalog["MarkInfo"] = {
        **(_fields(doc, cast("Dictionary", mark)) if mark is not None else {}),
        "Marked": True,
        "UserProperties": True,
    }


def _verify_parent_key(element: StructureElement, key: int) -> None:
    doc = element.document
    tree = _ensure_root(doc).get("ParentTree")
    if not isinstance(tree, Dictionary):
        msg = "object reference has no structure parent tree"
        raise MalformedPdfError(msg)

    def owners(budget: _core._OperationBudget) -> bool:
        found = False
        for node, _ in _discover_tree_nodes(tree, "structure parent tree", budget):
            nums = node.get("Nums")
            if isinstance(nums, Array):
                for i in range(0, len(nums), 2):
                    budget.poll()
                    if nums[i] == key:
                        if found or i + 1 >= len(nums) or nums[i + 1] != element.obj:
                            return False
                        found = True
        return found

    if not _core.read_operation(doc._core(), owners):  # noqa: SLF001
        msg = "the parent tree does not map the replaced object's key to this element"
        raise MalformedPdfError(msg)


def _drawn_xobjects(
    page: Page, stream: Stream | None, resources: Dictionary | None, budget: _core._OperationBudget
) -> Iterator[Stream]:
    xobjects = _sub(resources, "XObject")
    if xobjects is None:
        return
    source = page.obj if stream is None else stream
    if source.xref is None or source.generation is None:
        return
    names = _core.object_reference_xobjects(page.document._core(), (source.xref, source.generation), stream is None)  # noqa: SLF001
    for name in names:
        budget.poll()
        item = xobjects.get(_name_text(name))
        if isinstance(item, Stream):
            yield item


def _page_xobjects(page: Page, resources: Dictionary | None, budget: _core._OperationBudget) -> Iterator[Stream]:
    yield from _drawn_xobjects(page, None, resources, budget)
    annotations = page.obj.get("Annots")
    if isinstance(annotations, Array):
        for annotation in annotations:
            budget.poll()
            appearances = _sub(annotation, "AP") if isinstance(annotation, Dictionary) else None
            if appearances is None:
                continue
            for kind in ("N", "R", "D"):
                entry = appearances.get(kind)
                if isinstance(entry, Stream):
                    yield entry
                elif isinstance(entry, Dictionary):
                    for name in entry:
                        budget.poll()
                        state = entry[name]
                        if isinstance(state, Stream):
                            yield state


def _page_uses_xobject(page: Page, target: Stream, budget: _core._OperationBudget) -> bool:
    """Follow actual Do operands and annotation appearances, retaining each Form's resource scope."""
    resources = _resources(page)
    seen: set[tuple[Stream, Dictionary | None]] = set()
    frames = [(_page_xobjects(page, resources, budget), resources)]
    while frames:
        items, inherited = frames[-1]
        item = next(items, None)
        if item is None:
            frames.pop()
            continue
        if item == target:
            return True
        if item.get("Subtype") != "Form":
            continue
        own = item.get("Resources")
        scope = inherited if own is None else own if isinstance(own, Dictionary) else None
        if scope is None or (item, scope) in seen:
            continue
        budget.charge("memory_size", 512)
        seen.add((item, scope))
        frames.append((_drawn_xobjects(page, item, scope, budget), scope))
    return False


def _target_references(kids: list[object], target: Dictionary) -> list[tuple[int, Dictionary]]:
    _reserve(target._document, len(kids))  # noqa: SLF001
    return [
        (i, kid)
        for i, kid in enumerate(kids)
        if isinstance(kid, Dictionary) and kid.get("Type") == "OBJR" and kid.get("Obj") == target
    ]


def _existing_parent_key(element: StructureElement, target: Dictionary) -> int:
    key = target.get("StructParent")
    if type(key) is not int or key < 0:
        msg = "the target needs an existing structure parent key"
        raise ValueError(msg)
    _verify_parent_key(element, key)
    return key


def _replace_reference(
    element: StructureElement, kids: list[object], previous: Dictionary, target: Dictionary, page: Dictionary
) -> int:
    doc = element.document
    if not isinstance(previous, Dictionary) or previous._document is not doc or not previous.is_indirect:  # noqa: SLF001
        msg = "replacement requires an indirect object of this document"
        raise ValueError(msg)
    matches = _target_references(kids, previous)
    transfer_key = len(matches) == 1
    if not transfer_key:
        default_page = element.obj.get("Pg")
        matches = [(i, kid) for i, kid in matches if kid.get("Pg", default_page) == page]
    if len(matches) != 1:
        msg = "the replaced target needs one object reference in this element or one on the specified page"
        raise ValueError(msg)
    key = _existing_parent_key(element, previous)
    index, old = matches[0]
    if old.is_indirect:
        old["Obj"], old["Pg"] = target, page
    else:
        kids[index] = {**_fields(doc, old), "Obj": target, "Pg": page}
    if transfer_key:
        del previous["StructParent"]
        return key
    return _core.parent_tree_append(doc._core(), _encode(doc, element.obj, 1))  # noqa: SLF001


def object_reference(
    element: StructureElement, target: Dictionary, page: Page | int, replace: Dictionary | None
) -> ObjectReference:
    doc = element.document
    source = _resolve_page_arg(doc, page)
    if not isinstance(target, Dictionary) or not target.is_indirect or target._document is not doc:  # noqa: SLF001
        msg = "an object reference requires an indirect object of this document"
        raise ValueError(msg)
    if "StructParents" in target or (
        "StructParent" in target and (not isinstance(target, Stream) or replace is not None)
    ):
        msg = "target already has a structure parent"
        raise ValueError(msg)
    if not isinstance(target, Stream):
        annots = source.obj.get("Annots")
        if not isinstance(annots, Array) or not any(item == target for item in annots):
            msg = "the annotation must belong to the specified page"
            raise ValueError(msg)
    elif target.get("Subtype") not in ("Form", "Image"):
        msg = "the target stream is not a Form or Image XObject"
        raise ValueError(msg)
    elif not _core.read_operation(doc._core(), lambda budget: _page_uses_xobject(source, target, budget)):  # noqa: SLF001
        msg = "the XObject must be drawn on the specified page or used in one of its annotation appearances"
        raise ValueError(msg)
    kids = _entries(doc, element.obj.get("K"))
    if replace is not None:
        target["StructParent"] = _replace_reference(element, kids, replace, target, source.obj)
    else:
        if "StructParent" in target:
            existing = _target_references(kids, target)
            default_page = element.obj.get("Pg")
            if not existing or any(kid.get("Pg", default_page) in (None, source.obj) for _, kid in existing):
                msg = "target already has a structure parent or an object reference on the specified page"
                raise ValueError(msg)
            _existing_parent_key(element, target)
        else:
            target["StructParent"] = _core.parent_tree_append(doc._core(), _encode(doc, element.obj, 1))  # noqa: SLF001
        kids.append({"Type": Name("OBJR"), "Obj": target, "Pg": source.obj})
    element.obj["K"] = kids
    assert target.xref is not None  # noqa: S101 - is_indirect checked before mutating
    return ObjectReference(xref=target.xref, page=source.index)
