"""The logical structure tree: ``Document.structure``, element handles, role maps, and content items."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias, cast, overload

from pyropdf import _core
from pyropdf._canvas._tags import TagRole
from pyropdf._errors import LimitError, StaleObjectError
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._metadata import _require_version, _validate_language_tag
from pyropdf._objects import Array, Dictionary, Name, Stream, String, _catalog, _Handle
from pyropdf._page import Page

if TYPE_CHECKING:
    from pyropdf._document import Document

_CODE_STRUCTURE_TREE = "structure_tree_count"
_CODE_TEXT_INPUT = "text_input_size"
_CODE_OBJECT_COUNT = "object_count"

#: Default element cap for ``StructureTree.walk``.
_DEFAULT_MAX_ELEMENTS = 65_536

# Conservative allowance for a handle, set slot, list slots and child iterator.
_WALK_ELEMENT_BYTES = 512

#: Maximum structure-tree depth including ``/StructTreeRoot`` (``structure_tree_count``).
_MAX_STRUCTURE_DEPTH = 256

#: Maximum depth of ``/ParentTree`` and ``/IDTree``, and maximum ``/RoleMap`` hops.
_MAX_TREE_DEPTH = 32

#: Maximum byte length of a PDF role name.
_MAX_NAME_SIZE = 127

#: Maximum character count of a text entry written onto a structure element.
_MAX_STRUCTURE_TEXT = 16 * 1024 * 1024

_PDF_DELIMITERS = frozenset("()<>[]{}/%")

_TEXT_PROPERTIES: dict[str, tuple[str, tuple[int, int]]] = {
    "title": ("T", (1, 3)),
    "alt": ("Alt", (1, 3)),
    "actual_text": ("ActualText", (1, 4)),
    "expanded": ("E", (1, 5)),
}


def _is_non_bool_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_non_negative_int(value: object, name: str) -> int:
    if not _is_non_bool_int(value):
        msg = f"{name} must be an int: {value!r}"
        raise TypeError(msg)
    assert isinstance(value, int)  # noqa: S101 - narrows the type for ty.
    if value < 0:
        msg = f"{name} must be non-negative: {value}"
        raise ValueError(msg)
    return value


def _validate_role_name(role: object, *, label: str = "role") -> str:
    """Validate a PDF structure role name (``TagRole`` or ASCII PDF name)."""
    if not isinstance(role, str):
        msg = f"{label} must be a str or TagRole: {role!r}"
        raise TypeError(msg)
    name = role.value if isinstance(role, TagRole) else str(role)
    if not (1 <= len(name) <= _MAX_NAME_SIZE):
        msg = f"{label} must be 1 to {_MAX_NAME_SIZE} characters: {role!r}"
        raise ValueError(msg)
    if any(not ("!" <= ch <= "~") or ch in _PDF_DELIMITERS for ch in name):
        msg = f"{label} must use printable non-delimiter ASCII characters: {role!r}"
        raise ValueError(msg)
    return name


def _validate_text_value(value: object, name: str) -> str:
    """Validate a structure element text string and enforce the 16 MiB input cap."""
    if not isinstance(value, str):
        msg = f"{name} must be a str: {value!r}"
        raise TypeError(msg)
    if len(value) > _MAX_STRUCTURE_TEXT:
        msg = f"{name} exceeds the {_MAX_STRUCTURE_TEXT}-character safety limit"
        raise LimitError(_CODE_TEXT_INPUT, msg)
    return value


def _resolve_sequence_index(index: object, count: int, label: str = "structure index") -> int:
    """Resolve a possibly negative sequence index against ``count`` items."""
    if not _is_non_bool_int(index):
        msg = f"{label} must be an int, not {type(index).__name__}"
        raise TypeError(msg)
    assert isinstance(index, int)  # noqa: S101 - narrows the type for ty.
    resolved = index + count if index < 0 else index
    if not 0 <= resolved < count:
        msg = f"{label} {index} out of range for {count} items"
        raise IndexError(msg)
    return resolved


def _clamp_insert_index(index: int | None, count: int) -> int:
    """Resolve an optional ``list.insert``-style index against ``count`` items."""
    if index is None:
        return count
    if not _is_non_bool_int(index):
        msg = f"index must be an int or None: {index!r}"
        raise TypeError(msg)
    return max(0, index + count) if index < 0 else min(index, count)


def _root_dict(document: Document) -> Dictionary | None:
    """Return the catalog's ``/StructTreeRoot`` dictionary, or ``None``."""
    root = _catalog(document).get("StructTreeRoot")
    if isinstance(root, Dictionary) and not isinstance(root, Stream):
        return root
    return None


def _ensure_root(document: Document) -> Dictionary:
    """Return an indirect ``/StructTreeRoot`` dictionary, creating it and ``/MarkInfo`` when absent."""
    catalog = _catalog(document)
    root = catalog.get("StructTreeRoot")
    if isinstance(root, Dictionary) and not isinstance(root, Stream) and root.is_indirect:
        return root
    _require_version(document, catalog, (1, 4))
    objects = document.objects
    if isinstance(root, Dictionary) and not isinstance(root, Stream):
        xref = objects.add(root)
    else:
        xref = objects.add({"Type": Name("StructTreeRoot"), "K": []})
    catalog["StructTreeRoot"] = objects[xref]
    mark_info = catalog.get("MarkInfo")
    if not isinstance(mark_info, Dictionary) or isinstance(mark_info, Stream):
        catalog["MarkInfo"] = {"Marked": True}
    elif mark_info.get("Marked") is not True:
        mark_info["Marked"] = True
    return cast("Dictionary", catalog["StructTreeRoot"])


def _is_struct_elem_dict(value: object) -> bool:
    """Return whether ``value`` is an indirect ``StructElem`` dictionary."""
    if not isinstance(value, Dictionary) or isinstance(value, Stream) or not value.is_indirect:
        return False
    if value.xref is None or value.generation is None:
        return False
    kind = value.get("Type")
    if isinstance(kind, Name):
        return str(kind) == "StructElem"
    return kind is None and isinstance(value.get("S"), Name)


def _page_from_dict(document: Document, value: object) -> Page | None:
    """Resolve an indirect ``/Pg`` dictionary to a live ``Page``, or ``None``."""
    if not isinstance(value, Dictionary) or isinstance(value, Stream) or not value.is_indirect:
        return None
    if value.xref is None or value.generation is None:
        return None
    if _core.page_index(document._core(), value.xref, value.generation) is None:  # noqa: SLF001
        return None
    return Page(document, value.xref, value.generation)


def _page_index_from_dict(document: Document, value: object) -> int | None:
    """Resolve an indirect ``/Pg`` dictionary to its zero-based page index, or ``None``."""
    if not isinstance(value, Dictionary) or isinstance(value, Stream) or not value.is_indirect:
        return None
    if value.xref is None or value.generation is None:
        return None
    return _core.page_index(document._core(), value.xref, value.generation)  # noqa: SLF001


def _resolve_page_arg(document: Document, page: Page | int) -> Page:
    """Validate a ``Page`` or page index argument for ``document``."""
    if isinstance(page, Page):
        if page.document is not document:
            msg = "page belongs to another document"
            raise ValueError(msg)
        _ = page.index
        return page
    if _is_non_bool_int(page):
        return document.pages[page]
    msg = f"page must be a Page or int: {page!r}"
    raise TypeError(msg)


def _raw_kids(holder: Dictionary) -> list[object]:
    """Return the raw ``/K`` entries of ``holder`` as a Python list."""
    kids = holder.get("K")
    if kids is None:
        return []
    if isinstance(kids, Array):
        return list(kids)
    return [kids]


def _element_slots(raw_kids: Sequence[object]) -> list[int]:
    """Return the indices in ``raw_kids`` that hold indirect ``StructElem`` dictionaries."""
    return [idx for idx, item in enumerate(raw_kids) if _is_struct_elem_dict(item)]


def _child_elements(document: Document, holder: Dictionary) -> list[StructureElement]:
    """Return the child ``StructureElement`` handles of ``holder`` in ``/K`` order."""
    result: list[StructureElement] = []
    for item in _raw_kids(holder):
        if _is_struct_elem_dict(item):
            elem_dict = cast("Dictionary", item)
            assert elem_dict.xref is not None  # noqa: S101
            assert elem_dict.generation is not None  # noqa: S101
            result.append(StructureElement(document, elem_dict.xref, elem_dict.generation))
    return result


def _write_kids(
    holder: Dictionary,
    items: list[object],
    candidates: list[int],
    *,
    is_root: bool = False,
) -> None:
    """Write ``items`` to ``holder['K']`` and record any retired indirect ``/K`` array."""
    existing = holder.get("K")
    if isinstance(existing, Array) and existing.is_indirect and existing.xref is not None:
        candidates.append(existing.xref)
    if not items:
        if is_root:
            holder["K"] = []
        elif "K" in holder:
            del holder["K"]
    else:
        holder["K"] = items


def _depth_limit_error() -> LimitError:
    msg = f"structure tree exceeds {_MAX_STRUCTURE_DEPTH} levels including /StructTreeRoot"
    return LimitError(_CODE_STRUCTURE_TREE, msg)


def _element_level(document: Document, elem_dict: Dictionary, *, forbid_xref: int | None = None) -> int:
    """Return the 1-based depth of ``elem_dict`` (where ``/StructTreeRoot`` is 1), checking ancestors."""
    root = _root_dict(document)
    root_xref = root.xref if root is not None else None
    level = 1
    current: object = elem_dict
    visited: set[int] = set()
    while isinstance(current, Dictionary) and current.is_indirect and current.xref is not None:
        if forbid_xref is not None and current.xref == forbid_xref:
            msg = "cannot move a structure element into its own descendant"
            raise ValueError(msg)
        if root_xref is not None and current.xref == root_xref:
            return level
        if current.xref in visited:
            break
        visited.add(current.xref)
        level += 1
        if level > _MAX_STRUCTURE_DEPTH:
            raise _depth_limit_error()
        current = current.get("P")
    return level


def _check_subtree_move(elem_dict: Dictionary, *, parent_level: int, forbid_xref: int | None) -> None:
    """Verify iteratively that moving ``elem_dict`` under ``parent_level`` introduces no cycle or depth overflow."""
    stack: list[tuple[Dictionary, int]] = [(elem_dict, 1)]
    seen: set[int] = set()
    while stack:
        current, height = stack.pop()
        xref = current.xref
        if xref is None or xref in seen:
            continue
        if forbid_xref is not None and xref == forbid_xref:
            msg = "cannot move a structure element into its own descendant"
            raise ValueError(msg)
        if parent_level + height > _MAX_STRUCTURE_DEPTH:
            raise _depth_limit_error()
        seen.add(xref)
        children = [cast("Dictionary", item) for item in _raw_kids(current) if _is_struct_elem_dict(item)]
        for child in reversed(children):
            if child.xref is not None and child.xref not in seen:
                if parent_level + height + 1 > _MAX_STRUCTURE_DEPTH:
                    raise _depth_limit_error()
                stack.append((child, height + 1))


def _collect_subtree_xrefs(roots: Iterable[Dictionary]) -> set[int]:
    """Collect the object numbers of ``roots`` and all reachable child ``StructElem`` dictionaries iteratively."""
    collected: set[int] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        xref = node.xref
        if xref is None or xref in collected:
            continue
        collected.add(xref)
        for item in _raw_kids(node):
            if _is_struct_elem_dict(item):
                child = cast("Dictionary", item)
                if child.xref is not None and child.xref not in collected:
                    stack.append(child)
    return collected


def _discover_tree_nodes(tree_root: Dictionary, label: str) -> list[tuple[Dictionary, int | None]]:
    """Discover number- or name-tree nodes iteratively in pre-order up to ``_MAX_TREE_DEPTH``."""
    order: list[tuple[Dictionary, int | None]] = []
    stack: list[tuple[Dictionary, int | None, int]] = [(tree_root, None, 1)]
    seen: set[int] = set()
    while stack:
        node, parent_idx, depth = stack.pop()
        if depth > _MAX_TREE_DEPTH:
            msg = f"{label} exceeds {_MAX_TREE_DEPTH} levels"
            raise LimitError(_CODE_STRUCTURE_TREE, msg)
        if node.is_indirect and node.xref is not None:
            if node.xref in seen:
                continue
            seen.add(node.xref)
        my_idx = len(order)
        order.append((node, parent_idx))
        kids = node.get("Kids")
        if isinstance(kids, Array):
            child_dicts = [item for item in kids if isinstance(item, Dictionary) and not isinstance(item, Stream)]
            stack.extend((kid, my_idx, depth + 1) for kid in reversed(child_dicts))
    return order


@dataclass(slots=True)
class _TreeCleanState:
    document: Document
    leaves_key: str
    removed_xrefs: set[int]
    candidates: list[int]
    is_parent_tree: bool
    empty: set[int] = field(default_factory=set)
    changed_nodes: set[int] = field(default_factory=set)
    bounds: dict[int, tuple[object, object]] = field(default_factory=dict)


def _clean_parent_value(state: _TreeCleanState, value: object) -> tuple[bool, bool, object]:
    """Return ``(drop_pair, changed, updated_value)`` for one ``/ParentTree`` value."""
    if isinstance(value, Dictionary) and value.is_indirect and value.xref in state.removed_xrefs:
        return True, True, None
    if not isinstance(value, Array):
        return False, False, value
    changed = False
    updated: list[object] = []
    for item in value:
        if isinstance(item, Dictionary) and item.is_indirect and item.xref in state.removed_xrefs:
            updated.append(None)
            changed = True
        else:
            updated.append(item)
    if not changed:
        return False, False, value
    if all(item is None for item in updated):
        if value.is_indirect and value.xref is not None:
            state.candidates.append(value.xref)
        return True, True, None
    if value.is_indirect and value.xref is not None:
        state.document.objects[value.xref] = updated
        return False, True, value
    return False, True, updated


def _write_array_target(document: Document, holder: Dictionary, key: str, arr: Array, values: list[object]) -> None:
    if arr.is_indirect and arr.xref is not None:
        document.objects[arr.xref] = values
    else:
        holder[key] = values


def _retire_container_and_node(node: Dictionary, container: object, candidates: list[int]) -> None:
    if isinstance(container, Array) and container.is_indirect and container.xref is not None:
        candidates.append(container.xref)
    if node.is_indirect and node.xref is not None:
        candidates.append(node.xref)


def _filter_leaf_pairs(state: _TreeCleanState, raw: list[object]) -> tuple[list[tuple[object, object]], bool]:
    kept: list[tuple[object, object]] = []
    changed = False
    for pos in range(0, len(raw) - 1, 2):
        key, val = raw[pos], raw[pos + 1]
        if state.is_parent_tree:
            drop, val_changed, new_val = _clean_parent_value(state, val)
            changed = changed or drop or val_changed
            if not drop:
                kept.append((key, new_val))
        elif isinstance(val, Dictionary) and val.is_indirect and val.xref in state.removed_xrefs:
            changed = True
        else:
            kept.append((key, val))
    return kept, changed


def _clean_tree_leaf(state: _TreeCleanState, node: Dictionary) -> tuple[bool, bool, tuple[object, object] | None]:
    """Clean one leaf's ``/Nums`` or ``/Names``; return ``(is_empty, changed, bounds)``."""
    arr = node.get(state.leaves_key)
    if not isinstance(arr, Array):
        return False, False, None
    kept, changed = _filter_leaf_pairs(state, list(arr))
    if not kept:
        _retire_container_and_node(node, arr, state.candidates)
        return True, True, None
    bounds = (kept[0][0], kept[-1][0])
    if changed:
        flat = [item for pair in kept for item in pair]
        _write_array_target(state.document, node, state.leaves_key, arr, flat)
        if "Limits" in node:
            node["Limits"] = [bounds[0], bounds[1]]
    return False, changed, bounds


def _node_bounds(node: Dictionary, known: tuple[object, object] | None) -> tuple[object, object] | None:
    if known is not None:
        return known
    limits = node.get("Limits")
    if isinstance(limits, Array) and len(limits) == 2:  # noqa: PLR2004 - /Limits is a [min, max] pair.
        return (limits[0], limits[1])
    return None


def _clean_tree_branch(
    state: _TreeCleanState,
    node: Dictionary,
    order: list[tuple[Dictionary, int | None]],
    kid_indices: list[int],
) -> tuple[bool, bool, tuple[object, object] | None]:
    """Update one intermediate ``/Kids`` node; return ``(is_empty, changed, bounds)``."""
    kept_indices = [kid_idx for kid_idx in kid_indices if kid_idx not in state.empty]
    kids_arr = node.get("Kids")
    if not kept_indices:
        _retire_container_and_node(node, kids_arr, state.candidates)
        return True, True, None
    first_bounds = _node_bounds(order[kept_indices[0]][0], state.bounds.get(kept_indices[0]))
    last_bounds = _node_bounds(order[kept_indices[-1]][0], state.bounds.get(kept_indices[-1]))
    branch_bounds = (first_bounds[0], last_bounds[1]) if first_bounds and last_bounds else None
    kids_removed = len(kept_indices) != len(kid_indices)
    sub_changed = kids_removed or any(kid_idx in state.changed_nodes for kid_idx in kept_indices)
    if kids_removed and isinstance(kids_arr, Array):
        kept_dicts: list[object] = [order[kid_idx][0] for kid_idx in kept_indices]
        _write_array_target(state.document, node, "Kids", kids_arr, kept_dicts)
    if sub_changed and "Limits" in node and branch_bounds is not None:
        node["Limits"] = [branch_bounds[0], branch_bounds[1]]
    return False, sub_changed, branch_bounds


def _children_by_parent(order: Sequence[tuple[Dictionary, int | None]]) -> dict[int, list[int]]:
    children_of: dict[int, list[int]] = {}
    for idx, (_, parent_idx) in enumerate(order):
        if parent_idx is not None:
            children_of.setdefault(parent_idx, []).append(idx)
    return children_of


def _clean_lookup_tree(root: Dictionary, tree_key: str, state: _TreeCleanState) -> None:
    """Clean ``state.removed_xrefs`` from ``root[tree_key]`` and maintain ``/Limits``."""
    tree_root = root.get(tree_key)
    if not isinstance(tree_root, Dictionary) or isinstance(tree_root, Stream):
        return
    order = _discover_tree_nodes(tree_root, tree_key)
    children_of = _children_by_parent(order)
    for idx in range(len(order) - 1, -1, -1):
        node, _ = order[idx]
        if state.leaves_key in node:
            is_empty, changed, node_bounds = _clean_tree_leaf(state, node)
        elif "Kids" in node:
            is_empty, changed, node_bounds = _clean_tree_branch(state, node, order, children_of.get(idx, []))
        else:
            continue
        if is_empty:
            state.empty.add(idx)
        if is_empty or changed:
            state.changed_nodes.add(idx)
        if node_bounds is not None:
            state.bounds[idx] = node_bounds
    if 0 in state.empty:
        del root[tree_key]


def _delete_children_at(
    document: Document,
    holder: Dictionary,
    indices: set[int],
    *,
    is_root: bool,
) -> None:
    """Remove the child elements at ``indices`` from ``holder`` and clean dependent trees in one transaction."""
    if not indices:
        return
    raw = _raw_kids(holder)
    slots = _element_slots(raw)
    drop_raw = {slots[pos] for pos in indices}
    removed_roots = [cast("Dictionary", raw[raw_idx]) for raw_idx in sorted(drop_raw)]
    removed_xrefs = _collect_subtree_xrefs(removed_roots)
    kept_raw = [item for raw_idx, item in enumerate(raw) if raw_idx not in drop_raw]
    root = _root_dict(document)
    candidates = sorted(removed_xrefs)
    with document.transaction("structure"):
        _write_kids(holder, kept_raw, candidates, is_root=is_root)
        if root is not None and removed_xrefs:
            parent_state = _TreeCleanState(document, "Nums", removed_xrefs, candidates, is_parent_tree=True)
            id_state = _TreeCleanState(document, "Names", removed_xrefs, candidates, is_parent_tree=False)
            _clean_lookup_tree(root, "ParentTree", parent_state)
            _clean_lookup_tree(root, "IDTree", id_state)
        if candidates:
            _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001


def _validate_new_element_options(  # noqa: PLR0913 - keyword options of add().
    document: Document,
    role: TagRole | str,
    *,
    index: int | None,
    page: Page | int | None,
    title: str | None,
    language: str | None,
    alt: str | None,
    actual_text: str | None,
    expanded: str | None,
) -> tuple[str, Page | None, dict[str, object], tuple[int, int]]:
    """Validate arguments for ``add(...)`` and return ``(role_name, page, extra_entries, min_version)``."""
    checked_role = _validate_role_name(role, label="role")
    if index is not None and not _is_non_bool_int(index):
        msg = f"index must be an int or None: {index!r}"
        raise TypeError(msg)
    resolved_page = _resolve_page_arg(document, page) if page is not None else None
    entries: dict[str, object] = {}
    minimum = (1, 3)
    if title is not None:
        entries["T"] = _validate_text_value(title, "title")
    if language is not None:
        entries["Lang"] = _validate_language_tag(language)
        minimum = max(minimum, (1, 4))
    if alt is not None:
        entries["Alt"] = _validate_text_value(alt, "alt")
    if actual_text is not None:
        entries["ActualText"] = _validate_text_value(actual_text, "actual_text")
        minimum = max(minimum, (1, 4))
    if expanded is not None:
        entries["E"] = _validate_text_value(expanded, "expanded")
        minimum = max(minimum, (1, 5))
    return checked_role, resolved_page, entries, minimum


def _insert_element_in_raw(
    raw: list[object],
    slots: Sequence[int],
    index: int | None,
    elem: Dictionary,
) -> list[object]:
    """Insert ``elem`` into ``raw`` at child-element position ``index``."""
    at = _clamp_insert_index(index, len(slots))
    updated = list(raw)
    raw_pos = len(updated) if at >= len(slots) else slots[at]
    updated.insert(raw_pos, elem)
    return updated


def _reorder_elements_in_raw(raw: Sequence[object], slots: Sequence[int], source: int, target: int) -> list[object]:
    """Reorder child element ``source`` to ``target`` while keeping non-element ``/K`` items in place."""
    elements = [raw[pos] for pos in slots]
    moved = elements.pop(source)
    elements.insert(target, moved)
    updated = list(raw)
    for slot_pos, elem in zip(slots, elements, strict=True):
        updated[slot_pos] = elem
    return updated


@dataclass(frozen=True, slots=True, kw_only=True)
class MarkedContentReference:
    """A marked-content item of a structure element (``/K`` integer or ``/Type /MCR`` dictionary)."""

    mcid: int
    """The marked-content identifier (``/MCID``)."""
    page: int | None = None
    """The zero-based page index where the marked content appears, or ``None``."""
    stream_xref: int | None = None
    """The object number of ``/Stm`` when outside the page's content stream, or ``None``."""

    def __post_init__(self) -> None:
        """Validate ``mcid``, ``page``, and ``stream_xref``."""
        _validate_non_negative_int(self.mcid, "mcid")
        if self.page is not None:
            _validate_non_negative_int(self.page, "page")
        if self.stream_xref is not None:
            _validate_non_negative_int(self.stream_xref, "stream_xref")


@dataclass(frozen=True, slots=True, kw_only=True)
class ObjectReference:
    """An indirect PDF object item of a structure element (``/Type /OBJR`` dictionary)."""

    xref: int
    """The object number of the referenced PDF object (``/Obj``)."""
    page: int | None = None
    """The zero-based page index where the object is rendered, or ``None``."""

    def __post_init__(self) -> None:
        """Validate ``xref`` and ``page``."""
        _validate_non_negative_int(self.xref, "xref")
        if self.page is not None:
            _validate_non_negative_int(self.page, "page")


def _validate_role_map_entries(entries: Mapping[str, str], *, check_keys: Iterable[str] | None = None) -> None:
    """Refuse cycles and chains longer than ``_MAX_TREE_DEPTH`` in ``entries``."""
    starts = entries.keys() if check_keys is None else check_keys
    for start in starts:
        current = start
        visited = {start}
        hops = 0
        while current in entries:
            nxt = entries[current]
            hops += 1
            if nxt in visited:
                msg = f"role_map cannot contain a cycle involving {nxt!r}"
                raise ValueError(msg)
            if hops > _MAX_TREE_DEPTH:
                msg = f"role_map chain exceeds {_MAX_TREE_DEPTH} hops"
                raise LimitError(_CODE_STRUCTURE_TREE, msg)
            visited.add(nxt)
            current = nxt


def _read_role_map_entries(document: Document) -> dict[str, str]:
    root = _root_dict(document)
    if root is None:
        return {}
    role_map = root.get("RoleMap")
    if not isinstance(role_map, Dictionary) or isinstance(role_map, Stream):
        return {}
    entries: dict[str, str] = {}
    for key in role_map:
        val = role_map.get(key)
        if isinstance(val, Name):
            entries[str(key)] = str(val)
    return entries


def _delete_role_map(document: Document) -> None:
    root = _root_dict(document)
    if root is None or "RoleMap" not in root:
        return
    existing = root.get("RoleMap")
    rm_xref = existing.xref if isinstance(existing, Dictionary) and existing.is_indirect else None
    with document.transaction("role map"):
        del root["RoleMap"]
        if rm_xref is not None:
            _core.object_free_unreferenced(document._core(), [rm_xref])  # noqa: SLF001


def _replace_role_map(document: Document, mapping: Mapping[str, TagRole | str]) -> None:
    if not isinstance(mapping, Mapping):
        msg = f"role_map must be a mapping: {type(mapping).__name__}"
        raise TypeError(msg)
    checked = {
        _validate_role_name(key, label="role"): _validate_role_name(val, label="target role")
        for key, val in mapping.items()
    }
    _validate_role_map_entries(checked)
    if not checked:
        _delete_role_map(document)
        return
    entries = {key: Name(val) for key, val in checked.items()}
    with document.transaction("role map"):
        root = _ensure_root(document)
        existing = root.get("RoleMap")
        if isinstance(existing, Dictionary) and not isinstance(existing, Stream) and existing.is_indirect:
            assert existing.xref is not None  # noqa: S101
            document.objects[existing.xref] = entries
        else:
            root["RoleMap"] = entries


class RoleMap(MutableMapping[str, str]):
    """The structure tree's ``/RoleMap`` dictionary as a live mutable mapping.

    Keys are custom role names and values are the role names they map to
    (ISO 32000-2 Table 354). Each edit runs in one transaction, creating
    ``/StructTreeRoot`` and ``/RoleMap`` when absent, removing ``/RoleMap``
    when the last entry is deleted, and refusing role cycles.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap ``document``; use ``doc.structure.role_map``."""
        self._document = document

    def _entries(self) -> dict[str, str]:
        self._document._ensure_open()  # noqa: SLF001
        return _read_role_map_entries(self._document)

    def __getitem__(self, key: str) -> str:
        """Return the mapped role name for ``key``, or raise :class:`KeyError`."""
        if not isinstance(key, str):
            raise KeyError(key)
        return self._entries()[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the custom role names in ``/RoleMap``."""
        return iter(self._entries())

    def __len__(self) -> int:
        """Return the number of valid role mappings."""
        return len(self._entries())

    def __setitem__(self, key: TagRole | str, value: TagRole | str) -> None:
        """Map custom role ``key`` to ``value`` in one transaction."""
        self.update({key: value})

    def __delitem__(self, key: str) -> None:
        """Remove ``key`` from ``/RoleMap``, deleting ``/RoleMap`` if it becomes empty."""
        self._document._ensure_open()  # noqa: SLF001
        if not isinstance(key, str):
            raise KeyError(key)
        root = _root_dict(self._document)
        role_map = root.get("RoleMap") if root is not None else None
        if (
            root is None
            or not isinstance(role_map, Dictionary)
            or isinstance(role_map, Stream)
            or key not in role_map
            or not isinstance(role_map.get(key), Name)
        ):
            raise KeyError(key)
        with self._document.transaction("role map"):
            del role_map[key]
            if len(role_map) == 0:
                rm_xref = role_map.xref if role_map.is_indirect else None
                del root["RoleMap"]
                if rm_xref is not None:
                    _core.object_free_unreferenced(self._document._core(), [rm_xref])  # noqa: SLF001

    def update(self, other: object = (), /, **values: TagRole | str) -> None:
        """Apply every mapping of ``other`` and ``values`` in one transaction, or none on error."""
        self._document._ensure_open()  # noqa: SLF001
        merged = dict(cast("Mapping[object, object] | Iterable[tuple[object, object]]", other))
        merged.update(values)
        checked = {
            _validate_role_name(key, label="role"): _validate_role_name(val, label="target role")
            for key, val in merged.items()
        }
        if not checked:
            return
        combined = {**_read_role_map_entries(self._document), **checked}
        _validate_role_map_entries(combined, check_keys=checked.keys())
        with self._document.transaction("role map"):
            root = _ensure_root(self._document)
            existing = root.get("RoleMap")
            if isinstance(existing, Dictionary) and not isinstance(existing, Stream):
                for key, val in checked.items():
                    existing[key] = Name(val)
            else:
                root["RoleMap"] = {key: Name(val) for key, val in checked.items()}

    def clear(self) -> None:
        """Remove every entry by deleting ``/RoleMap`` in one transaction."""
        self._document._ensure_open()  # noqa: SLF001
        _delete_role_map(self._document)

    def __repr__(self) -> str:
        """Return ``RoleMap({...})`` with the active mappings."""
        return f"RoleMap({self._entries()!r})"


#: An item in a structure element's ``/K`` reading order.
StructureItem: TypeAlias = "StructureElement | MarkedContentReference | ObjectReference"


class StructureChildren(Sequence["StructureElement"]):
    """The child :class:`StructureElement` handles of one :class:`StructureElement` in ``/K`` order.

    Supports indexing, slicing, ``del``, :meth:`add`, and :meth:`move`. Non-element
    content items in ``/K`` (marked-content IDs, ``/MCR``, and ``/OBJR`` dictionaries)
    remain in place when child elements are added, reordered, or deleted.
    """

    __slots__ = ("_parent",)

    def __init__(self, parent: StructureElement) -> None:
        """Wrap ``parent``; use ``element.children``."""
        self._parent = parent

    def _items(self) -> list[StructureElement]:
        holder = self._parent._dict()  # noqa: SLF001
        return _child_elements(self._parent.document, holder)

    @overload
    def __getitem__(self, index: int) -> StructureElement: ...
    @overload
    def __getitem__(self, index: slice) -> list[StructureElement]: ...
    def __getitem__(self, index: int | slice) -> StructureElement | list[StructureElement]:
        """Return a child element, or a list of child elements for a slice."""
        items = self._items()
        if isinstance(index, slice):
            return items[index]
        return items[_resolve_sequence_index(index, len(items), "child index")]

    def __len__(self) -> int:
        """Return the number of child structure elements."""
        return len(self._items())

    def __iter__(self) -> Iterator[StructureElement]:
        """Iterate over the child structure elements in ``/K`` order."""
        return iter(self._items())

    def __delitem__(self, index: int | slice) -> None:
        """Remove one or more child elements, cleaning ``/ParentTree``, ``/IDTree``, and orphaned objects."""
        holder = self._parent._dict()  # noqa: SLF001
        raw = _raw_kids(holder)
        slots = _element_slots(raw)
        if isinstance(index, slice):
            selected = set(range(*index.indices(len(slots))))
        else:
            selected = {_resolve_sequence_index(index, len(slots), "child index")}
        _delete_children_at(self._parent.document, holder, selected, is_root=False)

    def add(  # noqa: PLR0913 - structure element creation options.
        self,
        role: TagRole | str,
        *,
        index: int | None = None,
        page: Page | int | None = None,
        title: str | None = None,
        language: str | None = None,
        alt: str | None = None,
        actual_text: str | None = None,
        expanded: str | None = None,
    ) -> StructureElement:
        """Create and insert a child :class:`StructureElement` in one transaction."""
        document = self._parent.document
        holder = self._parent._dict()  # noqa: SLF001
        parent_level = _element_level(document, holder)
        if parent_level + 1 > _MAX_STRUCTURE_DEPTH:
            raise _depth_limit_error()
        checked_role, resolved_page, entries, minimum = _validate_new_element_options(
            document,
            role,
            index=index,
            page=page,
            title=title,
            language=language,
            alt=alt,
            actual_text=actual_text,
            expanded=expanded,
        )
        raw = _raw_kids(holder)
        slots = _element_slots(raw)
        catalog = _catalog(document)
        candidates: list[int] = []
        with document.transaction("structure"):
            _require_version(document, catalog, minimum)
            value: dict[str, object] = {"Type": Name("StructElem"), "S": Name(checked_role), "P": holder}
            if resolved_page is not None:
                value["Pg"] = resolved_page.obj
            value.update(entries)
            xref = document.objects.add(value)
            node = cast("Dictionary", document.objects[xref])
            updated = _insert_element_in_raw(raw, slots, index, node)
            _write_kids(holder, updated, candidates, is_root=False)
            if candidates:
                _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001
        assert node.generation is not None  # noqa: S101
        return StructureElement(document, xref, node.generation)

    def move(self, index: int, to: int) -> None:
        """Move child element ``index`` to child-element position ``to`` in one transaction."""
        document = self._parent.document
        holder = self._parent._dict()  # noqa: SLF001
        raw = _raw_kids(holder)
        slots = _element_slots(raw)
        source = _resolve_sequence_index(index, len(slots), "child index")
        target = _resolve_sequence_index(to, len(slots), "child index")
        if source == target:
            return
        updated = _reorder_elements_in_raw(raw, slots, source, target)
        candidates: list[int] = []
        with document.transaction("structure"):
            _write_kids(holder, updated, candidates, is_root=False)
            if candidates:
                _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001

    def __repr__(self) -> str:
        """Return ``StructureChildren([...])``, or a stale marker when the parent is stale."""
        try:
            return f"StructureChildren({self._items()!r})"
        except StaleObjectError:
            return f"<stale StructureChildren parent_xref={self._parent.xref}>"


class StructureElement:
    """A handle on an indirect ``StructElem`` dictionary in ``Document.structure``.

    Properties read the current version of the document, and assigning or deleting
    ``role``, ``title``, ``language``, ``alt``, ``actual_text``, ``expanded``, or
    ``page`` updates the element in one transaction. A handle whose element was
    deleted raises :class:`StaleObjectError`, and works again if undo restores it.
    """

    __slots__ = ("_document", "_id")

    def __init__(self, document: Document, xref: int, generation: int = 0) -> None:
        """Name structure element ``xref`` of ``generation``; ``Document.structure`` creates handles."""
        self._document = document
        self._id = (xref, generation)

    def _dict(self) -> Dictionary:
        core = self._document._core()  # noqa: SLF001
        if _root_dict(self._document) is None:
            msg = f"structure element {self._id[0]} {self._id[1]} R is no longer in the structure tree"
            raise StaleObjectError(msg)
        found = _core.object_lookup(core, self._id[0])
        if found is None or found[0] != self._id[1] or not isinstance(found[1], tuple) or found[1][0] != "d":
            msg = f"structure element {self._id[0]} {self._id[1]} R does not exist in this version"
            raise StaleObjectError(msg)
        d = Dictionary(self._document, self._id)
        if not _is_struct_elem_dict(d):
            msg = f"object {self._id[0]} {self._id[1]} R is not a structure element"
            raise StaleObjectError(msg)
        return d

    @property
    def document(self) -> Document:
        """Return the document that owns this structure element."""
        return self._document

    @property
    def xref(self) -> int:
        """Return the element's indirect object number."""
        return self._id[0]

    @property
    def obj(self) -> Dictionary:
        """Return the element's dictionary in the low-level object model."""
        return self._dict()

    @property
    def role(self) -> str:
        """Return or set the element's structure type name (``/S``)."""
        value = self._dict().get("S")
        return str(value) if isinstance(value, Name) else ""

    @role.setter
    def role(self, value: TagRole | str) -> None:
        checked = _validate_role_name(value, label="role")
        d = self._dict()
        with self._document.transaction("structure role"):
            d["S"] = Name(checked)

    @property
    def standard_role(self) -> str:
        """Return ``role`` resolved iteratively through ``doc.structure.role_map``."""
        return self._document.structure.resolve_role(self.role)

    def _read_text(self, pdf_key: str) -> str | None:
        value = self._dict().get(pdf_key)
        return value.text if isinstance(value, String) else None

    def _write_text(self, attr_name: str, value: str) -> None:
        pdf_key, minimum = _TEXT_PROPERTIES[attr_name]
        checked = _validate_text_value(value, attr_name)
        d = self._dict()
        catalog = _catalog(self._document)
        with self._document.transaction(f"structure {attr_name}"):
            _require_version(self._document, catalog, minimum)
            d[pdf_key] = checked

    def _delete_key(self, pdf_key: str, step_name: str) -> None:
        d = self._dict()
        if pdf_key in d:
            with self._document.transaction(step_name):
                del d[pdf_key]

    @property
    def title(self) -> str | None:
        """Return or set the element's title (``/T``), or ``None`` when absent; ``del`` removes ``/T``."""
        return self._read_text("T")

    @title.setter
    def title(self, value: str) -> None:
        self._write_text("title", value)

    @title.deleter
    def title(self) -> None:
        self._delete_key("T", "structure title")

    @property
    def language(self) -> str | None:
        """Return or set the element's BCP 47 language tag (``/Lang``), or ``None``; ``del`` removes ``/Lang``."""
        return self._read_text("Lang")

    @language.setter
    def language(self, value: str) -> None:
        checked = _validate_language_tag(value)
        d = self._dict()
        catalog = _catalog(self._document)
        with self._document.transaction("structure language"):
            _require_version(self._document, catalog, (1, 4))
            d["Lang"] = checked

    @language.deleter
    def language(self) -> None:
        self._delete_key("Lang", "structure language")

    @property
    def alt(self) -> str | None:
        """Return or set the alternate description (``/Alt``), or ``None``; ``del`` removes ``/Alt``."""
        return self._read_text("Alt")

    @alt.setter
    def alt(self, value: str) -> None:
        self._write_text("alt", value)

    @alt.deleter
    def alt(self) -> None:
        self._delete_key("Alt", "structure alt")

    @property
    def actual_text(self) -> str | None:
        """Return or set the replacement text (``/ActualText``), or ``None``; ``del`` removes ``/ActualText``."""
        return self._read_text("ActualText")

    @actual_text.setter
    def actual_text(self, value: str) -> None:
        self._write_text("actual_text", value)

    @actual_text.deleter
    def actual_text(self) -> None:
        self._delete_key("ActualText", "structure actual_text")

    @property
    def expanded(self) -> str | None:
        """Return or set the abbreviation expansion (``/E``, ISO 32000-2 §14.9.5), or ``None``; ``del`` removes it."""
        return self._read_text("E")

    @expanded.setter
    def expanded(self, value: str) -> None:
        self._write_text("expanded", value)

    @expanded.deleter
    def expanded(self) -> None:
        self._delete_key("E", "structure expanded")

    @property
    def page(self) -> Page | None:
        """Return or set the element's page (``/Pg``), or ``None``; ``del`` removes ``/Pg``."""
        return _page_from_dict(self._document, self._dict().get("Pg"))

    @page.setter
    def page(self, value: Page | int) -> None:
        resolved = _resolve_page_arg(self._document, value)
        d = self._dict()
        with self._document.transaction("structure page"):
            d["Pg"] = resolved.obj

    @page.deleter
    def page(self) -> None:
        d = self._dict()
        if "Pg" not in d:
            return
        old_pg = d.get("Pg")
        valid_pg = old_pg if _page_from_dict(self._document, old_pg) is not None else None
        with self._document.transaction("structure page"):
            if valid_pg is not None:
                for item in _raw_kids(d):
                    if isinstance(item, Dictionary) and not isinstance(item, Stream):
                        kind = item.get("Type")
                        if isinstance(kind, Name) and str(kind) in {"MCR", "OBJR"} and "Pg" not in item:
                            item["Pg"] = valid_pg
            del d["Pg"]

    @property
    def parent(self) -> StructureElement | None:
        """Return the parent :class:`StructureElement`, or ``None`` for a top-level element."""
        parent = self._dict().get("P")
        root = _root_dict(self._document)
        if not isinstance(parent, Dictionary) or isinstance(parent, Stream) or not parent.is_indirect:
            return None
        if parent.xref is None or parent.generation is None:
            return None
        if root is not None and parent.xref == root.xref:
            return None
        if _is_struct_elem_dict(parent):
            return StructureElement(self._document, parent.xref, parent.generation)
        return None

    @property
    def children(self) -> StructureChildren:
        """Return a live sequence of the element's child :class:`StructureElement` handles."""
        self._dict()
        return StructureChildren(self)

    @property
    def items(self) -> tuple[StructureElement | MarkedContentReference | ObjectReference, ...]:
        """Return the child elements, marked-content items, and object references in ``/K`` reading order."""
        d = self._dict()
        default_page = _page_index_from_dict(self._document, d.get("Pg"))
        out: list[StructureItem] = []
        for item in _raw_kids(d):
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                out.append(MarkedContentReference(mcid=item, page=default_page, stream_xref=None))
                continue
            if not isinstance(item, Dictionary) or isinstance(item, Stream):
                continue
            kind = item.get("Type")
            if isinstance(kind, Name) and str(kind) == "MCR":
                mcid = item.get("MCID")
                if isinstance(mcid, int) and not isinstance(mcid, bool) and mcid >= 0:
                    pg = _page_index_from_dict(self._document, item.get("Pg")) if "Pg" in item else default_page
                    stm = item.get("Stm")
                    stm_xref = stm.xref if isinstance(stm, Dictionary) and stm.is_indirect else None
                    out.append(MarkedContentReference(mcid=mcid, page=pg, stream_xref=stm_xref))
            elif isinstance(kind, Name) and str(kind) == "OBJR":
                target = item.get("Obj")
                if isinstance(target, _Handle) and target.is_indirect and target.xref is not None:
                    pg = _page_index_from_dict(self._document, item.get("Pg")) if "Pg" in item else default_page
                    out.append(ObjectReference(xref=target.xref, page=pg))
            elif _is_struct_elem_dict(item):
                assert item.xref is not None  # noqa: S101
                assert item.generation is not None  # noqa: S101
                out.append(StructureElement(self._document, item.xref, item.generation))
        return tuple(out)

    def _resolve_move_destination(
        self,
        parent: StructureElement | None,
    ) -> tuple[Dictionary, int, int | None, bool]:
        if parent is None:
            target_holder = _root_dict(self._document)
            if target_holder is None or not target_holder.is_indirect:
                msg = "structure tree has no indirect root"
                raise StaleObjectError(msg)
            return target_holder, 1, None, True
        if not isinstance(parent, StructureElement):
            msg = f"parent must be a StructureElement or None: {parent!r}"
            raise TypeError(msg)
        if parent.document is not self._document:
            msg = "parent belongs to another document"
            raise ValueError(msg)
        target_holder = parent._dict()  # noqa: SLF001
        if parent.xref == self.xref:
            msg = "cannot move a structure element into itself"
            raise ValueError(msg)
        parent_level = _element_level(self._document, target_holder, forbid_xref=self.xref)
        return target_holder, parent_level, parent.xref, False

    def _move_within_same_holder(self, holder: Dictionary, index: int | None, *, is_root: bool) -> bool:
        raw = _raw_kids(holder)
        slots = _element_slots(raw)
        matches = [pos for pos, raw_idx in enumerate(slots) if cast("Dictionary", raw[raw_idx]).xref == self.xref]
        if not matches:
            return False
        source = matches[0]
        count = len(slots)
        target = count - 1 if index is None else (max(0, index + count) if index < 0 else min(index, count - 1))
        if source != target:
            updated = _reorder_elements_in_raw(raw, slots, source, target)
            candidates: list[int] = []
            with self._document.transaction("structure move"):
                _write_kids(holder, updated, candidates, is_root=is_root)
                if candidates:
                    _core.object_free_unreferenced(self._document._core(), candidates)  # noqa: SLF001
        return True

    def move(self, *, parent: StructureElement | None = None, index: int | None = None) -> None:
        """Move this element under ``parent`` (``None`` for ``/StructTreeRoot``) at child position ``index``.

        Updates both ``/K`` containers and this element's ``/P`` in one transaction,
        refusing moves into another document, into itself or a descendant, or past
        the 256-level depth limit.
        """
        elem_dict = self._dict()
        if index is not None and not _is_non_bool_int(index):
            msg = f"index must be an int or None: {index!r}"
            raise TypeError(msg)
        target_holder, parent_level, forbid_xref, target_is_root = self._resolve_move_destination(parent)
        _check_subtree_move(elem_dict, parent_level=parent_level, forbid_xref=forbid_xref)
        old_parent = elem_dict.get("P")
        old_holder = (
            old_parent
            if isinstance(old_parent, Dictionary) and not isinstance(old_parent, Stream) and old_parent.is_indirect
            else None
        )
        if (
            old_holder is not None
            and old_holder.xref == target_holder.xref
            and self._move_within_same_holder(target_holder, index, is_root=target_is_root)
        ):
            return
        root = _root_dict(self._document)
        old_is_root = root is not None and old_holder is not None and old_holder.xref == root.xref
        candidates: list[int] = []
        with self._document.transaction("structure move"):
            if old_holder is not None:
                old_raw = _raw_kids(old_holder)
                kept_old = [
                    item
                    for item in old_raw
                    if not (_is_struct_elem_dict(item) and cast("Dictionary", item).xref == self.xref)
                ]
                _write_kids(old_holder, kept_old, candidates, is_root=old_is_root)
            new_raw = _raw_kids(target_holder)
            new_slots = _element_slots(new_raw)
            updated_new = _insert_element_in_raw(new_raw, new_slots, index, elem_dict)
            _write_kids(target_holder, updated_new, candidates, is_root=target_is_root)
            elem_dict["P"] = target_holder
            if candidates:
                _core.object_free_unreferenced(self._document._core(), candidates)  # noqa: SLF001

    def __eq__(self, other: object) -> bool:
        """Compare document and object identity."""
        if not isinstance(other, StructureElement):
            return NotImplemented
        return (self._document, self._id) == (other._document, other._id)

    def __hash__(self) -> int:
        """Hash document and object identity."""
        return hash((id(self._document), self._id))

    def __repr__(self) -> str:
        """Return ``StructureElement(xref=..., role=...)``, or a stale marker."""
        try:
            return f"StructureElement(xref={self._id[0]}, role={self.role!r})"
        except StaleObjectError:
            return f"<stale StructureElement xref={self._id[0]}>"


class StructureTree(Sequence[StructureElement]):
    """The document's logical structure tree as its top-level elements, as ``doc.structure`` returns it.

    Each element's :attr:`~StructureElement.children` and :attr:`~StructureElement.items`
    expose the hierarchy and marked-content items, :meth:`walk` visits every element
    in pre-order, and :attr:`role_map` reads and edits ``/RoleMap``. ``del doc.structure``
    removes ``/StructTreeRoot`` and frees unreferenced structure objects.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap ``document``; use ``Document.structure``."""
        self._document = document

    def _items(self) -> list[StructureElement]:
        self._document._ensure_open()  # noqa: SLF001
        root = _root_dict(self._document)
        if root is None:
            return []
        return _child_elements(self._document, root)

    @property
    def obj(self) -> Dictionary | None:
        """Return the ``/StructTreeRoot`` dictionary, or ``None`` when the document is untagged."""
        self._document._ensure_open()  # noqa: SLF001
        return _root_dict(self._document)

    @property
    def role_map(self) -> RoleMap:
        """Return or replace ``/RoleMap`` as a live :class:`RoleMap`; ``del`` removes ``/RoleMap``."""
        self._document._ensure_open()  # noqa: SLF001
        return RoleMap(self._document)

    @role_map.setter
    def role_map(self, mapping: Mapping[str, TagRole | str]) -> None:
        self._document._ensure_open()  # noqa: SLF001
        _replace_role_map(self._document, mapping)

    @role_map.deleter
    def role_map(self) -> None:
        self._document._ensure_open()  # noqa: SLF001
        _delete_role_map(self._document)

    def resolve_role(self, role: TagRole | str) -> str:
        """Resolve ``role`` iteratively through ``/RoleMap`` (up to 32 hops, stopping on cycles)."""
        self._document._ensure_open()  # noqa: SLF001
        if not isinstance(role, str):
            msg = f"role must be a str or TagRole: {role!r}"
            raise TypeError(msg)
        current = role.value if isinstance(role, TagRole) else str(role)
        entries = _read_role_map_entries(self._document)
        visited = {current}
        for _ in range(_MAX_TREE_DEPTH):
            nxt = entries.get(current)
            if nxt is None or nxt in visited:
                return current
            visited.add(nxt)
            current = nxt
        nxt = entries.get(current)
        if nxt is not None and nxt not in visited:
            msg = f"role_map chain exceeds {_MAX_TREE_DEPTH} hops"
            raise LimitError(_CODE_STRUCTURE_TREE, msg)
        return current

    @overload
    def __getitem__(self, index: int) -> StructureElement: ...
    @overload
    def __getitem__(self, index: slice) -> list[StructureElement]: ...
    def __getitem__(self, index: int | slice) -> StructureElement | list[StructureElement]:
        """Return a top-level element, or a list of top-level elements for a slice."""
        items = self._items()
        if isinstance(index, slice):
            return items[index]
        return items[_resolve_sequence_index(index, len(items), "structure index")]

    def __len__(self) -> int:
        """Return the number of top-level structure elements."""
        return len(self._items())

    def __iter__(self) -> Iterator[StructureElement]:
        """Iterate over the top-level structure elements in ``/K`` order."""
        return iter(self._items())

    def __delitem__(self, index: int | slice) -> None:
        """Remove one or more top-level elements, cleaning ``/ParentTree``, ``/IDTree``, and orphaned objects."""
        self._document._ensure_open()  # noqa: SLF001
        root = _root_dict(self._document)
        slots = _element_slots(_raw_kids(root)) if root is not None else []
        if isinstance(index, slice):
            selected = set(range(*index.indices(len(slots))))
        else:
            selected = {_resolve_sequence_index(index, len(slots), "structure index")}
        if root is not None and selected:
            _delete_children_at(self._document, root, selected, is_root=True)

    def move(self, index: int, to: int) -> None:
        """Move top-level element ``index`` to position ``to`` in one transaction."""
        self._document._ensure_open()  # noqa: SLF001
        root = _root_dict(self._document)
        raw = _raw_kids(root) if root is not None else []
        slots = _element_slots(raw)
        source = _resolve_sequence_index(index, len(slots), "structure index")
        target = _resolve_sequence_index(to, len(slots), "structure index")
        if source == target or root is None:
            return
        updated = _reorder_elements_in_raw(raw, slots, source, target)
        candidates: list[int] = []
        with self._document.transaction("structure"):
            _write_kids(root, updated, candidates, is_root=True)
            if candidates:
                _core.object_free_unreferenced(self._document._core(), candidates)  # noqa: SLF001

    def add(  # noqa: PLR0913 - structure element creation options.
        self,
        role: TagRole | str,
        *,
        index: int | None = None,
        page: Page | int | None = None,
        title: str | None = None,
        language: str | None = None,
        alt: str | None = None,
        actual_text: str | None = None,
        expanded: str | None = None,
    ) -> StructureElement:
        """Create and insert a top-level :class:`StructureElement` in one transaction.

        Creates ``/StructTreeRoot`` and sets ``MarkInfo/Marked`` to ``True`` when
        the document has no structure tree yet.
        """
        self._document._ensure_open()  # noqa: SLF001
        checked_role, resolved_page, entries, minimum = _validate_new_element_options(
            self._document,
            role,
            index=index,
            page=page,
            title=title,
            language=language,
            alt=alt,
            actual_text=actual_text,
            expanded=expanded,
        )
        catalog = _catalog(self._document)
        candidates: list[int] = []
        with self._document.transaction("structure"):
            root = _ensure_root(self._document)
            _require_version(self._document, catalog, minimum)
            raw = _raw_kids(root)
            slots = _element_slots(raw)
            value: dict[str, object] = {"Type": Name("StructElem"), "S": Name(checked_role), "P": root}
            if resolved_page is not None:
                value["Pg"] = resolved_page.obj
            value.update(entries)
            xref = self._document.objects.add(value)
            node = cast("Dictionary", self._document.objects[xref])
            updated = _insert_element_in_raw(raw, slots, index, node)
            _write_kids(root, updated, candidates, is_root=True)
            if candidates:
                _core.object_free_unreferenced(self._document._core(), candidates)  # noqa: SLF001
        assert node.generation is not None  # noqa: S101
        return StructureElement(self._document, xref, node.generation)

    def walk(self, *, max_elements: int | None = _DEFAULT_MAX_ELEMENTS) -> Iterator[StructureElement]:
        """Iterate over every :class:`StructureElement` in pre-order, bounded and without recursion.

        Raises :class:`LimitError` with code ``"structure_tree_count"`` past 256
        levels (counting ``/StructTreeRoot``) or ``"object_count"`` past
        ``max_elements`` (``None`` lifts the element cap), returning no partial result.
        """
        _validate_optional_positive_int("max_elements", max_elements)
        self._document._ensure_open()  # noqa: SLF001

        def read(operation: _core._OperationBudget) -> list[StructureElement]:
            root = _root_dict(self._document)
            if root is None:
                return []
            seen = {root.xref} if root.xref is not None else set()
            result: list[StructureElement] = []

            # Keep only the path to the next child on the stack. A wide /K array
            # is read one item at a time, including when max_elements is small.
            def kids(holder: Dictionary) -> Iterator[object]:
                value = holder.get("K")
                return iter(value) if isinstance(value, Array) else iter((value,))

            end = object()
            stack = [kids(root)]
            while stack:
                item = next(stack[-1], end)
                if item is end:
                    stack.pop()
                    continue
                if not _is_struct_elem_dict(item):
                    continue
                child = cast("Dictionary", item)
                xref = child.xref
                assert xref is not None  # noqa: S101
                assert child.generation is not None  # noqa: S101
                if xref in seen:
                    continue
                if len(stack) + 1 > _MAX_STRUCTURE_DEPTH:
                    raise _depth_limit_error()
                if max_elements is not None and len(result) >= max_elements:
                    msg = f"structure tree exceeds max_elements={max_elements}"
                    raise LimitError(_CODE_OBJECT_COUNT, msg)
                # Account for the returned handle, visited set and traversal
                # frame before retaining another element.
                operation.charge("memory_size", _WALK_ELEMENT_BYTES)
                seen.add(xref)
                result.append(StructureElement(self._document, xref, child.generation))
                stack.append(kids(child))
            return result

        return iter(_core.read_operation(self._document._core(), read))  # noqa: SLF001

    def __repr__(self) -> str:
        """Return ``StructureTree([...])`` with the top-level elements."""
        return f"StructureTree({self._items()!r})"


def _delete_structure_tree(document: Document) -> None:
    """Remove ``/StructTreeRoot`` from ``document`` and free unreferenced structure objects."""
    catalog = _catalog(document)
    if "StructTreeRoot" not in catalog:
        return
    existing = catalog.get("StructTreeRoot")
    candidates: list[int] = []
    if isinstance(existing, Dictionary) and not isinstance(existing, Stream):
        if existing.is_indirect and existing.xref is not None:
            candidates.append(existing.xref)
        top_dicts = [cast("Dictionary", item) for item in _raw_kids(existing) if _is_struct_elem_dict(item)]
        candidates.extend(sorted(_collect_subtree_xrefs(top_dicts)))
    with document.transaction("structure"):
        del catalog["StructTreeRoot"]
        if candidates:
            _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001


for _public in (
    MarkedContentReference,
    ObjectReference,
    RoleMap,
    StructureChildren,
    StructureElement,
    StructureTree,
):
    _public.__module__ = "pyropdf"

__all__ = [
    "MarkedContentReference",
    "ObjectReference",
    "RoleMap",
    "StructureChildren",
    "StructureElement",
    "StructureTree",
]
