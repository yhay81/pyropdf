"""Canvas: drawing paths, images, and other pages on a page, in paint order."""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pyropdf import _core
from pyropdf._content import (
    BlendMode,
    ClosePath,
    Color,
    CurveTo,
    FillRule,
    LineCap,
    LineJoin,
    LineTo,
    MoveTo,
    Path,
    Segment,
    Stroke,
)
from pyropdf._errors import LimitError, PdfError
from pyropdf._fonts import (
    _DEFAULT_MAX_GENERATED_TEXT_SIZE,
    Font,
    _max_lines,
    _text_input,
    _textbox_text,
)
from pyropdf._geometry import Matrix, Point, Rect
from pyropdf._images import PageImage, _image_stream, add_image
from pyropdf._objects import Name, Stream
from pyropdf._operations import _DEFAULT_MAX_CONTENT_SIZE

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import TracebackType
    from typing import Self, TypeAlias

    from pyropdf._core import Pixmap
    from pyropdf._document import Document
    from pyropdf._objects import Dictionary
    from pyropdf._page import Page

    PathLike: TypeAlias = "PathBuilder | Path | Iterable[Segment]"
    _Affine: TypeAlias = tuple[float, float, float, float, float, float]

#: The control-point factor that approximates a quarter circle with a cubic curve.
_KAPPA = 4 * (math.sqrt(2) - 1) / 3
#: The height of a wavy line's turning points in units of its breadth.
_WAVE_HEIGHT = 2.4142135623765633
#: The control-point factor of a curve pulled toward a corner, as pymupdf's ``draw_curve``.
_KAPPA_CURVE = 0.55228474983
#: The largest magnitude of a PDF real that readers hold as a 32-bit float.
_FLOAT32_MAX = 3.4028234663852886e38
#: The deepest nesting of clip, transform, opacity, and blend scopes.
_MAX_SCOPES = 16
#: The most path and clip points that one canvas holds.
_MAX_POINTS = 1_048_576
_BLACK = Color(0.0, 0.0, 0.0)
_CAPS = {LineCap.BUTT: 0, LineCap.ROUND: 1, LineCap.SQUARE: 2}
_JOINS = {LineJoin.MITER: 0, LineJoin.ROUND: 1, LineJoin.BEVEL: 2}
_BLEND_NAMES = {
    BlendMode.NORMAL: "Normal",
    BlendMode.MULTIPLY: "Multiply",
    BlendMode.SCREEN: "Screen",
    BlendMode.OVERLAY: "Overlay",
    BlendMode.DARKEN: "Darken",
    BlendMode.LIGHTEN: "Lighten",
    BlendMode.COLOR_DODGE: "ColorDodge",
    BlendMode.COLOR_BURN: "ColorBurn",
    BlendMode.HARD_LIGHT: "HardLight",
    BlendMode.SOFT_LIGHT: "SoftLight",
    BlendMode.DIFFERENCE: "Difference",
    BlendMode.EXCLUSION: "Exclusion",
    BlendMode.HUE: "Hue",
    BlendMode.SATURATION: "Saturation",
    BlendMode.COLOR: "Color",
    BlendMode.LUMINOSITY: "Luminosity",
}
#: The alignments of :meth:`Canvas.textbox`, in the order of the core's codes.
_ALIGNMENTS = ("left", "center", "right", "justify")
#: The cosine and sine of each clockwise quarter turn, in display space.
_TURNS = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}
#: The Standard 14 fonts that draw their own built-in encoding.
_SYMBOLIC = frozenset({"Symbol", "ZapfDingbats"})
#: Operators that change the graphics state that later content inherits.
_STATE_OPERATORS = frozenset(
    {"cm", "gs", "W", "W*", "w", "J", "j", "M", "d", "ri", "i", "Tc", "Tw", "Tz", "TL", "Tf", "Tr", "Ts"}
)


def _number(value: object, name: str) -> float:
    """Return a finite number within PDF real range."""
    if isinstance(value, (bool, str, bytes)) or not hasattr(value, "__float__"):
        msg = f"{name} must be a number: {value!r}"
        raise TypeError(msg)
    number = float(cast("float", value))
    if not (math.isfinite(number) and abs(number) <= _FLOAT32_MAX):
        msg = f"{name} must be a finite number within PDF real range: {value!r}"
        raise ValueError(msg)
    return number


def _point(value: Sequence[float], name: str) -> Point:
    """Return a point from a :class:`Point` or a pair of numbers."""
    try:
        point = Point(*value)
    except (TypeError, ValueError) as exc:
        msg = f"{name} must be a point (x, y): {value!r}"
        raise ValueError(msg) from exc
    _number(point.x, name)
    _number(point.y, name)
    return point


def _rect(value: Sequence[float], name: str) -> Rect:
    """Return a rectangle with a positive size."""
    try:
        rect = Rect(*value)
    except (TypeError, ValueError) as exc:
        msg = f"{name} must be a rectangle (x0, y0, x1, y1): {value!r}"
        raise ValueError(msg) from exc
    for coordinate in rect:
        _number(coordinate, name)
    if not (rect.x1 > rect.x0 and rect.y1 > rect.y0):
        msg = f"{name} must have x0 < x1 and y0 < y1: {value!r}"
        raise ValueError(msg)
    return rect


def _unit(value: object, name: str) -> float:
    """Return a number from 0 to 1."""
    number = _number(value, name)
    if not 0 <= number <= 1:
        msg = f"{name} must be a number from 0 to 1: {value!r}"
        raise ValueError(msg)
    return number


def _quarter_turn(rotate: object) -> int:
    """Return a clockwise right-angle turn normalized to 0 through 270 degrees."""
    if isinstance(rotate, bool) or not isinstance(rotate, int) or rotate % 90 != 0:
        msg = f"rotate must be a multiple of 90: {rotate!r}"
        raise ValueError(msg)
    return rotate % 360


def _fmt(value: float) -> bytes:
    """Return a number as the shortest content-stream real that keeps four decimals."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return b"0" if text in ("", "-0") else text.encode("ascii")


def _numbers(*values: float) -> bytes:
    return b" ".join(_fmt(value) for value in values)


def _color(value: object, name: str) -> Color:
    if not isinstance(value, Color):
        msg = f"{name} must be a pyropdf.Color, not {type(value).__name__}"
        raise TypeError(msg)
    for component in ("red", "green", "blue", "alpha"):
        _unit(getattr(value, component), f"{name}.{component}")
    return value


def _stroke(value: Stroke | Color | None) -> Stroke | None:
    """Return a stroke from a :class:`Stroke` or a :class:`Color`, checked."""
    if value is None:
        return None
    stroke = Stroke(paint=value) if isinstance(value, Color) else value
    if not isinstance(stroke, Stroke):
        msg = f"stroke must be a pyropdf.Stroke or Color, not {type(value).__name__}"
        raise TypeError(msg)
    if not isinstance(stroke.paint, Color):
        msg = "a canvas strokes with a Color; pattern paints cannot be drawn yet"
        raise TypeError(msg)
    _color(stroke.paint, "stroke.paint")
    # A width of 0 draws the thinnest line that the device can show.
    if _number(stroke.width, "stroke.width") < 0:
        msg = f"stroke.width must be non-negative: {stroke.width!r}"
        raise ValueError(msg)
    if _number(stroke.miter_limit, "stroke.miter_limit") < 1:
        msg = f"stroke.miter_limit must be at least 1: {stroke.miter_limit!r}"
        raise ValueError(msg)
    dashes = [_number(length, "stroke.dashes") for length in stroke.dashes]
    if any(length < 0 for length in dashes) or (dashes and not any(dashes)):
        msg = f"stroke.dashes must be non-negative lengths, not all zero: {stroke.dashes!r}"
        raise ValueError(msg)
    _number(stroke.dash_offset, "stroke.dash_offset")
    LineCap(stroke.cap)
    LineJoin(stroke.join)
    return stroke


def _stroke_ops(stroke: Stroke) -> list[bytes]:
    """Return the operators that set a checked stroke's paint and line style."""
    paint = cast("Color", stroke.paint)
    return [
        _numbers(*paint.rgb) + b" RG",
        _numbers(stroke.width) + b" w",
        f"{_CAPS[LineCap(stroke.cap)]} J {_JOINS[LineJoin(stroke.join)]} j".encode(),
        _numbers(stroke.miter_limit) + b" M",
        b"[" + _numbers(*stroke.dashes) + b"] " + _numbers(stroke.dash_offset) + b" d",
    ]


def _canvas_font(text: str, font: Font | None) -> Font:
    """Return the font that draws ``text``: ``font``, else Helvetica.

    A Standard 14 font draws WinAnsi text only.
    """
    if font is None:
        font = Font.standard("Helvetica")
    elif not isinstance(font, Font):
        msg = f"font must be a pyropdf.Font, not {type(font).__name__}"
        raise TypeError(msg)
    try:
        text.encode("cp1252")
    except UnicodeEncodeError as exc:
        msg = (
            "Standard 14 fonts can only print WinAnsi (Latin-1-equivalent) characters; "
            "embedded fonts come with issue 153"
        )
        raise ValueError(msg) from exc
    return font


def _size(value: object) -> float:
    """Return a positive font size."""
    size = _number(value, "size")
    if not size > 0:
        msg = f"size must be positive: {value!r}"
        raise ValueError(msg)
    return size


def _line_height(value: object) -> float | None:
    """Return a positive line-height multiplier, or ``None`` for the font's default."""
    if value is None:
        return None
    line_height = _number(value, "line_height")
    if not line_height > 0:
        msg = f"line_height must be positive: {value!r}"
        raise ValueError(msg)
    return line_height


def _text_paint(fill: Color | None, stroke: Stroke | Color | None) -> tuple[Color | None, Stroke | None]:
    """Return the fill and stroke of text, which needs at least one."""
    if fill is None and stroke is None:
        msg = "text needs a fill or a stroke"
        raise ValueError(msg)
    return (None if fill is None else _color(fill, "fill")), _stroke(stroke)


@dataclass(frozen=True, slots=True)
class TextFit:
    """How text fits a :meth:`Canvas.textbox`: the height left below its last line.

    A negative :attr:`spare_height` is the shortfall of text that did not
    fit, and was not drawn.
    """

    spare_height: float

    @property
    def fits(self) -> bool:
        """Whether the text fit its box and was drawn."""
        return self.spare_height >= 0


def _segments(path: object) -> list[Segment]:
    """Return the segments of a path, a builder, or an iterable of segments, checked."""
    if isinstance(path, (PathBuilder, Path)):
        segments: Iterable[object] = path.segments
    elif isinstance(path, Iterable) and not isinstance(path, (str, bytes)):
        segments = path
    else:
        msg = f"path must be a PathBuilder, a Path, or segments, not {type(path).__name__}"
        raise TypeError(msg)
    checked: list[Segment] = []
    for segment in segments:
        if not isinstance(segment, (MoveTo, LineTo, CurveTo, ClosePath)):
            msg = f"a path segment must be MoveTo, LineTo, CurveTo, or ClosePath, not {type(segment).__name__}"
            raise TypeError(msg)
        checked.append(segment)
    if not checked:
        msg = "the path has no segments"
        raise ValueError(msg)
    if not isinstance(checked[0], MoveTo):
        msg = "a path starts with MoveTo"
        raise ValueError(msg)  # noqa: TRY004 - the segments' order, not their type
    return checked


def _path_bytes(segments: list[Segment]) -> tuple[bytes, int]:
    """Return the path construction operators of ``segments`` and their point count."""
    parts: list[bytes] = []
    points = 0
    for segment in segments:
        if isinstance(segment, MoveTo):
            parts.append(_numbers(*_point(segment.to, "point")) + b" m")
            points += 1
        elif isinstance(segment, LineTo):
            parts.append(_numbers(*_point(segment.to, "point")) + b" l")
            points += 1
        elif isinstance(segment, CurveTo):
            c1, c2, to = _point(segment.c1, "c1"), _point(segment.c2, "c2"), _point(segment.to, "to")
            parts.append(_numbers(*c1, *c2, *to) + b" c")
            points += 3
        else:
            parts.append(b"h")
    return b"\n".join(parts), points


def _affine(function: Callable[[float, float], tuple[float, float]]) -> _Affine:
    """Return the matrix of an affine map from three of its points."""
    ex, ey = function(0.0, 0.0)
    ax, ay = function(1.0, 0.0)
    cx, cy = function(0.0, 1.0)
    return (ax - ex, ay - ey, cx - ex, cy - ey, ex, ey)


def _display_from_user(
    rotation: int, box: tuple[float, float, float, float]
) -> Callable[[float, float], tuple[float, float]]:
    """Return the map from a page's user space to its display space."""
    x0, y0, x1, y1 = box

    def convert(x: float, y: float) -> tuple[float, float]:
        if rotation == 90:  # noqa: PLR2004 - a quarter turn
            return (y - y0, x - x0)
        if rotation == 180:  # noqa: PLR2004 - a half turn
            return (x1 - x, y - y0)
        if rotation == 270:  # noqa: PLR2004 - three quarter turns
            return (y1 - y, x1 - x)
        return (x - x0, y1 - y)

    return convert


def _user_from_display(rotation: int, box: tuple[float, float, float, float]) -> _Affine:
    """Return the matrix from a page's display space to its user space."""
    x0, y0, x1, y1 = box

    def convert(x: float, y: float) -> tuple[float, float]:
        if rotation == 90:  # noqa: PLR2004 - a quarter turn
            return (x0 + y, y0 + x)
        if rotation == 180:  # noqa: PLR2004 - a half turn
            return (x1 - x, y0 + y)
        if rotation == 270:  # noqa: PLR2004 - three quarter turns
            return (x1 - y, y1 - x)
        return (x0 + x, y1 - y)

    return _affine(convert)


def _placement(
    rect: Rect, width: float, height: float, *, keep: bool, rotation: int
) -> tuple[Rect, Callable[[float, float], tuple[float, float]]]:
    """Fit a ``width`` by ``height`` picture, turned clockwise by ``rotation``, into ``rect``.

    Returns the rectangle it covers and the map from the picture's own
    top-left-origin coordinates, unturned, to display space.
    """
    turned = rotation % 180 == 90  # noqa: PLR2004 - a quarter turn swaps the sides
    seen_w, seen_h = (height, width) if turned else (width, height)
    if keep:
        scale = min(rect.width / seen_w, rect.height / seen_h)
        out_w, out_h = seen_w * scale, seen_h * scale
    else:
        out_w, out_h = rect.width, rect.height
    left = rect.x0 + (rect.width - out_w) / 2
    top = rect.y0 + (rect.height - out_h) / 2
    # The unturned picture's size in display units.
    in_w, in_h = (out_h, out_w) if turned else (out_w, out_h)

    def place(x: float, y: float) -> tuple[float, float]:
        px, py = x / width * in_w, y / height * in_h
        if rotation == 90:  # noqa: PLR2004 - a quarter turn
            px, py = in_h - py, px
        elif rotation == 180:  # noqa: PLR2004 - a half turn
            px, py = in_w - px, in_h - py
        elif rotation == 270:  # noqa: PLR2004 - three quarter turns
            px, py = py, in_w - px
        return (left + px, top + py)

    return Rect(left, top, left + out_w, top + out_h), place


class PathBuilder:
    """Geometry for :meth:`Canvas.path` and :meth:`Canvas.clip`, built from segments.

    Every method adds segments in display coordinates and returns the
    builder, so calls chain: ``PathBuilder().move_to(a).line_to(b).close()``.
    The constructors (:meth:`rect`, :meth:`ellipse`, :meth:`circle`,
    :meth:`polygon`, :meth:`sector`, :meth:`zigzag`, and :meth:`wave`) each
    add complete subpaths.
    """

    __slots__ = ("_current", "_segments", "_start")

    def __init__(self) -> None:
        """Start an empty path."""
        self._segments: list[Segment] = []
        self._current: Point | None = None
        self._start: Point | None = None

    @property
    def segments(self) -> tuple[Segment, ...]:
        """Return the segments added so far."""
        return tuple(self._segments)

    @property
    def current_point(self) -> Point | None:
        """Return the end of the last segment, or ``None`` before the first."""
        return self._current

    def move_to(self, point: Sequence[float]) -> Self:
        """Start a subpath at ``point``."""
        to = _point(point, "point")
        self._segments.append(MoveTo(to))
        self._current = self._start = to
        return self

    def _require_current(self) -> Point:
        if self._current is None:
            msg = "start the path with move_to()"
            raise ValueError(msg)
        return self._current

    def line_to(self, point: Sequence[float]) -> Self:
        """Add a line to ``point``."""
        self._require_current()
        to = _point(point, "point")
        self._segments.append(LineTo(to))
        self._current = to
        return self

    def curve_to(self, control1: Sequence[float], control2: Sequence[float], point: Sequence[float]) -> Self:
        """Add a cubic Bézier curve to ``point``."""
        self._require_current()
        to = _point(point, "point")
        self._segments.append(CurveTo(_point(control1, "control1"), _point(control2, "control2"), to))
        self._current = to
        return self

    def quad_to(self, control: Sequence[float], point: Sequence[float]) -> Self:
        """Add a quadratic Bézier curve to ``point``, as the equal cubic curve."""
        start = self._require_current()
        c = _point(control, "control")
        to = _point(point, "point")
        control1 = Point(start.x + 2 / 3 * (c.x - start.x), start.y + 2 / 3 * (c.y - start.y))
        control2 = Point(to.x + 2 / 3 * (c.x - to.x), to.y + 2 / 3 * (c.y - to.y))
        return self.curve_to(control1, control2, to)

    def close(self) -> Self:
        """Close the current subpath with a line to its start."""
        self._require_current()
        self._segments.append(ClosePath())
        self._current = self._start
        return self

    def rect(self, rect: Sequence[float], *, radius: float = 0) -> Self:
        """Add a rectangle, with corners rounded by ``radius`` points."""
        r = _rect(rect, "rect")
        rounding = _number(radius, "radius")
        if rounding < 0:
            msg = f"radius must not be negative: {radius!r}"
            raise ValueError(msg)
        rounding = min(rounding, (r.x1 - r.x0) / 2, (r.y1 - r.y0) / 2)
        if rounding == 0:
            self.move_to((r.x0, r.y0)).line_to((r.x1, r.y0)).line_to((r.x1, r.y1)).line_to((r.x0, r.y1))
            return self.close()
        k = rounding * (1 - _KAPPA)
        self.move_to((r.x0 + rounding, r.y0))
        self.line_to((r.x1 - rounding, r.y0))
        self.curve_to((r.x1 - k, r.y0), (r.x1, r.y0 + k), (r.x1, r.y0 + rounding))
        self.line_to((r.x1, r.y1 - rounding))
        self.curve_to((r.x1, r.y1 - k), (r.x1 - k, r.y1), (r.x1 - rounding, r.y1))
        self.line_to((r.x0 + rounding, r.y1))
        self.curve_to((r.x0 + k, r.y1), (r.x0, r.y1 - k), (r.x0, r.y1 - rounding))
        self.line_to((r.x0, r.y0 + rounding))
        self.curve_to((r.x0, r.y0 + k), (r.x0 + k, r.y0), (r.x0 + rounding, r.y0))
        return self.close()

    def ellipse(self, rect: Sequence[float]) -> Self:
        """Add the ellipse inside ``rect``."""
        r = _rect(rect, "rect")
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        rx, ry = (r.x1 - r.x0) / 2, (r.y1 - r.y0) / 2
        kx, ky = rx * _KAPPA, ry * _KAPPA
        self.move_to((r.x1, cy))
        self.curve_to((r.x1, cy + ky), (cx + kx, r.y1), (cx, r.y1))
        self.curve_to((cx - kx, r.y1), (r.x0, cy + ky), (r.x0, cy))
        self.curve_to((r.x0, cy - ky), (cx - kx, r.y0), (cx, r.y0))
        self.curve_to((cx + kx, r.y0), (r.x1, cy - ky), (r.x1, cy))
        return self.close()

    def circle(self, center: Sequence[float], radius: float) -> Self:
        """Add a circle."""
        c = _point(center, "center")
        size = _number(radius, "radius")
        if not size > 0:
            msg = f"radius must be positive: {radius!r}"
            raise ValueError(msg)
        return self.ellipse((c.x - size, c.y - size, c.x + size, c.y + size))

    def polygon(self, points: Iterable[Sequence[float]], *, close: bool = True) -> Self:
        """Add straight segments through ``points``, closed unless ``close`` is false."""
        corners = [_point(point, "points") for point in points]
        if len(corners) < 2:  # noqa: PLR2004 - a segment needs two points.
            msg = "points must contain at least two points"
            raise ValueError(msg)
        self.move_to(corners[0])
        for corner in corners[1:]:
            self.line_to(corner)
        return self.close() if close else self

    def sector(
        self,
        center: Sequence[float],
        radius: float,
        start: float,
        sweep: float,
        *,
        pie: bool = True,
    ) -> Self:
        """Add an arc of ``sweep`` degrees around ``center``, from ``start`` degrees.

        Angles run clockwise on the page from the positive x axis. With
        ``pie``, lines join the arc's ends to the centre, which closes it.
        """
        c = _point(center, "center")
        size = _number(radius, "radius")
        if not size > 0:
            msg = f"radius must be positive: {radius!r}"
            raise ValueError(msg)
        first = math.radians(_number(start, "start"))
        total = math.radians(_number(sweep, "sweep"))
        if total == 0:
            msg = "sweep must not be zero"
            raise ValueError(msg)
        total = max(-2 * math.pi, min(2 * math.pi, total))
        steps = max(1, math.ceil(abs(total) / (math.pi / 2) - 1e-9))
        step = total / steps
        factor = 4 / 3 * math.tan(step / 4)

        def at(angle: float) -> Point:
            return Point(c.x + size * math.cos(angle), c.y + size * math.sin(angle))

        begin = at(first)
        if pie:
            self.move_to(c).line_to(begin)
        else:
            self.move_to(begin)
        angle = first
        for _ in range(steps):
            end = angle + step
            p0, p3 = at(angle), at(end)
            control1 = Point(p0.x - factor * size * math.sin(angle), p0.y + factor * size * math.cos(angle))
            control2 = Point(p3.x + factor * size * math.sin(end), p3.y - factor * size * math.cos(end))
            self.curve_to(control1, control2, p3)
            angle = end
        return self.close() if pie else self

    @staticmethod
    def _turns(start: Point, end: Point, breadth: float, height: float, *, on_line: bool) -> list[Point]:
        """Return the turning points of a wave from ``start`` to ``end``."""
        if not breadth > 0:
            msg = f"breadth must be positive: {breadth!r}"
            raise ValueError(msg)
        length = math.dist(start, end)
        count = 4 * round(length / (4 * breadth))
        if count < 4:  # noqa: PLR2004 - one full phase has four steps.
            msg = "the points are too close for one wave of this breadth"
            raise ValueError(msg)
        step = length / count
        ux, uy = (end.x - start.x) / length, (end.y - start.y) / length
        points = []
        for i in range(1, count):
            if i % 4 == 1:
                offset = -height
            elif i % 4 == 3:  # noqa: PLR2004 - the second turn of each phase.
                offset = height
            elif on_line:
                offset = 0.0
            else:
                continue
            u, v = i * step, offset * step
            points.append(Point(start.x + u * ux - v * uy, start.y + u * uy + v * ux))
        return points

    def zigzag(self, start: Sequence[float], end: Sequence[float], *, breadth: float = 2) -> Self:
        """Add a zigzag line from ``start`` to ``end`` whose teeth are ``breadth`` points wide."""
        first = _point(start, "start")
        last = _point(end, "end")
        turns = self._turns(first, last, _number(breadth, "breadth"), 1.0, on_line=False)
        return self.polygon([first, *turns, last], close=False)

    def wave(self, start: Sequence[float], end: Sequence[float], *, breadth: float = 2) -> Self:
        """Add a wavy line from ``start`` to ``end`` whose waves are ``breadth`` points wide."""
        first = _point(start, "start")
        last = _point(end, "end")
        points = [first, *self._turns(first, last, _number(breadth, "breadth"), _WAVE_HEIGHT, on_line=True), last]
        self.move_to(first)
        for i in range(0, len(points) - 2, 2):
            begin, corner, finish = points[i], points[i + 1], points[i + 2]
            self.curve_to(
                (begin.x + (corner.x - begin.x) * _KAPPA_CURVE, begin.y + (corner.y - begin.y) * _KAPPA_CURVE),
                (finish.x + (corner.x - finish.x) * _KAPPA_CURVE, finish.y + (corner.y - finish.y) * _KAPPA_CURVE),
                finish,
            )
        return self

    def __repr__(self) -> str:
        """Return the segment count."""
        return f"PathBuilder({len(self._segments)} segments)"


class _Scope(AbstractContextManager[None]):
    """One clip, transform, opacity, or blend scope of a canvas."""

    def __init__(self, canvas: Canvas, opening: bytes, *, group: float | None = None) -> None:
        self._canvas = canvas
        self._opening = opening
        self._group = group

    def __enter__(self) -> None:
        self._canvas._push(self._opening, group=self._group)  # noqa: SLF001

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._canvas._pop()  # noqa: SLF001


class _Images:
    """The images and pages that a document's canvases wrote, keyed by their content."""

    __slots__ = ("forms", "images")

    def __init__(self) -> None:
        self.images: dict[bytes, int] = {}
        self.forms: dict[tuple[int, int, int, bytes], int] = {}


class Canvas:
    """Drawing on one page in paint order, written as one content stream.

    Get one with :meth:`Page.draw` and use it in a ``with`` block: the
    drawing is written when the block ends normally, in one transaction and
    one undo step, and discarded after an exception. Everything is in
    display coordinates, the top-left-origin space of :attr:`Page.rect`,
    and drawn in the order of the calls, over the page's content or, with
    ``underlay=True``, beneath it.

    Paths take a ``fill`` :class:`Color` and a ``stroke``, a :class:`Stroke`
    or a :class:`Color` for a 1-point line; a path with neither is stroked
    with a 1-point black line. A color's alpha below 1 paints it
    translucent. The scopes :meth:`clip`, :meth:`transform`,
    :meth:`opacity`, and :meth:`blend` are context managers that apply to
    everything drawn inside them. :meth:`text` and :meth:`textbox` draw
    text in a :class:`Font`; embedded fonts come with issue 153 (DESIGN.md
    section 7).

    A canvas holds at most 1,048,576 path points, of paths and clips
    together, nests at most 16 scopes, and writes at most 64 MiB of
    content; more raise :class:`LimitError` with code
    ``content_element_count`` or ``content_input_size``, or
    ``ValueError`` for the nesting.
    """

    __slots__ = (
        "_closed",
        "_depth",
        "_entered",
        "_fonts",
        "_gstates",
        "_layers",
        "_page",
        "_points",
        "_underlay",
        "_xobjects",
    )

    def __init__(self, page: Page, *, underlay: bool = False) -> None:
        """Start an empty canvas on ``page``; :meth:`Page.draw` is the usual way."""
        from pyropdf._page import Page  # noqa: PLC0415 - pyropdf._page imports this module.

        if not isinstance(page, Page):
            msg = f"page must be a pyropdf.Page, not {type(page).__name__}"
            raise TypeError(msg)
        self._page = page
        self._underlay = bool(underlay)
        # One buffer per open opacity group; the first is the page's content.
        self._layers: list[tuple[list[bytes], float | None]] = [([], None)]
        self._depth = 0
        self._points = 0
        self._xobjects: dict[str, int] = {}
        self._fonts: dict[str, str] = {}
        self._gstates: dict[tuple[float | None, float | None, str | None], str] = {}
        self._closed = False
        self._entered = False

    # Writing.

    def __enter__(self) -> Self:
        """Open the canvas's transaction and return the canvas; leaving the block writes it."""
        if self._closed or self._entered:
            msg = "a canvas is used in one with block; start another with page.draw()"
            raise ValueError(msg)
        _core.begin_transaction(self._page.document._core(), "draw")  # noqa: SLF001
        self._entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Write the drawing after a normal exit; discard it after an exception."""
        if self._closed or not self._entered:
            return
        self._closed = True
        core = self._page.document._core()  # noqa: SLF001
        keep = exc_type is None and bool(self._layers[0][0])
        if keep and self._depth:
            _core.end_transaction(core, False)  # noqa: FBT003 - the core's flag
            msg = "a canvas scope is still open"
            raise RuntimeError(msg)
        try:
            if keep:
                self._commit()
        except BaseException:
            _core.end_transaction(core, False)  # noqa: FBT003 - the core's flag
            raise
        _core.end_transaction(core, keep)

    def _check_open(self) -> None:
        if self._closed or not self._entered:
            msg = "draw inside the canvas's with block: with page.draw() as canvas: ..."
            raise ValueError(msg)

    def _emit(self, data: bytes) -> None:
        self._check_open()
        self._layers[-1][0].append(data)

    def _count(self, points: int) -> None:
        self._points += points
        if self._points > _MAX_POINTS:
            code = "content_element_count"
            msg = f"the canvas holds more than {_MAX_POINTS} path points"
            raise LimitError(code, msg)

    def _push(self, opening: bytes, *, group: float | None) -> None:
        self._check_open()
        if self._depth >= _MAX_SCOPES:
            msg = f"canvas scopes nest at most {_MAX_SCOPES} deep"
            raise ValueError(msg)
        self._depth += 1
        if group is not None:
            self._layers.append(([], group))
        else:
            self._emit(b"q\n" + opening)

    def _pop(self) -> None:
        self._depth -= 1
        parts, group = self._layers[-1]
        if group is None or len(self._layers) == 1:
            self._layers[-1][0].append(b"Q")
            return
        self._layers.pop()
        if not parts:
            return
        name = self._add_xobject(self._group_form(b"\n".join(parts)))
        gstate = self._gstate(fill_alpha=group, stroke_alpha=group)
        self._emit(b"q\n/" + gstate.encode() + b" gs\n/" + name.encode() + b" Do\nQ")

    def _group_form(self, content: bytes) -> int:
        """Write ``content`` as a transparency group over the whole page, in display space."""
        _rotation, width, height = self._page._geometry()  # noqa: SLF001
        document = self._page.document
        return document.objects.add_stream(
            content,
            {
                "Type": Name("XObject"),
                "Subtype": Name("Form"),
                "BBox": [0, 0, width, height],
                "Group": {"S": Name("Transparency")},
                "Resources": self._resources_value(),
            },
        )

    def _resources_value(self) -> dict[str, object]:
        objects = self._page.document.objects
        resources: dict[str, object] = {}
        if self._xobjects:
            resources["XObject"] = {name: objects[xref] for name, xref in self._xobjects.items()}
        if self._gstates:
            resources["ExtGState"] = {name: _gstate_dict(key) for key, name in self._gstates.items()}
        if self._fonts:
            resources["Font"] = {name: _font_dict(base) for name, base in self._fonts.items()}
        return resources

    def _add_font(self, font: Font) -> str:
        base = cast("str", font.name)
        for name, known in self._fonts.items():
            if known == base:
                return name
        name = f"PyroF{len(self._fonts) + 1}"
        self._fonts[name] = base
        return name

    def _add_xobject(self, xref: int) -> str:
        for name, known in self._xobjects.items():
            if known == xref:
                return name
        name = f"PyroX{len(self._xobjects) + 1}"
        self._xobjects[name] = xref
        return name

    def _gstate(
        self, *, fill_alpha: float | None = None, stroke_alpha: float | None = None, blend: str | None = None
    ) -> str:
        key = (fill_alpha, stroke_alpha, blend)
        name = self._gstates.get(key)
        if name is None:
            name = f"PyroG{len(self._gstates) + 1}"
            self._gstates[key] = name
        return name

    def _commit(self) -> None:
        page = self._page
        document = page.document
        content = b"\n".join(self._layers[0][0])
        rotation, box = _page_space(page)
        matrix = _user_from_display(rotation, box)
        names = _merge_resources(page, self._xobjects, self._gstates, self._fonts)
        if names:
            for old, new in names.items():
                content = content.replace(b"/" + old.encode() + b" ", b"/" + new.encode() + b" ")
        data = b"q\n" + _numbers(*matrix) + b" cm\n" + content + b"\nQ\n"
        if len(data) > _DEFAULT_MAX_CONTENT_SIZE:
            code = "content_input_size"
            msg = f"the canvas writes {len(data)} bytes of content, over the {_DEFAULT_MAX_CONTENT_SIZE}-byte limit"
            raise LimitError(code, msg)
        objects = document.objects
        drawn = objects[objects.add_stream(data)]
        existing = _contents(page)
        if self._underlay:
            streams = [drawn, *existing]
        elif existing and not _isolated(page):
            opening = objects[objects.add_stream(b"q\n", filter=None)]
            closing = objects[objects.add_stream(b"\nQ\n", filter=None)]
            streams = [opening, *existing, closing, drawn]
        else:
            streams = [*existing, drawn]
        page.obj["Contents"] = streams

    # Paths.

    def path(
        self,
        path: PathLike,
        *,
        fill: Color | None = None,
        stroke: Stroke | Color | None = None,
        fill_rule: FillRule = FillRule.NONZERO,
    ) -> None:
        """Draw a path: a :class:`PathBuilder`, a :class:`Path` item's geometry, or segments."""
        self._check_open()
        segments = _segments(path)
        if fill is None and stroke is None:
            stroke = _BLACK
        painted_fill = None if fill is None else _color(fill, "fill")
        painted_stroke = _stroke(stroke)
        even_odd = FillRule(fill_rule) is FillRule.EVEN_ODD
        construction, points = _path_bytes(segments)
        self._count(points)
        parts = [b"q"]
        fill_alpha = None if painted_fill is None or painted_fill.alpha >= 1 else painted_fill.alpha
        stroke_paint = None if painted_stroke is None else cast("Color", painted_stroke.paint)
        stroke_alpha = None if stroke_paint is None or stroke_paint.alpha >= 1 else stroke_paint.alpha
        if fill_alpha is not None or stroke_alpha is not None:
            parts.append(b"/" + self._gstate(fill_alpha=fill_alpha, stroke_alpha=stroke_alpha).encode() + b" gs")
        if painted_fill is not None:
            parts.append(_numbers(*painted_fill.rgb) + b" rg")
        if painted_stroke is not None:
            parts.extend(_stroke_ops(painted_stroke))
        parts.append(construction)
        if painted_fill is not None and painted_stroke is not None:
            parts.append(b"B*" if even_odd else b"B")
        elif painted_fill is not None:
            parts.append(b"f*" if even_odd else b"f")
        else:
            parts.append(b"S")
        parts.append(b"Q")
        self._emit(b"\n".join(parts))

    def line(self, start: Sequence[float], end: Sequence[float], *, stroke: Stroke | Color | None = None) -> None:
        """Draw a straight line, with a 1-point black stroke by default."""
        self.path(PathBuilder().move_to(start).line_to(end), stroke=_BLACK if stroke is None else stroke)

    def rect(
        self,
        rect: Sequence[float],
        *,
        radius: float = 0,
        fill: Color | None = None,
        stroke: Stroke | Color | None = None,
    ) -> None:
        """Draw a rectangle, with corners rounded by ``radius`` points."""
        self.path(PathBuilder().rect(rect, radius=radius), fill=fill, stroke=stroke)

    def circle(
        self,
        center: Sequence[float],
        radius: float,
        *,
        fill: Color | None = None,
        stroke: Stroke | Color | None = None,
    ) -> None:
        """Draw a circle."""
        self.path(PathBuilder().circle(center, radius), fill=fill, stroke=stroke)

    def ellipse(
        self,
        rect: Sequence[float],
        *,
        fill: Color | None = None,
        stroke: Stroke | Color | None = None,
    ) -> None:
        """Draw the ellipse inside ``rect``."""
        self.path(PathBuilder().ellipse(rect), fill=fill, stroke=stroke)

    def polygon(
        self,
        points: Iterable[Sequence[float]],
        *,
        close: bool = True,
        fill: Color | None = None,
        stroke: Stroke | Color | None = None,
        fill_rule: FillRule = FillRule.NONZERO,
    ) -> None:
        """Draw straight segments through ``points``, closed unless ``close`` is false."""
        self.path(PathBuilder().polygon(points, close=close), fill=fill, stroke=stroke, fill_rule=fill_rule)

    def bezier(
        self,
        start: Sequence[float],
        control1: Sequence[float],
        control2: Sequence[float],
        end: Sequence[float],
        *,
        stroke: Stroke | Color | None = None,
    ) -> None:
        """Draw a cubic Bézier curve, with a 1-point black stroke by default."""
        builder = PathBuilder().move_to(start).curve_to(control1, control2, end)
        self.path(builder, stroke=_BLACK if stroke is None else stroke)

    # Images and pages.

    # Text.

    def text(  # noqa: PLR0913 - the text style options.
        self,
        text: str,
        at: Sequence[float],
        *,
        font: Font | None = None,
        size: float = 11,
        fill: Color | None = _BLACK,
        stroke: Stroke | Color | None = None,
        line_height: float | None = None,
        rotate: int = 0,
        max_text_size: int | None = _DEFAULT_MAX_GENERATED_TEXT_SIZE,
    ) -> None:
        """Draw text whose first baseline starts at ``at``.

        Lines are split with :meth:`str.splitlines` and placed ``line_height``
        times ``size`` apart, by default at the font's own line height.
        ``rotate`` turns the text clockwise about ``at`` by 0, 90, 180, or
        270 degrees, so that at 90 the lines run down the page and stack
        leftward; :meth:`transform` turns it by any other angle. ``font``
        defaults to Helvetica. Text is filled, stroked, or both; at least one
        is needed. The text is limited to 1 MiB of UTF-8 and 4,096 lines by
        default (``max_text_size``, ``None`` for trusted input), refused with
        :class:`LimitError` code ``text_input_size`` or ``text_line_count``.
        """
        self._check_open()
        checked = _text_input(text, max_text_size)
        rotation = _quarter_turn(rotate)
        lines = checked.splitlines()
        most = _max_lines(max_text_size)
        if most is not None and len(lines) > most:
            code = "text_line_count"
            msg = f"the text has {len(lines)} lines, over the limit of {most}"
            raise LimitError(code, msg)
        origin = _point(at, "at")
        font_size = _size(size)
        filled, stroked = _text_paint(fill, stroke)
        spacing = _line_height(line_height)
        if not any(lines):
            return
        face = _canvas_font("\n".join(lines), font)
        leading = face._font.leading(font_size, spacing)  # noqa: SLF001
        cos, sin = _TURNS[rotation]
        placed = [
            (line, origin.x - index * leading * sin, origin.y + index * leading * cos)
            for index, line in enumerate(lines)
            if line
        ]
        self._emit_text(face, font_size, (filled, stroked), rotation, [(line, x, y, 0.0) for line, x, y in placed])

    def textbox(  # noqa: PLR0913 - the text style options.
        self,
        rect: Sequence[float],
        text: str,
        *,
        font: Font | None = None,
        size: float = 11,
        align: Literal["left", "center", "right", "justify"] = "left",
        line_height: float | None = None,
        fill: Color | None = _BLACK,
        stroke: Stroke | Color | None = None,
        rotate: int = 0,
        max_text_size: int | None = _DEFAULT_MAX_GENERATED_TEXT_SIZE,
    ) -> TextFit:
        """Wrap text inside ``rect`` and report how it fits.

        Lines break at Unicode line-break opportunities (UAX #14) and, for a
        word wider than the box, between grapheme clusters. They are placed
        as :meth:`text` places them: the first baseline one
        :attr:`Font.ascender` below the top of ``rect`` and later ones
        ``line_height`` times ``size`` apart, by default at the font's own
        line height, and the text fits when the descender of the last line
        does. When the text does not fit, nothing is drawn and
        :attr:`TextFit.spare_height` is the negative shortfall;
        :meth:`Font.text_height` measures the height that the text needs
        without drawing it. Tabs become single spaces. ``rotate`` turns the
        text clockwise inside ``rect`` by 0, 90, 180, or 270 degrees: at 90
        lines run down the box and stack from its right edge leftward, so
        they wrap at the box's height and fill its width, and at 270 they run
        up and stack from the left edge. The font, paint, and size options
        follow :meth:`text`.
        """
        self._check_open()
        box = _rect(rect, "rect")
        rotation = _quarter_turn(rotate)
        normalized = _textbox_text(text, max_text_size)
        if align not in _ALIGNMENTS:
            msg = f"align must be one of {', '.join(_ALIGNMENTS)}: {align!r}"
            raise ValueError(msg)
        upright = rotation in (0, 180)
        width, height = (box.x1 - box.x0, box.y1 - box.y0) if upright else (box.y1 - box.y0, box.x1 - box.x0)
        font_size = _size(size)
        filled, stroked = _text_paint(fill, stroke)
        spacing = _line_height(line_height)
        if not normalized:
            return TextFit(spare_height=height)
        face = _canvas_font(normalized, font)
        lines, _needed, spare, ascent, leading = face._font.layout(  # noqa: SLF001
            normalized, width, height, font_size, spacing, align == "justify", _max_lines(max_text_size)
        )
        if spare < 0:
            return TextFit(spare_height=spare)
        cos, sin = _TURNS[rotation]
        # The turned box's top-left corner, from which lines run along (cos, sin) and stack along (-sin, cos).
        corner = {0: (box.x0, box.y0), 90: (box.x1, box.y0), 180: (box.x1, box.y1), 270: (box.x0, box.y1)}[rotation]
        placed = []
        for index, (line, line_width, justify) in enumerate(lines):
            if not line:
                continue
            free = width - line_width
            across = {"center": free / 2, "right": free}.get(align, 0.0)
            spaces = line.count(" ")
            word_space = max(free, 0.0) / spaces if justify and spaces else 0.0
            along = max(across, 0.0)
            down = ascent * font_size + index * leading
            placed.append(
                (line, corner[0] + along * cos - down * sin, corner[1] + along * sin + down * cos, word_space)
            )
        self._emit_text(face, font_size, (filled, stroked), rotation, placed)
        return TextFit(spare_height=spare)

    def _emit_text(
        self,
        font: Font,
        size: float,
        paint: tuple[Color | None, Stroke | None],
        rotation: int,
        lines: list[tuple[str, float, float, float]],
    ) -> None:
        """Draw lines of text, each at its origin in display space with its word spacing."""
        if not lines:
            return
        fill, stroke = paint
        parts = [b"q"]
        fill_alpha = None if fill is None or fill.alpha >= 1 else fill.alpha
        stroke_paint = None if stroke is None else cast("Color", stroke.paint)
        stroke_alpha = None if stroke_paint is None or stroke_paint.alpha >= 1 else stroke_paint.alpha
        if fill_alpha is not None or stroke_alpha is not None:
            parts.append(b"/" + self._gstate(fill_alpha=fill_alpha, stroke_alpha=stroke_alpha).encode() + b" gs")
        if fill is not None:
            parts.append(_numbers(*fill.rgb) + b" rg")
        if stroke is not None:
            parts.extend(_stroke_ops(stroke))
        name = self._add_font(font)
        parts.append(b"BT\n/" + name.encode() + b" " + _fmt(size) + b" Tf")
        if stroke is not None:
            parts.append(b"2 Tr" if fill is not None else b"1 Tr")
        cos, sin = _TURNS[rotation]
        word_space = 0.0
        for line, x, y, spacing in lines:
            if spacing != word_space:
                parts.append(_fmt(spacing) + b" Tw")
                word_space = spacing
            # Display space points down, so the text's own y axis is flipped up.
            parts.append(_numbers(cos, sin, sin, -cos, _number(x, "at"), _number(y, "at")) + b" Tm")
            parts.append(b"<" + font._font.encode(line).hex().encode() + b"> Tj")  # noqa: SLF001
        parts.append(b"ET\nQ")
        self._emit(b"\n".join(parts))

    def image(
        self,
        rect: Sequence[float],
        source: str | os.PathLike[str] | bytes | bytearray | memoryview | Pixmap | PageImage | int,
        *,
        keep_aspect: bool = True,
        rotate: int = 0,
    ) -> None:
        """Draw a JPEG, PNG, or JPEG 2000 image, a :class:`Pixmap`, or an image of this document into ``rect``.

        An encoded image is a path or bytes; JPEG and JPEG 2000 data is
        stored as it is, and PNG data is decoded and stored with Flate, its
        alpha as a soft mask. A :class:`Pixmap` is stored as PNG data would
        be. An image of this document, a :class:`PageImage` or the object
        number of an image XObject, is drawn again without a copy.
        ``rotate`` turns the image clockwise by 0, 90, 180, or 270 degrees,
        and ``keep_aspect`` centres it in ``rect`` at its aspect ratio. The
        same encoded image drawn several times, by any canvas of the
        document, is stored once.
        """
        from pyropdf._core import Pixmap  # noqa: PLC0415 - the stub names the extension's class.

        self._check_open()
        box = _rect(rect, "rect")
        rotation = _quarter_turn(rotate)
        document = self._page.document
        if isinstance(source, PageImage):
            if source.page.document is not document:
                msg = "an image of another document cannot be drawn again; draw its pixmap() or stored data"
                raise ValueError(msg)
            xref = source.item.xref
            if xref is None:
                msg = "an inline image has no image object; draw its pixmap()"
                raise ValueError(msg)
            stream = _image_stream(document, xref)
        elif isinstance(source, int) and not isinstance(source, bool):
            xref = source
            stream = _image_stream(document, xref)
        else:
            if isinstance(source, Pixmap):
                data = source.to_bytes("png")
            elif isinstance(source, (bytes, bytearray, memoryview)):
                data = bytes(source)
            elif isinstance(source, (str, os.PathLike)):
                with open(source, "rb") as file:  # noqa: PTH123 - os.PathLike and str alike.
                    data = file.read()
            else:
                msg = (
                    "source must be a path, bytes, a Pixmap, a PageImage, or an object number, "
                    f"not {type(source).__name__}"
                )
                raise TypeError(msg)
            xref = self._stored_image(data)
            stream = document.objects[xref]
        width, height = _number(stream.get("Width", 0), "Width"), _number(stream.get("Height", 0), "Height")
        if not (width > 0 and height > 0):
            msg = f"image object {xref} has no positive /Width and /Height"
            raise ValueError(msg)
        _, place = _placement(box, width, height, keep=bool(keep_aspect), rotation=rotation)
        # Image space has its first row at the top of the unit square.
        matrix = _affine(lambda u, v: place(u * width, (1 - v) * height))
        name = self._add_xobject(xref)
        self._emit(b"q\n" + _numbers(*matrix) + b" cm\n/" + name.encode() + b" Do\nQ")

    def _stored_image(self, data: bytes) -> int:
        """Return the image object holding ``data``, written once per document."""
        document = self._page.document
        cache = _cache(document)
        digest = hashlib.sha256(data).digest()
        xref = cache.images.get(digest)
        if xref is not None and _still_image(document, xref, len(data)):
            return xref
        xref = add_image(document, data)
        cache.images[digest] = xref
        return xref

    def page(
        self,
        rect: Sequence[float],
        source: Page,
        *,
        clip: Sequence[float] | None = None,
        keep_aspect: bool = True,
        rotate: int = 0,
    ) -> None:
        """Draw another page, of any open document or this one, into ``rect``.

        The page is placed as a Form XObject, as vector content with its
        fonts and images; a page of another document is copied with the
        objects it reaches when this call is made. ``clip`` is a rectangle
        in the source page's display coordinates, those of its
        :attr:`Page.rect`: only the part of the page inside it is drawn,
        scaled into ``rect`` in place of the whole page. ``rotate`` turns
        the page, or its clipped part, clockwise by 0, 90, 180, or 270
        degrees, and ``keep_aspect`` centres it in ``rect`` at its aspect
        ratio after the turn. A page placed several times, by any canvas of
        this document, is stored once while its content is unchanged.
        Annotations and form fields of the source page are not drawn.
        """
        from pyropdf._page import Page  # noqa: PLC0415 - pyropdf._page imports this module.

        self._check_open()
        if not isinstance(source, Page):
            msg = f"source must be a pyropdf.Page, not {type(source).__name__}"
            raise TypeError(msg)
        box = _rect(rect, "rect")
        rotation = _quarter_turn(rotate)
        source_rotation, source_box = _page_space(source)
        _, width, height = source._geometry()  # noqa: SLF001
        whole = Rect(0, 0, width, height)
        shown = whole if clip is None else _rect(clip, "clip")
        if not shown.intersects(whole):
            msg = f"clip {tuple(shown)} does not overlap the source page {tuple(whole)}"
            raise ValueError(msg)
        # Only the part of the clip on the page is drawn.
        shown &= whole
        covered, place = _placement(box, shown.width, shown.height, keep=bool(keep_aspect), rotation=rotation)
        to_display = _display_from_user(source_rotation, source_box)

        def convert(x: float, y: float) -> tuple[float, float]:
            dx, dy = to_display(x, y)
            return place(dx - shown.x0, dy - shown.y0)

        matrix = _affine(convert)
        name = self._add_xobject(self._form(source, source_box))
        self._emit(
            b"q\n"
            + _numbers(covered.x0, covered.y0, covered.width, covered.height)
            + b" re W n\n"
            + _numbers(*matrix)
            + b" cm\n/"
            + name.encode()
            + b" Do\nQ"
        )

    def _form(self, source: Page, box: tuple[float, float, float, float]) -> int:
        """Return a Form XObject of this document that draws ``source`` in its user space."""
        document = self._page.document
        content = source.operations.to_bytes()
        cache = _cache(document)
        key = (id(source.document), source.xref, source._generation, hashlib.sha256(content).digest())  # noqa: SLF001
        xref = cache.forms.get(key)
        if xref is not None and _still_form(document, xref, box):
            return xref
        resources = _core.copy_page_resources(
            document._core(),  # noqa: SLF001
            source.document._core(),  # noqa: SLF001
            source.xref,
            source._generation,  # noqa: SLF001
        )
        if resources is None:
            raise source._stale()  # noqa: SLF001
        xref = document.objects.add_stream(
            content,
            {
                "Type": Name("XObject"),
                "Subtype": Name("Form"),
                "BBox": list(box),
                "Resources": document.objects[resources],
            },
        )
        cache.forms[key] = xref
        return xref

    # Scopes.

    def clip(self, path: PathLike, *, fill_rule: FillRule = FillRule.NONZERO) -> AbstractContextManager[None]:
        """Clip what the block draws to a path."""
        self._check_open()
        construction, points = _path_bytes(_segments(path))
        self._count(points)
        operator = b"W* n" if FillRule(fill_rule) is FillRule.EVEN_ODD else b"W n"
        return _Scope(self, construction + b"\n" + operator)

    def transform(self, matrix: Matrix | Sequence[float]) -> AbstractContextManager[None]:
        """Transform what the block draws by ``matrix``, applied before the enclosing transforms."""
        self._check_open()
        try:
            m = Matrix(*matrix)
        except (TypeError, ValueError, PdfError) as exc:
            msg = f"matrix must be a Matrix or six numbers: {matrix!r}"
            raise ValueError(msg) from exc
        values = tuple(_number(value, "matrix") for value in m)
        return _Scope(self, _numbers(*values) + b" cm")

    def opacity(self, alpha: float) -> AbstractContextManager[None]:
        """Composite what the block draws as one group, then draw it at ``alpha``."""
        self._check_open()
        return _Scope(self, b"", group=_unit(alpha, "alpha"))

    def blend(self, mode: BlendMode | str) -> AbstractContextManager[None]:
        """Blend what the block draws with the content below it."""
        self._check_open()
        try:
            blend_mode = BlendMode(mode)
        except ValueError as exc:
            msg = f"mode must be a BlendMode: {mode!r}"
            raise ValueError(msg) from exc
        name = self._gstate(blend=_BLEND_NAMES[blend_mode])
        return _Scope(self, b"/" + name.encode() + b" gs")

    def __repr__(self) -> str:
        """Return the page and whether the canvas is still open."""
        state = "closed" if self._closed else "open"
        return f"<Canvas on {self._page!r}, {state}>"


def _gstate_dict(key: tuple[float | None, float | None, str | None]) -> dict[str, object]:
    fill_alpha, stroke_alpha, blend = key
    entries: dict[str, object] = {"Type": Name("ExtGState")}
    if fill_alpha is not None:
        entries["ca"] = fill_alpha
    if stroke_alpha is not None:
        entries["CA"] = stroke_alpha
    if blend is not None:
        entries["BM"] = Name(blend)
    return entries


def _font_dict(base: str) -> dict[str, object]:
    """Return the font dictionary of a Standard 14 font, which needs no embedding."""
    font: dict[str, object] = {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name(base)}
    if base not in _SYMBOLIC:
        font["Encoding"] = Name("WinAnsiEncoding")
    return font


def _page_space(page: Page) -> tuple[int, tuple[float, float, float, float]]:
    space = _core.page_space(page.document._core(), page.xref, page._generation)  # noqa: SLF001
    if space is None:
        raise page._stale()  # noqa: SLF001
    return space


def _cache(document: Document) -> _Images:
    cache = document._drawn  # noqa: SLF001
    if cache is None:
        cache = document._drawn = _Images()  # noqa: SLF001
    return cache


def _still_image(document: Document, xref: int, size: int) -> bool:
    """Return whether ``xref`` is still the image object that a canvas stored from ``size`` bytes."""
    stream = document.objects.get(xref) if xref in document.objects else None
    if not isinstance(stream, Stream) or stream.get("Subtype") != "Image":
        return False
    if stream.get("Filter") in ("DCTDecode", "JPXDecode"):
        return len(stream.read(decode=False)) == size
    return True


def _still_form(document: Document, xref: int, box: tuple[float, float, float, float]) -> bool:
    """Return whether ``xref`` is still a Form XObject with ``box``."""
    stream = document.objects.get(xref) if xref in document.objects else None
    if not isinstance(stream, Stream) or stream.get("Subtype") != "Form":
        return False
    bbox = stream.get("BBox")
    return bbox is not None and [float(value) for value in bbox] == [float(value) for value in box]


def _contents(page: Page) -> list[Stream]:
    """Return the page's content streams, in order."""
    contents = page.obj.get("Contents")
    if contents is None:
        return []
    if isinstance(contents, Stream):
        return [contents]
    return [item for item in contents if isinstance(item, Stream)]


def _isolated(page: Page) -> bool:
    """Return whether the page's content leaves the graphics state as it found it."""
    try:
        operations = page.operations
    except Exception:  # noqa: BLE001 - content that cannot be read is wrapped.
        return False
    depth = 0
    for operation in operations:
        operator = getattr(operation, "operator", None)
        if operator == "q":
            depth += 1
        elif operator == "Q":
            depth -= 1
            if depth < 0:
                return False
        elif depth == 0 and operator in _STATE_OPERATORS:
            return False
    return depth == 0


def _merge_resources(
    page: Page,
    xobjects: dict[str, int],
    gstates: dict[tuple[float | None, float | None, str | None], str],
    fonts: dict[str, str],
) -> dict[str, str]:
    """Add the canvas's resources to the page's own, renaming any that collide.

    The page gets its own direct ``/Resources`` when it inherits or shares
    them, so no other page changes. Returns the renamed names.
    """
    if not xobjects and not gstates and not fonts:
        return {}
    document = page.document
    own = page.obj.get("Resources")
    if own is None or own.is_indirect:
        inherited = own if own is not None else _inherited_resources(page)
        page.obj["Resources"] = {} if inherited is None else dict(cast("Dictionary", inherited).items())
    resources = page.obj["Resources"]
    renamed: dict[str, str] = {}
    for key, entries in (
        ("XObject", {name: document.objects[xref] for name, xref in xobjects.items()}),
        ("ExtGState", {name: _gstate_dict(state) for state, name in gstates.items()}),
        ("Font", {name: _font_dict(base) for name, base in fonts.items()}),
    ):
        if not entries:
            continue
        sub = resources.get(key)
        if sub is None:
            resources[key] = {}
        elif sub.is_indirect:
            resources[key] = dict(sub.items())
        sub = resources[key]
        for name, value in entries.items():
            unique = name
            serial = 0
            while unique in sub:
                serial += 1
                unique = f"{name}_{serial}"
            if unique != name:
                renamed[name] = unique
            sub[unique] = value
    return renamed


def _inherited_resources(page: Page) -> object | None:
    """Return the ``/Resources`` that the page inherits from its page tree."""
    node = page.obj.get("Parent")
    for _ in range(64):
        if node is None:
            return None
        if "Resources" in node:
            return node["Resources"]
        node = node.get("Parent")
    return None


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (Canvas, PathBuilder, TextFit):
    _public.__module__ = "pyropdf"
