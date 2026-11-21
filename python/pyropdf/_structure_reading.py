"""Interpret structure children using the live object tree and page character provenance."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._errors import LimitError, MalformedPdfError
from pyropdf._objects import Dictionary, Name, Stream, String
from pyropdf._operations import InlineImage, Operation, Operations, _from_core
from pyropdf._structure import (
    _DEFAULT_MAX_ELEMENTS,
    _MAX_STRUCTURE_DEPTH,
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


def _marked_operations(
    operations: Operations,
    properties: object,
    pieces: dict[int, list[str]],
    budget: _core._OperationBudget,
    *,
    expand: bool,
) -> Iterator[tuple[int, Operation | InlineImage, _Mark, bool]]:
    stack = [_Mark()]
    for index, op in enumerate(operations):
        budget.poll()
        mark = stack[-1]
        if isinstance(op, Operation) and op.operator in ("BDC", "BMC"):
            if len(stack) >= _MAX_STRUCTURE_DEPTH:
                raise _depth_limit_error()
            budget.charge("memory_size", 128)
            child = _open_mark(mark, op, properties, pieces, budget, expand=expand)
            stack.append(child)
            yield index, op, child, child.mcid != mark.mcid
        elif op.operator == "EMC":
            if len(stack) == 1:
                msg = "unmatched EMC in tagged content"
                raise MalformedPdfError(msg)
            stack.pop()
            budget.release("memory_size", 128)
        else:
            yield index, op, mark, False
    if len(stack) != 1:
        msg = "unclosed marked-content sequence in tagged content"
        raise MalformedPdfError(msg)


def _page_text(page: Page, budget: _core._OperationBudget, *, expand: bool) -> dict[int, str | None]:
    resources = _resources(page)
    properties = resources.get("Properties") if resources is not None else None
    pieces: dict[int, list[str]] = {}
    incomplete: set[int] = set()
    glyphs = None
    for index, op, mark, _ in _marked_operations(page.operations, properties, pieces, budget, expand=expand):
        if mark.mcid is None or mark.artifact or mark.replaced:
            continue
        if op.operator in ("Tj", "TJ", "'", '"'):
            if glyphs is None:
                glyphs = _glyphs_by_operation(page, budget)
            pieces[mark.mcid].extend(glyphs.get(index, ()))
        elif op.operator == "Do" and _is_form(resources, op.operands):
            incomplete.add(mark.mcid)
    return {mcid: None if mcid in incomplete else "".join(parts) for mcid, parts in pieces.items()}


def _form_replacements(
    tree: StructureTree, stream_xref: int, budget: _core._OperationBudget, *, expand: bool
) -> dict[int, str | None]:
    # A substitution on the entire referenced MCID is independent of where the
    # Form was painted. Glyphs and inherited named properties need its placement.
    doc = tree._document  # noqa: SLF001
    stream = doc.objects[stream_xref]
    if not isinstance(stream, Stream) or stream.get("Subtype") != Name("Form") or stream.generation is None:
        return {}
    raw = _core.stream_operations(doc._core(), stream_xref, stream.generation)  # noqa: SLF001
    if raw is None:
        return {}
    resources = stream.get("Resources")
    properties = resources.get("Properties") if isinstance(resources, Dictionary) else None
    pieces: dict[int, list[str]] = {}
    replacements: dict[int, str | None] = {}
    for _, _, mark, opens_mcid in _marked_operations(_from_core(raw), properties, pieces, budget, expand=expand):
        if opens_mcid and mark.mcid is not None:
            budget.charge("memory_size", 128)
            if mark.artifact:
                replacements[mark.mcid] = ""
            elif mark.replaced:
                replacements[mark.mcid] = pieces[mark.mcid][0]
    return replacements


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


def _marked_text(
    tree: StructureTree,
    item: MarkedContentReference,
    cache: dict[tuple[str, int], dict[int, str | None]],
    budget: _core._OperationBudget,
    *,
    expand: bool,
) -> str | None:
    if item.stream_xref is not None:
        key = ("form", item.stream_xref)
        if key not in cache:
            budget.charge("memory_size", 192)
            cache[key] = _form_replacements(tree, item.stream_xref, budget, expand=expand)
    elif item.page is not None:
        key = ("page", item.page)
        if key not in cache:
            budget.charge("memory_size", 192)
            cache[key] = _page_text(tree._document.pages[item.page], budget, expand=expand)  # noqa: SLF001
    else:
        return None
    return cache[key].get(item.mcid)


def reading_order(tree: StructureTree, budget: _core._OperationBudget, *, expand: bool) -> tuple[StructureText, ...]:
    result: list[StructureText] = []
    cache: dict[tuple[str, int], dict[int, str | None]] = {}
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
            if isinstance(child, ObjectReference):
                text = _object_text(owner, child)
            else:
                text = _marked_text(tree, child, cache, budget, expand=expand)
            row = StructureText(element=owner, item=child, text=text)
        budget.charge("memory_size", 160 + (4 * len(row.text) if row.text is not None else 0))
        result.append(row)
    return tuple(result)


def _pdf2(element: StructureElement) -> bool:
    ns = element.obj.get("NS")
    name = ns.get("NS") if isinstance(ns, Dictionary) else None
    return isinstance(name, String) and name.text == _PDF2


@dataclass(slots=True)
class _Siblings:
    role: str
    pdf2: bool
    effective: str | None
    counts: dict[tuple[str, bool], int] = field(default_factory=dict)
    first: str | None = None
    last: str | None = None
    ruby: list[str] = field(default_factory=list)
    mixed: bool = False
    inline_content: bool = False

    def add(self, role: str, *, standard: bool, is_element: bool, budget: _core._OperationBudget) -> None:
        key = role, standard
        if key not in self.counts:
            budget.charge("memory_size", 192)
        self.counts[key] = self.counts.get(key, 0) + 1
        # A mixed-use parent with direct content or a definite inline child
        # cannot also be serving as a grouping element (14.8.4.2).
        self.inline_content |= not is_element or (standard and role in {"Em", "Strong", "Span", "Sub"})
        if is_element:
            if self.first is None:
                self.first = role
            self.last = role
            self.mixed |= not standard
            # Five entries suffice to distinguish every Ruby sequence from an overlong one.
            if self.role == "Ruby" and len(self.ruby) < 5:  # noqa: PLR2004
                self.ruby.append(role)

    def records(self, number: int | None) -> Iterator[StructureChildRule]:
        if self.pdf2 and self.role == "Ruby" and not self.mixed:
            for required in ("RB", "RT"):
                self.counts.setdefault((required, True), 0)
        for (child, known), count in self.counts.items():
            rule, allowed = self.judge(child, count) if self.pdf2 and known else ("other namespace", None)
            yield StructureChildRule(
                parent_xref=number, parent_role=self.role, child_role=child, count=count, rule=rule, allowed=allowed
            )

    def judge(self, child: str, count: int) -> tuple[str, bool | None]:
        rule, allowed = occurrence(self.role, child, count)
        if rule == "role-specific" and self.effective != self.role and self.effective is not None:
            rule, allowed = occurrence(self.effective, child, count)
        if rule == "grouping only" and self.inline_content:
            allowed = False
        if rule == "ruby sequence" and not self.mixed and "NonStruct" not in self.ruby:
            allowed = self.ruby in (["RB", "RT"], ["RB", "RP", "RT", "RP"])
        if child == "Caption" and (count > 1 or child not in (self.first, self.last)):
            return "single first or last caption", False
        return rule, allowed


def child_rules(tree: StructureTree, budget: _core._OperationBudget) -> tuple[StructureChildRule, ...]:
    root = _Siblings("StructTreeRoot", pdf2=True, effective="StructTreeRoot")
    groups: dict[int | None, _Siblings] = {None: root}
    uses_pdf2 = False
    for _, parent, child in _walk(tree, budget):
        siblings = groups[parent.xref if parent is not None else None]
        if isinstance(child, StructureElement):
            role, standard = child.role, _pdf2(child)
            uses_pdf2 |= standard
            # Only role-specific entries can inherit containment. An explicit
            # prohibition in the immediate parent's column always takes precedence.
            effective = siblings.effective if role in {"Part", "Div", "NonStruct"} else role
            budget.charge("memory_size", 512)
            groups[child.xref] = _Siblings(role, standard, effective if standard else None)
            siblings.add(role, standard=standard, is_element=True, budget=budget)
        else:
            siblings.add("content item", standard=True, is_element=False, budget=budget)
    if uses_pdf2:
        root.counts.setdefault(("Document", True), 0)
    return tuple(record for number, siblings in groups.items() for record in siblings.records(number))
