"""Presentations and thumbnails: transitions, display durations, sub-page steps, and transition actions."""

from __future__ import annotations

import enum
import itertools
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pyropdf import _core
from pyropdf._errors import LimitError, StaleObjectError
from pyropdf._objects import Array, Dictionary, Name, Stream, String, _catalog, _Handle
from pyropdf._pixmap import Colorspace, Pixmap
from pyropdf._viewer_actions import require_navigation_version

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._geometry import Point
    from pyropdf._page import Page
    from pyropdf._targets import Destination, RemoteDestination, Target

#: Maximum magnitude of a PDF real read by hayro as 32-bit float.
_FLOAT32_MAX = 3.4028234663852886e38
#: Maximum number of ``NavNode`` dictionaries in one page's ``/PresSteps`` chain.
_MAX_NAVIGATION_STEPS = 4096
#: Maximum UTF-8 bytes of target strings in one ``NavigationStep`` or ``/PresSteps`` write.
_MAX_STEP_TEXT_BYTES = 1024 * 1024
#: Page count sentinel used while validating a standalone ``NavigationStep``.
_UNBOUNDED_PAGES = 1 << 62


class TransitionStyle(enum.StrEnum):
    """Visual style of a page transition (``/S``, ISO 32000-2 Table 165)."""

    SPLIT = "Split"
    """Two lines sweep across the screen, revealing the page."""
    BLINDS = "Blinds"
    """Multiple parallel lines sweep in the same direction."""
    BOX = "Box"
    """A rectangular box sweeps inward from the edges or outward from the center."""
    WIPE = "Wipe"
    """A single line sweeps across the screen from one edge to the other."""
    DISSOLVE = "Dissolve"
    """The old page dissolves gradually to reveal the new one."""
    GLITTER = "Glitter"
    """Dissolve sweeping across the page in a wide band."""
    REPLACE = "R"
    """The new page replaces the old one immediately without a visual effect."""
    FLY = "Fly"
    """Changes fly in or out to or from a location (PDF 1.5)."""
    PUSH = "Push"
    """The old page slides out while the new page slides in (PDF 1.5)."""
    COVER = "Cover"
    """The new page slides in on top of the old page (PDF 1.5)."""
    UNCOVER = "Uncover"
    """The old page slides out, uncovering the new page (PDF 1.5)."""
    FADE = "Fade"
    """The new page fades in smoothly through the old page (PDF 1.5)."""


class TransitionDimension(enum.StrEnum):
    """Axis along which a ``Split`` or ``Blinds`` transition runs (``/Dm``, ISO 32000-2 Table 165)."""

    HORIZONTAL = "H"
    """Horizontal lines."""
    VERTICAL = "V"
    """Vertical lines."""


class TransitionMotion(enum.StrEnum):
    """Direction of motion for ``Split``, ``Box``, or ``Fly`` transitions (``/M``, ISO 32000-2 Table 165)."""

    INWARD = "I"
    """From the edges of the page toward the center."""
    OUTWARD = "O"
    """From the center of the page toward the edges."""


_PDF_15_STYLES = frozenset(
    {
        TransitionStyle.FLY,
        TransitionStyle.PUSH,
        TransitionStyle.COVER,
        TransitionStyle.UNCOVER,
        TransitionStyle.FADE,
    }
)
_DIMENSION_STYLES = frozenset({TransitionStyle.SPLIT, TransitionStyle.BLINDS})
_MOTION_STYLES = frozenset({TransitionStyle.SPLIT, TransitionStyle.BOX, TransitionStyle.FLY})
_DIRECTION_ANGLES: dict[TransitionStyle, frozenset[int]] = {
    TransitionStyle.WIPE: frozenset({0, 90, 180, 270}),
    TransitionStyle.GLITTER: frozenset({0, 270, 315}),
    TransitionStyle.FLY: frozenset({0, 270}),
    TransitionStyle.PUSH: frozenset({0, 270}),
    TransitionStyle.COVER: frozenset({0, 270}),
    TransitionStyle.UNCOVER: frozenset({0, 270}),
}


def _non_negative_float(value: object, name: str) -> float:
    """Validate a finite non-negative PDF real within float32 range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{name} must be a number: {value!r}"
        raise TypeError(msg)
    if not math.isfinite(value) or value < 0 or value > _FLOAT32_MAX:
        msg = f"{name} must be finite, non-negative, and within PDF real-number range: {value!r}"
        raise ValueError(msg)
    return float(value)


def _enum_member(kind: type[enum.StrEnum], value: object, name: str) -> enum.StrEnum:
    if not isinstance(value, str):
        msg = f"{name} must be a {kind.__name__} or str: {value!r}"
        raise TypeError(msg)
    return kind(value)


def _validate_fly_options(style: TransitionStyle, scale: object, rectangular: object) -> float | None:
    checked_scale: float | None = None
    if scale is not None:
        checked_scale = _non_negative_float(scale, "scale")
        if style is not TransitionStyle.FLY:
            msg = f"scale applies only to Fly transitions, not {style.value!r}"
            raise ValueError(msg)
    if rectangular is not None:
        if not isinstance(rectangular, bool):
            msg = f"rectangular must be a bool or None: {rectangular!r}"
            raise TypeError(msg)
        if style is not TransitionStyle.FLY:
            msg = f"rectangular applies only to Fly transitions, not {style.value!r}"
            raise ValueError(msg)
    return checked_scale


def _validate_direction(direction: object, style: TransitionStyle, scale: float | None) -> None:
    if isinstance(direction, bool) or not isinstance(direction, (int, str)):
        msg = f"direction must be an int, 'none', or None: {direction!r}"
        raise TypeError(msg)
    if isinstance(direction, str):
        if direction != "none":
            msg = f"direction string must be 'none', not {direction!r}"
            raise ValueError(msg)
        if style is not TransitionStyle.FLY or scale is None or scale == 1.0:
            msg = "direction='none' applies only to Fly transitions with scale != 1.0"
            raise ValueError(msg)
        return
    allowed = _DIRECTION_ANGLES.get(style, frozenset())
    if not allowed:
        msg = f"direction does not apply to {style.value!r} transitions"
        raise ValueError(msg)
    if direction not in allowed:
        msg = f"direction for {style.value!r} must be one of {sorted(allowed)}: {direction}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class Transition:
    """How a viewer transitions to a page during a presentation (``/Trans``, ISO 32000-2 Table 165).

    Optional parameters apply only to the styles for which ISO 32000-2 defines
    them: ``dimension`` on ``Split`` and ``Blinds``; ``motion`` on ``Split``,
    ``Box``, and ``Fly``; ``direction`` on ``Wipe``, ``Glitter``, ``Fly``,
    ``Push``, ``Cover``, and ``Uncover`` (with ``"none"`` only on ``Fly`` when
    ``scale`` is set and not ``1.0``); and ``scale`` and ``rectangular`` on
    ``Fly``. Incompatible combinations raise :class:`ValueError`.
    """

    style: TransitionStyle = TransitionStyle.REPLACE
    duration: float | None = None
    """Duration of the transition effect itself in seconds (``/D``)."""
    dimension: TransitionDimension | None = None
    """Horizontal or vertical axis for ``Split`` and ``Blinds`` (``/Dm``)."""
    motion: TransitionMotion | None = None
    """Inward or outward motion for ``Split``, ``Box``, and ``Fly`` (``/M``)."""
    direction: int | Literal["none"] | None = None
    """Counterclockwise angle in degrees (0, 90, 180, 270, 315) or ``"none"`` (``/Di``)."""
    scale: float | None = None
    """Starting or ending scale factor for ``Fly`` transitions (``/SS``, PDF 1.5)."""
    rectangular: bool | None = None
    """Whether the flown area is rectangular and opaque for ``Fly`` transitions (``/B``, PDF 1.5)."""

    def __post_init__(self) -> None:
        """Validate style and style-specific parameters."""
        style = cast("TransitionStyle", _enum_member(TransitionStyle, self.style, "style"))
        object.__setattr__(self, "style", style)

        if self.duration is not None:
            object.__setattr__(self, "duration", _non_negative_float(self.duration, "duration"))

        if self.dimension is not None:
            dim = _enum_member(TransitionDimension, self.dimension, "dimension")
            if style not in _DIMENSION_STYLES:
                msg = f"dimension applies only to Split and Blinds transitions, not {style.value!r}"
                raise ValueError(msg)
            object.__setattr__(self, "dimension", dim)

        if self.motion is not None:
            mot = _enum_member(TransitionMotion, self.motion, "motion")
            if style not in _MOTION_STYLES:
                msg = f"motion applies only to Split, Box, and Fly transitions, not {style.value!r}"
                raise ValueError(msg)
            object.__setattr__(self, "motion", mot)

        checked_scale = _validate_fly_options(style, self.scale, self.rectangular)
        if checked_scale is not None:
            object.__setattr__(self, "scale", checked_scale)

        if self.direction is not None:
            _validate_direction(self.direction, style, self.scale)


@dataclass(frozen=True, slots=True)
class TransitionAction:
    """An action that updates the display using a :class:`Transition` (``/S /Trans``, ISO 32000-2 Table 219)."""

    transition: Transition

    def __post_init__(self) -> None:
        """Validate ``transition``."""
        if not isinstance(self.transition, Transition):
            msg = f"transition must be a Transition: {self.transition!r}"
            raise TypeError(msg)


def _validate_step_action(action: object, name: str, page_count: int) -> int:
    """Validate an action target and return its UTF-8 byte length."""
    from pyropdf._targets import _target_to_core  # noqa: PLC0415 - _targets imports TransitionAction from this module.

    try:
        spec = _target_to_core(action, page_count)
    except TypeError as error:
        msg = (
            f"{name} must be a Destination, RemoteDestination, Launch, NamedAction, "
            f"TransitionAction, ThreadAction, URI string, or None: {type(action).__name__}"
        )
        raise TypeError(msg) from error
    utf8_bytes = sum(len(text.encode("utf-8")) for text in spec[4:] if text is not None)
    if utf8_bytes > _MAX_STEP_TEXT_BYTES:
        msg = f"{name} target text exceeds the {_MAX_STEP_TEXT_BYTES}-byte UTF-8 limit"
        raise ValueError(msg)
    return utf8_bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class NavigationStep:
    """One sub-page navigation step of a page's presentation sequence (``NavNode``, ISO 32000-2 Table 166).

    ``next_action`` runs when advancing forward (``/NA``), ``previous_action``
    when stepping backward (``/PA``), and ``duration`` sets how many seconds the
    viewer waits before advancing automatically (``/Dur``).
    """

    next_action: Target | None = None
    previous_action: Target | None = None
    duration: float | None = None

    def __post_init__(self) -> None:
        """Validate actions and duration."""
        _validate_step_action(self.next_action, "next_action", _UNBOUNDED_PAGES)
        _validate_step_action(self.previous_action, "previous_action", _UNBOUNDED_PAGES)
        if self.duration is not None:
            object.__setattr__(self, "duration", _non_negative_float(self.duration, "duration"))


def _is_dict(value: object) -> bool:
    return isinstance(value, Dictionary) and not isinstance(value, Stream)


def _transition_dict(transition: Transition) -> tuple[dict[str, object], tuple[int, int]]:
    """Serialize ``transition`` into a ``Transition`` dictionary and its minimum PDF version."""
    entries: dict[str, object] = {
        "Type": Name("Trans"),
        "S": Name(transition.style.value),
    }
    minimum = (1, 5) if transition.style in _PDF_15_STYLES else (1, 1)
    if transition.duration is not None:
        entries["D"] = transition.duration
    if transition.dimension is not None:
        entries["Dm"] = Name(transition.dimension.value)
    if transition.motion is not None:
        entries["M"] = Name(transition.motion.value)
    if transition.direction is not None:
        if transition.direction == "none":
            entries["Di"] = Name("None")
            minimum = max(minimum, (1, 5))
        else:
            entries["Di"] = transition.direction
    if transition.scale is not None:
        entries["SS"] = transition.scale
        minimum = max(minimum, (1, 5))
    if transition.rectangular is not None:
        entries["B"] = transition.rectangular
        minimum = max(minimum, (1, 5))
    return entries, minimum


def _transition_action_dict(action: TransitionAction) -> tuple[dict[str, object], tuple[int, int]]:
    """Serialize ``action`` into an ``ActionTransition`` dictionary and its minimum PDF version."""
    trans_dict, _ = _transition_dict(action.transition)
    return (
        {
            "Type": Name("Action"),
            "S": Name("Trans"),
            "Trans": trans_dict,
        },
        (1, 5),
    )


def _read_optional_non_negative(raw: object) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) and 0 <= raw <= _FLOAT32_MAX:
        return float(raw)
    return None


def _parse_enum_name(kind: type[enum.StrEnum], raw: object) -> enum.StrEnum | None:
    if not isinstance(raw, Name):
        return None
    try:
        return kind(str(raw))
    except ValueError:
        return None


def _parse_transition_direction(
    raw: object,
    style: TransitionStyle,
    scale: float | None,
) -> int | Literal["none"] | None:
    if isinstance(raw, Name) and str(raw) == "None":
        if style is TransitionStyle.FLY and scale is not None and scale != 1.0:
            return "none"
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
        angle = int(raw)
        if angle == raw and angle in _DIRECTION_ANGLES.get(style, frozenset()):
            return angle
    return None


def _read_transition(obj: object) -> Transition | None:
    """Read a ``/Trans`` dictionary, ignoring non-meaningful or malformed optional entries."""
    if not _is_dict(obj):
        return None
    trans = cast("Dictionary", obj)
    raw_style = trans.get("S")
    style = (
        TransitionStyle.REPLACE
        if raw_style is None
        else cast("TransitionStyle | None", _parse_enum_name(TransitionStyle, raw_style))
    )
    if style is None:
        return None

    dimension = (
        cast("TransitionDimension | None", _parse_enum_name(TransitionDimension, trans.get("Dm")))
        if style in _DIMENSION_STYLES
        else None
    )
    motion = (
        cast("TransitionMotion | None", _parse_enum_name(TransitionMotion, trans.get("M")))
        if style in _MOTION_STYLES
        else None
    )
    scale = _read_optional_non_negative(trans.get("SS")) if style is TransitionStyle.FLY else None
    raw_b = trans.get("B")
    rectangular = raw_b if style is TransitionStyle.FLY and isinstance(raw_b, bool) else None
    direction = _parse_transition_direction(trans.get("Di"), style, scale)

    return Transition(
        style=style,
        duration=_read_optional_non_negative(trans.get("D")),
        dimension=dimension,
        motion=motion,
        direction=direction,
        scale=scale,
        rectangular=rectangular,
    )


def _read_transition_action(holder: Dictionary) -> TransitionAction | None:
    """Return ``TransitionAction`` when ``holder['A']`` is a ``/S /Trans`` action, else ``None``."""
    action = holder.get("A")
    if not _is_dict(action):
        return None
    action_dict = cast("Dictionary", action)
    if action_dict.get("S") != "Trans":
        return None
    trans = _read_transition(action_dict.get("Trans"))
    return None if trans is None else TransitionAction(trans)


def _to_display(
    crop: tuple[float, float, float, float],
    rotation: int,
    unit: float,
    x: float,
    y: float,
) -> tuple[float, float]:
    """Convert PDF user-space ``(x, y)`` to display space (matching ``pyropdf_engine::display::to_display``)."""
    cx0, cy0, cx1, cy1 = crop
    if rotation == 90:  # noqa: PLR2004
        u, v = y - cy0, x - cx0
    elif rotation == 180:  # noqa: PLR2004
        u, v = cx1 - x, y - cy0
    elif rotation == 270:  # noqa: PLR2004
        u, v = cy1 - y, cx1 - x
    else:
        u, v = x - cx0, cy1 - y
    return (u * unit, v * unit)


def _to_pdf(
    crop: tuple[float, float, float, float],
    rotation: int,
    unit: float,
    x: float,
    y: float,
) -> tuple[float, float]:
    """Convert display-space ``(x, y)`` to PDF user space (matching ``pyropdf_engine::display::to_pdf``)."""
    cx0, cy0, cx1, cy1 = crop
    ux, uy = x / unit, y / unit
    if rotation == 90:  # noqa: PLR2004
        return (cx0 + uy, cy0 + ux)
    if rotation == 180:  # noqa: PLR2004
        return (cx1 - ux, cy0 + uy)
    if rotation == 270:  # noqa: PLR2004
        return (cx1 - uy, cy1 - ux)
    return (cx0 + ux, cy1 - uy)


def _pdf_real(value: float, label: str) -> float:
    if not math.isfinite(value) or abs(value) > _FLOAT32_MAX:
        msg = f"destination {label} {value!r} is outside the PDF real-number range"
        raise ValueError(msg)
    return value


def _goto_action_dict(document: Document, page_idx: int, point: Point | None, zoom: float | None) -> dict[str, object]:
    pages = document.pages
    if page_idx >= len(pages):
        msg = f"destination page {page_idx} is out of range (0..{len(pages) - 1})"
        raise ValueError(msg)
    target_page = pages[page_idx]
    if point is None and zoom is None:
        return {"Type": Name("Action"), "S": Name("GoTo"), "D": [target_page.obj, Name("Fit")]}
    if point is not None:
        space = _core.page_space(document._core(), target_page.xref, target_page._generation)  # noqa: SLF001
        if space is None:
            raise target_page._stale()  # noqa: SLF001
        rotation, crop, unit = space
        px, py = _to_pdf(crop, rotation, unit, point.x, point.y)
        left: float | None = _pdf_real(px, "left")
        top: float | None = _pdf_real(py, "top")
    else:
        left, top = None, None
    checked_zoom = None if zoom is None else _pdf_real(zoom, "zoom")
    return {"Type": Name("Action"), "S": Name("GoTo"), "D": [target_page.obj, Name("XYZ"), left, top, checked_zoom]}


def _action_dict_for_target(document: Document, target: Target) -> tuple[dict[str, object], tuple[int, int]]:
    """Build an action dictionary and its minimum PDF version for ``/NA`` or ``/PA`` on a ``NavNode``."""
    from pyropdf._actions import _target_action_dict  # noqa: PLC0415

    return _target_action_dict(document, target)


def _read_file_spec(value: object) -> str | None:
    if _is_dict(value):
        spec = cast("Dictionary", value)
        value = spec.get("UF") if "UF" in spec else spec.get("F")
    if isinstance(value, String):
        return value.text or None
    return None


def _opt_num(items: Array, index: int) -> float | None:
    if index >= len(items):
        return None
    raw = items[index]
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
        return float(raw)
    return None


def _decode_fit_view(
    items: Array,
    fit_name: str,
    crop: tuple[float, float, float, float],
    rotation: int,
    unit: float,
) -> tuple[tuple[float, float] | None, float | None]:
    if fit_name == "XYZ":
        left = _opt_num(items, 2)
        top = _opt_num(items, 3)
        raw_zoom = _opt_num(items, 4)
        point = (
            _to_display(crop, rotation, unit, crop[0] if left is None else left, crop[3] if top is None else top)
            if left is not None or top is not None
            else None
        )
        zoom = raw_zoom if raw_zoom is not None and raw_zoom > 0.0 else None
        return point, zoom
    if fit_name in {"FitH", "FitBH"}:
        top = _opt_num(items, 2)
        return (_to_display(crop, rotation, unit, crop[0], top) if top is not None else None), None
    if fit_name in {"FitV", "FitBV"}:
        left = _opt_num(items, 2)
        return (_to_display(crop, rotation, unit, left, crop[3]) if left is not None else None), None
    return None, None


def _read_remote_destination(action: Dictionary) -> RemoteDestination | None:
    from pyropdf._targets import RemoteDestination  # noqa: PLC0415 - _targets imports TransitionAction.

    file = _read_file_spec(action.get("F"))
    if not file:
        return None
    dest = action.get("D")
    page, name = 0, None
    if isinstance(dest, Name) and str(dest):
        name = str(dest)
    elif isinstance(dest, String) and len(dest) > 0:
        name = dest.text
    elif isinstance(dest, Array) and len(dest) and type(dest[0]) is int and dest[0] >= 0:
        page = dest[0]
    else:
        return None
    view = action.get("SD")
    if isinstance(view, Array) and 2 <= len(view) <= 6 and isinstance(view[0], String) and isinstance(view[1], Name):  # noqa: PLR2004 - Table 149 syntax.
        try:
            return RemoteDestination(
                file,
                page,
                name,
                structure_id=bytes(view[0]),
                structure_fit=str(view[1]),
                structure_operands=tuple(view[2:]),
            )
        except ValueError:
            pass
    return RemoteDestination(file, page, name)


class _ActionReadContext:
    """Per-walk lookup state for resolving ``/NA`` and ``/PA`` action dictionaries."""

    __slots__ = ("_document", "_named", "_page_by_id", "_pages", "_threads")

    def __init__(self, document: Document) -> None:
        self._document = document
        self._pages = _core.page_objects(document._core())  # noqa: SLF001
        self._page_by_id = {pair: idx for idx, pair in enumerate(self._pages)}
        self._named: dict[str, Destination | None] | None = None
        self._threads: dict[tuple[int, int], int] | None = None

    def _thread_map(self) -> dict[tuple[int, int], int]:
        if self._threads is None:
            from pyropdf._threads import _catalog_thread_ids  # noqa: PLC0415

            ids = _catalog_thread_ids(self._document)
            self._threads = {pair: idx for idx, pair in enumerate(ids)}
        return self._threads

    def named_destination(self, name: str) -> Destination | None:
        if self._named is None:
            self._named = dict(self._document.destinations)
        return self._named.get(name)

    def _resolve_page_entry(self, first: object) -> tuple[int, tuple[int, int]] | None:
        if _is_dict(first):
            page_dict = cast("Dictionary", first)
            if not page_dict.is_indirect or page_dict.xref is None or page_dict.generation is None:
                return None
            pair = (page_dict.xref, page_dict.generation)
            page_idx = self._page_by_id.get(pair)
            return None if page_idx is None else (page_idx, pair)
        if isinstance(first, int) and not isinstance(first, bool) and 0 <= first < len(self._pages):
            return first, self._pages[first]
        return None

    def explicit_destination(self, items: Array) -> Destination | None:
        from pyropdf._targets import Destination  # noqa: PLC0415 - _targets imports TransitionAction.

        if len(items) < 1:
            return None
        resolved = self._resolve_page_entry(items[0])
        if resolved is None:
            return None
        page_idx, pair = resolved
        space = _core.page_space(self._document._core(), pair[0], pair[1])  # noqa: SLF001
        if space is None:
            return None
        rotation, crop, unit = space
        fit = items[1] if len(items) >= 2 else None  # noqa: PLR2004
        fit_name = str(fit) if isinstance(fit, Name) else ""
        point, zoom = _decode_fit_view(items, fit_name, crop, rotation, unit)
        return Destination(page_idx, point, zoom)

    def _read_goto(self, action: Dictionary) -> Destination | None:
        dest = action.get("D")
        if isinstance(dest, Name):
            return self.named_destination(str(dest))
        if isinstance(dest, String):
            return self.named_destination(bytes(dest).decode("utf-8", "replace"))
        if isinstance(dest, Array):
            return self.explicit_destination(dest)
        return None

    def read_action(self, action_obj: object) -> Target | None:  # noqa: PLR0911
        from pyropdf._actions import _read_custom_action_dict  # noqa: PLC0415
        from pyropdf._targets import Launch, NamedAction  # noqa: PLC0415 - _targets imports TransitionAction.

        if not _is_dict(action_obj):
            return None
        action = cast("Dictionary", action_obj)
        kind = action.get("S")
        if not isinstance(kind, Name):
            return None
        if kind in {"URI", "Launch", "GoTo"}:
            expanded = _read_custom_action_dict(self._document, action)
            if expanded is not None:
                return expanded
        match str(kind):
            case "URI":
                uri = action.get("URI")
                return bytes(uri).decode("utf-8", "replace") if isinstance(uri, String) and len(uri) > 0 else None
            case "Named":
                name = action.get("N")
                return NamedAction(str(name)) if isinstance(name, Name) and str(name) else None
            case "Launch":
                file = _read_file_spec(action.get("F"))
                return Launch(file) if file else None
            case "GoToR":
                return _read_remote_destination(action)
            case "GoTo":
                return self._read_goto(action)
            case _:
                return _read_custom_action_dict(self._document, action, thread_index_map=self._thread_map())


def _indirect_xref(obj: object) -> int | None:
    if isinstance(obj, _Handle) and obj.is_indirect:
        return obj.xref
    return None


def _collect_node_action_xrefs(node: Dictionary) -> list[int]:
    xrefs: list[int] = []
    for action_key in ("NA", "PA"):
        action = node.get(action_key)
        action_xref = _indirect_xref(action)
        if action_xref is not None:
            xrefs.append(action_xref)
        if _is_dict(action):
            trans_xref = _indirect_xref(cast("Dictionary", action).get("Trans"))
            if trans_xref is not None:
                xrefs.append(trans_xref)
    return xrefs


def _unvisited_nav_neighbors(node: Dictionary, visited_nodes: set[int]) -> list[Dictionary]:
    neighbors: list[Dictionary] = []
    for key in ("Next", "Prev"):
        neighbor = node.get(key)
        if _is_dict(neighbor):
            xref = _indirect_xref(neighbor)
            if xref is None or xref not in visited_nodes:
                neighbors.append(cast("Dictionary", neighbor))
    return neighbors


def _collect_nav_candidates(root: object) -> list[int]:
    """Collect indirect object numbers along an existing ``/PresSteps`` chain iteratively."""
    if not _is_dict(root):
        return []
    stack: list[Dictionary] = [cast("Dictionary", root)]
    visited_nodes: set[int] = set()
    candidates: list[int] = []
    seen_candidates: set[int] = set()

    def record(xref: int | None) -> None:
        if xref is not None and xref not in seen_candidates:
            seen_candidates.add(xref)
            candidates.append(xref)

    steps = 0
    while stack and steps < _MAX_NAVIGATION_STEPS:
        node = stack.pop()
        steps += 1
        node_xref = _indirect_xref(node)
        if node_xref is not None:
            if node_xref in visited_nodes:
                continue
            visited_nodes.add(node_xref)
            record(node_xref)
        for xref in _collect_node_action_xrefs(node):
            record(xref)
        stack.extend(_unvisited_nav_neighbors(node, visited_nodes))
    return candidates


def _read_page_duration(page: Page) -> float | None:
    _ = page.index
    return _read_optional_non_negative(page.obj.get("Dur"))


def _write_page_duration(page: Page, duration: float) -> None:
    checked = _non_negative_float(duration, "duration")
    document = page.document
    with document.transaction("page duration"):
        _ = page.index
        page.obj["Dur"] = checked


def _delete_page_duration(page: Page) -> None:
    _ = page.index
    if "Dur" in page.obj:
        with page.document.transaction("page duration"):
            if "Dur" in page.obj:
                del page.obj["Dur"]


def _read_page_transition(page: Page) -> Transition | None:
    _ = page.index
    return _read_transition(page.obj.get("Trans"))


def _write_page_transition(page: Page, transition: Transition) -> None:
    if not isinstance(transition, Transition):
        msg = f"transition must be a Transition: {transition!r}"
        raise TypeError(msg)
    entries, minimum = _transition_dict(transition)
    document = page.document
    with document.transaction("page transition"):
        _ = page.index
        if minimum >= (1, 5):
            require_navigation_version(document, _catalog(document), minimum)
        old_xref = _indirect_xref(page.obj.get("Trans"))
        page.obj["Trans"] = entries
        if old_xref is not None:
            _core.object_free_unreferenced(document._core(), [old_xref])  # noqa: SLF001


def _delete_page_transition(page: Page) -> None:
    _ = page.index
    if "Trans" not in page.obj:
        return
    document = page.document
    with document.transaction("page transition"):
        old_xref = _indirect_xref(page.obj.get("Trans"))
        if "Trans" in page.obj:
            del page.obj["Trans"]
        if old_xref is not None:
            _core.object_free_unreferenced(document._core(), [old_xref])  # noqa: SLF001


def _read_navigation_steps(page: Page) -> tuple[NavigationStep, ...]:
    _ = page.index
    first = page.obj.get("PresSteps")
    if not _is_dict(first):
        return ()
    ctx = _ActionReadContext(page.document)
    steps: list[NavigationStep] = []
    seen: set[int] = set()
    current: Dictionary = cast("Dictionary", first)
    while True:
        xref = _indirect_xref(current)
        if xref is not None:
            if xref in seen:
                break
            seen.add(xref)
        if len(steps) >= _MAX_NAVIGATION_STEPS:
            code = "page_entry_count"
            msg = f"page /PresSteps exceeds the {_MAX_NAVIGATION_STEPS}-step safety limit"
            raise LimitError(code, msg)
        steps.append(
            NavigationStep(
                next_action=ctx.read_action(current.get("NA")),
                previous_action=ctx.read_action(current.get("PA")),
                duration=_read_optional_non_negative(current.get("Dur")),
            )
        )
        nxt = current.get("Next")
        if not _is_dict(nxt):
            break
        current = cast("Dictionary", nxt)
    return tuple(steps)


def _validate_steps_iterable(steps: Iterable[NavigationStep], page_count: int) -> list[NavigationStep]:
    if isinstance(steps, (str, bytes, bytearray, memoryview)) or not isinstance(steps, Iterable):
        msg = f"navigation_steps must be an iterable of NavigationStep records: {type(steps).__name__}"
        raise TypeError(msg)
    items = list(itertools.islice(steps, _MAX_NAVIGATION_STEPS + 1))
    if len(items) > _MAX_NAVIGATION_STEPS:
        msg = f"navigation_steps cannot contain more than {_MAX_NAVIGATION_STEPS} steps"
        raise ValueError(msg)
    total_bytes = 0
    for index, step in enumerate(items):
        if not isinstance(step, NavigationStep):
            msg = f"navigation step {index} must be a NavigationStep: {type(step).__name__}"
            raise TypeError(msg)
        total_bytes += _validate_step_action(step.next_action, "next_action", page_count)
        total_bytes += _validate_step_action(step.previous_action, "previous_action", page_count)
        if total_bytes > _MAX_STEP_TEXT_BYTES:
            msg = f"navigation_steps target text exceeds the {_MAX_STEP_TEXT_BYTES}-byte UTF-8 limit"
            raise ValueError(msg)
    return items


def _write_nav_chain(document: Document, items: list[NavigationStep]) -> tuple[int, tuple[int, int]]:
    minimum = (1, 5)
    prepared: list[tuple[dict[str, object] | None, dict[str, object] | None, float | None]] = []
    for step in items:
        na: dict[str, object] | None = None
        pa: dict[str, object] | None = None
        if step.next_action is not None:
            na, req = _action_dict_for_target(document, step.next_action)
            minimum = max(minimum, req)
        if step.previous_action is not None:
            pa, req = _action_dict_for_target(document, step.previous_action)
            minimum = max(minimum, req)
        prepared.append((na, pa, step.duration))
    xrefs = [document.objects.add({"Type": Name("NavNode")}) for _ in prepared]
    for index, (na, pa, dur) in enumerate(prepared):
        node_dict: dict[str, object] = {"Type": Name("NavNode")}
        if na is not None:
            node_dict["NA"] = na
        if pa is not None:
            node_dict["PA"] = pa
        if index > 0:
            node_dict["Prev"] = document.objects[xrefs[index - 1]]
        if index + 1 < len(xrefs):
            node_dict["Next"] = document.objects[xrefs[index + 1]]
        if dur is not None:
            node_dict["Dur"] = dur
        document.objects[xrefs[index]] = node_dict
    return xrefs[0], minimum


def _write_navigation_steps(page: Page, steps: Iterable[NavigationStep]) -> None:
    document = page.document
    items = _validate_steps_iterable(steps, _UNBOUNDED_PAGES)
    _ = page.index
    if items:
        _validate_steps_iterable(items, len(document.pages))
    elif "PresSteps" not in page.obj:
        return

    with document.transaction("page navigation steps"):
        _ = page.index
        candidates = _collect_nav_candidates(page.obj.get("PresSteps"))
        if not items:
            if "PresSteps" in page.obj:
                del page.obj["PresSteps"]
        else:
            first_xref, minimum = _write_nav_chain(document, items)
            require_navigation_version(document, _catalog(document), minimum)
            page.obj["PresSteps"] = document.objects[first_xref]
        if candidates:
            _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001


def _delete_navigation_steps(page: Page) -> None:
    _ = page.index
    if "PresSteps" not in page.obj:
        return
    document = page.document
    with document.transaction("page navigation steps"):
        candidates = _collect_nav_candidates(page.obj.get("PresSteps"))
        if "PresSteps" in page.obj:
            del page.obj["PresSteps"]
        if candidates:
            _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001


def _read_page_thumbnail(page: Page) -> Pixmap | None:
    _ = page.index
    thumb = page.obj.get("Thumb")
    if not isinstance(thumb, Stream) or not thumb.is_indirect:
        return None
    try:
        return _core.thumbnail_image(page.document._core(), page.xref, page._generation)  # noqa: SLF001
    except StaleObjectError as error:
        raise page._stale() from error  # noqa: SLF001


def _write_page_thumbnail(page: Page, pixmap: Pixmap) -> None:
    if not isinstance(pixmap, Pixmap):
        msg = f"thumbnail must be a Pixmap: {pixmap!r}"
        raise TypeError(msg)
    _ = page.index
    opaque = pixmap.without_alpha() if pixmap.alpha else pixmap
    samples = bytes(memoryview(opaque))
    colorspace = Name("DeviceGray" if opaque.colorspace is Colorspace.GRAY else "DeviceRGB")
    thumb_dict: dict[str, object] = {
        "Type": Name("XObject"),
        "Subtype": Name("Image"),
        "Width": opaque.width,
        "Height": opaque.height,
        "ColorSpace": colorspace,
        "BitsPerComponent": 8,
    }
    document = page.document
    with document.transaction("page thumbnail"):
        _ = page.index
        require_navigation_version(document, _catalog(document), (1, 3))
        old_xref = _indirect_xref(page.obj.get("Thumb"))
        xref = document.objects.add_stream(samples, dictionary=thumb_dict, filter="FlateDecode")
        page.obj["Thumb"] = document.objects[xref]
        if old_xref is not None:
            _core.object_free_unreferenced(document._core(), [old_xref])  # noqa: SLF001


def _delete_page_thumbnail(page: Page) -> None:
    _ = page.index
    if "Thumb" not in page.obj:
        return
    document = page.document
    with document.transaction("page thumbnail"):
        old_xref = _indirect_xref(page.obj.get("Thumb"))
        if "Thumb" in page.obj:
            del page.obj["Thumb"]
        if old_xref is not None:
            _core.object_free_unreferenced(document._core(), [old_xref])  # noqa: SLF001


for _public in (
    TransitionStyle,
    TransitionDimension,
    TransitionMotion,
    Transition,
    TransitionAction,
    NavigationStep,
):
    _public.__module__ = "pyropdf"

__all__ = [
    "NavigationStep",
    "Transition",
    "TransitionAction",
    "TransitionDimension",
    "TransitionMotion",
    "TransitionStyle",
]
