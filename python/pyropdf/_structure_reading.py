"""Interpret structure children using the live object tree and page character provenance."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pyropdf._errors import LimitError, MalformedPdfError
from pyropdf._objects import Array, Dictionary, Name, String
from pyropdf._operations import Operation
from pyropdf._structure import (
    _CODE_STRUCTURE_TREE,
    _DEFAULT_MAX_ELEMENTS,
    _MAX_STRUCTURE_DEPTH,
    _MAX_TREE_DEPTH,
    MarkedContentReference,
    ObjectReference,
    StructureChildRule,
    StructureElement,
    StructureText,
    StructureTree,
    _depth_limit_error,
    _iter_kids,
    _structure_item,
)
from pyropdf._structure_rules import occurrence
from pyropdf._xobjects import _resources

if TYPE_CHECKING:
    from pyropdf import _core
    from pyropdf._page import Page
    from pyropdf._structure import StructureItem

_PDF2 = "http://iso.org/pdf2/ssn"
_CODE_OBJECT_COUNT = "object_count"


def _root_items(tree: StructureTree, budget: _core._OperationBudget) -> Iterator[StructureItem]:
    root = tree.obj
    if root is None:
        return
    for value in _iter_kids(root):
        budget.poll()
        item = _structure_item(tree._document, value, None)  # noqa: SLF001
        if not isinstance(item, StructureElement):
            msg = "the children of StructTreeRoot must be indirect structure elements"
            raise MalformedPdfError(msg)
        yield item


def _walk(
    tree: StructureTree, budget: _core._OperationBudget
) -> Iterator[tuple[int, StructureElement | None, StructureItem]]:
    seen = set()
    count = 0
    frames: list[tuple[StructureElement | None, Iterator[StructureItem]]] = [(None, _root_items(tree, budget))]
    while frames:
        parent, children = frames[-1]
        child = next(children, None)
        if child is None:
            frames.pop()
            continue
        budget.poll()
        count += 1
        if count > _DEFAULT_MAX_ELEMENTS:
            msg = f"structure interpretation exceeds {_DEFAULT_MAX_ELEMENTS} child entries"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        if isinstance(child, StructureElement):
            if child.xref in seen:
                msg = "structure /K contains a cycle or a repeated element"
                raise MalformedPdfError(msg)
            if len(frames) >= _MAX_STRUCTURE_DEPTH:
                raise _depth_limit_error()
            budget.charge("memory_size", 512)
            seen.add(child.xref)
        yield len(frames), parent, child
        if isinstance(child, StructureElement):
            frames.append((child, child._iter_items(budget)))  # noqa: SLF001


@dataclass(frozen=True, slots=True)
class _Mark:
    mcid: int | None = None
    replaced: bool = False
    artifact: bool = False


def _open_mark(  # noqa: PLR0913 - one parsed operation and its surrounding marked-content state.
    outer: _Mark,
    op: Operation,
    properties: object,
    pieces: dict[int, list[str]],
    budget: _core._OperationBudget,
    *,
    expand: bool,
) -> _Mark:
    tag = op.operands[0] if op.operands else None
    props = op.operands[1] if op.operator == "BDC" and len(op.operands) == 2 else None  # noqa: PLR2004
    if isinstance(props, Name) and isinstance(properties, Dictionary):
        props = properties.get(str(props))
    mark = _Mark(outer.mcid, outer.replaced, outer.artifact or tag == Name("Artifact"))
    if not isinstance(props, Mapping):
        return mark
    mcid = props.get("MCID")
    if isinstance(mcid, int) and not isinstance(mcid, bool) and mcid >= 0:
        if mcid in pieces or outer.mcid is not None:
            msg = f"duplicate or nested marked-content identifier {mcid} in a page's content"
            raise MalformedPdfError(msg)
        budget.charge("memory_size", 128)
        pieces[mcid] = []
        mark = _Mark(mcid, mark.replaced, mark.artifact)
    value = props.get("E") if expand else None
    if not isinstance(value, String):
        value = props.get("ActualText")
    if mark.mcid is not None and not mark.artifact and not mark.replaced and isinstance(value, String):
        budget.charge("memory_size", 64 + 4 * len(value))
        pieces[mark.mcid].append(value.text)
        return _Mark(mark.mcid, replaced=True)
    return mark


def _glyphs_by_operation(page: Page, budget: _core._OperationBudget) -> dict[int, list[str]]:
    glyphs: dict[int, list[str]] = {}
    for char in page.chars():
        if char.operation is not None:
            budget.charge("memory_size", 96 + 4 * len(char.text))
            glyphs.setdefault(char.operation, []).append(char.text)
    return glyphs


def _is_form(resources: Dictionary | None, operands: tuple[object, ...]) -> bool:
    xobjects = resources.get("XObject") if resources is not None else None
    target = xobjects.get(str(operands[0])) if operands and isinstance(xobjects, Dictionary) else None
    return isinstance(target, Dictionary) and target.get("Subtype") == Name("Form")


def _page_text(page: Page, budget: _core._OperationBudget, *, expand: bool) -> dict[int, str | None]:
    # Form XObjects and annotations have independent MCID spaces on the same page.
    resources = _resources(page)
    properties = resources.get("Properties") if resources is not None else None
    stack = [_Mark()]
    pieces: dict[int, list[str]] = {}
    incomplete: set[int] = set()
    glyphs = None
    for index, op in enumerate(page.operations):
        budget.poll()
        mark = stack[-1]
        if isinstance(op, Operation) and op.operator in ("BDC", "BMC"):
            budget.charge("memory_size", 128)
            stack.append(_open_mark(mark, op, properties, pieces, budget, expand=expand))
        elif op.operator == "EMC":
            if len(stack) == 1:
                msg = "unmatched EMC in tagged page content"
                raise MalformedPdfError(msg)
            stack.pop()
            budget.release("memory_size", 128)
        elif mark.mcid is not None and not mark.artifact and not mark.replaced:
            if op.operator in ("Tj", "TJ", "'", '"'):
                if glyphs is None:
                    glyphs = _glyphs_by_operation(page, budget)
                pieces[mark.mcid].extend(glyphs.get(index, ()))
            elif op.operator == "Do" and _is_form(resources, op.operands):
                incomplete.add(mark.mcid)
    if len(stack) != 1:
        msg = "unclosed marked-content sequence on tagged page"
        raise MalformedPdfError(msg)
    return {mcid: None if mcid in incomplete else "".join(parts) for mcid, parts in pieces.items()}


def _replacement(child: StructureElement, *, expand: bool) -> str | None:
    if expand and (value := child.expanded) is not None:
        return value
    if (value := child.actual_text) is not None:
        return value
    return child.alt if child.standard_role in {"Figure", "Formula", "Form"} else None


def _object_text(element: StructureElement, item: ObjectReference) -> str | None:
    obj = element.document.objects[item.xref]
    if not isinstance(obj, Dictionary):
        return None
    if obj.get("Subtype") == Name("Widget"):
        # A merged field/widget holds its own alternate name. Otherwise /Parent
        # names the field to which this particular widget belongs.
        field = obj if "TU" in obj else obj.get("Parent")
        alternate = field.get("TU") if isinstance(field, Dictionary) else None
        if isinstance(alternate, String):
            return alternate.text
    contents = obj.get("Contents")
    return contents.text if isinstance(contents, String) else None


def reading_order(tree: StructureTree, budget: _core._OperationBudget, *, expand: bool) -> tuple[StructureText, ...]:
    result: list[StructureText] = []
    pages: dict[int, dict[int, str | None]] = {}
    suppressed = _MAX_STRUCTURE_DEPTH + 1
    for depth, owner, child in _walk(tree, budget):
        if depth > suppressed:
            continue
        suppressed = _MAX_STRUCTURE_DEPTH + 1
        if isinstance(child, StructureElement):
            if child.standard_role == "Artifact":
                suppressed = depth
                continue
            text = _replacement(child, expand=expand)
            if text is None:
                continue
            row = StructureText(element=child, item=None, text=text)
            suppressed = depth
        else:
            assert owner is not None  # noqa: S101 - validated at the root.
            text = None
            if isinstance(child, ObjectReference):
                text = _object_text(owner, child)
            elif child.stream_xref is None and child.page is not None:
                if child.page not in pages:
                    budget.charge("memory_size", 192)
                    pages[child.page] = _page_text(owner.document.pages[child.page], budget, expand=expand)
                text = pages[child.page].get(child.mcid)
            row = StructureText(element=owner, item=child, text=text)
        budget.charge("memory_size", 160 + (4 * len(row.text) if row.text is not None else 0))
        result.append(row)
    return tuple(result)


_PATH_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "sh", "BI"}
_TEXT_SHOW = {"Tj", "TJ", "'", '"'}


def _paint(op: object, resources: Dictionary | None) -> str | None:
    if not isinstance(op, Operation):
        return "graphic"  # InlineImage
    if op.operator in _PATH_PAINT:
        return "graphic"
    if op.operator in _TEXT_SHOW:
        if op.operands:
            value = op.operands[-1]
            strings = value if op.operator == "TJ" and isinstance(value, (tuple, list)) else (value,)
            if any(isinstance(item, bytes) and item for item in strings):
                return "text"
        return None
    if op.operator == "Do" and op.operands:
        xobjects = resources.get("XObject") if resources is not None else None
        obj = xobjects.get(str(op.operands[0])) if isinstance(xobjects, Dictionary) else None
        return "graphic" if isinstance(obj, Dictionary) and obj.get("Subtype") == "Image" else "unknown"
    return None


def _page_paint(page: Page, budget: _core._OperationBudget) -> dict[int, str]:
    resources = _resources(page)
    properties = resources.get("Properties") if resources is not None else None
    result: dict[int, str] = {}
    marks = [_Mark()]
    pieces: dict[int, list[str]] = {}
    for op in page.operations:
        budget.poll()
        current = marks[-1]
        if isinstance(op, Operation) and op.operator in {"BMC", "BDC"}:
            if len(marks) >= _MAX_STRUCTURE_DEPTH:
                raise _depth_limit_error()
            budget.charge("memory_size", 128)
            child = _open_mark(current, op, properties, pieces, budget, expand=False)
            marks.append(child)
            if child.mcid is not None and child.replaced and not child.artifact:
                result.setdefault(child.mcid, "text")
        elif op.operator == "EMC":
            if len(marks) == 1:
                msg = "unmatched EMC in tagged page content"
                raise MalformedPdfError(msg)
            marks.pop()
            budget.release("memory_size", 128)
        elif current.mcid is not None and not current.replaced and not current.artifact:
            kind = _paint(op, resources)
            previous = result.get(current.mcid)
            if kind is not None and (
                previous is None or kind == "graphic" or (kind == "unknown" and previous == "text")
            ):
                result[current.mcid] = kind
    if len(marks) != 1:
        msg = "unclosed marked-content sequence on tagged page"
        raise MalformedPdfError(msg)
    return result


def _text_content(
    tree: StructureTree,
    item: MarkedContentReference | ObjectReference,
    pages: dict[int, dict[int, str]],
    budget: _core._OperationBudget,
) -> bool | None:
    if isinstance(item, ObjectReference):
        obj = tree._document.objects[item.xref]  # noqa: SLF001
        return False if isinstance(obj, Dictionary) and obj.get("Subtype") == "Image" else None
    if item.stream_xref is not None or item.page is None:
        return None
    if item.page not in pages:
        budget.charge("memory_size", 192)
        pages[item.page] = _page_paint(tree._document.pages[item.page], budget)  # noqa: SLF001
    return {"text": True, "graphic": False}.get(pages[item.page].get(item.mcid, "unknown"))


_PDF1 = "http://iso.org/pdf/ssn"
_STANDARD = "PDF 2.0"
_TRANSPARENT = {"Part", "Div", "NonStruct"}
_TEXT_ROLES = {"RB", "RT", "RP", "WT", "WP"}
_INLINE = {"Em", "Strong", "Span", "Sub", "Ruby", "RB", "RT", "RP", "Warichu", "WT", "WP", "Lbl"}
_GROUPING = {"Document", "DocumentFragment", "Part", "Sect", "Aside"}
_OTHER = "other namespace"


def _namespace_mapping(namespace: Dictionary, role: str) -> tuple[object, str]:
    uri = namespace.get("NS")
    if not isinstance(uri, String) or not uri:
        return None, "missing namespace identifier"
    mapping = namespace.get("RoleMapNS")
    target = mapping.get(role) if isinstance(mapping, Dictionary) else None
    # The two standard identifiers fit even in UTF-16 with a byte-order mark.
    uri_text = uri.text if len(uri) <= 4 * len(_PDF2) else None
    return target, {_PDF2: _STANDARD, _PDF1: _OTHER}.get(uri_text, "unmapped namespace role")


def _role(element: StructureElement, budget: _core._OperationBudget) -> tuple[str, str]:
    role = element.role
    ns = element.obj.get("NS")
    if ns is None:
        # The global role map ends in PDF 1.7. Keep the authored name for this
        # out-of-scope relationship, as in the original Annex L reports.
        return role, _OTHER
    visited = set()
    for hops in range(_MAX_TREE_DEPTH + 1):
        budget.poll()
        if not isinstance(ns, Dictionary) or ns.xref is None:
            return role, "invalid namespace reference"
        key = ns.xref, role
        if key in visited:
            return role, "cyclic namespace role map"
        target, space = _namespace_mapping(ns, role)
        if target is None:
            return role, space
        if hops == _MAX_TREE_DEPTH:
            break
        budget.charge("memory_size", 128)
        visited.add(key)
        if isinstance(target, Name):
            return element.document.structure.resolve_role(str(target)), _OTHER
        if isinstance(target, Array) and len(target) == 2:  # noqa: PLR2004 - role and namespace
            next_role, ns = target
            if isinstance(next_role, Name):
                role = str(next_role)
                continue
        return role, "unmapped namespace role"
    msg = f"structure interpretation exceeds {_MAX_TREE_DEPTH} namespace role-map hops"
    raise LimitError(_CODE_STRUCTURE_TREE, msg)


@dataclass(slots=True)
class _Children:
    counts: dict[tuple[str, str], int] = field(default_factory=dict)
    first: str | None = None
    last: str | None = None
    order: list[str] = field(default_factory=list)
    inline: bool = False
    grouping: bool = False
    mixed: bool = False
    text: bool | None = True

    def add(self, role: str, space: str, content: bool | None, budget: _core._OperationBudget) -> None:  # noqa: FBT001 - three-valued text-content verdict
        key = role, space
        if key not in self.counts:
            budget.charge("memory_size", 192)
        self.counts[key] = self.counts.get(key, 0) + 1
        self.mixed |= space != _STANDARD
        self.inline |= role == "content item" or (space == _STANDARD and role in _INLINE)
        self.grouping |= space == _STANDARD and role in _GROUPING
        if self.first is None:
            self.first = role
        self.last = role
        if role == "content item":
            if content is False or (self.text is True and content is None):
                self.text = content
        elif len(self.order) < 5:  # noqa: PLR2004 - one more than the longest Ruby/Warichu assembly.
            self.order.append(role)


@dataclass(slots=True)
class _Siblings:
    role: str
    space: str
    usage: bool | None
    owner: _Siblings | None = None
    direct: _Children = field(default_factory=_Children)
    flat: _Children = field(default_factory=_Children)

    def add(self, role: str, space: str, content: bool | None, budget: _core._OperationBudget) -> None:  # noqa: FBT001
        self.direct.add(role, space, content, budget)
        if not (space == _STANDARD and role in _TRANSPARENT):
            (self.owner or self).flat.add(role, space, content, budget)

    def records(self, number: int | None, budget: _core._OperationBudget) -> Iterator[StructureChildRule]:
        budget.charge("memory_size", 320 * (len(self.direct.counts) + len(self.flat.counts)) + 128)
        observations = self.direct.counts.copy()
        if self.owner is None:
            observations.update(self.flat.counts)
        if self.space == _STANDARD and self.role == "Ruby" and not self.flat.mixed:
            for required in ("RB", "RT"):
                observations.setdefault((required, _STANDARD), 0)
        for (child, space), count in observations.items():
            if self.space != _STANDARD or space != _STANDARD:
                rule, allowed = (self.space if self.space != _STANDARD else space), None
            else:
                rule, allowed = self.judge(child, count)
            yield StructureChildRule(
                parent_xref=number, parent_role=self.role, child_role=child, count=count, rule=rule, allowed=allowed
            )

    def judge(self, child: str, count: int) -> tuple[str, bool | None]:
        rule, allowed = occurrence(self.role, child, count)
        context = self
        if rule == "role-specific" and self.owner is not None:
            context = self.owner
            if context.space != _STANDARD:
                return context.space, None
            count = context.flat.counts.get((child, _STANDARD), count)
            rule, allowed = occurrence(context.role, child, count)
        seen = context.flat
        if rule == "grouping only":
            # Section 14.8.4.2 excludes inline children and direct content. The author
            # can identify the intended use when the visible children fit several uses.
            if seen.inline or context.usage is False:
                allowed = False
            elif context.usage is True or (seen.grouping and not seen.mixed):
                allowed = True
        elif rule == "role-specific" and context.role in _TEXT_ROLES and child == "content item":
            return "text content of " + context.role, seen.text
        elif rule == "ruby sequence" and not seen.mixed:
            allowed = seen.order in (["RB", "RT"], ["RB", "RP", "RT", "RP"])
        elif rule == "warichu sequence" and not seen.mixed:
            # The base standard permits an unstructured assembly. If its punctuation
            # and text are tagged as a group, their order is WP, WT, WP.
            allowed = seen.order == ["WP", "WT", "WP"]
        if child == "Caption" and (count > 1 or child not in (seen.first, seen.last)):
            return "single first or last caption", False
        return rule, allowed


def _usage(usage: Mapping[int, bool] | None, budget: _core._OperationBudget) -> dict[int, bool]:
    if usage is None:
        return {}
    if not isinstance(usage, Mapping):
        msg = "grouping_usage must map structure element object numbers to bool"
        raise TypeError(msg)
    result = {}
    for number, grouping in usage.items():
        budget.poll()
        if len(result) >= _DEFAULT_MAX_ELEMENTS:
            msg = "too many grouping usage declarations"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        if type(number) is not int or number <= 0 or type(grouping) is not bool:
            msg = "grouping_usage requires positive object numbers and bool values"
            raise ValueError(msg)
        budget.charge("memory_size", 128)
        result[number] = grouping
    return result


def child_rules(
    tree: StructureTree, budget: _core._OperationBudget, usage: Mapping[int, bool] | None = None
) -> tuple[StructureChildRule, ...]:
    declared = _usage(usage, budget)
    root = _Siblings("StructTreeRoot", _STANDARD, None)
    groups: dict[int | None, _Siblings] = {None: root}
    pages: dict[int, dict[int, str]] = {}
    uses_pdf2 = False
    for _, parent, child in _walk(tree, budget):
        siblings = groups[parent.xref if parent is not None else None]
        if isinstance(child, StructureElement):
            role, space = _role(child, budget)
            uses_pdf2 |= space == _STANDARD
            owner = (siblings.owner or siblings) if space == _STANDARD and role in _TRANSPARENT else None
            budget.charge("memory_size", 1024)
            groups[child.xref] = _Siblings(role, space, declared.pop(child.xref, None), owner)
            siblings.add(role, space, None, budget)
        else:
            text = None
            if (siblings.owner or siblings).role in _TEXT_ROLES:
                text = _text_content(tree, child, pages, budget)
            siblings.add("content item", _STANDARD, text, budget)
    if declared:
        msg = "grouping_usage contains an object outside the structure tree"
        raise ValueError(msg)
    if uses_pdf2:
        root.direct.counts.setdefault(("Document", _STANDARD), 0)
    return tuple(record for number, siblings in groups.items() for record in siblings.records(number, budget))
