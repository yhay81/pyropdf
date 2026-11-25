"""Structural ICC and recipient-hierarchy rules of single-file PDF/X-4 and PDF/VT-1."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._objects import Array, Dictionary, Name, Stream

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyropdf._document import Document

_COMPONENTS = {b"GRAY": 1, b"RGB ": 3, b"CMYK": 4}
_ICC_HEADER_SIZE = 128
_CHILDREN_PER_CHUNK = 8192
# Upper bound for one visited (object number, generation) key and its share
# of the set, plus a live iterator frame for each internal node on the path.
_NODE_BYTES = 1024
# XML 1.0 (Fifth Edition), productions [4], [4a] and [7] for an Nmtoken.
_NMTOKEN = re.compile(
    r"[:A-Z_a-z0-9.\-\u00b7\u0300-\u036f\u203f-\u2040\u00c0-\u00d6\u00d8-\u00f6"
    r"\u00f8-\u02ff\u0370-\u037d\u037f-\u1fff\u200c-\u200d\u2070-\u218f\u2c00-\u2fef"
    r"\u3001-\ud7ff\uf900-\ufdcf\ufdf0-\ufffd\U00010000-\U000effff]+"
)


def icc_problem(stream: Stream) -> tuple[str, str] | None:
    """Return the bad ICC entry and reason, retaining stream and document decode budgets."""
    try:
        data = stream.read()
    except _core.MalformedPdfError:
        return "", "The embedded ICC output profile cannot be decoded"
    if len(data) < _ICC_HEADER_SIZE or data[36:40] != b"acsp":
        return "", "The embedded ICC output profile needs a complete header with the acsp signature"
    if data[12:16] != b"prtr":
        return "", "The embedded ICC profile must have output-device class prtr"
    channels = _COMPONENTS.get(data[16:20])
    if channels is None:
        return "", "The characterized printing condition must be Gray, RGB, or CMYK"
    count = stream.get("N")
    if type(count) is not int or count != channels:
        return "N", "The ICC stream component count must match its device colour model"
    return None


class _InvalidHierarchyError(Exception):
    """Stop on the first invalid hierarchy entry and retain its exact address."""

    def __init__(self, obj: Dictionary, path: str, message: str) -> None:
        self.xref = obj.xref
        self.path = path
        super().__init__(message)


def _children(owner: Dictionary, chunks: object) -> Iterator[tuple[str, object]]:
    """Yield one child at a time, checking each group before indexing its members."""
    if not isinstance(chunks, Array) or not chunks:
        raise _InvalidHierarchyError(owner, "DParts", "A part with children requires a nonempty array of child arrays")
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, Array):
            raise _InvalidHierarchyError(owner, "DParts", f"Child group {index} must be an array")
        minimum = 1 if index == len(chunks) - 1 else _CHILDREN_PER_CHUNK
        if not minimum <= len(chunk) <= _CHILDREN_PER_CHUNK:
            raise _InvalidHierarchyError(
                owner, f"DParts/{index}", "Only the final group can have fewer than 8192 children"
            )
        for offset, child in enumerate(chunk):
            yield f"DParts/{index}/{offset}", child


def _page_index(document: Document, obj: object) -> int | None:
    """Read an indirect page boundary through the store's existing page index."""
    if not isinstance(obj, Dictionary) or obj.xref is None or obj.generation is None:
        return None
    return _core.page_index(document._core(), obj.xref, obj.generation)  # noqa: SLF001


def _leaf_end(document: Document, node: Dictionary, expected: int, budget: _core._OperationBudget) -> int:
    """Check this leaf's interval and consume its pages in tree order."""
    start = _page_index(document, node.get("Start"))
    if start is None:
        raise _InvalidHierarchyError(node, "Start", "A leaf part must start at an indirect page of this document")
    if start != expected:
        raise _InvalidHierarchyError(node, "Start", "Leaf ranges must cover the pages exactly once in page-tree order")
    end = start
    if "End" in node:
        last = _page_index(document, node["End"])
        if last is None or last <= start:
            raise _InvalidHierarchyError(
                node, "End", "End must name a later page and is omitted for a single-page leaf"
            )
        end = last
    for index in range(start, end + 1):
        budget.poll()
        page = document.pages[index].obj
        if page.get("DPart") != node:
            raise _InvalidHierarchyError(page, "DPart", "Each page must refer to its own reachable leaf document part")
    return end + 1


def _root_levels(root: Dictionary, depth: int, budget: _core._OperationBudget) -> None:
    names = root.get("NodeNameList")
    if not isinstance(names, Array) or len(names) != depth:
        raise _InvalidHierarchyError(root, "NodeNameList", "The root must name each document-part level")
    for index, name in enumerate(names):
        budget.poll()
        if not isinstance(name, Name) or _NMTOKEN.fullmatch(name) is None:
            raise _InvalidHierarchyError(root, f"NodeNameList/{index}", "Each level must have an XML NMTOKEN name")
    if "RecordLevel" in root:
        level = root["RecordLevel"]
        if type(level) is not int or not 0 <= level < depth:
            raise _InvalidHierarchyError(
                root, "RecordLevel", "The optional zero-based record level must exist in the tree"
            )


def _child(parent: Dictionary, path: str, item: object) -> Dictionary:
    """Resolve an indirect and correctly parented document-part dictionary."""
    if not isinstance(item, Dictionary) or isinstance(item, Stream) or item.xref is None:
        raise _InvalidHierarchyError(parent, path, "A hierarchy entry must refer to an indirect DPart dictionary")
    if item.get("Parent") != parent:
        raise _InvalidHierarchyError(item, "Parent", "The part must refer to its immediate parent")
    if "Type" in item and item["Type"] != Name("DPart"):
        raise _InvalidHierarchyError(item, "Type", "A document part with a Type entry must use /DPart")
    return item


def _walk(document: Document, root: Dictionary, budget: _core._OperationBudget) -> None:
    """Use ordered, iterative traversal and the page index without expanding child groups or page ranges."""
    if "Type" in root and root["Type"] != Name("DPartRoot"):
        raise _InvalidHierarchyError(root, "Type", "A document part root with a Type entry must use /DPartRoot")
    stack: list[tuple[Dictionary, Iterator[tuple[str, object]]]] = [
        (root, iter((("DPartRootNode", root.get("DPartRootNode")),)))
    ]
    seen = set()
    depth = covered = 0
    count = len(document.pages)
    while stack:
        budget.poll()
        parent, cursor = stack[-1]
        entry = next(cursor, None)
        if entry is None:
            stack.pop()
            continue
        path, node = entry
        node = _child(parent, path, node)
        identity = (node.xref, node.generation)
        if identity in seen:
            raise _InvalidHierarchyError(parent, path, "A document part must occur exactly once in the hierarchy")
        budget.check("object_count", len(seen) + 1)
        budget.charge("memory_size", _NODE_BYTES)
        seen.add(identity)
        depth = max(depth, len(stack))
        if "DParts" in node:
            for key in ("Start", "End"):
                if key in node:
                    raise _InvalidHierarchyError(node, key, "A part with children cannot own a page range")
            stack.append((node, _children(node, node["DParts"])))
        else:
            covered = _leaf_end(document, node, covered, budget)
    if covered < count:
        raise _InvalidHierarchyError(
            document.pages[covered].obj, "DPart", "The page is outside the reachable part ranges"
        )
    _root_levels(root, depth, budget)


def hierarchy_problem(document: Document, root: Dictionary) -> tuple[int | None, str, str] | None:
    """Return the first broken hierarchy entry, or defer to the core report for an invalid page tree."""
    try:
        _core.read_operation(document._core(), lambda budget: _walk(document, root, budget))  # noqa: SLF001
    except _InvalidHierarchyError as error:
        return error.xref, error.path, str(error)
    except _core.MalformedPdfError:
        # The native structural report already contains the broken page tree.
        return None
    return None
