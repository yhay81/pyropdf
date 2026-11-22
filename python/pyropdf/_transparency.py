"""ISO 32000-2 Annex Q transparency detection on the current store version.

The content parser supplies PDF operations. The walk retains only the pieces
of graphics state Annex Q tests at paint time; it never rasterizes the page.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._errors import LimitError, PdfError
from pyropdf._objects import Array, Dictionary, Name, Stream
from pyropdf._operations import InlineImage, Operation, Operations

if TYPE_CHECKING:
    from pyropdf._page import Page

_FILL = {"f", "F", "f*", "B", "B*", "b", "b*"}
_STROKE = {"S", "s", "B", "B*", "b", "b*"}
_TEXT = {"Tj", "TJ", "'", '"'}
_SELECTORS = {"g", "rg", "k", "cs", "sc", "scn"}
_MAX_DEPTH = 32
_MAX_PAGE_DEPTH = 64


def _named(resources: Dictionary | None, category: str, name: object) -> Dictionary | None:
    table = resources.get(category) if resources is not None else None
    candidate = table.get(str(name)) if isinstance(name, Name) and isinstance(table, Dictionary) else None
    return candidate if isinstance(candidate, Dictionary) else None


def _blending(mode: object) -> bool:
    if isinstance(mode, Array):
        return len(mode) != 1 or mode[0] != "Normal"
    return isinstance(mode, Name) and mode != "Normal"


@dataclass(frozen=True, slots=True)
class _State:
    fill: bool = False
    stroke: bool = False
    blend: bool = False
    mask: bool = False
    fill_pattern: Dictionary | None = None
    stroke_pattern: Dictionary | None = None
    font: Dictionary | None = None
    text_mode: int = 0

    def graphics(self, node: Dictionary | None) -> _State:
        if node is None:
            return self
        fill, stroke = node.get("ca"), node.get("CA")
        font = node.get("Font")
        return replace(
            self,
            fill=fill < 1 if type(fill) is int or isinstance(fill, float) else self.fill,
            stroke=stroke < 1 if type(stroke) is int or isinstance(stroke, float) else self.stroke,
            blend=_blending(node["BM"]) if "BM" in node else self.blend,
            mask=isinstance(node["SMask"], Dictionary) if "SMask" in node else self.mask,
            font=font[0] if isinstance(font, Array) and len(font) and isinstance(font[0], Dictionary) else self.font,
        )

    def select(self, operation: Operation, resources: Dictionary | None) -> _State:
        operator, values = operation.operator, operation.operands
        lower = operator.lower()
        if lower in _SELECTORS:
            pattern = _named(resources, "Pattern", values[-1]) if lower == "scn" and values else None
            return replace(self, stroke_pattern=pattern) if operator.isupper() else replace(self, fill_pattern=pattern)
        if operator == "Tf" and values:
            return replace(self, font=_named(resources, "Font", values[0]))
        if operator == "Tr" and values and type(values[0]) is int:
            return replace(self, text_mode=values[0])
        if operator == "gs" and values:
            return self.graphics(_named(resources, "ExtGState", values[0]))
        return self


class _AnnexQ:
    def __init__(self, budget: _core._OperationBudget) -> None:
        self.budget = budget
        self.active: set[Stream] = set()
        self.done: set[tuple[Stream, Dictionary | None, _State]] = set()

    def page(self, page: Page) -> bool:
        node = page.obj
        resources = node.get("Resources")
        depth = 0
        while not isinstance(resources, Dictionary):
            parent = node.get("Parent")
            if not isinstance(parent, Dictionary):
                resources = None
                break
            depth += 1
            self.budget.check("object_depth", depth)
            self.budget.charge("interpretation_fuel", 1)
            if depth > _MAX_PAGE_DEPTH:
                msg = "page resource inheritance exceeds 64 ancestors"
                code = "object_depth"
                raise LimitError(code, msg)
            node = parent
            resources = node.get("Resources")
        if self.operations(page.operations, resources, _State()):
            return True
        annotations = page.obj.get("Annots")
        if isinstance(annotations, Array):
            for annotation in annotations:
                self.budget.charge("interpretation_fuel", 1)
                if isinstance(annotation, Dictionary) and self.annotation(annotation, resources):
                    return True
        return False

    def annotation(self, annotation: Dictionary, resources: Dictionary | None) -> bool:
        if _blending(annotation.get("BM")):
            return True
        appearances = annotation.get("AP")
        if isinstance(appearances, Dictionary):
            for key in ("N", "R", "D"):
                entry = appearances.get(key)
                if isinstance(entry, Stream):
                    if self.stream(entry, resources, _State()):
                        return True
                elif isinstance(entry, Dictionary):
                    for name in entry:
                        self.budget.charge("interpretation_fuel", 1)
                        if self.stream(entry[name], resources, _State()):
                            return True
        return False

    def stream(self, target: object, resources: Dictionary | None, state: _State) -> bool:
        if not isinstance(target, Stream):
            return False
        group = target.get("Group")
        if isinstance(group, Dictionary) and group.get("S") == "Transparency":
            return True
        if target.get("Subtype") == "Image":
            return isinstance(target.get("SMask"), Stream) or target.get("SMaskInData") in (1, 2)
        own = target.get("Resources")
        environment = own if isinstance(own, Dictionary) else resources
        key = (target, environment, state)
        if key in self.done:
            return False
        if target in self.active:
            msg = "cycle in page transparency resources"
            raise PdfError(msg)
        if len(self.active) >= _MAX_DEPTH:
            msg = "page transparency resources nest deeper than 32"
            code = "object_depth"
            raise LimitError(code, msg)
        self.budget.check("object_depth", len(self.active) + 1)
        self.budget.charge("memory_size", 512)
        self.active.add(target)
        try:
            data = target.read()
            self.budget.charge("content_input_size", len(data))
            cap = self.budget.limit("content_operation_count")
            available = cap - self.budget.used("content_operation_count") if cap is not None else None
            ops = (
                Operations.parse(data)
                if available is None
                else Operations.parse(data, max_operations=max(0, available))
            )
            self.budget.charge("content_operation_count", len(ops))
            found = self.operations(ops, environment, state)
            self.done.add(key)
            return found
        finally:
            self.active.remove(target)

    def pattern(self, pattern: Dictionary | None, resources: Dictionary | None, state: _State) -> bool:
        if isinstance(pattern, Stream):
            return self.stream(pattern, resources, replace(state, fill_pattern=None, stroke_pattern=None))
        if pattern is None:
            return False
        params = pattern.get("ExtGState")
        if isinstance(params, Dictionary):
            shading = state.graphics(params)
            return shading.fill or shading.blend or shading.mask
        return False

    def type3(self, font: Dictionary | None, resources: Dictionary | None, state: _State) -> bool:
        if font is None or font.get("Subtype") != "Type3":
            return False
        charprocs, own = font.get("CharProcs"), font.get("Resources")
        if isinstance(charprocs, Dictionary):
            for name in charprocs:
                self.budget.charge("interpretation_fuel", 1)
                if self.stream(charprocs[name], own if isinstance(own, Dictionary) else resources, state):
                    return True
        return False

    def paint(self, operation: Operation | InlineImage, resources: Dictionary | None, state: _State) -> bool:
        op = operation.operator
        text = op in _TEXT
        fill = op in _FILL or (text and state.text_mode in (0, 2, 4, 6))
        stroke = op in _STROKE or (text and state.text_mode in (1, 2, 5, 6))
        composite = op in ("sh", "Do", "BI")
        if not (fill or stroke or composite):
            return False
        if state.mask or state.blend or ((fill or composite) and state.fill) or (stroke and state.stroke):
            return True
        if (fill and self.pattern(state.fill_pattern, resources, state)) or (
            stroke and self.pattern(state.stroke_pattern, resources, state)
        ):
            return True
        if text:
            return self.type3(state.font, resources, state)
        if isinstance(operation, InlineImage):
            return operation.dictionary.get("SMaskInData") in (1, 2)
        return bool(
            op == "Do"
            and operation.operands
            and self.stream(_named(resources, "XObject", operation.operands[0]), resources, state)
        )

    def operations(self, ops: Operations, resources: Dictionary | None, state: _State) -> bool:
        stack: list[_State] = []
        for operation in ops:
            self.budget.charge("interpretation_fuel", 1)
            if self.paint(operation, resources, state):
                return True
            if operation.operator == "q":
                self.budget.charge("memory_size", 256)
                stack.append(state)
            elif operation.operator == "Q" and stack:
                state = stack.pop()
                self.budget.release("memory_size", 256)
            elif isinstance(operation, Operation):
                state = state.select(operation, resources)
        return False


def uses_transparency(page: Page) -> bool:
    """Evaluate the page in one read budget, including all annotation appearances."""

    def read(budget: _core._OperationBudget) -> bool:
        return _AnnexQ(budget).page(page)

    return _core.read_operation(page.document._core(), read)  # noqa: SLF001
