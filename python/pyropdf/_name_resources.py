"""The catalog's resource name trees (ISO 32000-2, 7.7.4 and 7.9.6)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._attachments import _version
from pyropdf._errors import MalformedPdfError
from pyropdf._metadata import _require_version
from pyropdf._objects import Array, Dictionary, Name, Stream, String, _catalog, _unlocked

if TYPE_CHECKING:
    from pyropdf._document import Document

# The other catalog trees have dedicated public writers.
_RESOURCE_VERSIONS = {
    "Dests": (1, 2),
    "AP": (1, 3),
    "Templates": (1, 3),
    "IDS": (1, 3),
    "URLS": (1, 3),
    "AlternatePresentations": (1, 4),
    "Renditions": (1, 5),
}
_DEPRECATED = frozenset({"IDS", "URLS", "AlternatePresentations"})
_LEAF_SIZE = 64


@dataclass(slots=True)
class _Node:
    obj: Dictionary
    parent: _Node | None
    index: int
    first: bytes | None = None
    last: bytes | None = None


def _read_leaf(
    node: _Node,
    names: Array,
    pairs: dict[bytes, tuple[_Node, int]],
    previous: bytes | None,
    budget: _core._OperationBudget,
) -> bytes | None:
    budget.check("named_destination_count", len(pairs) + len(names) // 2)
    budget.charge("memory_size", 128 * (len(names) // 2))
    for index in range(0, len(names), 2):
        key = names[index]
        if not isinstance(key, String) or (previous is not None and key <= previous):
            msg = "catalog name tree keys must be unique byte strings in ascending order"
            raise MalformedPdfError(msg)
        budget.charge("named_destination_text_size", len(key))
        previous = bytes(key)
        if node.first is None:
            node.first = previous
        node.last = previous
        pairs[previous] = (node, index)
    return previous


def _scan(root: Dictionary, budget: _core._OperationBudget) -> tuple[dict[bytes, tuple[_Node, int]], list[_Node]]:
    pairs: dict[bytes, tuple[_Node, int]] = {}
    nodes: list[_Node] = []
    stack = [(_Node(root, None, 0), 1)]
    seen: set[int] = set()
    previous: bytes | None = None
    while stack:
        node, depth = stack.pop()
        budget.poll()
        budget.check("named_destination_count", len(nodes) + 1)
        budget.check("named_destination_depth", depth)
        obj = node.obj
        if obj.xref is not None:
            if obj.xref in seen:
                msg = "the catalog name tree contains a cycle or repeated node"
                raise MalformedPdfError(msg)
            seen.add(obj.xref)
        budget.charge("memory_size", 128 * (1 + len(obj)))
        nodes.append(node)
        names, kids = obj.get("Names"), obj.get("Kids")
        if isinstance(names, Array) and kids is None and len(names) % 2 == 0:
            previous = _read_leaf(node, names, pairs, previous, budget)
        elif isinstance(kids, Array) and names is None:
            budget.charge("named_destination_edge_count", len(kids))
            budget.charge("memory_size", 128 * len(kids))
            for index in reversed(range(len(kids))):
                child = kids[index]
                if not isinstance(child, Dictionary) or isinstance(child, Stream) or not child.is_indirect:
                    msg = "a catalog name tree child must be an indirect dictionary"
                    raise MalformedPdfError(msg)
                stack.append((_Node(child, node, index), depth + 1))
        else:
            msg = "a catalog name tree node needs either key/value pairs or children"
            raise MalformedPdfError(msg)
    _check_ranges(nodes)
    return pairs, nodes


def _check_ranges(nodes: list[_Node]) -> None:
    for node in reversed(nodes):
        parent = node.parent
        if parent is None:
            continue
        limits = node.obj.get("Limits")
        if (
            node.first is None
            or node.last is None
            or not isinstance(limits, Array)
            or len(limits) != len(("lower", "upper"))
            or not all(isinstance(v, String) for v in limits)
            or (bytes(limits[0]), bytes(limits[1])) != (node.first, node.last)
        ):
            msg = "a catalog name tree child's Limits must match its first and last byte-string keys"
            raise MalformedPdfError(msg)
        parent.last = node.last if parent.last is None else parent.last
        parent.first = node.first


def _resource_key(key: object) -> bytes:
    if not isinstance(key, bytes):
        msg = "catalog resource keys must be PDF byte strings"
        raise TypeError(msg)
    return bytes(key)


def _check_resource_owner(document: Document, value: Dictionary | Array) -> None:
    if value._document is not document:  # noqa: SLF001 - a resource must belong to this store.
        msg = "a catalog resource from another document cannot be written"
        raise ValueError(msg)


def _is_capture_set(document: Document, value: object) -> bool:
    if not isinstance(value, Dictionary) or isinstance(value, Stream):
        return False
    _check_resource_owner(document, value)
    return value.get("Type", Name("SpiderContentSet")) == Name("SpiderContentSet") and value.get("S") in (
        Name("SPS"),
        Name("SIS"),
    )


def _capture_value(document: Document, value: object, budget: _core._OperationBudget) -> bool:
    if not isinstance(value, (Array, list, tuple)):
        return isinstance(value, Dictionary) and value.is_indirect and _is_capture_set(document, value)
    if isinstance(value, Array):
        _check_resource_owner(document, value)
    count = len(value)
    budget.charge("named_destination_edge_count", count)
    budget.charge("memory_size", 128 * count)
    for member in value:
        budget.poll()
        if not _is_capture_set(document, member):
            return False
    return True


def _edited_leaves(node: _Node, key: bytes, value: object, *, remove: bool) -> list[dict[str, object]]:
    names = node.obj.get("Names")
    # An empty root, including /Kids [], can receive its first leaf.
    items = {bytes(names[i]): names[i + 1] for i in range(0, len(names), 2)} if isinstance(names, Array) else {}
    if remove:
        del items[key]
    else:
        items[key] = value
    entries = sorted(items.items())
    replacements = []
    for start in range(0, len(entries), _LEAF_SIZE):
        part = entries[start : start + _LEAF_SIZE]
        leaf = dict(node.obj)
        leaf.pop("Kids", None)
        leaf.update(Names=[item for entry in part for item in entry], Limits=[part[0][0], part[-1][0]])
        replacements.append(leaf)
    return replacements


def _replace_path(
    doc: Document, node: _Node, replacements: list[dict[str, object]]
) -> tuple[Dictionary | None, list[int]]:
    retired = []
    while node.parent is not None:
        if node.obj.xref is not None:
            retired.append(node.obj.xref)
        parent = node.parent
        kids = list(parent.obj["Kids"])
        kids[node.index : node.index + 1] = [doc.objects[doc.objects.add(rep)] for rep in replacements]
        replacements = []
        if kids:
            updated = dict(parent.obj)
            low, high = kids[0].get("Limits"), kids[-1].get("Limits")
            if not isinstance(low, Array) or not len(low) or not isinstance(high, Array) or len(high) != 2:  # noqa: PLR2004 - inclusive lower and upper bounds.
                msg = "name tree children must declare key limits"
                raise MalformedPdfError(msg)
            updated.update(Kids=kids, Limits=[low[0], high[1]])
            replacements.append(updated)
        node = parent
    if node.obj.xref is not None:
        retired.append(node.obj.xref)
    if not replacements:
        return None, retired
    if len(replacements) == 1:
        root = replacements[0]
        root.pop("Limits", None)
    else:
        root = dict(node.obj)
        root.pop("Names", None)
        root.pop("Limits", None)
        root["Kids"] = [doc.objects[doc.objects.add(rep)] for rep in replacements]
    return doc.objects[doc.objects.add(root)], retired


def _publish(doc: Document, owner: Dictionary, kind: str, tree: Dictionary | None, retired: list[int]) -> None:
    catalog = _catalog(doc)
    # A direct name dictionary belongs only to this catalog. Detach an
    # indirect one so other owners retain the original names and lexemes.
    replacement = doc.objects[doc.objects.add(owner)] if owner.is_indirect else owner
    if tree is None:
        del replacement[kind]
    else:
        replacement[kind] = tree
    if owner.xref is not None:
        retired.append(owner.xref)
    if not replacement:
        del catalog["Names"]
        if replacement.xref is not None:
            retired.append(replacement.xref)
    elif owner.is_indirect:
        catalog["Names"] = replacement
    if retired:
        _core.object_free_nominated(_unlocked(doc), retired)


class NamedResources(Mapping[bytes, object]):
    """Live byte-string keys and PDF objects in one catalog resource name tree.

    Use :meth:`Document.named_resources` with ``AP``, ``Templates``, ``IDS``,
    ``URLS``, ``AlternatePresentations``, ``Renditions`` or ``Dests``.
    Values use the document's object model. An ``IDS`` or ``URLS`` value
    can also be an array of Web Capture page and image sets. Reading a target
    has no viewer side effects. Malformed or cyclic trees raise :class:`MalformedPdfError`.
    """

    __slots__ = ("_document", "_kind")

    def __init__(self, document: Document, kind: str) -> None:
        """Select an ISO catalog resource tree by its PDF key."""
        if kind not in _RESOURCE_VERSIONS:
            msg = f"unsupported resource name tree: {kind!r}"
            raise ValueError(msg)
        self._document, self._kind = document, kind

    def _root(self) -> Dictionary | None:
        names = _catalog(self._document).get("Names")
        if names is None:
            return None
        if not isinstance(names, Dictionary) or isinstance(names, Stream):
            msg = "the catalog Names entry must be a dictionary"
            raise MalformedPdfError(msg)
        root = names.get(self._kind)
        if root is not None and (not isinstance(root, Dictionary) or isinstance(root, Stream)):
            msg = "the catalog name tree root must be a dictionary"
            raise MalformedPdfError(msg)
        return root

    def _entries(self, budget: _core._OperationBudget) -> tuple[dict[bytes, tuple[_Node, int]], list[_Node]]:
        root = self._root()
        return ({}, []) if root is None else _scan(root, budget)

    def __iter__(self) -> Iterator[bytes]:
        """Return the raw keys in PDF byte order."""
        return iter(_core.read_operation(_unlocked(self._document), lambda budget: list(self._entries(budget)[0])))

    def __len__(self) -> int:
        """Return the number of entries."""
        return _core.read_operation(_unlocked(self._document), lambda budget: len(self._entries(budget)[0]))

    def __getitem__(self, key: bytes) -> object:
        """Return the referenced PDF object; a missing key raises :class:`KeyError`."""
        raw = _resource_key(key)

        def read(budget: _core._OperationBudget) -> object:
            node, index = self._entries(budget)[0][raw]
            return node.obj["Names"][index + 1]

        return _core.read_operation(_unlocked(self._document), read)

    def __setitem__(self, key: bytes, value: object) -> None:
        """Insert or replace one resource, preserving the other names and trees."""
        self._edit(_resource_key(key), value, remove=False)

    def __delitem__(self, key: bytes) -> None:
        """Remove a resource name while retaining the resource object for other owners."""
        self._edit(_resource_key(key), None, remove=True)

    def _edit(self, key: bytes, value: object, *, remove: bool) -> None:
        doc, kind = self._document, self._kind
        with doc.transaction("edit catalog named resource"):

            def prepare(budget: _core._OperationBudget) -> tuple[dict[bytes, tuple[_Node, int]], list[_Node]]:
                budget.check("named_destination_text_size", len(key))
                budget.charge("memory_size", len(key))
                indexed, tree_nodes = self._entries(budget)
                if not remove and key not in indexed:
                    budget.check("named_destination_count", len(indexed) + 1)
                    budget.charge("named_destination_text_size", len(key))
                if not remove:
                    self._validate_value(value, budget)
                return indexed, tree_nodes

            pairs, nodes = _core.read_operation(_unlocked(doc), prepare)
            existing = pairs.get(key)
            if remove and existing is None:
                raise KeyError(key)
            catalog = _catalog(doc)
            owner = catalog.get("Names")
            if not remove:
                if existing is None and kind in _DEPRECATED and _version(doc) >= (2, 0):
                    msg = f"new {kind} resource entries are deprecated in PDF 2.0"
                    raise ValueError(msg)
                _require_version(doc, catalog, _RESOURCE_VERSIONS[kind])
            if not nodes:
                tree = doc.objects[doc.objects.add({"Names": [key, value]})]
                if owner is None:
                    catalog["Names"] = {kind: tree}
                else:
                    _publish(doc, owner, kind, tree, [])
                return
            node = (
                existing[0]
                if existing
                else next(
                    (n for n in nodes if n.last is not None and n.last >= key and "Names" in n.obj),
                    nodes[-1],
                )
            )
            replacements = _edited_leaves(node, key, value, remove=remove)
            tree, retired = _replace_path(doc, node, replacements)
            if not isinstance(owner, Dictionary):
                msg = "the catalog resource tree has no name dictionary"
                raise MalformedPdfError(msg)
            _publish(doc, owner, kind, tree, retired)

    def _validate_value(self, value: object, budget: _core._OperationBudget) -> None:
        if self._kind == "Dests":
            if isinstance(value, (Dictionary, Array, dict, list, tuple)):
                return
        elif self._kind in {"IDS", "URLS"}:
            if _capture_value(self._document, value, budget):
                return
        elif isinstance(value, Dictionary) and value.is_indirect:
            if self._kind == "AP" and isinstance(value, Stream) and value.get("Subtype") == Name("Form"):
                return
            if self._kind == "Templates" and value.get("Type") == Name("Page"):
                return
            if self._kind == "Renditions" and value.get("S") in (Name("MR"), Name("SR")):
                return
            if self._kind == "AlternatePresentations" and value.get("Type") == Name("SlideShow"):
                return
        msg = f"invalid resource object for the {self._kind} name tree"
        raise ValueError(msg)


NamedResources.__module__ = "pyropdf"
