"""Cleaning: normalize a page's content and drop the resources that it never names.

``page.clean_contents()`` writes the content again from the values of its
operations, one operation per line, so comments and redundant white space
go. A ``Q`` without its ``q`` is dropped, a ``q`` left open is closed at the
end, and an empty ``q Q`` or ``BT ET`` pair is removed: none of these change
what renders. The page then holds its own ``/Resources`` with only the
fonts, XObjects, graphics states, color spaces, patterns, shadings, and
property lists that the content names, and every other entry as it was.
Annotation and widget appearance streams are cleaned the same way, each
with its own resources.

Resources stay whole where something drawn may borrow them: a Form XObject
or a Type 3 font without resources of its own draws with the resources of
the content that uses it (ISO 32000-2 7.8.3 and 9.6.4). The default color
spaces stay too, since device color operators use them by name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyropdf import _core
from pyropdf._errors import LimitError
from pyropdf._objects import Array, Dictionary, Name, Stream
from pyropdf._operations import _DEFAULT_MAX_CONTENT_SIZE, InlineImage, Operation, Operations
from pyropdf._xobjects import _parse_stream_operations

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyropdf._page import Page

#: The operators that name a resource: its category and the operand that
#: holds the name, the last for ``scn`` and ``SCN``.
_NAMED = {
    "Tf": ("Font", 0),
    "Do": ("XObject", 0),
    "gs": ("ExtGState", 0),
    "sh": ("Shading", 0),
    "cs": ("ColorSpace", 0),
    "CS": ("ColorSpace", 0),
    "scn": ("Pattern", -1),
    "SCN": ("Pattern", -1),
    "BDC": ("Properties", 1),
    "DP": ("Properties", 1),
}

#: The resource categories that content names entries of.
_CATEGORIES = frozenset(category for category, _ in _NAMED.values())

#: Color space names that no resource defines, in operators and in inline images.
_FIXED_SPACES = frozenset({"DeviceGray", "DeviceRGB", "DeviceCMYK", "Pattern", "G", "RGB", "CMYK", "I", "Indexed"})

#: Color spaces that device color operators use by name (ISO 32000-2 8.6.5.6).
_DEFAULT_SPACES = frozenset({"DefaultGray", "DefaultRGB", "DefaultCMYK"})

#: Page tree levels that the search for inherited resources climbs.
_MAX_TREE_DEPTH = 64

#: Annotations and distinct appearance streams that ``clean_page`` collects on one page.
_MAX_APPEARANCES = 4096

_WHITESPACE = b"\x00\t\n\x0c\r "


def clean_page(page: Page, *, annotations: bool) -> None:
    """Clean ``page``'s content and, with ``annotations``, its appearance streams, in one transaction."""
    document = page.document

    def run(budget: _core._OperationBudget) -> None:
        # Read and clean everything before writing, so a refusal leaves the document unchanged.
        operations, used = clean(page.operations, budget)
        resources = _pruned(_resources(page, budget), used, budget)
        appearances = _appearances(page, budget) if annotations else []
        cleaned: list[tuple[Stream, bytes, dict[str, Any] | None]] = []
        for stream in appearances:
            budget.poll()
            stream_operations, stream_used = clean(_parse_stream_operations(stream), budget)
            cleaned.append(
                (stream, _bytes(stream_operations, budget), _pruned(stream.get("Resources"), stream_used, budget))
            )
        with document.transaction("clean contents"):
            if "Contents" in page.obj or operations:
                page.operations = operations
            if resources is not None:
                page.obj["Resources"] = resources
            for stream, stream_bytes, stream_resources in cleaned:
                budget.poll()
                stream.write(stream_bytes)
                if stream_resources is not None:
                    stream["Resources"] = stream_resources

    _core.read_operation(document._core(), run)  # noqa: SLF001


def _poll(budget: _core._OperationBudget | None, index: int = 0) -> None:
    if budget is not None and index % 256 == 0:
        budget.poll()


def clean(
    operations: Operations,
    budget: _core._OperationBudget | None = None,
) -> tuple[Operations, dict[str, set[str]]]:
    """Return ``operations`` written afresh and balanced, and the resource names they use by category."""
    _poll(budget)
    if not _blank(operations.tail):
        msg = "the content holds bytes that hayro stops reading at; it is left unchanged"
        raise ValueError(msg)
    used: dict[str, set[str]] = {category: set() for category in _CATEGORIES}
    out: list[Operation | InlineImage] = []
    depth = 0
    for index, operation in enumerate(operations):
        _poll(budget, index)
        _note(used, operation)
        if isinstance(operation, InlineImage):
            out.append(InlineImage(operation.dictionary, operation.data))
            continue
        operator = operation.operator
        if operator == "q":
            depth += 1
        elif operator == "Q":
            if not depth:
                continue
            depth -= 1
            if out and out[-1].operator == "q":
                out.pop()
                continue
        elif operator == "ET" and out and out[-1].operator == "BT":
            out.pop()
            continue
        out.append(Operation(operator, operation.operands))
    out.extend(Operation("Q") for _ in range(depth))
    return Operations(out), used


def _note(used: dict[str, set[str]], operation: Operation | InlineImage) -> None:
    """Add the resource that ``operation`` names, if any, to ``used``."""
    if isinstance(operation, InlineImage):
        category, name = "ColorSpace", operation.dictionary.get("CS", operation.dictionary.get("ColorSpace"))
    else:
        named = _NAMED.get(operation.operator)
        if named is None or len(operation.operands) <= max(named[1], 0):
            return
        category, name = named[0], operation.operands[named[1]]
    if isinstance(name, Name) and (category != "ColorSpace" or name not in _FIXED_SPACES):
        used[category].add(name)


def _blank(tail: bytes) -> bool:
    """Return whether ``tail`` holds only white space and comments."""
    comment = False
    for byte in tail:
        if byte in b"\r\n":
            comment = False
        elif byte == ord("%"):
            comment = True
        elif not comment and byte not in _WHITESPACE:
            return False
    return True


def _bytes(operations: Operations, budget: _core._OperationBudget | None = None) -> bytes:
    data = operations.to_bytes()
    if len(data) > _DEFAULT_MAX_CONTENT_SIZE:
        code = "content_input_size"
        msg = f"cleaned content of {len(data)} bytes exceeds the {_DEFAULT_MAX_CONTENT_SIZE}-byte limit"
        raise LimitError(code, msg)
    if budget is not None:
        budget.check("content_input_size", len(data))
        budget.charge("memory_size", len(data))
    return data


def _resources(page: Page, budget: _core._OperationBudget | None = None) -> Dictionary | None:
    """Return the page's resources, its own or inherited from the page tree."""
    node: Any = page.obj
    for _ in range(_MAX_TREE_DEPTH):
        if budget is not None:
            budget.poll()
        if not isinstance(node, Dictionary):
            return None
        resources = node.get("Resources")
        if isinstance(resources, Dictionary):
            return resources
        node = node.get("Parent")
    return None


def _borrows(resources: Dictionary, used: dict[str, set[str]], budget: _core._OperationBudget | None = None) -> bool:
    """Return whether a used Form XObject or Type 3 font has no resources and so draws with ``resources``."""
    for category, subtype in (("XObject", "Form"), ("Font", "Type3")):
        entries = resources.get(category)
        if not isinstance(entries, Dictionary):
            continue
        for name in used[category]:
            if budget is not None:
                budget.poll()
            entry = entries.get(name)
            if isinstance(entry, Dictionary) and entry.get("Subtype") == subtype and "Resources" not in entry:
                return True
    return False


def _pruned(
    resources: object,
    used: dict[str, set[str]],
    budget: _core._OperationBudget | None = None,
) -> dict[str, Any] | None:
    """Return ``resources`` keeping only the named entries of each category, or ``None`` when none would go."""
    if not isinstance(resources, Dictionary) or _borrows(resources, used, budget):
        return None
    pruned: dict[str, Any] = {}
    changed = False
    for key, value in resources.items():
        if budget is not None:
            budget.poll()
        if key not in _CATEGORIES or not isinstance(value, Dictionary):
            pruned[key] = value
            continue
        names = (used[key] | _DEFAULT_SPACES) if key == "ColorSpace" else used[key]
        kept = {name: entry for name, entry in value.items() if name in names}
        if len(kept) == len(value):
            pruned[key] = value
            continue
        changed = True
        if kept:
            pruned[key] = kept
    return pruned if changed else None


def _appearances(page: Page, budget: _core._OperationBudget) -> list[Stream]:
    """Return the distinct appearance streams of the page's annotations and widgets."""
    annots = page.obj.get("Annots")
    if not isinstance(annots, Array):
        return []
    if len(annots) > _MAX_APPEARANCES:
        code = "annotation_count"
        msg = f"a page has more than {_MAX_APPEARANCES} annotations"
        raise LimitError(code, msg)
    streams: dict[int, Stream] = {}
    visited = 0
    for annotation in annots:
        budget.poll()
        appearances = annotation.get("AP") if isinstance(annotation, Dictionary) else None
        if not isinstance(appearances, Dictionary):
            continue
        for key in ("N", "R", "D"):
            entry = appearances.get(key)
            candidates: Iterable[object] = (
                (entry,) if isinstance(entry, Stream) else entry.values() if isinstance(entry, Dictionary) else ()
            )
            for stream in candidates:
                budget.poll()
                visited += 1
                if visited > _MAX_APPEARANCES * 4:
                    code = "annotation_count"
                    msg = f"clean_contents visits more than {_MAX_APPEARANCES * 4} appearance entries"
                    raise LimitError(code, msg)
                if isinstance(stream, Stream) and stream.xref is not None and stream.xref not in streams:
                    if len(streams) >= _MAX_APPEARANCES:
                        code = "annotation_count"
                        msg = f"clean_contents reaches more than {_MAX_APPEARANCES} appearance streams"
                        raise LimitError(code, msg)
                    budget.charge("memory_size", 128)
                    streams[stream.xref] = stream
    return list(streams.values())
