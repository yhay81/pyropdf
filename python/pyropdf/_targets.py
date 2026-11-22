"""Where links lead: destinations and actions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias

from pyropdf._actions import (
    EmbeddedDestination,
    HideAction,
    ImportDataAction,
    JavaScriptAction,
    ResetFormAction,
    SetLayerStateAction,
    SubmitFormAction,
    _custom_target_budget_text,
    _is_custom_target,
)
from pyropdf._geometry import Point
from pyropdf._interchange import DocumentPartAction
from pyropdf._media_actions import GoTo3DView, RenditionAction, RichMediaExecute
from pyropdf._presentations import TransitionAction
from pyropdf._threads import ThreadAction
from pyropdf._viewer_actions import StructureDestination, UriAction, WindowsLaunch

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyropdf._core import _Target
    from pyropdf._document import Document
    from pyropdf._structure import StructureElement


def _page_index(page: object, name: str) -> int:
    if isinstance(page, bool) or not isinstance(page, int):
        msg = f"{name} must be an int: {page!r}"
        raise TypeError(msg)
    if page < 0:
        msg = f"{name} must not be negative: {page}"
        raise ValueError(msg)
    return page


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        msg = f"{name} must be a str: {value!r}"
        raise TypeError(msg)
    if not value:
        msg = f"{name} must not be empty"
        raise ValueError(msg)
    return value


@dataclass(frozen=True, slots=True, init=False)
class Destination:
    """A place in this document: a zero-based page, a point, and a zoom.

    ``point`` is the top left of the view and ``zoom`` its magnification.
    The point is in the page's display coordinates. Without a point or a
    zoom the viewer fits the page in the window; ``zoom=None`` keeps the
    viewer's zoom.
    """

    page: int
    point: Point | None
    zoom: float | None

    def __init__(self, page: int, point: Sequence[float] | None = None, zoom: float | None = None) -> None:
        """Validate the page, point, and zoom; ``point`` takes any pair of numbers."""
        _page_index(page, "page")
        if zoom is not None:
            if isinstance(zoom, bool) or not isinstance(zoom, (int, float)):
                msg = f"zoom must be a number: {zoom!r}"
                raise TypeError(msg)
            if not math.isfinite(zoom) or zoom < 0:
                msg = f"zoom must be finite and not negative: {zoom!r}"
                raise ValueError(msg)
            zoom = float(zoom)
        object.__setattr__(self, "page", page)
        object.__setattr__(self, "point", None if point is None else Point(point))
        object.__setattr__(self, "zoom", zoom)


@dataclass(frozen=True, slots=True)
class RemoteDestination:
    """A place in another PDF file: a zero-based page or a named destination."""

    file: str
    page: int = 0
    name: str | None = None
    """A named destination in that file, used instead of ``page``."""

    structure_id: bytes | None = field(default=None, kw_only=True)
    """The optional byte-string structure element ID in the remote document."""
    structure_fit: str = field(default="Fit", kw_only=True)
    structure_operands: tuple[float | None, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        """Validate the file, page, and name."""
        _text(self.file, "file")
        _page_index(self.page, "page")
        if self.name is not None:
            _text(self.name, "name")

        if self.structure_id is not None:
            if not isinstance(self.structure_id, bytes):
                msg = "a remote structure ID is a PDF byte string"
                raise TypeError(msg)
            if len(self.structure_id) > 1024 * 1024:
                from pyropdf._errors import LimitError  # noqa: PLC0415

                code = "annotation_input_size"
                msg = "the remote structure ID exceeds the 1-MiB action input limit"
                raise LimitError(code, msg)
            view = StructureDestination(
                element=self.structure_id,
                fallback=Destination(self.page),
                fit=self.structure_fit,
                operands=self.structure_operands,
            )
            object.__setattr__(self, "structure_operands", view.operands)
        elif self.structure_fit != "Fit" or self.structure_operands:
            msg = "a remote structure view requires its element ID"
            raise ValueError(msg)

    def resolve_structure(self, document: Document) -> StructureElement | None:
        """Resolve the optional structure ID in the supplied remote PDF without opening a file."""
        from pyropdf import _core  # noqa: PLC0415
        from pyropdf._fragments import _structure_id  # noqa: PLC0415
        from pyropdf._objects import _unlocked  # noqa: PLC0415
        from pyropdf._structure import StructureElement  # noqa: PLC0415

        key = self.structure_id
        if key is None:
            return None
        obj = _core.read_operation(_unlocked(document), lambda budget: _structure_id(document, key, budget))
        return (
            StructureElement(document, obj.xref, obj.generation or 0)
            if obj is not None and obj.xref is not None
            else None
        )

    def resolve(self, document: Document) -> Destination | None:
        """Resolve the page or named target in a caller-supplied remote document."""
        if self.name is not None:
            return document.destinations.get(self.name)
        return Destination(self.page) if self.page < len(document.pages) else None


@dataclass(frozen=True, slots=True)
class Launch:
    """Open a file with the application the viewer associates with it."""

    file: str

    def __post_init__(self) -> None:
        """Validate the file."""
        _text(self.file, "file")


@dataclass(frozen=True, slots=True)
class NamedAction:
    """A viewer action by name (ISO 32000-2 Table 213).

    The standard names are ``"NextPage"``, ``"PrevPage"``, ``"FirstPage"``,
    and ``"LastPage"``; viewers may know others.
    """

    name: str

    def __post_init__(self) -> None:
        """Validate the name."""
        _text(self.name, "name")

    def resolve(self, document: Document, *, current_page: int) -> Destination | None:
        """Return the target page of a standard navigation action, or None when unavailable."""
        _page_index(current_page, "current_page")
        count = len(document.pages)
        if current_page >= count:
            return None
        page = {"FirstPage": 0, "LastPage": count - 1, "NextPage": current_page + 1, "PrevPage": current_page - 1}.get(
            self.name
        )
        return Destination(page) if page is not None and 0 <= page < count else None


#: Where a link or trigger leads: a destination, a file to open, a viewer,
#: transition, thread, script, hide, layer-state, or form action, or a URI string.
Target: TypeAlias = (
    Destination
    | RemoteDestination
    | StructureDestination
    | UriAction
    | WindowsLaunch
    | EmbeddedDestination
    | GoTo3DView
    | RenditionAction
    | RichMediaExecute
    | DocumentPartAction
    | Launch
    | NamedAction
    | TransitionAction
    | ThreadAction
    | JavaScriptAction
    | HideAction
    | SetLayerStateAction
    | SubmitFormAction
    | ResetFormAction
    | ImportDataAction
    | str
)


def _destination(page: int, point: tuple[float, float] | None, zoom: float | None) -> Destination:
    return Destination(page, None if point is None else Point(point), zoom)


def _target_from_core(target: _Target) -> Target | None:
    """Build a target record from the core's tuple."""
    kind, page, point, zoom, text, name = target
    match kind:
        case "goto" if page is not None:
            return _destination(page, point, zoom)
        case "uri" if text:
            return text
        case "gotor" if text:
            return RemoteDestination(text, page or 0, name or None)
        case "launch" if text:
            return Launch(text)
        case "named" if name:
            return NamedAction(name)
        case _:
            return None


def _target_to_core(target: object, page_count: int) -> _Target:  # noqa: PLR0911
    """Return the core's tuple for a target, validating its page."""
    match target:
        case None:
            return ("none", None, None, None, None, None)
        case ThreadAction(thread=thread, file=file):
            return ("none", None, None, None, file, thread if isinstance(thread, str) else None)
        case _ if _is_custom_target(target):
            return ("none", None, None, None, _custom_target_budget_text(target), None)
        case Destination(page=page, point=point, zoom=zoom):
            if page >= page_count:
                msg = f"destination page {page} is out of range (0..{page_count - 1})"
                raise ValueError(msg)
            return ("goto", page, None if point is None else (point.x, point.y), zoom, None, None)
        case str():
            return ("uri", None, None, None, _text(target, "URI"), None)
        case RemoteDestination(file=file, page=page, name=name):
            return ("gotor", page, None, None, file, name)
        case Launch(file=file):
            return ("launch", None, None, None, file, None)
        case NamedAction(name=name):
            return ("named", None, None, None, None, name)
        case _:
            msg = (
                "a target is a Destination, RemoteDestination, EmbeddedDestination, DocumentPartAction, "
                "Launch, NamedAction, TransitionAction, ThreadAction, JavaScriptAction, HideAction, "
                "SetLayerStateAction, SubmitFormAction, ResetFormAction, ImportDataAction, "
                f"URI string, or None: {type(target).__name__}"
            )
            raise TypeError(msg)


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (Destination, RemoteDestination, Launch, NamedAction):
    _public.__module__ = "pyropdf"
