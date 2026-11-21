"""Media action values and their links to Screen, 3D and RichMedia annotations."""

from __future__ import annotations

import enum
import math
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from pyropdf import _core, typed
from pyropdf._errors import LimitError
from pyropdf._media import _MAX_ITEMS, _MAX_TEXT, _base_or_extension, _bounded, _text
from pyropdf._objects import Array, Dictionary, Name, Stream, String

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from contextlib import AbstractContextManager

    from pyropdf._document import Document
    from pyropdf._page import Page


class RenditionOperation(enum.IntEnum):
    """The operation requested of a viewer by a rendition action."""

    PLAY = 0
    STOP = 1
    PAUSE = 2
    RESUME = 3
    PLAY_OR_RESUME = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class RenditionAction:
    """A Screen target, optional rendition, operation and inert script."""

    annotation: int | None = None
    operation: RenditionOperation | None = None
    rendition: typed.RenditionMedia | typed.RenditionSelector | None = None
    script: str | Stream | None = None

    def __post_init__(self) -> None:
        """Check the fields required by each operation."""
        if self.operation is not None:
            if isinstance(self.operation, bool):
                msg = "operation must be a rendition operation"
                raise TypeError(msg)
            object.__setattr__(self, "operation", RenditionOperation(self.operation))
        elif self.script is None:
            msg = "a rendition action requires an operation or script"
            raise ValueError(msg)
        if self.operation in (RenditionOperation.PLAY, RenditionOperation.PLAY_OR_RESUME) and self.rendition is None:
            msg = "this operation requires a rendition"
            raise ValueError(msg)
        if self.rendition is not None and not isinstance(
            self.rendition, (typed.RenditionMedia, typed.RenditionSelector)
        ):
            msg = "expected a media or selector rendition"
            raise TypeError(msg)
        if isinstance(self.script, str):
            _text(self.script)
        elif self.script is not None and not isinstance(self.script, Stream):
            msg = "script must be a string or stream"
            raise TypeError(msg)

    @property
    def media_candidates(self) -> tuple[typed.RenditionMedia, ...]:
        """Return media renditions in depth-first selector preference order.

        Shared candidates keep their positions. Player capabilities and playback
        conditions are available on the typed dictionaries and are not evaluated.
        """
        if self.rendition is None:
            return ()
        return _candidates(self.rendition)

    def edit_rendition(self) -> AbstractContextManager[typed.RenditionMedia | typed.RenditionSelector]:
        """Edit the live rendition tree in one undo step, validating it before commit.

        The context yields the typed rendition. For a selector, its R array
        retains preference order. Media clip section MH and BE dictionaries
        carry their required and best-effort offsets separately.
        """
        if self.rendition is None:
            msg = "the action has no rendition to edit"
            raise ValueError(msg)
        return _edit_rendition(self.rendition)


@contextmanager
def _edit_rendition(
    root: typed.RenditionMedia | typed.RenditionSelector,
) -> Generator[typed.RenditionMedia | typed.RenditionSelector, None, None]:
    with root.obj._document.transaction("edit rendition"):  # noqa: SLF001
        yield root
        _candidates(root)


def _target_number(value: int | None) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = "annotation must be a positive indirect object number"
        raise ValueError(msg)


ViewSelection: TypeAlias = int | str | Name | typed.ThreeDView


@dataclass(frozen=True, slots=True, kw_only=True)
class GoTo3DView:
    """Select a target's 3D view by index, internal name, PDF selector name, or view dictionary."""

    annotation: int
    view: ViewSelection

    def __post_init__(self) -> None:
        """Validate view selection syntax; the writer resolves it against the annotation."""
        _target_number(self.annotation)
        if isinstance(self.view, Name) and self.view not in {"F", "L", "N", "P", "D"}:
            msg = "unknown 3D view selector"
            raise ValueError(msg)
        if isinstance(self.view, str):
            _text(self.view)
        elif isinstance(self.view, bool) or not (
            isinstance(self.view, typed.ThreeDView) or (isinstance(self.view, int) and self.view >= 0)
        ):
            msg = "view must be a nonnegative index, string, selector name, or 3D view"
            raise ValueError(msg)


CommandScalar: TypeAlias = bool | int | float | str
CommandArgument: TypeAlias = Array | tuple[CommandScalar, ...] | CommandScalar


def _command_argument(value: CommandArgument) -> None:
    """Validate flat PDF command arguments, including existing referenced arrays."""
    if isinstance(value, (Array, tuple)):
        _bounded(value)
        items = value
    else:
        items = (value,)
    text_bytes = 0
    for item in items:
        if isinstance(item, String):
            text_bytes += len(item)
        elif isinstance(item, str) and not isinstance(item, Name):
            if len(item) > _MAX_TEXT:
                text_bytes = _MAX_TEXT + 1
            else:
                text_bytes += len(item.encode("utf-8"))
        elif isinstance(item, float):
            if not math.isfinite(item):
                msg = "media command numbers must be finite"
                raise ValueError(msg)
        elif not isinstance(item, (int, bool)):
            msg = "media command arguments must be strings, booleans or numbers"
            raise TypeError(msg)
        if text_bytes > _MAX_TEXT:
            msg = "media command arguments exceed the annotation text limit"
            code = "annotation_input_size"
            raise LimitError(code, msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class RichMediaExecute:
    """An inert command and optional arguments for an instance of a RichMedia annotation."""

    annotation: int
    command: str
    instance: typed.RichMediaInstance | None = None
    arguments: CommandArgument | None = None

    def __post_init__(self) -> None:
        """Check the target, command name, arguments and instance kind."""
        _target_number(self.annotation)
        _text(self.command)
        if self.instance is not None and not isinstance(self.instance, typed.RichMediaInstance):
            msg = "instance must be a RichMediaInstance"
            raise TypeError(msg)
        if self.arguments is not None:
            _command_argument(self.arguments)


MediaAction: TypeAlias = RenditionAction | GoTo3DView | RichMediaExecute


def _target(doc: Document, number: int, kinds: tuple[str, ...]) -> Dictionary:
    from pyropdf._actions import _resolve_annot_xref  # noqa: PLC0415

    obj, _ = _resolve_annot_xref(doc, number)
    if obj.get("Subtype") not in kinds:
        msg = f"action target must be a {' or '.join(kinds)} annotation"
        raise ValueError(msg)
    # /P is optional. Old files may need a search of the page tree.
    page = obj.get("P")
    if isinstance(page, Dictionary):
        if (
            page.xref is None
            or page.generation is None
            or _core.page_index(doc._core(), page.xref, page.generation) is None  # noqa: SLF001
        ):
            msg = "action target refers to a page outside the page tree"
            raise ValueError(msg)
        arrays = (page.get("Annots"),)
    else:
        arrays = (p.obj.get("Annots") for p in doc.pages)
    for array in arrays:
        if isinstance(array, Array):
            _bounded(array)
            if obj in array:
                return obj
    msg = "action target does not belong to its page"
    raise ValueError(msg)


def _view_array(obj: Dictionary) -> Array | None:
    if obj.get("Subtype") == "RichMedia":
        content = obj.get("RichMediaContent")
        views = content.get("Views") if isinstance(content, Dictionary) else None
    else:
        model = obj.get("3DD")
        if isinstance(model, Dictionary) and not isinstance(model, Stream):
            model = model.get("3DD")
        views = model.get("VA") if isinstance(model, Stream) else None
    if isinstance(views, Array):
        _bounded(views)
        return views
    return None


def _view_value(obj: Dictionary, view: ViewSelection) -> object:
    if isinstance(view, typed.ThreeDView):
        if not isinstance(view.obj.get("XN"), String):
            msg = "a 3D view must have an external name"
            raise TypeError(msg)
        return view.obj
    if isinstance(view, Name):
        return view
    views = _view_array(obj)
    if views is not None:
        if isinstance(view, int) and view < len(views):
            return view
        for entry in views:
            if isinstance(entry, Dictionary):
                name = entry.get("IN", entry.get("XN") if obj.get("Subtype") == "RichMedia" else None)
                if isinstance(name, String) and name.text == view:
                    return view
    msg = "the target annotation has no such view"
    raise ValueError(msg)


def _has_instance(obj: Dictionary, instance: Dictionary) -> bool:
    content = obj.get("RichMediaContent")
    configs = content.get("Configurations") if isinstance(content, Dictionary) else None
    if not instance.is_indirect or not isinstance(configs, Array):
        return False
    _bounded(configs)
    count = 0
    for config in configs:
        instances = config.get("Instances") if isinstance(config, Dictionary) else None
        if isinstance(instances, Array):
            count += len(instances)
            if count > _MAX_ITEMS:
                msg = "RichMedia instances exceed 4096 entries"
                code = "annotation_count"
                raise LimitError(code, msg)
            if instance in instances:
                return True
    return False


def _dictionary(value: object, description: str) -> Dictionary:
    if not isinstance(value, Dictionary) or isinstance(value, Stream):
        msg = f"{description} must be a dictionary"
        raise TypeError(msg)
    return value


def _check_offset(value: object) -> None:
    offset = _dictionary(value, "media offset")
    kind = offset.get("S")
    if kind == "T":
        span = _dictionary(offset.get("T"), "media offset timespan")
        seconds = span.get("V")
        if span.get("S") == "S" and type(seconds) is not bool and isinstance(seconds, (int, float)) and seconds >= 0:
            return
    elif kind == "F":
        frame = offset.get("F")
        if type(frame) is int and frame >= 0:
            return
    elif kind == "M" and isinstance(offset.get("M"), String):
        return
    msg = "a media offset requires nonnegative seconds, a nonnegative frame number or a marker string"
    raise ValueError(msg)


def _check_section_bounds(node: Dictionary) -> None:
    for key in ("MH", "BE"):
        limits = node.get(key)
        if limits is not None:
            interval = _dictionary(limits, "media clip section limits")
            for bound in ("B", "E"):
                offset = interval.get(bound)
                if offset is not None:
                    _check_offset(offset)


def _children(node: Dictionary, *, clip: bool) -> Iterator[tuple[Dictionary, bool]]:
    subtype = node.get("S")
    if not clip and subtype == "SR":
        choices = node.get("R")
        if not isinstance(choices, Array):
            msg = "a selector rendition requires an array of renditions"
            raise ValueError(msg)
        _bounded(choices)
        for child in choices:
            yield _dictionary(child, "selector candidate"), False
    elif not clip and subtype == "MR":
        child = node.get("C")
        if child is not None:
            yield _dictionary(child, "media clip"), True
        elif not isinstance(node.get("P"), Dictionary):
            msg = "a media rendition requires a clip or play parameters"
            raise ValueError(msg)
    elif clip and subtype == "MCS":
        _check_section_bounds(node)
        yield _dictionary(node.get("D"), "next-level media clip"), True
    elif not (clip and subtype == "MCD"):
        msg = "expected a media clip subtype" if clip else "expected a rendition subtype"
        raise ValueError(msg)


def _candidates(root: typed.RenditionMedia | typed.RenditionSelector) -> tuple[typed.RenditionMedia, ...]:
    def read(budget: _core._OperationBudget) -> tuple[typed.RenditionMedia, ...]:
        active: set[Dictionary] = set()
        leaves: list[typed.RenditionMedia] = []
        stack: list[tuple[Dictionary | None, Iterator[tuple[Dictionary, bool]], int]] = [
            (None, iter(((root.obj, False),)), 0)
        ]
        count = 0
        while stack:
            parent, children, charge = stack[-1]
            edge = next(children, None)
            if edge is None:
                stack.pop()
                if parent is not None:
                    active.remove(parent)
                    budget.release("memory_size", charge)
                continue
            node, clip = edge
            count += 1
            if count > _MAX_ITEMS:
                msg = "rendition relationships exceed 4096 entries"
                code = "annotation_count"
                raise LimitError(code, msg)
            if node in active:
                msg = "cycle in rendition selectors or media clip sections"
                raise ValueError(msg)
            # The iterator visits one sibling at a time. Include the direct
            # object's path in the retained handle charge.
            charge = 512 + 8 * len(node._path)  # noqa: SLF001
            budget.charge("memory_size", charge)
            if not clip and node.get("S") == "MR":
                budget.charge("memory_size", 160 + 8 * len(node._path))  # noqa: SLF001
                leaves.append(typed.RenditionMedia(node))
            active.add(node)
            stack.append((node, _children(node, clip=clip), charge))
        return tuple(leaves)

    return _core.read_operation(root.obj._core(), read)  # noqa: SLF001


def _rendition_dict(doc: Document, action: RenditionAction) -> dict[str, object]:
    entries: dict[str, object] = {"S": Name("Rendition")}
    if action.annotation is not None:
        entries["AN"] = _target(doc, action.annotation, ("Screen",))
    if action.rendition is not None:
        required = "MR" if isinstance(action.rendition, typed.RenditionMedia) else "SR"
        if required != action.rendition.S:
            msg = "rendition dictionary has the wrong subtype"
            raise ValueError(msg)
        _candidates(action.rendition)
        entries["R"] = action.rendition.obj
    if action.operation is not None:
        entries["OP"] = int(action.operation)
    if action.script is not None:
        entries["JS"] = action.script
    return entries


def _rich_action_version(doc: Document) -> tuple[int, int]:
    from pyropdf._annotations import _version  # noqa: PLC0415

    minimum = _base_or_extension(doc, 3)
    if _version(doc) < minimum:
        msg = "the target RichMedia annotation requires PDF 2.0 or Adobe PDF 1.7 extension level 3"
        raise ValueError(msg)
    return minimum


def media_action_dict(doc: Document, action: MediaAction) -> tuple[dict[str, object], tuple[int, int]]:
    """Resolve relationships within the current version before serializing an action."""
    if isinstance(action, RenditionAction):
        return _rendition_dict(doc, action), (1, 5)
    if isinstance(action, GoTo3DView):
        target = _target(doc, action.annotation, ("3D", "RichMedia"))
        minimum = _rich_action_version(doc) if target.get("Subtype") == "RichMedia" else (1, 6)
        return {"S": Name("GoTo3DView"), "TA": target, "V": _view_value(target, action.view)}, minimum
    target = _target(doc, action.annotation, ("RichMedia",))
    cmd: dict[str, object] = {"C": action.command}
    if action.arguments is not None:
        _command_argument(action.arguments)
        cmd["A"] = action.arguments
    entries = {"S": Name("RichMediaExecute"), "TA": target, "CMD": cmd}
    if action.instance is not None:
        if not _has_instance(target, action.instance.obj):
            msg = "instance does not belong to the target RichMedia annotation"
            raise ValueError(msg)
        entries["TI"] = action.instance.obj
    return entries, _rich_action_version(doc)


def _as_xref(obj: object) -> int | None:
    return obj.xref if isinstance(obj, Dictionary) else None


def _read_rendition(action: Dictionary) -> RenditionAction | None:
    r = action.get("R")
    rendition = None
    if isinstance(r, Dictionary):
        if r.get("S") == "MR":
            rendition = typed.RenditionMedia(r)
        elif r.get("S") == "SR":
            rendition = typed.RenditionSelector(r)
        else:
            return None
    js = action.get("JS")
    return RenditionAction(
        annotation=_as_xref(action.get("AN")),
        operation=action.get("OP"),
        rendition=rendition,
        script=js.text if isinstance(js, String) else js,
    )


def read_media_action(action: Dictionary) -> MediaAction | None:  # noqa: PLR0911 - one result per PDF action form.
    """Read the target and its selectors without interpreting the media or script."""
    try:
        if action.get("S") == "Rendition":
            return _read_rendition(action)
        target = _as_xref(action.get("TA"))
        if target is None:
            return None
        if action.get("S") == "GoTo3DView":
            view = action.get("V")
            if isinstance(view, Dictionary):
                view = typed.ThreeDView(view)
            elif isinstance(view, String):
                view = view.text
            if not isinstance(view, (int, str, typed.ThreeDView)):
                return None
            return GoTo3DView(annotation=target, view=view)
        cmd = action.get("CMD")
        if not isinstance(cmd, Dictionary):
            return None
        name = cmd.get("C")
        if not isinstance(name, String):
            return None
        arguments = cmd.get("A")
        instance = action.get("TI")
        return RichMediaExecute(
            annotation=target,
            command=name.text,
            arguments=arguments.text if isinstance(arguments, String) else arguments,
            instance=None if instance is None else typed.RichMediaInstance(instance),
        )
    except (TypeError, ValueError):
        return None


class MovieAction:
    """An existing Movie action interpreted on its destination page (ISO 32000-1 §12.6.4.9).

    Pass the destination page of the link or outline, or the page associated
    with the action. Titles identify movie annotations on that page.
    """

    __slots__ = ("_action", "_page")

    def __init__(self, page: Page, action: typed.ActionMovie) -> None:
        """Bind the action dictionary to the page on which it selects its movie."""
        if not isinstance(action, typed.ActionMovie) or action.obj.get("S") != Name("Movie"):
            msg = "expected an existing Movie action"
            raise TypeError(msg)
        if action.obj._document is not page.document:  # noqa: SLF001
            msg = "the action and its destination page belong to different documents"
            raise ValueError(msg)
        self._action = action
        self._page = page

    def _find(self, value: str | Dictionary) -> typed.AnnotMovie:
        def resolve(budget: _core._OperationBudget) -> typed.AnnotMovie:
            annots = self._page.obj.get("Annots")
            result = None
            if isinstance(annots, Array):
                _bounded(annots)
                for annot in annots:
                    budget.poll()
                    if not isinstance(annot, Dictionary) or annot.get("Subtype") != Name("Movie"):
                        continue
                    title = annot.get("T")
                    if annot == value or (isinstance(value, str) and isinstance(title, String) and title.text == value):
                        if result is not None:
                            msg = "the movie title is ambiguous on the destination page"
                            raise ValueError(msg)
                        result = typed.AnnotMovie(annot)
            if result is None:
                msg = "the movie target does not exist on the destination page"
                raise ValueError(msg)
            return result

        return _core.read_operation(self._page.obj._core(), resolve)  # noqa: SLF001

    @property
    def target(self) -> typed.AnnotMovie:
        """Resolve the action's unique title or direct annotation reference on the destination page.

        Assign a title to write a title-based target, or an indirect movie
        annotation view to write a reference. Both forms replace the previous
        target in one transaction.
        """
        ref, title = self._action.obj.get("Annotation"), self._action.obj.get("T")
        if ref is None and isinstance(title, String):
            return self._find(title.text)
        if title is None and isinstance(ref, Dictionary) and ref.is_indirect:
            return self._find(ref)
        msg = "a movie action requires exactly one title or indirect annotation reference"
        raise ValueError(msg)

    @target.setter
    def target(self, value: str | typed.AnnotMovie) -> None:
        if isinstance(value, str) and not isinstance(value, Name):
            if value:
                _text(value)
            key, retired, target = "T", "Annotation", value
        elif isinstance(value, typed.AnnotMovie) and value.obj.is_indirect:
            key, retired, target = "Annotation", "T", value.obj
        else:
            msg = "expected a movie title or an indirect movie annotation"
            raise TypeError(msg)
        with self._page.document.transaction("retarget movie action"):
            self._find(target)
            if retired in self._action.obj:
                del self._action.obj[retired]
            self._action.obj[key] = target

    def _time(self, key: str) -> tuple[int, int | None] | None:
        activation = self.target.obj.get("A")
        fallback = activation.get(key) if isinstance(activation, Dictionary) else None
        value = self._action.obj.get(key)
        if value is None:
            value = fallback
        if value is None:
            return None
        scale = None
        if isinstance(value, Array):
            if len(value) != 2:  # noqa: PLR2004 - [time, time scale].
                msg = f"movie /{key} time array requires a time and a scale"
                raise ValueError(msg)
            value, scale = value
            if type(scale) is not int or scale <= 0:
                msg = f"movie /{key} time scale must be a positive integer"
                raise ValueError(msg)
        if isinstance(value, String) and len(value) == 8:  # noqa: PLR2004 - signed 64-bit PDF time.
            value = int.from_bytes(value, "big", signed=True)
        if type(value) is not int or value < (1 if key == "Duration" else 0):
            msg = f"invalid movie /{key} time"
            raise ValueError(msg)
        return value, scale

    def _set_time(self, key: str, value: tuple[int, int | None] | None) -> None:
        if value is None:
            self._action.obj.pop(key, None)
            return
        if not isinstance(value, tuple) or len(value) != 2:  # noqa: PLR2004 - ticks and optional scale.
            msg = "movie time requires a pair of ticks and an optional time scale"
            raise TypeError(msg)
        ticks, scale = value
        if type(ticks) is not int or ticks < (1 if key == "Duration" else 0) or ticks >= 1 << 63:
            msg = f"movie /{key} ticks are outside the signed 64-bit time range"
            raise ValueError(msg)
        if scale is not None and (type(scale) is not int or scale <= 0):
            msg = "movie time scale must be a positive integer"
            raise ValueError(msg)
        encoded = ticks if ticks < 1 << 31 else ticks.to_bytes(8, "big")
        self._action.obj[key] = encoded if scale is None else [encoded, scale]

    @property
    def start(self) -> tuple[int, int | None]:
        """Effective start as (ticks, ticks per second), using the movie's time scale when the second value is None.

        Assign a pair to override the annotation or None to inherit its setting.
        """
        return self._time("Start") or (0, None)

    @start.setter
    def start(self, value: tuple[int, int | None] | None) -> None:
        self._set_time("Start", value)

    @property
    def duration(self) -> tuple[int, int | None] | None:
        """Effective (ticks, ticks per second), or None to play to the end. Assign None to inherit the annotation."""
        return self._time("Duration")

    @duration.setter
    def duration(self, value: tuple[int, int | None] | None) -> None:
        self._set_time("Duration", value)
