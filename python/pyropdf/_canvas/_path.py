"""Canvas geometry, coordinate transforms, paint/stroke validation, and vector path building."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pyropdf._content import (
    BlendMode,
    ClosePath,
    Color,
    ColorStop,
    CurveTo,
    Gradient,
    LinearGradient,
    LineCap,
    LineJoin,
    LineTo,
    MoveTo,
    Path,
    RadialGradient,
    Segment,
    ShadingPattern,
    ShadingType,
    Stroke,
    TilingPattern,
)
from pyropdf._geometry import Matrix, Point, Rect

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Self, TypeAlias

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
#: The most path and clip points that one canvas holds, and the most stops of one gradient.
_MAX_POINTS = 1_048_576
#: A bound on the memory of one gradient stop on its way to the core: its tuple, its floats, and the core's copy.
_STOP_SIZE = 256
_IDENTITY = Matrix(1, 0, 0, 1, 0, 0)
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
#: The cosine and sine of each clockwise quarter turn, in display space.
_TURNS = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


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


def _paint(value: object, name: str) -> Color | Gradient | ShadingPattern:
    """Return what a canvas paints with, checked as far as it can be without its stops."""
    if isinstance(value, (LinearGradient, RadialGradient)):
        return value
    if (
        isinstance(value, ShadingPattern)
        and value.shading_type in (ShadingType.AXIAL, ShadingType.RADIAL)
        and value._source is not None  # noqa: SLF001 - the content model's retained paint.
    ):
        return value
    if isinstance(value, (ShadingPattern, TilingPattern)):
        msg = f"{name} is a {type(value).__name__}, which does not hold what it paints; draw a Color or a gradient"
        raise TypeError(msg)
    if not isinstance(value, Color):
        msg = f"{name} must be a pyropdf.Color or a gradient, not {type(value).__name__}"
        raise TypeError(msg)
    return _color(value, name)


def _stroke(value: Stroke | Color | Gradient | None) -> Stroke | None:
    """Return a stroke from a :class:`Stroke`, a :class:`Color`, or a gradient, checked."""
    if value is None:
        return None
    stroke = Stroke(paint=value) if isinstance(value, (Color, LinearGradient, RadialGradient)) else value
    if not isinstance(stroke, Stroke):
        msg = f"stroke must be a pyropdf.Stroke, a Color, or a gradient, not {type(value).__name__}"
        raise TypeError(msg)
    _paint(stroke.paint, "stroke.paint")
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
    """Return the operators that set a checked stroke's color and line style."""
    return [_numbers(*cast("Color", stroke.paint).rgb) + b" RG", *_line_style(stroke)]


def _line_style(stroke: Stroke) -> list[bytes]:
    """Return the operators that set a checked stroke's line style."""
    return [
        _numbers(stroke.width) + b" w",
        f"{_CAPS[LineCap(stroke.cap)]} J {_JOINS[LineJoin(stroke.join)]} j".encode(),
        _numbers(stroke.miter_limit) + b" M",
        b"[" + _numbers(*stroke.dashes) + b"] " + _numbers(stroke.dash_offset) + b" d",
    ]


@dataclass(frozen=True, slots=True)
class _Painting:
    """The operators that set a paint, and the alpha and the soft mask that it paints through."""

    operators: tuple[bytes, ...]
    alpha: float | None
    mask: str | None


def _stops(gradient: Gradient) -> list[tuple[float, float, float, float, float]]:
    """Return the stops of a gradient as the core takes them, checked but for their order."""
    stops = []
    for stop in gradient.stops:
        if not isinstance(stop, ColorStop):
            msg = f"a gradient's stops must be pyropdf.ColorStop, not {type(stop).__name__}"
            raise TypeError(msg)
        color = _color(stop.color, "stop.color")
        red, green, blue = map(float, color.rgb)
        stops.append((_number(stop.offset, "stop.offset"), red, green, blue, float(color.alpha)))
    return stops


def _bounds(segments: list[Segment], pad: float) -> tuple[float, float, float, float]:
    """Return a box around the points of ``segments``, and ``pad`` beyond them."""
    x0 = y0 = math.inf
    x1 = y1 = -math.inf
    for segment in segments:
        if isinstance(segment, CurveTo):
            points = (segment.c1, segment.c2, segment.to)
        elif isinstance(segment, (MoveTo, LineTo)):
            points = (segment.to,)
        else:
            continue
        for point in points:
            x, y = _point(point, "point")
            x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


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
    rotation: int, box: tuple[float, float, float, float], unit: float
) -> Callable[[float, float], tuple[float, float]]:
    """Return the map from a page's user space to its display space, in points of ``unit`` per user space unit."""
    x0, y0, x1, y1 = box

    def turn(x: float, y: float) -> tuple[float, float]:
        if rotation == 90:  # noqa: PLR2004 - a quarter turn
            return (y - y0, x - x0)
        if rotation == 180:  # noqa: PLR2004 - a half turn
            return (x1 - x, y - y0)
        if rotation == 270:  # noqa: PLR2004 - three quarter turns
            return (y1 - y, x1 - x)
        return (x - x0, y1 - y)

    def convert(x: float, y: float) -> tuple[float, float]:
        u, v = turn(x, y)
        return (u * unit, v * unit)

    return convert


def _user_from_display(rotation: int, box: tuple[float, float, float, float], unit: float) -> _Affine:
    """Return the inverse page transform without subtracting nearly equal translated points."""
    x0, y0, x1, y1 = box
    scale = 1 / unit
    if rotation == 90:  # noqa: PLR2004 - a quarter turn
        return (0, scale, scale, 0, x0, y0)
    if rotation == 180:  # noqa: PLR2004 - a half turn
        return (-scale, 0, 0, scale, x1, y0)
    if rotation == 270:  # noqa: PLR2004 - three quarter turns
        return (0, -scale, -scale, 0, x1, y1)
    return (scale, 0, 0, -scale, x0, y1)


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


PathBuilder.__module__ = "pyropdf"
