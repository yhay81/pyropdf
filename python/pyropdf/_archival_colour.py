"""Static evidence for the PDF/A restrictions on *used* device colour spaces."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._errors import LimitError, MalformedPdfError
from pyropdf._objects import Array, Dictionary, Name, Stream
from pyropdf._operations import InlineImage, Operation, Operations

if TYPE_CHECKING:
    from pyropdf._document import Document

_DEVICES = {"DeviceGray": "gray", "DeviceRGB": "rgb", "DeviceCMYK": "cmyk"}
_DEFAULTS = {"gray": "DefaultGray", "rgb": "DefaultRGB", "cmyk": "DefaultCMYK"}
_COMPONENTS = {1: "gray", 3: "rgb", 4: "cmyk"}
_PAINT_FILL = {"f", "F", "f*", "B", "B*", "b", "b*"}
_PAINT_STROKE = {"s", "S", "B", "B*", "b", "b*"}
_TEXT_SHOW = {"Tj", "TJ", "'", '"'}
_SELECT = {"g": "DeviceGray", "rg": "DeviceRGB", "k": "DeviceCMYK"}
_EXPLICIT = frozenset((*_SELECT, "cs"))
_INDEPENDENT = "independent"
_UNKNOWN = "unknown"
_MAX_DEPTH = 32
_ICC_SIGNATURES = {b"GRAY": "gray", b"RGB ": "rgb", b"CMYK": "cmyk"}
_ICC_HEADER_SIZE = 128
_MAX_ICC_SIZE = 8 * 1024 * 1024


def _lookup(resources: Dictionary | None, category: str, key: object) -> object:
    entries = resources.get(category) if resources is not None else None
    return entries.get(str(key)) if isinstance(entries, Dictionary) and isinstance(key, str) else None


def _family(space: object) -> str | None:
    """Recognize component counts of known CIE-based spaces, including ICC stream dictionaries."""
    if not isinstance(space, Array) or len(space) < 2:  # noqa: PLR2004 - a family and its parameters.
        return None
    family, parameters = space[0], space[1]
    if family in ("CalGray", "CalRGB", "Lab") and isinstance(parameters, Dictionary):
        return "gray" if family == "CalGray" else "rgb"
    if family == "ICCBased" and isinstance(parameters, Stream):
        count = parameters.get("N")
        return _COMPONENTS.get(count) if type(count) is int else None
    return None


def _space(space: object, resources: Dictionary | None) -> str:
    """Resolve an explicit selection against its local resources and applicable default."""
    if isinstance(space, Name) and space not in _DEVICES:
        space = _lookup(resources, "ColorSpace", space)
    if isinstance(space, Name) and space in _DEVICES:
        device = _DEVICES[space]
        default = _lookup(resources, "ColorSpace", _DEFAULTS[device])
        if default is None:
            return device
        return _INDEPENDENT if _family(default) == device else _UNKNOWN
    return _INDEPENDENT if _family(space) is not None else _UNKNOWN


@dataclass(frozen=True, slots=True)
class _State:
    fill: str
    stroke: str
    text_mode: int = 0
    type3: bool = False

    def select(self, op: Operation, resources: Dictionary | None) -> _State:
        command, values = op.operator, op.operands
        if command.lower() in _SELECT or (command.lower() == "cs" and values):
            selection = Name(_SELECT[command.lower()]) if command.lower() in _SELECT else values[0]
            color = _space(selection, resources)
            return replace(self, stroke=color) if command.isupper() else replace(self, fill=color)
        if command == "Tr" and values and type(values[0]) is int:
            return replace(self, text_mode=values[0])
        if command == "Tf" and values:
            font = _lookup(resources, "Font", values[0])
            return replace(self, type3=isinstance(font, Dictionary) and font.get("Subtype") == "Type3")
        if command == "gs" and values:
            gs = _lookup(resources, "ExtGState", values[0])
            font_entry = gs.get("Font") if isinstance(gs, Dictionary) else None
            if isinstance(font_entry, Array) and len(font_entry) and isinstance(font_entry[0], Dictionary):
                return replace(self, type3=font_entry[0].get("Subtype") == "Type3")
        return self


class _Check:
    def __init__(self, budget: _core._OperationBudget, *, part4: bool) -> None:
        self.budget = budget
        self.part4 = part4
        self.unsolved = False
        self.active: set[Stream] = set()

    def intent(self, entries: object) -> str | None:
        if isinstance(entries, Array):
            for entry in entries:
                self.budget.charge("interpretation_fuel", 1)
                if isinstance(entry, Dictionary) and entry.get("S") == "GTS_PDFA1":
                    profile = entry.get("DestOutputProfile")
                    if not isinstance(profile, Stream):
                        return _UNKNOWN
                    try:
                        data = profile.read(max_size=_MAX_ICC_SIZE)
                    except LimitError as error:
                        document_limit = self.budget.limit("stream_data_size")
                        if error.code != "stream_data_size" or (document_limit and document_limit < _MAX_ICC_SIZE):
                            raise
                        return _UNKNOWN
                    except MalformedPdfError:
                        return _UNKNOWN
                    self.budget.charge("content_input_size", len(data))
                    if len(data) < _ICC_HEADER_SIZE or data[36:40] != b"acsp":
                        return _UNKNOWN
                    return _ICC_SIGNATURES.get(data[16:20], _UNKNOWN)
        return None

    def used(self, space: str, intent: str | None, group: str | None) -> str | None:
        if space == _INDEPENDENT or (intent == space and space != _UNKNOWN):
            return None
        if space == "gray" and intent in _COMPONENTS.values():
            return None
        alternate = self.part4 and space in ("rgb", "cmyk")
        if alternate and group == space:
            return None
        if _UNKNOWN in (space, intent) or (alternate and group == _UNKNOWN):
            self.unsolved = True
            return None
        return space

    def group(
        self, owner: Dictionary, resources: Dictionary | None, inherited: str | None, intent: str | None
    ) -> tuple[str | None, str | None]:
        """Read the blending space and check the group's own explicit use of a device space."""
        data = owner.get("Group")
        if not isinstance(data, Dictionary) or data.get("S") != "Transparency" or "CS" not in data:
            return inherited, None
        space = data["CS"]
        if isinstance(space, Name) and space not in _DEVICES:
            space = _lookup(resources, "ColorSpace", space)
        if isinstance(space, Name) and space in _DEVICES:
            return None, self.used(_space(space, resources), intent, inherited)
        if isinstance(space, Array) and len(space) and space[0] in ("CalRGB", "ICCBased"):
            return _family(space) or _UNKNOWN, None
        return _UNKNOWN, None

    def stream(
        self, item: object, resources: Dictionary | None, state: _State, intent: str | None, group: str | None
    ) -> str | None:
        if not isinstance(item, Stream):
            self.unsolved = True
            return None
        if item.get("Subtype") == "Image":
            space = state.fill if item.get("ImageMask") is True else _space(item.get("ColorSpace"), resources)
            return self.used(space, intent, group)
        if item.get("Subtype") != "Form" or item in self.active:
            self.unsolved = True
            return None
        depth = len(self.active) + 1
        self.budget.check("object_depth", depth)
        if depth > _MAX_DEPTH:
            code = "object_depth"
            msg = "PDF/A colour resources nest deeper than 32"
            raise LimitError(code, msg)
        self.budget.charge("memory_size", 128)
        self.active.add(item)
        try:
            data = item.read()
            self.budget.charge("content_input_size", len(data))
            cap = self.budget.limit("content_operation_count")
            remaining = cap - self.budget.used("content_operation_count") if cap is not None else None
            ops = (
                Operations.parse(data)
                if remaining is None
                else Operations.parse(data, max_operations=max(1, remaining))
            )
            self.budget.charge("content_operation_count", len(ops))
            own = item.get("Resources")
            local = own if isinstance(own, Dictionary) else resources
            blending, device = self.group(item, local, group, intent)
            return device or self.operations(ops, local, state, intent, blending)
        except MalformedPdfError:
            self.unsolved = True
            return None
        finally:
            self.active.remove(item)
            self.budget.release("memory_size", 128)

    def paint(
        self,
        op: Operation | InlineImage,
        resources: Dictionary | None,
        state: _State,
        intent: str | None,
        group: str | None,
    ) -> str | None:
        command = op.operator
        if isinstance(op, InlineImage):
            fields = op.dictionary
            value = fields.get("CS", fields.get("ColorSpace"))
            if isinstance(value, Name):
                value = Name({"G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK"}.get(str(value), str(value)))
            color = state.fill if fields.get("IM", fields.get("ImageMask")) is True else _space(value, resources)
            return self.used(color, intent, group)
        values = op.operands
        if command == "gs" and values:
            ext = _lookup(resources, "ExtGState", values[0])
            if isinstance(ext, Dictionary) and isinstance(ext.get("SMask"), Dictionary):
                self.unsolved = True
        if command == "Do" and values:
            return self.stream(_lookup(resources, "XObject", values[0]), resources, state, intent, group)
        if command == "sh" and values:
            shading = _lookup(resources, "Shading", values[0])
            color = _space(shading.get("ColorSpace"), resources) if isinstance(shading, Dictionary) else _UNKNOWN
            return self.used(color, intent, group)
        text = command in _TEXT_SHOW
        if text and state.type3:
            self.unsolved = True
            return None
        fill = command in _PAINT_FILL or (text and state.text_mode in (0, 2, 4, 6))
        stroke = command in _PAINT_STROKE or (text and state.text_mode in (1, 2, 5, 6))
        return (self.used(state.fill, intent, group) if fill else None) or (
            self.used(state.stroke, intent, group) if stroke else None
        )

    def operations(
        self, ops: Operations, resources: Dictionary | None, state: _State, intent: str | None, group: str | None
    ) -> str | None:
        stack = []
        try:
            for op in ops:
                self.budget.charge("interpretation_fuel", 1)
                if (device := self.paint(op, resources, state, intent, group)) is not None:
                    return device
                if not isinstance(op, Operation):
                    continue
                command = op.operator
                if command == "q":
                    self.budget.charge("memory_size", 128)
                    stack.append(state)
                elif command == "Q" and stack:
                    state = stack.pop()
                    self.budget.release("memory_size", 128)
                else:
                    state = state.select(op, resources)
                    if command.lower() in _EXPLICIT:
                        color = state.stroke if command.isupper() else state.fill
                        if (device := self.used(color, intent, group)) is not None:
                            return device
            return None
        finally:
            self.budget.release("memory_size", 128 * len(stack))

    def document(self, doc: Document) -> str | None:
        global_intent = self.intent(doc.catalog.get("OutputIntents"))
        for page in doc.pages:
            self.budget.charge("interpretation_fuel", 1)
            node = page.obj
            resources = node.get("Resources")
            depth = 0
            while not isinstance(resources, Dictionary) and isinstance(parent := node.get("Parent"), Dictionary):
                depth += 1
                self.budget.check("object_depth", depth)
                if depth > _MAX_DEPTH:
                    code = "object_depth"
                    msg = "PDF/A page resource inheritance exceeds 32 ancestors"
                    raise LimitError(code, msg)
                node, resources = parent, parent.get("Resources")
            local = resources if isinstance(resources, Dictionary) else None
            initial = _space(Name("DeviceGray"), local)
            intent = self.intent(page.obj.get("OutputIntents")) if self.part4 else None
            intent = intent or global_intent
            group, device = self.group(page.obj, local, None, intent)
            if device is not None:
                return device
            try:
                operations = page.operations
            except MalformedPdfError:
                self.unsolved = True
                continue
            if (device := self.operations(operations, local, _State(initial, initial), intent, group)) is not None:
                return device
            if page.obj.get("Annots"):
                self.unsolved = True
        return None


def archival_colour(document: Document, claim: str) -> tuple[str | None, bool]:
    """Return a demonstrated device violation and whether some colour uses remain unchecked."""

    def read(budget: _core._OperationBudget) -> tuple[str | None, bool]:
        screen = _Check(budget, part4=claim.startswith("PDF/A-4"))
        return screen.document(document), screen.unsolved

    return _core.read_operation(document._core(), read)  # noqa: SLF001
