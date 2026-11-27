"""Interpret structure children using the live object tree and page character provenance."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pyropdf import _core
from pyropdf._errors import LimitError, MalformedPdfError
from pyropdf._geometry import Rect
from pyropdf._objects import Array, Dictionary, Name, String
from pyropdf._operations import Operation
from pyropdf._structure import (
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
    _page_index_from_dict,
    _structure_item,
)
from pyropdf._structure_rules import occurrence
from pyropdf._text import _DEFAULT_MAX_TEXT_GLYPHS, _DEFAULT_MAX_TEXT_SIZE, build_layout, build_words, text_options
from pyropdf._xobjects import _resources

if TYPE_CHECKING:
    from pyropdf._markdown import TextPageDict
    from pyropdf._page import Page
    from pyropdf._structure import StructureItem
    from pyropdf._text import Word, _CoreLine

_PDF2 = "http://iso.org/pdf2/ssn"
_CODE_OBJECT_COUNT = "object_count"
_CODE_STRUCTURE_TREE = "structure_tree_count"


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


_CODE_TABLE_CELL_COUNT = "table_cell_count"
_MIN_STRUCTURE_COVERAGE = 0.7
_MAX_TABLE_CELLS = 4096
_MAX_TABLE_SPAN = 1024
_HEADING_NUMBER_RE = re.compile(r"^H([1-9]\d*)$")
_NUMBERED_LABEL_RE = re.compile(r"^(\d{1,3})[.)]?$")
_BACKTICK_RUN_RE = re.compile(r"`+")
_SECTION_ROLES = frozenset({"Part", "Art", "Sect", "Aside"})
_PARAGRAPH_ROLES = frozenset({"P", "Caption", "BibEntry", "Note"})
_ORDERED_NUMBERING = frozenset({"Decimal", "UpperRoman", "LowerRoman", "UpperAlpha", "LowerAlpha"})
_BULLET_LABELS = frozenset("・•●○◦▪‣–—-*")
_ZERO_RECT = Rect(0.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True, kw_only=True)
class StructureBlock:
    """One semantic block extracted from a page's PDF structure tree."""

    kind: Literal["heading", "paragraph", "list_item", "table", "image", "blockquote", "code"]
    page: int
    bbox: Rect
    text: str
    markdown: str
    level: int | None = None
    rows: tuple[tuple[str | None, ...], ...] | None = None
    cell_anchors: tuple[int, ...] | None = None
    alt_text: str | None = None
    language: str | None = None
    raw_section_depth: int | None = None


@dataclass(slots=True)
class PageStructureData:
    """Per-page structure extraction result paired with cached geometric layout."""

    blocks: list[StructureBlock] | None
    width: float
    height: float
    layout: TextPageDict | None = None
    words: list[Word] | None = None


def escape_markdown_alt(text: str) -> str:
    """Escape alternate text for Markdown ``![alt]()`` syntax."""
    collapsed = " ".join(text.split())
    return collapsed.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def format_code_block(text: str) -> str:
    """Wrap ``text`` in a Markdown fenced code block whose fence never collides with ``text``."""
    longest = max((len(match.group(0)) for match in _BACKTICK_RUN_RE.finditer(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def format_inline_code(text: str) -> str:
    """Wrap ``text`` in inline Markdown backticks."""
    longest = max((len(match.group(0)) for match in _BACKTICK_RUN_RE.finditer(text)), default=0)
    ticks = "`" * (longest + 1)
    padded = f" {text} " if text.startswith("`") or text.endswith("`") else text
    return f"{ticks}{padded}{ticks}"


def format_blockquote(text: str) -> str:
    """Prefix every line of ``text`` with a Markdown block quote marker."""
    lines = text.splitlines()
    if not lines:
        return ""
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _wrap_inline_emphasis(text: str, *, bold: bool, italic: bool) -> str:
    """Wrap the non-whitespace core of ``text`` with Markdown emphasis markers."""
    core = text.strip()
    if not core or not (bold or italic):
        return text
    marker = "***" if bold and italic else ("**" if bold else "*")
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return f"{lead}{marker}{core}{marker}{trail}"


def _union_rect(left: Rect | None, right: Rect | None) -> Rect | None:
    """Return the bounding union of two optional display-space rectangles."""
    if left is None:
        return right
    if right is None:
        return left
    return Rect(
        min(left.x0, right.x0),
        min(left.y0, right.y0),
        max(left.x1, right.x1),
        max(left.y1, right.y1),
    )


def _non_ws_count(text: str) -> int:
    """Return the count of non-whitespace characters in ``text``."""
    return sum(1 for ch in text if not ch.isspace())


def _step_namespace_role(
    element: StructureElement,
    ns: object,
    role: str,
    visited: set[tuple[int, str]],
    budget: _core._OperationBudget,
) -> tuple[bool, str, object]:
    """Advance one ``/RoleMapNS`` hop; return ``(done, next_role, next_ns)``."""
    if not isinstance(ns, Dictionary) or ns.xref is None:
        return True, role, None
    key = (ns.xref, role)
    if key in visited:
        return True, role, None
    target, space = _namespace_mapping(ns, role)
    if target is None:
        resolved = element.document.structure.resolve_role(role) if space == _OTHER else role
        return True, resolved, None
    budget.charge("memory_size", 128)
    visited.add(key)
    if isinstance(target, Name):
        return True, element.document.structure.resolve_role(str(target)), None
    if isinstance(target, Array) and len(target) == 2 and isinstance(target[0], Name):  # noqa: PLR2004
        return False, str(target[0]), target[1]
    return True, role, None


def _resolved_export_role(element: StructureElement, budget: _core._OperationBudget) -> str:
    """Resolve ``element``'s role through ``/RoleMapNS`` and ``/RoleMap`` for semantic export."""
    raw_role = element.role
    if raw_role == "Artifact":
        return "Artifact"
    ns: object = element.obj.get("NS")
    if ns is None:
        return element.document.structure.resolve_role(raw_role)
    role = raw_role
    visited: set[tuple[int, str]] = set()
    for hops in range(_MAX_TREE_DEPTH + 1):
        budget.poll()
        done, role, ns = _step_namespace_role(element, ns, role, visited, budget)
        if done:
            return role
        if hops == _MAX_TREE_DEPTH:
            break
    msg = f"structure interpretation exceeds {_MAX_TREE_DEPTH} namespace role-map hops"
    raise LimitError(_CODE_STRUCTURE_TREE, msg)


def _element_attr_dicts(element: StructureElement) -> list[Dictionary]:
    """Return attribute dictionaries from ``/A`` and ``/C`` (via ``/ClassMap``) on ``element``."""
    result: list[Dictionary] = []
    elem_dict = element.obj
    cls = elem_dict.get("C")
    if cls is not None:
        root = element.document.structure.obj
        class_map = root.get("ClassMap") if root is not None else None
        if isinstance(class_map, Dictionary):
            names = list(cls) if isinstance(cls, Array) else [cls]
            for item in names:
                if isinstance(item, Name):
                    mapped = class_map.get(str(item))
                    if isinstance(mapped, Dictionary):
                        result.append(mapped)
                    elif isinstance(mapped, Array):
                        result.extend(entry for entry in mapped if isinstance(entry, Dictionary))
    attrs = elem_dict.get("A")
    if isinstance(attrs, Dictionary):
        result.append(attrs)
    elif isinstance(attrs, Array):
        result.extend(entry for entry in attrs if isinstance(entry, Dictionary))
    return result


def _table_cell_spans(element: StructureElement) -> tuple[int, int]:
    """Return ``(col_span, row_span)`` from ``/A`` Table attributes, defaulting to ``(1, 1)``."""
    col_span = 1
    row_span = 1
    for attr in _element_attr_dicts(element):
        if attr.get("O") != Name("Table"):
            continue
        col = attr.get("ColSpan")
        if isinstance(col, int) and not isinstance(col, bool) and col >= 1:
            col_span = min(col, _MAX_TABLE_SPAN)
        row = attr.get("RowSpan")
        if isinstance(row, int) and not isinstance(row, bool) and row >= 1:
            row_span = min(row, _MAX_TABLE_SPAN)
    return col_span, row_span


def _list_is_ordered(element: StructureElement) -> bool:
    """Return whether an ``L`` element declares ordered ``/ListNumbering``."""
    for attr in _element_attr_dicts(element):
        if attr.get("O") != Name("List"):
            continue
        numbering = attr.get("ListNumbering")
        if isinstance(numbering, Name) and str(numbering) in _ORDERED_NUMBERING:
            return True
    return False


@dataclass(frozen=True, slots=True)
class _RichMark:
    mcid: int | None = None
    replaced: bool = False
    artifact: bool = False
    lang: str | None = None


@dataclass(frozen=True, slots=True)
class _TextPiece:
    text: str
    line_key: tuple[int, int] | None
    bbox: Rect | None
    bold: bool = False
    italic: bool = False


class _PageMcidIndex:
    """Cached per-page MCID text, boxes, languages, and trustworthiness counts."""

    __slots__ = (
        "_graphic_bboxes",
        "artifact_char_count",
        "external_char_count",
        "has_artifact_ops",
        "height",
        "incomplete_mcids",
        "layout",
        "mcid_bbox",
        "mcid_lang",
        "mcid_non_ws_counts",
        "mcid_pieces",
        "mcid_replacement",
        "page",
        "page_stream_char_count",
        "present_mcids",
        "width",
        "words",
    )

    def __init__(self, page: Page, budget: _core._OperationBudget, *, expand: bool) -> None:
        self.page = page
        self.mcid_replacement: dict[int, str] = {}
        self.mcid_lang: dict[int, str] = {}
        self.incomplete_mcids: set[int] = set()
        self.present_mcids: set[int] = set()
        self.mcid_pieces: dict[int, list[_TextPiece]] = {}
        self.mcid_bbox: dict[int, Rect] = {}
        self.mcid_non_ws_counts: dict[int, int] = {}
        self.page_stream_char_count = 0
        self.external_char_count = 0
        self.artifact_char_count = 0
        self.has_artifact_ops = False
        self._graphic_bboxes: dict[int, Rect] | None = None

        op_mcid, op_artifact, mcid_ops = self._scan_operations(page, budget, expand=expand)
        self.width, self.height, self.layout, self.words = self._scan_layout(
            page, op_mcid, op_artifact, mcid_ops, budget
        )

    def _open_rich_mark(
        self,
        outer: _RichMark,
        op: Operation,
        properties: object,
        budget: _core._OperationBudget,
        *,
        expand: bool,
    ) -> _RichMark:
        tag = op.operands[0] if op.operands else None
        props = op.operands[1] if op.operator == "BDC" and len(op.operands) == 2 else None  # noqa: PLR2004
        if isinstance(props, Name) and isinstance(properties, Dictionary):
            props = properties.get(str(props))
        artifact = outer.artifact or tag == Name("Artifact")
        lang = outer.lang
        if isinstance(props, Mapping):
            raw_lang = props.get("Lang")
            if isinstance(raw_lang, String) and raw_lang.text:
                lang = raw_lang.text
        mark = _RichMark(outer.mcid, outer.replaced, artifact, lang)
        if not isinstance(props, Mapping):
            return mark
        mcid = props.get("MCID")
        if isinstance(mcid, int) and not isinstance(mcid, bool) and mcid >= 0:
            if mcid in self.present_mcids or outer.mcid is not None:
                msg = f"duplicate or nested marked-content identifier {mcid} in a page's content"
                raise MalformedPdfError(msg)
            budget.charge("memory_size", 128)
            self.present_mcids.add(mcid)
            if lang is not None:
                self.mcid_lang[mcid] = lang
            mark = _RichMark(mcid, mark.replaced, mark.artifact, lang)
        value = props.get("E") if expand else None
        if not isinstance(value, String):
            value = props.get("ActualText")
        if mark.mcid is not None and not mark.artifact and not mark.replaced and isinstance(value, String):
            budget.charge("memory_size", 64 + 4 * len(value))
            self.mcid_replacement[mark.mcid] = value.text
            return _RichMark(mark.mcid, replaced=True, artifact=False, lang=lang)
        return mark

    def _record_non_mark_op(  # noqa: PLR0913 - records one content operation into MCID tables.
        self,
        index: int,
        op: object,
        mark: _RichMark,
        *,
        resources: Dictionary | None,
        op_mcid: dict[int, int],
        op_artifact: set[int],
        mcid_ops: dict[int, list[int]],
    ) -> None:
        operator = op.operator if isinstance(op, Operation) else None
        if operator in _TEXT_SHOW:
            if mark.artifact:
                self.has_artifact_ops = True
                op_artifact.add(index)
            elif mark.mcid is not None:
                op_mcid[index] = mark.mcid
                mcid_ops.setdefault(mark.mcid, []).append(index)
            return
        if (
            isinstance(op, Operation)
            and mark.mcid is not None
            and not mark.artifact
            and not mark.replaced
            and operator == "Do"
            and _is_form(resources, op.operands)
        ):
            self.incomplete_mcids.add(mark.mcid)
        elif mark.artifact:
            self.has_artifact_ops = True

    def _scan_operations(
        self, page: Page, budget: _core._OperationBudget, *, expand: bool
    ) -> tuple[dict[int, int], set[int], dict[int, list[int]]]:
        resources = _resources(page)
        properties = resources.get("Properties") if resources is not None else None
        stack = [_RichMark()]
        op_mcid: dict[int, int] = {}
        op_artifact: set[int] = set()
        mcid_ops: dict[int, list[int]] = {}
        for index, op in enumerate(page.operations):
            budget.poll()
            mark = stack[-1]
            if isinstance(op, Operation) and op.operator in ("BDC", "BMC"):
                if len(stack) >= _MAX_STRUCTURE_DEPTH:
                    raise _depth_limit_error()
                budget.charge("memory_size", 128)
                stack.append(self._open_rich_mark(mark, op, properties, budget, expand=expand))
            elif isinstance(op, Operation) and op.operator == "EMC":
                if len(stack) == 1:
                    msg = "unmatched EMC in tagged page content"
                    raise MalformedPdfError(msg)
                stack.pop()
                budget.release("memory_size", 128)
            else:
                self._record_non_mark_op(
                    index,
                    op,
                    mark,
                    resources=resources,
                    op_mcid=op_mcid,
                    op_artifact=op_artifact,
                    mcid_ops=mcid_ops,
                )
        if len(stack) != 1:
            msg = "unclosed marked-content sequence on tagged page"
            raise MalformedPdfError(msg)
        return op_mcid, op_artifact, mcid_ops

    def _record_line_chars(  # noqa: PLR0913 - per-line scan helper over core layout tuples.
        self,
        line: _CoreLine,
        line_key: tuple[int, int],
        *,
        op_mcid: dict[int, int],
        op_artifact: set[int],
        op_pieces: dict[int, list[_TextPiece]],
        budget: _core._OperationBudget,
    ) -> None:
        pending_space_mcid: int | None = None
        last_mcid: int | None = None
        for span in line[1]:
            flags = span[5]
            bold = bool(flags & 16)
            italic = bool(flags & 2)
            for char_text, char_box, _, is_synthetic, source in span[1][1]:
                if is_synthetic:
                    if last_mcid is not None:
                        pending_space_mcid = last_mcid
                    continue
                op_idx = source[1] if source is not None else None
                mcid = op_mcid.get(op_idx) if op_idx is not None else None
                self._count_char(char_text, op_idx, mcid, op_artifact)
                if op_idx is not None and mcid is not None:
                    prefix = " " if pending_space_mcid == mcid else ""
                    rect = Rect(*char_box)
                    self._append_op_char(
                        op_pieces,
                        op_idx,
                        prefix + char_text,
                        line_key=line_key,
                        rect=rect,
                        bold=bold,
                        italic=italic,
                        budget=budget,
                    )
                    self.mcid_bbox[mcid] = _union_rect(self.mcid_bbox.get(mcid), rect) or rect
                pending_space_mcid = None
                last_mcid = mcid

    def _append_op_char(  # noqa: PLR0913 - coalesces adjacent chars of one operation and style.
        self,
        op_pieces: dict[int, list[_TextPiece]],
        op_idx: int,
        text: str,
        *,
        line_key: tuple[int, int],
        rect: Rect,
        bold: bool,
        italic: bool,
        budget: _core._OperationBudget,
    ) -> None:
        pieces = op_pieces.setdefault(op_idx, [])
        if pieces:
            prev = pieces[-1]
            if prev.line_key == line_key and prev.bold == bold and prev.italic == italic:
                budget.charge("memory_size", 4 * len(text))
                pieces[-1] = _TextPiece(
                    prev.text + text,
                    line_key,
                    _union_rect(prev.bbox, rect),
                    bold=bold,
                    italic=italic,
                )
                return
        budget.charge("memory_size", 64 + 4 * len(text))
        pieces.append(_TextPiece(text, line_key, rect, bold=bold, italic=italic))

    def _count_char(self, char_text: str, op_idx: int | None, mcid: int | None, op_artifact: set[int]) -> None:
        non_ws = _non_ws_count(char_text)
        if not non_ws:
            return
        if op_idx in op_artifact:
            self.artifact_char_count += non_ws
        elif op_idx is None:
            self.external_char_count += non_ws
        else:
            self.page_stream_char_count += non_ws
            if mcid is not None:
                self.mcid_non_ws_counts[mcid] = self.mcid_non_ws_counts.get(mcid, 0) + non_ws

    def _scan_layout(
        self,
        page: Page,
        op_mcid: dict[int, int],
        op_artifact: set[int],
        mcid_ops: dict[int, list[int]],
        budget: _core._OperationBudget,
    ) -> tuple[float, float, TextPageDict, list[Word]]:
        options = text_options(
            clip=None,
            clipped=False,
            ligatures=True,
            whitespace=True,
            dehyphenate=False,
            unmapped="skip",
            max_glyphs=_DEFAULT_MAX_TEXT_GLYPHS,
            max_size=_DEFAULT_MAX_TEXT_SIZE,
        )
        width, height, blocks, _ = page._layout(  # noqa: SLF001
            options, chars=True, max_glyphs=_DEFAULT_MAX_TEXT_GLYPHS, max_size=_DEFAULT_MAX_TEXT_SIZE
        )
        op_pieces: dict[int, list[_TextPiece]] = {}
        for block_index, block in enumerate(blocks):
            budget.poll()
            for line_index, line in enumerate(block[1]):
                self._record_line_chars(
                    line,
                    (block_index, line_index),
                    op_mcid=op_mcid,
                    op_artifact=op_artifact,
                    op_pieces=op_pieces,
                    budget=budget,
                )
        for mcid, ops in mcid_ops.items():
            combined = [piece for op_idx in ops for piece in op_pieces.get(op_idx, ())]
            if combined:
                self.mcid_pieces[mcid] = combined
        text_blocks = [
            (
                block_box,
                [
                    (
                        line_box,
                        [
                            (span_box, (span_text[0], []), size, origin, font, flags, style, squad)
                            for span_box, span_text, size, origin, font, flags, style, squad in spans
                        ],
                        line_words,
                        direction,
                        wmode,
                        lquad,
                    )
                    for line_box, spans, line_words, direction, wmode, lquad in lines
                ],
            )
            for block_box, lines in blocks
        ]
        layout_dict = build_layout(width, height, text_blocks).to_dict()
        words = build_words(blocks)
        return width, height, layout_dict, words

    def graphic_bbox(self, mcid: int, budget: _core._OperationBudget) -> Rect | None:
        """Return the display-space bounding box of painted graphics/images tagged with ``mcid``."""
        if self._graphic_bboxes is None:
            bboxes: dict[int, Rect] = {}
            for item in self.page.content(text_runs=False):
                budget.poll()
                for mark in item.context.marked_content:
                    if mark.mcid is not None:
                        budget.charge("memory_size", 64)
                        bboxes[mark.mcid] = _union_rect(bboxes.get(mark.mcid), item.bbox) or item.bbox
            self._graphic_bboxes = bboxes
        return self._graphic_bboxes.get(mcid)

    def mcid_content(self, mcid: int) -> tuple[list[_TextPiece] | None, Rect | None, str | None]:
        """Return ``(pieces_or_None_if_incomplete, bbox, lang)`` for one page-stream MCID."""
        bbox = self.mcid_bbox.get(mcid)
        lang = self.mcid_lang.get(mcid)
        if mcid in self.mcid_replacement:
            text = self.mcid_replacement[mcid]
            return ([_TextPiece(text, None, bbox)], bbox, lang)
        if mcid in self.incomplete_mcids:
            return (None, bbox, lang)
        return (self.mcid_pieces.get(mcid, []), bbox, lang)


@dataclass(frozen=True, slots=True)
class _Fragment:
    text: str
    md: str
    line_key: tuple[int, int] | None
    bbox: Rect | None


def _needs_fragment_space(prev: _Fragment, curr: _Fragment) -> bool:
    """Return whether a word space should join ``prev`` and ``curr``."""
    from pyropdf._markdown import _WORD_GAP, _joins_without_space  # noqa: PLC0415

    if not prev.text or not curr.text:
        return False
    if prev.text[-1].isspace() or curr.text[0].isspace():
        return False
    if prev.line_key != curr.line_key or prev.line_key is None or curr.line_key is None:
        return not (_joins_without_space(prev.text[-1]) and _joins_without_space(curr.text[0]))
    if prev.bbox is None or curr.bbox is None:
        return False
    height = max(prev.bbox.height, curr.bbox.height, 1.0)
    gap = curr.bbox.x0 - prev.bbox.x1 if curr.bbox.x0 >= prev.bbox.x0 else prev.bbox.x0 - curr.bbox.x1
    return gap > height * _WORD_GAP


def _join_fragments(fragments: list[_Fragment], *, preserve_ws: bool) -> tuple[str, str]:
    """Join inline fragments into ``(plain_text, markdown_text)``."""
    if not fragments:
        return "", ""
    if preserve_ws:
        plain_parts: list[str] = []
        prev_line = fragments[0].line_key
        for frag in fragments:
            if plain_parts and frag.line_key is not None and prev_line is not None and frag.line_key != prev_line:
                plain_parts.append("\n")
            plain_parts.append(frag.text)
            if frag.line_key is not None:
                prev_line = frag.line_key
        raw = "".join(plain_parts).strip("\n")
        return raw, raw

    plain_out: list[str] = []
    md_out: list[str] = []
    prev: _Fragment | None = None
    for frag in fragments:
        if not frag.text:
            continue
        if prev is not None and _needs_fragment_space(prev, frag):
            plain_out.append(" ")
            md_out.append(" ")
        plain_out.append(frag.text)
        md_out.append(frag.md)
        prev = frag
    plain = re.sub(r"[ \t\r\n]+", " ", "".join(plain_out)).strip()
    md = re.sub(r"[ \t\r\n]+", " ", "".join(md_out)).strip()
    return plain, md


@dataclass(slots=True)
class _TextCollector:
    """Accumulates inline text fragments for one block, splitting cleanly across pages."""

    kind: Literal["heading", "paragraph", "list_item", "blockquote", "code"]
    level: int | None = None
    raw_section_depth: int | None = None
    language: str | None = None
    default_page: int | None = None
    preserve_ws: bool = False
    allow_emphasis: bool = True
    by_page: dict[int, list[_Fragment]] = field(default_factory=dict)
    bboxes: dict[int, Rect] = field(default_factory=dict)
    langs: dict[int, str] = field(default_factory=dict)
    page_order: list[int] = field(default_factory=list)

    def add_fragment(
        self,
        page: int | None,
        fragment: _Fragment,
        *,
        bbox: Rect | None = None,
        language: str | None = None,
    ) -> None:
        target = self.default_page if page is None else page
        if target is None:
            return
        if target not in self.by_page:
            self.by_page[target] = []
            self.page_order.append(target)
        self.by_page[target].append(fragment)
        if bbox is not None:
            self.bboxes[target] = _union_rect(self.bboxes.get(target), bbox) or bbox
        if language is not None and target not in self.langs:
            self.langs[target] = language

    def has_content(self) -> bool:
        return any(frags for frags in self.by_page.values())

    def drain_blocks(self) -> list[StructureBlock]:
        blocks: list[StructureBlock] = []
        for page in self.page_order:
            plain, md = _join_fragments(self.by_page[page], preserve_ws=self.preserve_ws)
            if not plain:
                continue
            bbox = self.bboxes.get(page, _ZERO_RECT)
            lang = self.langs.get(page, self.language)
            rendered_md = self._render_markdown(plain, md)
            blocks.append(
                StructureBlock(
                    kind=self.kind,
                    page=page,
                    bbox=bbox,
                    text=plain,
                    markdown=rendered_md,
                    level=self.level,
                    language=lang,
                    raw_section_depth=self.raw_section_depth,
                )
            )
        self.by_page.clear()
        self.bboxes.clear()
        self.langs.clear()
        self.page_order.clear()
        return blocks

    def _render_markdown(self, plain: str, md: str) -> str:
        if self.kind == "heading":
            return f"{'#' * (self.level or 1)} {plain}"
        if self.kind == "blockquote":
            return format_blockquote(md)
        if self.kind == "code":
            return format_code_block(plain)
        return md


@dataclass(slots=True)
class _CellEntry:
    text: str
    col_span: int
    row_span: int
    page: int | None
    bbox: Rect | None


@dataclass(slots=True)
class _TableState:
    page: int | None
    language: str | None
    section: Literal["head", "body", "foot"] = "body"
    head_rows: list[list[_CellEntry]] = field(default_factory=list)
    body_rows: list[list[_CellEntry]] = field(default_factory=list)
    foot_rows: list[list[_CellEntry]] = field(default_factory=list)
    current_row: list[_CellEntry] | None = None
    cell_count: int = 0
    bbox_by_page: dict[int, Rect] = field(default_factory=dict)
    captions_after: list[StructureBlock] = field(default_factory=list)

    def has_rows(self) -> bool:
        return bool(self.head_rows or self.body_rows or self.foot_rows)

    def start_row(self) -> None:
        self.current_row = []
        target = {"head": self.head_rows, "body": self.body_rows, "foot": self.foot_rows}[self.section]
        target.append(self.current_row)

    def add_cell(self, entry: _CellEntry) -> None:
        self.cell_count += entry.col_span * entry.row_span
        if self.cell_count > _MAX_TABLE_CELLS:
            msg = f"structure table exceeds {_MAX_TABLE_CELLS} cells"
            raise LimitError(_CODE_TABLE_CELL_COUNT, msg)
        if self.current_row is None:
            self.start_row()
        assert self.current_row is not None  # noqa: S101
        self.current_row.append(entry)
        if entry.page is not None and entry.bbox is not None:
            self.bbox_by_page[entry.page] = _union_rect(self.bbox_by_page.get(entry.page), entry.bbox) or entry.bbox

    def _target_page(self, all_rows: list[list[_CellEntry]]) -> int | None:
        if self.page is not None:
            return self.page
        return next((cell.page for row in all_rows for cell in row if cell.page is not None), None)

    def build_blocks(self, *, as_table: bool) -> list[StructureBlock]:
        all_rows = [row for row in (*self.head_rows, *self.body_rows, *self.foot_rows) if row]
        target_page = self._target_page(all_rows) if all_rows else None
        if target_page is None:
            return list(self.captions_after)
        if not as_table:
            paragraphs = [
                StructureBlock(
                    kind="paragraph",
                    page=target_page if cell.page is None else cell.page,
                    bbox=cell.bbox or _ZERO_RECT,
                    text=cell.text,
                    markdown=cell.text,
                    language=self.language,
                )
                for row in all_rows
                for cell in row
                if cell.text
            ]
            return [*paragraphs, *self.captions_after]

        rows_grid, anchors = _build_table_grid(all_rows)
        if not rows_grid or not rows_grid[0]:
            return list(self.captions_after)
        bbox = self.bbox_by_page.get(target_page, _ZERO_RECT)
        table_block = StructureBlock(
            kind="table",
            page=target_page,
            bbox=bbox,
            text="",
            markdown="",
            rows=rows_grid,
            cell_anchors=anchors,
            language=self.language,
        )
        return [table_block, *self.captions_after]


def _place_grid_cell(
    grid: list[list[str | None]],
    occupied: list[list[tuple[int, int] | None]],
    r_idx: int,
    c_idx: int,
    cell: _CellEntry,
) -> None:
    """Place one spanned cell into ``grid`` and ``occupied``."""
    for dr in range(cell.row_span):
        rr = r_idx + dr
        while len(grid) <= rr:
            grid.append([])
            occupied.append([])
        needed = c_idx + cell.col_span
        if len(grid[rr]) < needed:
            pad = needed - len(grid[rr])
            grid[rr].extend([None] * pad)
            occupied[rr].extend([None] * pad)
        for dc in range(cell.col_span):
            cc = c_idx + dc
            if dr == 0 and dc == 0:
                grid[rr][cc] = cell.text
            occupied[rr][cc] = (r_idx, c_idx)


def _normalize_grid(
    grid: list[list[str | None]],
    occupied: list[list[tuple[int, int] | None]],
    max_cols: int,
) -> tuple[tuple[tuple[str | None, ...], ...], tuple[int, ...]]:
    """Pad ragged rows to ``max_cols`` and build the flat anchor index table."""
    if len(grid) * max_cols > _MAX_TABLE_CELLS:
        msg = f"structure table exceeds {_MAX_TABLE_CELLS} cells"
        raise LimitError(_CODE_TABLE_CELL_COUNT, msg)
    anchors: list[int] = []
    normalized_rows: list[tuple[str | None, ...]] = []
    for r_idx, row in enumerate(grid):
        if len(row) < max_cols:
            row.extend([""] * (max_cols - len(row)))
            occupied[r_idx].extend([None] * (max_cols - len(occupied[r_idx])))
        for c_idx in range(max_cols):
            occ = occupied[r_idx][c_idx]
            if occ is None:
                if row[c_idx] is None:
                    row[c_idx] = ""
                anchors.append(r_idx * max_cols + c_idx)
            else:
                anchors.append(occ[0] * max_cols + occ[1])
        normalized_rows.append(tuple(row))
    return tuple(normalized_rows), tuple(anchors)


def _build_table_grid(
    all_rows: list[list[_CellEntry]],
) -> tuple[tuple[tuple[str | None, ...], ...], tuple[int, ...]]:
    """Expand ``ColSpan`` and ``RowSpan`` entries into a rectangular grid and anchor map."""
    grid: list[list[str | None]] = []
    occupied: list[list[tuple[int, int] | None]] = []
    max_cols = 0
    for r_idx, row in enumerate(all_rows):
        while len(grid) <= r_idx:
            grid.append([])
            occupied.append([])
        c_idx = 0
        for cell in row:
            while c_idx < len(occupied[r_idx]) and occupied[r_idx][c_idx] is not None:
                c_idx += 1
            _place_grid_cell(grid, occupied, r_idx, c_idx, cell)
            max_cols = max(max_cols, c_idx + cell.col_span)
            c_idx += cell.col_span
    if max_cols == 0:
        return (), ()
    return _normalize_grid(grid, occupied, max_cols)


@dataclass(slots=True)
class _ListState:
    depth: int
    ordered: bool
    item_count: int = 0


@dataclass(slots=True)
class _ListItemState:
    depth: int
    ordered: bool
    ordinal: int
    language: str | None
    default_page: int | None
    label: _TextCollector = field(default_factory=lambda: _TextCollector(kind="paragraph", allow_emphasis=False))
    body: _TextCollector = field(default_factory=lambda: _TextCollector(kind="list_item"))
    flushed: bool = False


def _format_list_item_text(label_text: str, body_plain: str, body_md: str, item: _ListItemState) -> tuple[str, str]:
    """Combine list label and body into Markdown list item ``(plain, markdown)``."""
    from pyropdf._markdown import _normalize_list_item  # noqa: PLC0415

    indent = "  " * item.depth
    lbl = label_text.strip()
    if lbl:
        if lbl in _BULLET_LABELS:
            prefix = f"{indent}- "
        elif (matched := _NUMBERED_LABEL_RE.match(lbl)) is not None:
            prefix = f"{indent}{matched.group(1)}. "
        elif item.ordered:
            prefix = f"{indent}{item.ordinal}. {lbl} "
        else:
            prefix = f"{indent}- {lbl} "
        return (prefix + body_plain).rstrip(), (prefix + body_md).rstrip()
    normalized_plain = _normalize_list_item(body_plain)
    if normalized_plain is not None:
        normalized_md = _normalize_list_item(body_md) or normalized_plain
        return indent + normalized_plain, indent + normalized_md
    prefix = f"{indent}{item.ordinal}. " if item.ordered else f"{indent}- "
    return (prefix + body_plain).rstrip(), (prefix + body_md).rstrip()


@dataclass(slots=True)
class _Frame:
    element: StructureElement | None
    role: str
    language: str | None
    page: int | None
    items: Iterator[StructureItem]
    section_depth: int
    collector: _TextCollector | None = None
    table: _TableState | None = None
    cell: tuple[int, int, list[_TextCollector]] | None = None
    list_state: _ListState | None = None
    list_item: _ListItemState | None = None
    in_label: bool = False
    in_blockquote: bool = False
    table_caption_after: _TableState | None = None
    strong: int = 0
    em: int = 0
    code: int = 0


def _record_scanned_leaf(
    child: MarkedContentReference | ObjectReference,
    elem_page: int | None,
    mcids: list[tuple[int, int]],
) -> tuple[int | None, bool]:
    pg = elem_page if child.page is None else child.page
    if isinstance(child, MarkedContentReference):
        if child.stream_xref is not None:
            return pg, True
        if pg is not None:
            mcids.append((pg, child.mcid))
    return pg, False


def _scan_subtree_mcids(
    root_elem: StructureElement,
    root_page: int | None,
    budget: _core._OperationBudget,
    seen_global: set[int],
    *,
    skip_caption_children: bool = False,
) -> tuple[list[tuple[int, int]], int | None, bool, list[StructureElement]]:
    """Collect ``(page, mcid)`` pairs under ``root_elem`` iteratively without emitting blocks."""
    mcids: list[tuple[int, int]] = []
    captions: list[StructureElement] = []
    first_page = root_page
    has_form_ref = False
    stack: list[tuple[StructureElement, int | None, int]] = [(root_elem, root_page, 1)]
    visited_count = 0
    while stack:
        elem, elem_page, depth = stack.pop()
        for child in elem._iter_items(budget):  # noqa: SLF001
            budget.poll()
            visited_count += 1
            if visited_count > _DEFAULT_MAX_ELEMENTS:
                msg = f"structure interpretation exceeds {_DEFAULT_MAX_ELEMENTS} child entries"
                raise LimitError(_CODE_OBJECT_COUNT, msg)
            if isinstance(child, StructureElement):
                if skip_caption_children and depth == 1 and _resolved_export_role(child, budget) == "Caption":
                    captions.append(child)
                    continue
                if child.xref in seen_global:
                    msg = "structure /K contains a cycle or a repeated element"
                    raise MalformedPdfError(msg)
                if depth >= _MAX_STRUCTURE_DEPTH:
                    raise _depth_limit_error()
                seen_global.add(child.xref)
                child_pg = _page_index_from_dict(child.document, child.obj.get("Pg"))
                next_pg = elem_page if child_pg is None else child_pg
                first_page = next_pg if first_page is None else first_page
                stack.append((child, next_pg, depth + 1))
            else:
                pg, is_form_ref = _record_scanned_leaf(child, elem_page, mcids)
                first_page = pg if first_page is None else first_page
                has_form_ref |= is_form_ref
    return mcids, first_page, has_form_ref, captions


class _StructureExportWalker:
    """Iterative structure-tree walker that builds per-page semantic blocks."""

    def __init__(
        self,
        tree: StructureTree,
        page_map: dict[int, Page],
        budget: _core._OperationBudget,
        *,
        expand: bool,
        table_strategy: Literal["lines", "text"] | None,
    ) -> None:
        self.tree = tree
        self.page_map = page_map
        self.budget = budget
        self.expand = expand
        self.table_strategy = table_strategy
        self.page_indexes: dict[int, _PageMcidIndex] = {}
        self.blocks_by_page: dict[int, list[StructureBlock]] = {idx: [] for idx in page_map}
        self.referenced_mcids: dict[int, set[int]] = {idx: set() for idx in page_map}
        self.artifact_mcids: dict[int, set[int]] = {idx: set() for idx in page_map}
        self.unresolved_form_pages: set[int] = set()
        self.form_replacement_pages: set[int] = set()
        self.element_count = 0
        self.seen: set[int] = set()
        self.frames: list[_Frame] = []

    def _page_index(self, page_num: int) -> _PageMcidIndex | None:
        if page_num not in self.page_map:
            return None
        cached = self.page_indexes.get(page_num)
        if cached is None:
            self.budget.charge("memory_size", 256)
            cached = _PageMcidIndex(self.page_map[page_num], self.budget, expand=self.expand)
            self.page_indexes[page_num] = cached
        return cached

    def _emit_block(self, block: StructureBlock) -> None:
        if block.page in self.blocks_by_page:
            self.budget.charge("memory_size", 160 + 4 * len(block.text))
            self.blocks_by_page[block.page].append(block)

    def _active_collector(self) -> _TextCollector | None:
        for frame in reversed(self.frames):
            if frame.cell is not None:
                return frame.cell[2][-1]
            if frame.in_label and frame.list_item is not None:
                return frame.list_item.label
            if frame.list_item is not None and not frame.list_item.flushed and frame.collector is None:
                return frame.list_item.body
            if frame.collector is not None:
                return frame.collector
        return None

    def _active_table(self) -> _TableState | None:
        for frame in reversed(self.frames):
            if frame.table is not None:
                return frame.table
        return None

    def _active_list(self) -> _ListState | None:
        for frame in reversed(self.frames):
            if frame.list_state is not None:
                return frame.list_state
        return None

    def _active_list_item(self) -> _ListItemState | None:
        for frame in reversed(self.frames):
            if frame.list_item is not None:
                return frame.list_item
        return None

    def _flush_enclosing_text(self) -> None:
        """Flush any active paragraph or list-item text before starting a nested block."""
        for frame in reversed(self.frames):
            if frame.cell is not None:
                return
            if frame.list_item is not None and not frame.list_item.flushed:
                self._flush_list_item(frame.list_item)
                return
            if frame.collector is not None and frame.collector.kind == "paragraph":
                for block in frame.collector.drain_blocks():
                    self._emit_block(block)
                return

    def _flush_list_item(self, item: _ListItemState) -> None:
        if item.flushed:
            return
        item.flushed = True
        lbl_blocks = item.label.drain_blocks()
        label_text = " ".join(b.text for b in lbl_blocks).strip()
        label_page = lbl_blocks[0].page if lbl_blocks else None
        label_bbox = lbl_blocks[0].bbox if lbl_blocks else None
        if not item.body.has_content():
            if label_text and label_page is not None:
                plain, md = _format_list_item_text("", label_text, label_text, item)
                self._emit_block(
                    StructureBlock(
                        kind="list_item",
                        page=label_page,
                        bbox=label_bbox or _ZERO_RECT,
                        text=plain,
                        markdown=md,
                        language=item.language,
                    )
                )
            return
        first = True
        for page in item.body.page_order:
            body_plain, body_md = _join_fragments(item.body.by_page[page], preserve_ws=False)
            if not body_plain:
                continue
            plain, md = _format_list_item_text(label_text if first else "", body_plain, body_md, item)
            bbox = item.body.bboxes.get(page)
            if first and label_page == page:
                bbox = _union_rect(label_bbox, bbox)
            self._emit_block(
                StructureBlock(
                    kind="list_item",
                    page=page,
                    bbox=bbox or _ZERO_RECT,
                    text=plain,
                    markdown=md,
                    language=item.body.langs.get(page, item.language),
                )
            )
            first = False
        item.body.by_page.clear()
        item.body.bboxes.clear()
        item.body.langs.clear()
        item.body.page_order.clear()

    def _append_inline_text(  # noqa: PLR0913 - inline fragment with provenance and style.
        self,
        text: str,
        page: int | None,
        *,
        line_key: tuple[int, int] | None,
        bbox: Rect | None,
        language: str | None,
        bold: bool = False,
        italic: bool = False,
    ) -> None:
        if not text:
            return
        frame = self.frames[-1]
        collector = self._active_collector()
        if collector is None:
            collector = _TextCollector(
                kind="blockquote" if frame.in_blockquote else "paragraph",
                language=language,
                default_page=page,
            )
            frame.collector = collector
        eff_bold = (bold or frame.strong > 0) and collector.allow_emphasis
        eff_italic = (italic or frame.em > 0) and collector.allow_emphasis
        if frame.code > 0 and collector.allow_emphasis:
            md = format_inline_code(text)
        elif eff_bold or eff_italic:
            md = _wrap_inline_emphasis(text, bold=eff_bold, italic=eff_italic)
        else:
            md = text
        collector.add_fragment(page, _Fragment(text, md, line_key, bbox), bbox=bbox, language=language)

    def _handle_figure(
        self,
        elem: StructureElement,
        page: int | None,
        lang: str | None,
        mcids: list[tuple[int, int]],
        *,
        has_form_ref: bool,
    ) -> None:
        alt = elem.alt
        desc = (
            elem.expanded
            if self.expand and elem.expanded is not None
            else elem.actual_text
            if elem.actual_text is not None
            else alt
        )
        fig_page = page
        fig_bbox: Rect | None = None
        for pg, mcid in mcids:
            if fig_page is None:
                fig_page = pg
            if pg in self.referenced_mcids:
                self.referenced_mcids[pg].add(mcid)
            pidx = self._page_index(pg)
            if pidx is not None:
                fig_bbox = _union_rect(fig_bbox, pidx.mcid_bbox.get(mcid))
                fig_bbox = _union_rect(fig_bbox, pidx.graphic_bbox(mcid, self.budget))
        if has_form_ref and fig_page is not None and desc is not None:
            self.form_replacement_pages.add(fig_page)
        if elem.actual_text is not None and alt is None and self._active_collector() is not None:
            self._append_inline_text(desc or "", fig_page, line_key=None, bbox=fig_bbox, language=lang)
            return
        if fig_page is None:
            return
        self._flush_enclosing_text()
        final_text = desc or ""
        rendered_md = f"![{escape_markdown_alt(final_text)}]()" if final_text else ""
        self._emit_block(
            StructureBlock(
                kind="image",
                page=fig_page,
                bbox=fig_bbox or _ZERO_RECT,
                text=final_text,
                markdown=rendered_md,
                alt_text=alt,
                language=lang,
            )
        )

    def _replacement_block(  # noqa: PLR0913 - constructs a standalone replaced block.
        self,
        elem: StructureElement,
        role: str,
        repl: str,
        *,
        page: int,
        bbox: Rect | None,
        lang: str | None,
        section_depth: int,
    ) -> StructureBlock | None:
        heading_level = _heading_level(role)
        if heading_level is not None or role == "H":
            lvl = heading_level if heading_level is not None else 1
            cleaned = repl.strip()
            return StructureBlock(
                kind="heading",
                page=page,
                bbox=bbox or _ZERO_RECT,
                text=cleaned,
                markdown=f"{'#' * lvl} {cleaned}",
                level=lvl,
                language=lang,
                raw_section_depth=section_depth if role == "H" else None,
            )
        standalone = self._active_collector() is None and self._active_table() is None
        if role == "Code" and standalone:
            return StructureBlock(
                kind="code",
                page=page,
                bbox=bbox or _ZERO_RECT,
                text=repl,
                markdown=format_code_block(repl),
                language=lang,
            )
        if role == "BlockQuote" and standalone:
            cleaned = repl.strip()
            return StructureBlock(
                kind="blockquote",
                page=page,
                bbox=bbox or _ZERO_RECT,
                text=cleaned,
                markdown=format_blockquote(cleaned),
                language=lang,
            )
        if (role == "Formula" and standalone) or (
            role == "P" and self._active_table() is None and self._active_list_item() is None
        ):
            cleaned = repl.strip()
            return StructureBlock(
                kind="paragraph",
                page=page,
                bbox=bbox or _ZERO_RECT,
                text=cleaned,
                markdown=cleaned,
                alt_text=elem.alt if role == "Formula" else None,
                language=lang,
            )
        return None

    def _apply_replacement(  # noqa: PLR0913 - dispatches replacement text into table, list, block, or inline context.
        self,
        elem: StructureElement,
        role: str,
        repl: str,
        *,
        page: int | None,
        bbox: Rect | None,
        lang: str | None,
        section_depth: int,
    ) -> None:
        if role in {"TH", "TD"} and (active_table := self._active_table()) is not None:
            col_span, row_span = _table_cell_spans(elem)
            active_table.add_cell(
                _CellEntry(text=repl.strip(), col_span=col_span, row_span=row_span, page=page, bbox=bbox)
            )
            return
        if role == "Lbl" and (li := self._active_list_item()) is not None and not li.flushed:
            li.label.add_fragment(page, _Fragment(repl, repl, None, bbox), bbox=bbox, language=lang)
            return
        if role == "LBody" and (li := self._active_list_item()) is not None and not li.flushed:
            li.body.add_fragment(page, _Fragment(repl, repl, None, bbox), bbox=bbox, language=lang)
            return
        if role == "LI" and page is not None:
            self._flush_enclosing_text()
            enclosing_list = self._active_list()
            if enclosing_list is not None:
                enclosing_list.item_count += 1
                depth, ordered, ordinal = enclosing_list.depth, enclosing_list.ordered, enclosing_list.item_count
            else:
                depth, ordered, ordinal = 0, False, 1
            item_state = _ListItemState(depth=depth, ordered=ordered, ordinal=ordinal, language=lang, default_page=page)
            cleaned = repl.strip()
            plain, md = _format_list_item_text("", cleaned, cleaned, item_state)
            self._emit_block(
                StructureBlock(
                    kind="list_item", page=page, bbox=bbox or _ZERO_RECT, text=plain, markdown=md, language=lang
                )
            )
            return
        block = (
            self._replacement_block(elem, role, repl, page=page, bbox=bbox, lang=lang, section_depth=section_depth)
            if page is not None
            else None
        )
        if block is not None:
            self._flush_enclosing_text()
            self._emit_block(block)
        else:
            self._append_inline_text(repl, page, line_key=None, bbox=bbox, language=lang)

    def _handle_replacement_element(
        self,
        elem: StructureElement,
        role: str,
        page: int | None,
        lang: str | None,
        section_depth: int,
    ) -> bool:
        """Handle elements that replace their subtree via ``/E``, ``/ActualText``, or ``/Alt``."""
        if role == "Figure":
            mcids, resolved_page, has_form_ref, captions = _scan_subtree_mcids(
                elem, page, self.budget, self.seen, skip_caption_children=elem.actual_text is None
            )
            self._handle_figure(elem, resolved_page, lang, mcids, has_form_ref=has_form_ref)
            if captions:
                parent = self.frames[-1]
                self.frames.append(
                    _Frame(
                        element=elem,
                        role="Figure",
                        language=lang,
                        page=resolved_page,
                        items=iter(captions),
                        section_depth=parent.section_depth,
                    )
                )
            return True
        repl = _replacement(elem, expand=self.expand)
        if repl is None:
            return False
        mcids, resolved_page, has_form_ref, _ = _scan_subtree_mcids(elem, page, self.budget, self.seen)
        bbox: Rect | None = None
        for pg, mcid in mcids:
            if pg in self.referenced_mcids:
                self.referenced_mcids[pg].add(mcid)
            pidx = self._page_index(pg)
            if pidx is not None:
                bbox = _union_rect(bbox, pidx.mcid_bbox.get(mcid))
        if has_form_ref and resolved_page is not None:
            self.form_replacement_pages.add(resolved_page)
        if not repl and role not in {"TH", "TD"}:
            return True
        self._apply_replacement(elem, role, repl, page=resolved_page, bbox=bbox, lang=lang, section_depth=section_depth)
        return True

    def _enter_element(self, elem: StructureElement) -> None:
        parent = self.frames[-1]
        self.element_count += 1
        if self.element_count > _DEFAULT_MAX_ELEMENTS:
            msg = f"structure interpretation exceeds {_DEFAULT_MAX_ELEMENTS} child entries"
            raise LimitError(_CODE_OBJECT_COUNT, msg)
        if elem.xref in self.seen:
            msg = "structure /K contains a cycle or a repeated element"
            raise MalformedPdfError(msg)
        if len(self.frames) >= _MAX_STRUCTURE_DEPTH:
            raise _depth_limit_error()
        self.budget.charge("memory_size", 512)
        self.seen.add(elem.xref)

        role = _resolved_export_role(elem, self.budget)
        lang = elem.language or parent.language
        elem_pg = _page_index_from_dict(elem.document, elem.obj.get("Pg"))
        page = parent.page if elem_pg is None else elem_pg

        if role == "Artifact":
            mcids, _, _, _ = _scan_subtree_mcids(elem, page, self.budget, self.seen)
            for pg, mcid in mcids:
                if pg in self.artifact_mcids:
                    self.artifact_mcids[pg].add(mcid)
            return

        if self._handle_replacement_element(elem, role, page, lang, parent.section_depth):
            return

        frame = self._make_frame(elem, role, lang, page, parent)
        self.frames.append(frame)

    def _configure_table_frame(self, frame: _Frame, elem: StructureElement, active_table: _TableState) -> bool:
        role = frame.role
        section_map: dict[str, Literal["head", "body", "foot"]] = {
            "THead": "head",
            "TBody": "body",
            "TFoot": "foot",
        }
        if role in section_map:
            active_table.section = section_map[role]
            return True
        if role == "TR":
            active_table.start_row()
            return True
        if role in {"TH", "TD"}:
            col_span, row_span = _table_cell_spans(elem)
            cell_collector = _TextCollector(
                kind="paragraph", language=frame.language, default_page=frame.page, allow_emphasis=False
            )
            frame.cell = (col_span, row_span, [cell_collector])
            return True
        if role == "Caption" and not any(ancestor.cell is not None for ancestor in self.frames):
            frame.collector = _TextCollector(kind="paragraph", language=frame.language, default_page=frame.page)
            if active_table.has_rows():
                frame.table_caption_after = active_table
            return True
        if role in _PARAGRAPH_ROLES:
            for ancestor in reversed(self.frames):
                if ancestor.cell is not None:
                    ancestor.cell[2].append(
                        _TextCollector(
                            kind="paragraph", language=frame.language, default_page=frame.page, allow_emphasis=False
                        )
                    )
                    return True
        return False

    def _configure_list_frame(self, frame: _Frame, elem: StructureElement) -> bool:
        role = frame.role
        if role == "L":
            self._flush_enclosing_text()
            enclosing_list = self._active_list()
            depth = 0 if enclosing_list is None else enclosing_list.depth + 1
            frame.list_state = _ListState(depth=depth, ordered=_list_is_ordered(elem))
            return True
        if role == "LI":
            self._flush_enclosing_text()
            enclosing_list = self._active_list()
            if enclosing_list is not None:
                enclosing_list.item_count += 1
                depth, ordered, ordinal = enclosing_list.depth, enclosing_list.ordered, enclosing_list.item_count
            else:
                depth, ordered, ordinal = 0, False, 1
            frame.list_item = _ListItemState(
                depth=depth, ordered=ordered, ordinal=ordinal, language=frame.language, default_page=frame.page
            )
            return True
        if role == "Lbl":
            frame.in_label = True
            return True
        return role == "LBody"

    def _configure_block_frame(self, frame: _Frame, parent: _Frame) -> None:
        role, lang, page = frame.role, frame.language, frame.page
        heading_level = _heading_level(role)
        if heading_level is not None or role == "H":
            self._flush_enclosing_text()
            frame.collector = _TextCollector(
                kind="heading",
                level=heading_level or 1,
                raw_section_depth=parent.section_depth if role == "H" else None,
                language=lang,
                default_page=page,
                allow_emphasis=False,
            )
            return
        if role == "BlockQuote":
            self._flush_enclosing_text()
            frame.collector = _TextCollector(kind="blockquote", language=lang, default_page=page)
            return
        if role == "Code" and self._active_collector() is None:
            self._flush_enclosing_text()
            frame.code = parent.code
            frame.collector = _TextCollector(
                kind="code", language=lang, default_page=page, preserve_ws=True, allow_emphasis=False
            )
            return
        if role in _PARAGRAPH_ROLES or role == "Formula":
            li = self._active_list_item()
            if parent.in_blockquote and parent.collector is not None and parent.collector.kind == "blockquote":
                frame.collector = _TextCollector(kind="blockquote", language=lang, default_page=page)
            elif li is not None and not li.flushed:
                if li.body.has_content():
                    self._flush_list_item(li)
                    frame.collector = _TextCollector(kind="paragraph", language=lang, default_page=page)
            else:
                self._flush_enclosing_text()
                frame.collector = _TextCollector(kind="paragraph", language=lang, default_page=page)

    def _make_frame(
        self, elem: StructureElement, role: str, lang: str | None, page: int | None, parent: _Frame
    ) -> _Frame:
        section_depth = parent.section_depth + (1 if role in _SECTION_ROLES else 0)
        frame = _Frame(
            element=elem,
            role=role,
            language=lang,
            page=page,
            items=elem._iter_items(self.budget),  # noqa: SLF001
            section_depth=section_depth,
            in_blockquote=parent.in_blockquote or role == "BlockQuote",
            strong=parent.strong + (1 if role == "Strong" else 0),
            em=parent.em + (1 if role == "Em" else 0),
            code=parent.code + (1 if role == "Code" else 0),
        )
        active_table = self._active_table()
        if active_table is not None and self._configure_table_frame(frame, elem, active_table):
            return frame
        if role == "Table":
            self._flush_enclosing_text()
            frame.table = _TableState(page=page, language=lang)
            return frame
        if self._configure_list_frame(frame, elem):
            return frame
        self._configure_block_frame(frame, parent)
        return frame

    def _finish_cell(self, frame: _Frame) -> None:
        assert frame.cell is not None  # noqa: S101
        col_span, row_span, collectors = frame.cell
        table = self._active_table()
        if table is None:
            return
        parts: list[str] = []
        cell_page: int | None = frame.page
        cell_bbox: Rect | None = None
        for collector in collectors:
            for blk in collector.drain_blocks():
                parts.append(blk.text)
                if cell_page is None:
                    cell_page = blk.page
                if blk.bbox != _ZERO_RECT:
                    cell_bbox = _union_rect(cell_bbox, blk.bbox)
        table.add_cell(
            _CellEntry(
                text="\n".join(parts),
                col_span=col_span,
                row_span=row_span,
                page=cell_page,
                bbox=cell_bbox,
            )
        )

    def _exit_frame(self, frame: _Frame) -> None:
        if frame.cell is not None:
            self._finish_cell(frame)
            return
        if frame.table is not None:
            for block in frame.table.build_blocks(as_table=self.table_strategy is not None):
                self._emit_block(block)
            return
        if frame.list_item is not None:
            self._flush_list_item(frame.list_item)
            return
        if frame.collector is not None:
            drained = frame.collector.drain_blocks()
            if frame.table_caption_after is not None:
                frame.table_caption_after.captions_after.extend(drained)
            else:
                for block in drained:
                    self._emit_block(block)

    def _handle_mcr_leaf(self, frame: _Frame, item: MarkedContentReference) -> None:
        pg = frame.page if item.page is None else item.page
        if pg is None:
            return
        if item.stream_xref is not None:
            if pg in self.page_map:
                self.unresolved_form_pages.add(pg)
            return
        if pg in self.referenced_mcids:
            self.referenced_mcids[pg].add(item.mcid)
        pidx = self._page_index(pg)
        if pidx is None:
            return
        pieces, bbox, mcid_lang = pidx.mcid_content(item.mcid)
        if pieces is None:
            self.unresolved_form_pages.add(pg)
            return
        lang = mcid_lang or frame.language
        for piece in pieces:
            self._append_inline_text(
                piece.text,
                pg,
                line_key=piece.line_key,
                bbox=piece.bbox or bbox,
                language=lang,
                bold=piece.bold,
                italic=piece.italic,
            )

    def _handle_leaf(self, item: MarkedContentReference | ObjectReference) -> None:
        frame = self.frames[-1]
        if isinstance(item, ObjectReference):
            if frame.element is None:
                return
            text = _object_text(frame.element, item)
            if text:
                pg = frame.page if item.page is None else item.page
                self._append_inline_text(text, pg, line_key=None, bbox=None, language=frame.language)
            return
        self._handle_mcr_leaf(frame, item)

    def run(self) -> dict[int, list[StructureBlock]]:
        doc_lang = self.tree._document.language  # noqa: SLF001
        self.frames = [
            _Frame(
                element=None,
                role="StructTreeRoot",
                language=doc_lang,
                page=None,
                items=_root_items(self.tree, self.budget),
                section_depth=0,
            )
        ]
        while self.frames:
            frame = self.frames[-1]
            child = next(frame.items, None)
            if child is None:
                self.frames.pop()
                self._exit_frame(frame)
                continue
            self.budget.poll()
            if isinstance(child, StructureElement):
                self._enter_element(child)
            else:
                self._handle_leaf(child)
        _resolve_unnumbered_headings(self.blocks_by_page)
        return self.blocks_by_page


def _heading_level(role: str) -> int | None:
    """Return the Markdown heading level (1–6) for ``Title`` or ``H1``–``Hn``, else ``None``."""
    if role == "Title":
        return 1
    matched = _HEADING_NUMBER_RE.match(role)
    if matched is None:
        return None
    return min(int(matched.group(1)), 6)


def _resolve_unnumbered_headings(blocks_by_page: dict[int, list[StructureBlock]]) -> None:
    """Normalize unnumbered ``H`` headings from their enclosing section nesting depth."""
    depths = [
        block.raw_section_depth
        for blocks in blocks_by_page.values()
        for block in blocks
        if block.raw_section_depth is not None
    ]
    if not depths:
        return
    has_top_h1 = any(
        block.kind == "heading" and block.raw_section_depth is None and block.level == 1
        for blocks in blocks_by_page.values()
        for block in blocks
    )
    base = 0 if has_top_h1 else min(depths)
    for page_idx, blocks in blocks_by_page.items():
        updated: list[StructureBlock] = []
        changed = False
        for block in blocks:
            if block.raw_section_depth is not None:
                level = max(1, min(6, block.raw_section_depth - base + 1))
                updated.append(
                    StructureBlock(
                        kind=block.kind,
                        page=block.page,
                        bbox=block.bbox,
                        text=block.text,
                        markdown=f"{'#' * level} {block.text}",
                        level=level,
                        language=block.language,
                    )
                )
                changed = True
            else:
                updated.append(block)
        if changed:
            blocks_by_page[page_idx] = updated


def _structure_block_non_ws(blocks: list[StructureBlock]) -> int:
    """Count non-whitespace characters emitted by ``blocks``."""
    total = 0
    for block in blocks:
        if block.kind == "table" and block.rows is not None:
            for row in block.rows:
                for cell in row:
                    if cell:
                        total += _non_ws_count(cell)
        else:
            total += _non_ws_count(block.text)
    return total


def _is_page_trustworthy(
    page_idx: int,
    pidx: _PageMcidIndex,
    blocks: list[StructureBlock],
    walker: _StructureExportWalker,
) -> bool:
    """Decide whether structure extraction for ``page_idx`` passes trustworthiness checks."""
    artifact_mcids = walker.artifact_mcids.get(page_idx, set())
    referenced = walker.referenced_mcids.get(page_idx, set()) - artifact_mcids
    artifact_mcid_chars = sum(pidx.mcid_non_ws_counts.get(mcid, 0) for mcid in artifact_mcids)
    page_stream_chars = max(0, pidx.page_stream_char_count - artifact_mcid_chars)
    external_chars = pidx.external_char_count
    total_non_artifact = page_stream_chars + external_chars
    if total_non_artifact == 0:
        return bool(blocks) or pidx.has_artifact_ops or bool(artifact_mcids)

    structure_chars = _structure_block_non_ws(blocks)
    if not blocks or structure_chars == 0:
        return False

    covered_stream_chars = sum(pidx.mcid_non_ws_counts.get(mcid, 0) for mcid in referenced)
    has_unresolved_form = page_idx in walker.unresolved_form_pages
    if page_stream_chars > 0 and external_chars == 0:
        return (covered_stream_chars / page_stream_chars) >= _MIN_STRUCTURE_COVERAGE
    if page_stream_chars == 0 and external_chars > 0:
        return (not has_unresolved_form) and (page_idx in walker.form_replacement_pages) and structure_chars > 0
    stream_ratio = covered_stream_chars / total_non_artifact
    coverage = (
        stream_ratio if has_unresolved_form else max(stream_ratio, min(1.0, structure_chars / total_non_artifact))
    )
    return coverage >= _MIN_STRUCTURE_COVERAGE


def _collect_structure_pages(  # noqa: PLR0913 - bundles export options under read_operation.
    tree: StructureTree,
    page_map: dict[int, Page],
    budget: _core._OperationBudget,
    *,
    table_strategy: Literal["lines", "text"] | None,
    use_structure: Literal["auto", "always", "never"],
    expand_abbreviations: bool,
) -> dict[int, PageStructureData] | None:
    walker = _StructureExportWalker(
        tree,
        page_map,
        budget,
        expand=expand_abbreviations,
        table_strategy=table_strategy,
    )
    blocks_by_page = walker.run()
    if use_structure == "auto" and walker.element_count == 0:
        return None
    result: dict[int, PageStructureData] = {}
    any_structured = False
    for page_idx in page_map:
        pidx = walker._page_index(page_idx)  # noqa: SLF001
        assert pidx is not None  # noqa: S101
        blocks = blocks_by_page.get(page_idx, [])
        trusted = use_structure == "always" or _is_page_trustworthy(page_idx, pidx, blocks, walker)
        any_structured |= trusted
        result[page_idx] = PageStructureData(
            blocks=blocks if trusted else None,
            width=pidx.width,
            height=pidx.height,
            layout=pidx.layout,
            words=pidx.words,
        )
    if use_structure == "auto" and not any_structured:
        return None
    return result


def extract_structure_pages(
    pages: Sequence[Page],
    *,
    table_strategy: Literal["lines", "text"] | None,
    use_structure: Literal["auto", "always", "never"],
    expand_abbreviations: bool,
) -> dict[int, PageStructureData] | None:
    """Extract per-page structure blocks when enabled and trustworthy, or return ``None`` for pure geometric mode."""
    if not pages or use_structure == "never":
        return None
    document = pages[0].document
    tree = document.structure
    if tree.obj is None:
        return None
    if use_structure == "auto":
        mark_info = document.mark_info
        if mark_info is None or not mark_info.marked or mark_info.suspects:
            return None

    page_map: dict[int, Page] = {}
    for page in pages:
        page_map.setdefault(page.index, page)

    try:
        return _core.read_operation(
            document._core(),  # noqa: SLF001
            lambda budget: _collect_structure_pages(
                tree,
                page_map,
                budget,
                table_strategy=table_strategy,
                use_structure=use_structure,
                expand_abbreviations=expand_abbreviations,
            ),
        )
    except MalformedPdfError:
        if use_structure == "auto":
            return None
        raise
