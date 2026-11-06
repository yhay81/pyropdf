"""Canvas: drawing paths, images, and other pages on a page, in paint order."""

from __future__ import annotations

import enum
import hashlib
import math
import os
import re
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from itertools import accumulate, groupby
from typing import TYPE_CHECKING, Literal, cast

from pyropdf import _core
from pyropdf._content import (
    BlendMode,
    ClosePath,
    Color,
    ColorStop,
    CurveTo,
    FillRule,
    Gradient,
    GradientExtend,
    Image,
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
from pyropdf._errors import LimitError, PdfError, StaleObjectError
from pyropdf._fonts import (
    _DEFAULT_MAX_GENERATED_TEXT_SIZE,
    _NO_TEXT,
    Font,
    _max_lines,
    _text_input,
    _textbox_text,
    _utf16,
    add_glyphs,
    embedded_font,
)
from pyropdf._geometry import Matrix, Point, Rect
from pyropdf._images import PageImage, _image_stream, add_image
from pyropdf._layers import Layer, LayerMembership
from pyropdf._objects import Array, Dictionary, Name, Stream
from pyropdf._operations import _DEFAULT_MAX_CONTENT_SIZE

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from types import TracebackType
    from typing import Self, TypeAlias

    from pyropdf._core import Pixmap
    from pyropdf._document import Document
    from pyropdf._objects import Objects
    from pyropdf._page import Page

    PathLike: TypeAlias = "PathBuilder | Path | Iterable[Segment]"
    _Affine: TypeAlias = tuple[float, float, float, float, float, float]
    # How text draws a glyph in a Type 3 font: its name, advance, and strike, from ``_Font.colour_glyphs``.
    _ColourGlyph: TypeAlias = tuple[str, float, int | None]

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
#: The alignments of :meth:`Canvas.textbox`, in the order of the core's codes.
_ALIGNMENTS = ("left", "center", "right", "justify")
#: The cosine and sine of each clockwise quarter turn, in display space.
_TURNS = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}
#: The Standard 14 fonts that draw their own built-in encoding.
_SYMBOLIC = frozenset({"Symbol", "ZapfDingbats"})
#: Operators that change the graphics state that later content inherits.
#: Levels of a parent tree that adding a key follows down its last kids.
_MAX_TREE_DEPTH = 32
#: The shape of a BCP 47 language tag, and its longest form.
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*", re.ASCII)
_MAX_LANGUAGE_TAG = 256
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


def _canvas_font(text: str, font: Font | None) -> Font:
    """Return the font that draws ``text``: ``font``, else Helvetica.

    A Standard 14 font draws WinAnsi text only.
    """
    if font is None:
        font = Font.standard("Helvetica")
    elif not isinstance(font, Font):
        msg = f"font must be a pyropdf.Font, not {type(font).__name__}"
        raise TypeError(msg)
    if font._font.standard_name is None:  # noqa: SLF001 - an embedded face shapes any text it has glyphs for.
        return font
    try:
        text.encode("cp1252")
    except UnicodeEncodeError as exc:
        msg = (
            "Standard 14 fonts can only print WinAnsi (Latin-1-equivalent) characters; "
            "load an OpenType face with Font.from_file() for other text"
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


def _text_paint(
    fill: Color | Gradient | None, stroke: Stroke | Color | Gradient | None
) -> tuple[Color | Gradient | None, Stroke | None]:
    """Return the checked fill and stroke of text, which needs at least one."""
    if fill is None and stroke is None:
        msg = "text needs a fill or a stroke"
        raise ValueError(msg)
    stroked = _stroke(stroke)
    filled = None if fill is None else _paint(fill, "fill")
    if isinstance(filled, ShadingPattern) or (stroked is not None and isinstance(stroked.paint, ShadingPattern)):
        msg = "text needs a Color or a gradient in place of a ShadingPattern"
        raise TypeError(msg)
    return filled, stroked


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


class TagRole(enum.StrEnum):
    """A structure type (ISO 32000-1 section 14.8.4), by its PDF name.

    :attr:`ARTIFACT` marks content that is not part of the document's
    logical structure, such as page headers, footers, and decoration.
    """

    PART = "Part"
    ARTICLE = "Art"
    SECTION = "Sect"
    DIV = "Div"
    BLOCK_QUOTE = "BlockQuote"
    CAPTION = "Caption"
    TOC = "TOC"
    TOC_ITEM = "TOCI"
    INDEX = "Index"
    NON_STRUCT = "NonStruct"
    PARAGRAPH = "P"
    HEADING_1 = "H1"
    HEADING_2 = "H2"
    HEADING_3 = "H3"
    HEADING_4 = "H4"
    HEADING_5 = "H5"
    HEADING_6 = "H6"
    LIST = "L"
    LIST_ITEM = "LI"
    LABEL = "Lbl"
    LIST_BODY = "LBody"
    TABLE = "Table"
    TABLE_ROW = "TR"
    TABLE_HEADER = "TH"
    TABLE_DATA = "TD"
    TABLE_HEAD = "THead"
    TABLE_BODY = "TBody"
    TABLE_FOOT = "TFoot"
    SPAN = "Span"
    QUOTE = "Quote"
    NOTE = "Note"
    REFERENCE = "Reference"
    BIB_ENTRY = "BibEntry"
    CODE = "Code"
    FIGURE = "Figure"
    FORMULA = "Formula"
    ARTIFACT = "Artifact"
    LINK = "Link"


class TableHeaderScope(enum.StrEnum):
    """What a table header cell heads (ISO 32000-1 section 14.8.5.7), by its PDF name."""

    ROW = "Row"
    COLUMN = "Column"
    BOTH = "Both"


class ListNumbering(enum.StrEnum):
    """How a list numbers its items (ISO 32000-1 section 14.8.5.5), by its PDF name."""

    NONE = "None"
    DISC = "Disc"
    CIRCLE = "Circle"
    SQUARE = "Square"
    DECIMAL = "Decimal"
    LOWER_ROMAN = "LowerRoman"
    UPPER_ROMAN = "UpperRoman"
    LOWER_ALPHA = "LowerAlpha"
    UPPER_ALPHA = "UpperAlpha"


def _tag_attribute(
    value: object,
    kind: type[enum.StrEnum],
    name: str,
    role: TagRole,
    takes: TagRole,
) -> str | None:
    """Return the PDF name of a role-specific tag attribute, refusing it on other roles."""
    if value is None:
        return None
    if role is not takes:
        msg = f"only {takes.name} tags take {name}: {role.name}"
        raise ValueError(msg)
    try:
        return kind(value).value
    except ValueError as exc:
        msg = f"{name} must be a {kind.__name__}: {value!r}"
        raise ValueError(msg) from exc


class _Element:
    """A structure element that a tag scope opened: its role, its entries, and its kids in order.

    A kid is a marked-content identifier of the canvas's content or a child element.
    """

    __slots__ = ("entries", "kids", "role")

    def __init__(self, role: str, entries: dict[str, object]) -> None:
        self.role = role
        self.entries = entries
        self.kids: list[int | _Element] = []


class _Scope(AbstractContextManager[None]):
    """One clip, transform, opacity, blend, or layer scope of a canvas."""

    def __init__(
        self,
        canvas: Canvas,
        opening: bytes,
        closing: bytes,
        *,
        group: float | None = None,
        matrix: Matrix | None = None,
    ) -> None:
        self._canvas = canvas
        self._opening = opening
        self._closing = closing
        self._group = group
        self._matrix = matrix

    def __enter__(self) -> None:
        self._canvas._push(self._opening, self._closing, group=self._group, matrix=self._matrix)  # noqa: SLF001

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._canvas._pop()  # noqa: SLF001


class _TagScope(AbstractContextManager[None]):
    """One tag scope of a canvas: a structure element, or ``None`` for an artifact."""

    def __init__(self, canvas: Canvas, element: _Element | None) -> None:
        self._canvas = canvas
        self._element = element

    def __enter__(self) -> None:
        self._canvas._push_tag(self._element)  # noqa: SLF001

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._canvas._pop_tag()  # noqa: SLF001


class _Images:
    """The images and pages that a document's canvases wrote, keyed by their content."""

    __slots__ = ("colour", "faces", "fonts", "forms", "images", "shadings")

    def __init__(self) -> None:
        self.fonts: dict[bytes, int] = {}
        # The Type 3 fonts of the faces that text drew in colour, by program digest.
        self.colour: dict[bytes, list[int]] = {}
        # The shading objects that those fonts' COLR glyphs share.
        self.shadings = _core._Shadings()  # noqa: SLF001
        # The faces of the Type0 fonts that free text was drawn in, by font object.
        self.faces: dict[int, Font] = {}
        self.images: dict[bytes, int] = {}
        # The Forms of placed pages, with the source objects each depends on.
        self.forms = _core._PageForms()  # noqa: SLF001


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
    translucent. A :class:`LinearGradient` or a :class:`RadialGradient` paints
    wherever a color does on a path, a shape, or text, and stops that differ in alpha
    paint through a soft mask, so that a fill and a stroke are then two
    paintings. Equal gradient objects are written once in a canvas. A gradient
    renders within 8 levels of 255 per channel of what its stops say at 72 dpi
    in hayro and poppler, and one that repeats within 16, away from its sharp
    changes and from the edges of what it paints. The scopes :meth:`clip`, :meth:`transform`,
    :meth:`opacity`, :meth:`blend`, :meth:`layer`, and :meth:`tag` are
    context managers that apply to everything drawn inside them. :meth:`text` and :meth:`textbox` draw
    text in a :class:`Font`; embedded fonts come with issue 153 (DESIGN.md
    section 7).

    A canvas holds at most 1,048,576 path points, of paths and clips
    together, nests at most 16 scopes, and writes at most 64 MiB of
    content; a gradient has at most 1,048,576 stops, and repeats at most
    4,096 times over what it paints; more raise :class:`LimitError` with code
    ``content_element_count`` or ``content_input_size``, or
    ``ValueError`` for the nesting. A gradient whose line has no extent, whose
    ends are one point or whose circles are one circle, raises ``ValueError``
    when it is drawn.
    """

    __slots__ = (
        "_buffered",
        "_charged",
        "_closed",
        "_copies",
        "_depth",
        "_elements",
        "_entered",
        "_fonts",
        "_gstates",
        "_layers",
        "_marked",
        "_masks",
        "_matrices",
        "_matrix",
        "_mcids",
        "_oc",
        "_operation",
        "_page",
        "_pattern_names",
        "_patterns",
        "_points",
        "_properties",
        "_scopes",
        "_shadings",
        "_tagged",
        "_tags",
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
        # The canvas's operation, while its with block runs; the bytes its
        # buffers hold; and the memory it charged the operation and holds.
        self._operation: _core._OperationBudget | None = None
        self._buffered = 0
        self._charged = 0
        self._xobjects: dict[str, int] = {}
        # The copy of each image of another document that the canvas drew, by its document and number.
        self._copies: dict[tuple[Document, int], int] = {}
        # A Standard 14 font's base name, or the object number of an embedded face's Type0 font.
        self._fonts: dict[str, str | int] = {}
        self._gstates: dict[tuple[float | None, float | None, str | None], str] = {}
        # The resource name of each group, or each membership's groups and policy, and the object it names.
        self._oc: dict[int | tuple[tuple[int, ...], str], str] = {}
        self._properties: dict[str, int] = {}
        # The objects of the canvas's gradients, which its drawings share.
        self._shadings: _core._Shadings | None = None
        # The resource name of each soft mask's ExtGState, and of each shading pattern by its shading, its
        # matrix, and whether it is in the page's content, whose object waits until the commit gives the page's
        # space; and the objects that are written.
        self._masks: dict[int, str] = {}
        self._pattern_names: dict[tuple[int, Matrix, bool], str] = {}
        self._patterns: dict[str, int] = {}
        # From the space of what the canvas draws to the default space of the buffer that it goes to: the
        # canvas's own display space, or the opacity group's. Pattern space ignores the CTM.
        self._matrix = _IDENTITY
        self._matrices: list[Matrix] = []
        # The closing of each open clip, transform, blend, and layer scope; None for an opacity group.
        self._scopes: list[bytes | None] = []
        # The open tag scopes, None for an artifact, and the elements that no element holds.
        self._tags: list[_Element | None] = []
        self._elements: list[_Element] = []
        # The element whose marked content is open in the page's content, and the next identifier.
        self._marked: _Element | None = None
        self._mcids = 0
        self._tagged = False
        self._closed = False
        self._entered = False

    # Writing.

    def __enter__(self) -> Self:
        """Open the canvas's transaction and return the canvas; leaving the block writes it."""
        if self._closed or self._entered:
            msg = "a canvas is used in one with block; start another with page.draw()"
            raise ValueError(msg)
        document = self._page.document
        # One operation from the document's limits holds the canvas's
        # preparation, its buffers, and its writes.
        self._operation = _core.begin_canvas(document._core())  # noqa: SLF001
        self._entered = True
        self._tagged = isinstance(document.catalog.get("StructTreeRoot"), Dictionary)
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
        operation = self._active
        keep = exc_type is None and bool(self._layers[0][0])
        try:
            if keep and self._depth:
                msg = "a canvas scope is still open"
                raise RuntimeError(msg)  # noqa: TRY301 - the canvas is discarded below.
            if keep:
                self._commit()
            self._release(self._charged)
        except BaseException:
            _core.abort_canvas(core, operation)
            raise
        finally:
            self._layers = [([], None)]
        if keep:
            _core.end_transaction(core, True)  # noqa: FBT003 - the core's flag
        else:
            _core.abort_canvas(core, operation)

    def _check_open(self) -> None:
        if self._closed or not self._entered:
            msg = "draw inside the canvas's with block: with page.draw() as canvas: ..."
            raise ValueError(msg)

    def _emit(self, data: bytes) -> None:
        """Append drawing ``data``, marked as its tag scope says when it goes to the page's content."""
        self._check_open()
        if len(self._layers) == 1:
            data = self._mark(data)
        self._append(self._layers[-1][0], data)

    def _append(self, parts: list[bytes], data: bytes) -> None:
        """Buffer ``data`` in ``parts``, after checking the content's size and charging the operation."""
        size = len(data) + 1  # and the newline that joins it
        if self._buffered + size > _DEFAULT_MAX_CONTENT_SIZE:
            code = "content_input_size"
            msg = f"the canvas holds over {_DEFAULT_MAX_CONTENT_SIZE} bytes of content"
            raise LimitError(code, msg)
        self._charge(size)
        self._buffered += size
        parts.append(data)

    @property
    def _active(self) -> _core._OperationBudget:
        """The canvas's operation, which its with block opened."""
        if self._operation is None:
            self._check_open()
            raise AssertionError
        return self._operation

    def _charge(self, size: int) -> None:
        """Charge ``size`` bytes of memory to the operation, after its deadline and cancellation."""
        self._active.charge("memory_size", size)
        self._charged += size

    def _release(self, size: int) -> None:
        if size:
            self._active.release("memory_size", size)
            self._charged -= size

    @contextmanager
    def _scratch(self, size: int) -> Generator[None]:
        """Charge ``size`` bytes of memory that a block allocates and frees."""
        self._charge(size)
        try:
            yield
        finally:
            self._release(size)

    def _mark(self, data: bytes) -> bytes:
        if not self._tags:
            # Drawing outside every tag scope of a tagged document is an artifact.
            return b"/Artifact BMC\n" + data + b"\nEMC" if self._tagged else data
        element = self._tags[-1]
        if element is None or self._marked is element:
            return data
        mcid = self._mcids
        self._mcids += 1
        element.kids.append(mcid)
        self._marked = element
        return b"/" + element.role.encode() + b" <</MCID " + str(mcid).encode() + b">> BDC\n" + data

    def _unmark(self) -> None:
        """Close the open marked content of an element, before a scope starts or ends."""
        if self._marked is not None:
            self._append(self._layers[0][0], b"EMC")
            self._marked = None

    def _count(self, points: int) -> None:
        self._points += points
        if self._points > _MAX_POINTS:
            code = "content_element_count"
            msg = f"the canvas holds more than {_MAX_POINTS} path points"
            raise LimitError(code, msg)

    def _enter_scope(self) -> None:
        self._check_open()
        if self._depth >= _MAX_SCOPES:
            msg = f"canvas scopes nest at most {_MAX_SCOPES} deep"
            raise ValueError(msg)
        self._depth += 1
        self._unmark()

    def _push(self, opening: bytes, closing: bytes, *, group: float | None, matrix: Matrix | None) -> None:
        self._enter_scope()
        self._matrices.append(self._matrix)
        if group is not None:
            self._scopes.append(None)
            self._layers.append(([], group))
            self._matrix = _IDENTITY
        else:
            self._scopes.append(closing)
            self._append(self._layers[-1][0], opening)
            if matrix is not None:
                self._matrix = matrix * self._matrix

    def _pop(self) -> None:
        self._depth -= 1
        self._unmark()
        self._matrix = self._matrices.pop()
        closing = self._scopes.pop()
        if closing is not None:
            self._append(self._layers[-1][0], closing)
            return
        parts, group = self._layers.pop()
        if not parts:
            return
        size = sum(len(part) + 1 for part in parts)
        with self._scratch(size):
            name = self._add_xobject(self._group_form(b"\n".join(parts)))
        # The group's buffer is written and freed.
        self._buffered -= size
        self._release(size)
        gstate = self._gstate(fill_alpha=cast("float", group), stroke_alpha=cast("float", group))
        self._emit(b"q\n/" + gstate.encode() + b" gs\n/" + name.encode() + b" Do\nQ")

    def _push_tag(self, element: _Element | None) -> None:
        if element is not None:
            if len(self._layers) > 1:
                msg = "a tag scope cannot start inside an opacity scope"
                raise ValueError(msg)
            if self._tags and self._tags[-1] is None:
                msg = "a tag scope cannot start inside an artifact"
                raise ValueError(msg)
        self._enter_scope()
        if element is None:
            self._append(self._layers[-1][0], b"/Artifact BMC")
        elif self._tags:
            cast("_Element", self._tags[-1]).kids.append(element)
        else:
            self._elements.append(element)
        self._tags.append(element)

    def _pop_tag(self) -> None:
        self._depth -= 1
        self._unmark()
        if self._tags.pop() is None:
            self._append(self._layers[-1][0], b"EMC")

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
        if self._gstates or self._masks:
            resources["ExtGState"] = {
                **{name: _gstate_dict(key) for key, name in self._gstates.items()},
                **{name: objects[xref] for xref, name in self._masks.items()},
            }
        if self._patterns:
            resources["Pattern"] = {name: objects[xref] for name, xref in self._patterns.items()}
        if self._fonts:
            resources["Font"] = {name: _font_value(objects, font) for name, font in self._fonts.items()}
        if self._properties:
            resources["Properties"] = {name: objects[xref] for name, xref in self._properties.items()}
        return resources

    def _add_font(self, font: Font) -> str:
        return self._font_name(cast("str", font.name))

    def _font_name(self, font: str | int) -> str:
        for name, known in self._fonts.items():
            if known == font:
                return name
        name = f"PyroF{len(self._fonts) + 1}"
        self._fonts[name] = font
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

    def _gradient(
        self,
        gradient: Gradient,
        area: tuple[float, float, float, float],
        bbox: tuple[float, float, float, float] | None = None,
        background: tuple[float, float, float, float] | None = None,
        *,
        sampled: bool = False,
    ) -> tuple[int, int | None, float]:
        """Return the objects of ``gradient`` that paints ``area``: its shading, its soft mask, and its alpha."""
        start, end = _point(gradient.start, "start"), _point(gradient.end, "end")
        radii = None
        if isinstance(gradient, RadialGradient):
            radii = (_number(gradient.start_radius, "start_radius"), _number(gradient.end_radius, "end_radius"))
        extend = GradientExtend(gradient.extend).value
        if not isinstance(gradient.stops, Sequence):
            msg = f"a gradient's stops must be a sequence, not {type(gradient.stops).__name__}"
            raise TypeError(msg)
        count = len(gradient.stops)
        if count > _MAX_POINTS:
            code = "content_element_count"
            msg = f"the gradient has {count} stops, over the limit of {_MAX_POINTS}"
            raise LimitError(code, msg)
        if self._shadings is None:
            self._shadings = _core._Shadings()  # noqa: SLF001
        with self._scratch(count * _STOP_SIZE):
            return _core.canvas_gradient(
                self._page.document._core(),  # noqa: SLF001
                self._shadings,
                (start.x, start.y),
                (end.x, end.y),
                radii,
                _stops(gradient),
                extend,
                area,
                bbox,
                background,
                sampled,
                self._active,
            )

    def _write_pattern(self, shading: int, matrix: Matrix) -> int:
        return _core.canvas_gradient_pattern(
            self._page.document._core(),  # noqa: SLF001
            cast("_core._Shadings", self._shadings),  # noqa: SLF001
            shading,
            (matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f),
            self._active,
        )

    def _write_page_patterns(self, matrix: _Affine) -> None:
        """Write the patterns of the page's content, which reach its default space through its opening ``matrix``."""
        written = Matrix(*(float(_fmt(value)) for value in matrix))
        for (shading, inner, in_page), name in self._pattern_names.items():
            if in_page:
                self._patterns[name] = self._write_pattern(shading, inner * written)

    def _pattern(self, shading: int, matrix: Matrix | None = None) -> str:
        """Return the resource name of the pattern that paints ``shading`` in the current space."""
        in_page = len(self._layers) == 1
        matrix = self._matrix if matrix is None else matrix
        key = (shading, matrix, in_page)
        name = self._pattern_names.get(key)
        if name is None:
            name = self._pattern_names[key] = f"PyroP{len(self._pattern_names) + 1}"
            if not in_page:
                self._patterns[name] = self._write_pattern(shading, matrix)
        return name

    def _setting(
        self, paint: Color | Gradient | ShadingPattern, segments: list[Segment], stroke: Stroke | None
    ) -> _Painting:
        """Return how to paint ``segments`` with ``paint``, as the fill, or as ``stroke`` if there is one."""
        if isinstance(paint, Color):
            operator = b" rg" if stroke is None else b" RG"
            return _Painting((_numbers(*paint.rgb) + operator,), None if paint.alpha >= 1 else paint.alpha, None)
        # Past the path by half a line, or by a miter or a square cap, and a thin line's own width.
        pad = 1.0 if stroke is None else max(stroke.width, 1.0) / 2 * max(stroke.miter_limit, math.sqrt(2)) + 1.0
        area = _bounds(segments, pad)
        matrix = self._matrix
        bbox = background = None
        if isinstance(paint, ShadingPattern):
            source = paint._source  # noqa: SLF001 - validated before opening the item's context.
            if source is None:
                msg = "the ShadingPattern does not retain its source"
                raise TypeError(msg)
            gradient, bbox, background = source.gradient(self._active)
            matrix = paint.matrix * self._matrix
            x0, y0, x1, y1 = Rect(*area) / paint.matrix
            area = (x0, y0, x1, y1)
        else:
            gradient = paint
        shading, mask, alpha = self._gradient(
            gradient, area, bbox, background, sampled=isinstance(paint, ShadingPattern)
        )
        name = self._pattern(shading, matrix).encode()
        space, operator = (b"/Pattern cs", b" scn") if stroke is None else (b"/Pattern CS", b" SCN")
        masked = None if mask is None else self._masks.setdefault(mask, f"PyroM{len(self._masks) + 1}")
        return _Painting((space, b"/" + name + operator), None if alpha >= 1 else alpha, masked)

    def _commit(self) -> None:
        page = self._page
        document = page.document
        objects = document.objects
        for oc in self._oc:
            for xref in (oc,) if isinstance(oc, int) else oc[0]:
                group = objects.get(xref)
                if not isinstance(group, Dictionary) or group.get("Type") != "OCG":
                    msg = f"object {xref}, a layer that the canvas draws, is no longer an optional content group"
                    raise StaleObjectError(msg)
        rotation, box = _page_space(page)
        matrix = _user_from_display(rotation, box)
        self._write_page_patterns(matrix)
        opening = b"q\n" + _numbers(*matrix) + b" cm\n"
        size = len(opening) + self._buffered + 2
        # Check the size and charge the joined content and its framed copy before making them.
        if size > _DEFAULT_MAX_CONTENT_SIZE:
            code = "content_input_size"
            msg = f"the canvas writes {size} bytes of content, over the {_DEFAULT_MAX_CONTENT_SIZE}-byte limit"
            raise LimitError(code, msg)
        self._charge(2 * size)
        data = opening + b"\n".join(self._layers[0][0]) + b"\nQ\n"
        if self._elements:
            # The elements name their content in a Form of the canvas's own, which has a parent tree key.
            form = objects.add_stream(
                data,
                {
                    "Type": Name("XObject"),
                    "Subtype": Name("Form"),
                    "BBox": list(box),
                    "Resources": self._resources_value(),
                },
            )
            objects[form]["StructParents"] = self._write_structure(form)
            name = f"PyroX{len(self._xobjects) + 1}"
            name = _merge_resources(page, {name: form}, {}, {}, {}).get(name, name)
            data = b"q\n/" + name.encode() + b" Do\nQ\n"
        else:
            names = _merge_resources(
                page, self._xobjects, self._gstates, self._fonts, self._properties, self._patterns, self._masks
            )
            for old, new in names.items():
                data = data.replace(b"/" + old.encode() + b" ", b"/" + new.encode() + b" ")
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

    def _write_structure(self, form: int) -> int:
        """Add the canvas's elements to the document's structure tree; return the Form's parent tree key.

        A document without a structure tree gets one. The elements join its
        ``Document`` element when the root holds that one element only, and
        the root otherwise.
        """
        page = self._page
        document = page.document
        objects = document.objects
        catalog = document.catalog
        root = catalog.get("StructTreeRoot")
        if not isinstance(root, Dictionary):
            catalog["StructTreeRoot"] = objects[objects.add({"Type": Name("StructTreeRoot"), "K": []})]
            root = cast("Dictionary", catalog["StructTreeRoot"])
            if not isinstance(catalog.get("MarkInfo"), Dictionary):
                catalog["MarkInfo"] = {}
            catalog["MarkInfo"]["Marked"] = True
        holder = _top_element(root)
        form_ref = objects[form]
        # Write each element, parents first, then their kids: MCIDs as marked-content references.
        nodes: dict[int, Dictionary] = {}
        owners: list[Dictionary | None] = [None] * self._mcids
        pending: list[tuple[_Element, Dictionary]] = [(element, holder) for element in reversed(self._elements)]
        written: list[_Element] = []
        while pending:
            element, parent = pending.pop()
            value: dict[str, object] = {"Type": Name("StructElem"), "S": Name(element.role), "P": parent}
            value["Pg"] = page.obj
            value.update(element.entries)
            node = objects[objects.add(value)]
            nodes[id(element)] = node
            written.append(element)
            pending.extend((kid, node) for kid in reversed(element.kids) if isinstance(kid, _Element))
        for element in written:
            node = nodes[id(element)]
            values: list[object] = []
            for kid in element.kids:
                if isinstance(kid, _Element):
                    values.append(nodes[id(kid)])
                else:
                    owners[kid] = node
                    values.append({"Type": Name("MCR"), "Pg": page.obj, "Stm": form_ref, "MCID": kid})
            node["K"] = values
        held = holder.get("K")
        if not isinstance(held, Array):
            holder["K"] = [] if held is None else [held]
            held = cast("Array", holder["K"])
        held.extend(nodes[id(element)] for element in self._elements)
        return _add_parent_tree_entry(document, root, owners)

    # Paths.

    def path(
        self,
        path: PathLike,
        *,
        fill: Color | Gradient | ShadingPattern | None = None,
        stroke: Stroke | Color | Gradient | None = None,
        fill_rule: FillRule = FillRule.NONZERO,
    ) -> None:
        """Draw a path: a :class:`PathBuilder`, a :class:`Path` item's geometry, or segments."""
        self._check_open()
        segments = _segments(path)
        if fill is None and stroke is None:
            stroke = _BLACK
        painted_fill = None if fill is None else _paint(fill, "fill")
        painted_stroke = _stroke(stroke)
        even_odd = FillRule(fill_rule) is FillRule.EVEN_ODD
        construction, points = _path_bytes(segments)
        self._count(points)
        filling = None if painted_fill is None else self._setting(painted_fill, segments, None)
        stroking = None
        if painted_stroke is not None:
            stroking = self._setting(
                cast("Color | Gradient | ShadingPattern", painted_stroke.paint), segments, painted_stroke
            )
        if filling is not None and stroking is not None and (filling.mask is not None or stroking.mask is not None):
            # A soft mask is one graphics state, so each paints in its own.
            first = self._painted(construction, filling, None, None, even_odd=even_odd)
            self._emit(first + b"\n" + self._painted(construction, None, stroking, painted_stroke, even_odd=even_odd))
        else:
            self._emit(self._painted(construction, filling, stroking, painted_stroke, even_odd=even_odd))

    def _painted(
        self,
        construction: bytes,
        filling: _Painting | None,
        stroking: _Painting | None,
        stroke: Stroke | None,
        *,
        even_odd: bool,
    ) -> bytes:
        """Return the operators that paint a constructed path with a fill, a stroke, or both."""
        parts = [b"q"]
        fill_alpha = None if filling is None else filling.alpha
        stroke_alpha = None if stroking is None else stroking.alpha
        if fill_alpha is not None or stroke_alpha is not None:
            parts.append(b"/" + self._gstate(fill_alpha=fill_alpha, stroke_alpha=stroke_alpha).encode() + b" gs")
        for painting in (filling, stroking):
            if painting is not None and painting.mask is not None:
                parts.append(b"/" + painting.mask.encode() + b" gs")
            if painting is not None:
                parts.extend(painting.operators)
        if stroke is not None and stroking is not None:
            parts.extend(_line_style(stroke))
        parts.append(construction)
        if filling is not None and stroking is not None:
            parts.append(b"B*" if even_odd else b"B")
        elif filling is not None:
            parts.append(b"f*" if even_odd else b"f")
        else:
            parts.append(b"S")
        parts.append(b"Q")
        return b"\n".join(parts)

    def line(
        self, start: Sequence[float], end: Sequence[float], *, stroke: Stroke | Color | Gradient | None = None
    ) -> None:
        """Draw a straight line, with a 1-point black stroke by default."""
        self.path(PathBuilder().move_to(start).line_to(end), stroke=_BLACK if stroke is None else stroke)

    def rect(
        self,
        rect: Sequence[float],
        *,
        radius: float = 0,
        fill: Color | Gradient | None = None,
        stroke: Stroke | Color | Gradient | None = None,
    ) -> None:
        """Draw a rectangle, with corners rounded by ``radius`` points."""
        self.path(PathBuilder().rect(rect, radius=radius), fill=fill, stroke=stroke)

    def circle(
        self,
        center: Sequence[float],
        radius: float,
        *,
        fill: Color | Gradient | None = None,
        stroke: Stroke | Color | Gradient | None = None,
    ) -> None:
        """Draw a circle."""
        self.path(PathBuilder().circle(center, radius), fill=fill, stroke=stroke)

    def ellipse(
        self,
        rect: Sequence[float],
        *,
        fill: Color | Gradient | None = None,
        stroke: Stroke | Color | Gradient | None = None,
    ) -> None:
        """Draw the ellipse inside ``rect``."""
        self.path(PathBuilder().ellipse(rect), fill=fill, stroke=stroke)

    def polygon(
        self,
        points: Iterable[Sequence[float]],
        *,
        close: bool = True,
        fill: Color | Gradient | None = None,
        stroke: Stroke | Color | Gradient | None = None,
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
        stroke: Stroke | Color | Gradient | None = None,
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
        fill: Color | Gradient | None = _BLACK,
        stroke: Stroke | Color | Gradient | None = None,
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
        is needed. Both accept a :class:`LinearGradient` or :class:`RadialGradient`,
        in the canvas coordinate space. If a soft mask requires separate fill
        and stroke objects, the second uses empty ``ActualText`` so extraction
        reads the text once. Gradient tolerances are those of :class:`Canvas`.
        The text is limited to 1 MiB of UTF-8 and 4,096 lines by
        default (``max_text_size``, ``None`` for trusted input), refused with
        :class:`LimitError` code ``text_input_size`` or ``text_line_count``.
        A colour glyph that COLR paints or a PNG strike holds draws in its own
        colours, which ``fill`` and ``stroke`` do not change, except for a COLR layer
        in the text's colour, which takes the fill. Any other colour glyph with no
        outline in an OpenType face raises :class:`PdfError`, naming the character,
        the glyph, and the paint; see :class:`Font`.
        """
        self._check_open()
        checked = _text_input(text, max_text_size)
        rotation = _quarter_turn(rotate)
        origin = _point(at, "at")
        font_size = _size(size)
        filled, stroked = _text_paint(fill, stroke)
        spacing = _line_height(line_height)
        # Count the lines, then charge them, before splitting.
        count = _line_count(checked)
        most = _max_lines(max_text_size)
        if most is not None and count > most:
            code = "text_line_count"
            msg = f"the text has {count} lines, over the limit of {most}"
            raise LimitError(code, msg)
        with self._scratch(_lines_size(checked, count)):
            lines = checked.splitlines()
            if not any(lines):
                return
            face = _canvas_font("\n".join(lines), font)
            leading = face._font.leading(font_size, spacing)  # noqa: SLF001
            cos, sin = _TURNS[rotation]
            placed = [
                (line, origin.x - index * leading * sin, origin.y + index * leading * cos, 0.0)
                for index, line in enumerate(lines)
                if line
            ]
            self._emit_text(face, font_size, (filled, stroked), rotation, placed)

    def textbox(  # noqa: PLR0913 - the text style options.
        self,
        rect: Sequence[float],
        text: str,
        *,
        font: Font | None = None,
        size: float = 11,
        align: Literal["left", "center", "right", "justify"] = "left",
        line_height: float | None = None,
        fill: Color | Gradient | None = _BLACK,
        stroke: Stroke | Color | Gradient | None = None,
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
        follow :meth:`text`, and text that fits refuses as there: a colour
        glyph with no outline raises :class:`PdfError`.
        """
        self._check_open()
        box = _rect(rect, "rect")
        rotation = _quarter_turn(rotate)
        with self._scratch(2 * sys.getsizeof(_text_input(text, max_text_size))):
            return self._textbox(
                box,
                text,
                font=font,
                size=size,
                align=align,
                line_height=line_height,
                paint=(fill, stroke),
                rotation=rotation,
                max_text_size=max_text_size,
            )

    def _textbox(  # noqa: PLR0913 - the text style options.
        self,
        box: Rect,
        text: str,
        *,
        font: Font | None,
        size: float,
        align: str,
        line_height: float | None,
        paint: tuple[Color | Gradient | None, Stroke | Color | Gradient | None],
        rotation: int,
        max_text_size: int | None,
    ) -> TextFit:
        """Lay out and draw a text box, once the memory of its normalized text is charged."""
        fill, stroke = paint
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
            normalized,
            width,
            height,
            font_size,
            spacing,
            align == "justify",
            _max_lines(max_text_size),
            operation=self._operation,
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
        paint: tuple[Color | Gradient | None, Stroke | None],
        rotation: int,
        lines: list[tuple[str, float, float, float]],
    ) -> None:
        """Draw lines of text, each at its origin in display space with its word spacing."""
        if not lines:
            return
        fill, stroke = paint
        segments = (
            self._text_bounds(font, size, rotation, lines)
            if (
                isinstance(fill, (LinearGradient, RadialGradient))
                or (stroke is not None and isinstance(stroke.paint, (LinearGradient, RadialGradient)))
            )
            else []
        )
        filling = None if fill is None else self._setting(fill, segments, None)
        stroking = None if stroke is None else self._setting(cast("Color | Gradient", stroke.paint), segments, stroke)
        split = filling is not None and stroking is not None and (filling.mask is not None or stroking.mask is not None)
        parts: list[bytes] = []
        if font._font.standard_name is None:  # noqa: SLF001
            # An embedded face's lines select their fonts as their glyphs ask.
            shown = self._shown_glyphs(font, size, lines)
            parts.append(b"BT")
        else:
            name = self._add_font(font)
            shown = _shown_winansi(font, lines, self._operation)
            parts.append(b"BT\n/" + name.encode() + b" " + _fmt(size) + b" Tf")
        cos, sin = _TURNS[rotation]
        for (_line, x, y, _spacing), operators in zip(lines, shown, strict=True):
            # Display space points down, so the text's own y axis is flipped up.
            parts.append(_numbers(cos, sin, sin, -cos, _number(x, "at"), _number(y, "at")) + b" Tm")
            parts.append(operators)
        parts.append(b"ET")
        shown_text = b"\n".join(parts)
        passes = ((filling, None), (None, stroking)) if split else ((filling, stroking),)
        with self._scratch(4 * len(shown_text) + 1024):
            painted = [
                self._paint_text(shown_text, filled, stroked, stroke, duplicate=bool(index))
                for index, (filled, stroked) in enumerate(passes)
            ]
            self._emit(b"\n".join(painted))

    def _paint_text(
        self,
        shown_text: bytes,
        filled: _Painting | None,
        stroked: _Painting | None,
        stroke: Stroke | None,
        *,
        duplicate: bool,
    ) -> bytes:
        """Return one painted text object, suppressing duplicate extraction when needed."""
        parts = [b"q"]
        if duplicate:
            # The second painting contributes ink but no second copy of the text.
            parts.append(b"/Span <</ActualText ()>> BDC")
        fill_alpha = None if filled is None else filled.alpha
        stroke_alpha = None if stroked is None else stroked.alpha
        if fill_alpha is not None or stroke_alpha is not None:
            parts.append(b"/" + self._gstate(fill_alpha=fill_alpha, stroke_alpha=stroke_alpha).encode() + b" gs")
        for painting in (filled, stroked):
            if painting is not None:
                if painting.mask is not None:
                    parts.append(b"/" + painting.mask.encode() + b" gs")
                parts.extend(painting.operators)
        if stroked is not None:
            parts.extend(_line_style(cast("Stroke", stroke)))
        parts.append(b"2 Tr" if filled is not None and stroked is not None else b"1 Tr" if stroked else b"0 Tr")
        parts.append(shown_text)
        if duplicate:
            parts.append(b"EMC")
        parts.append(b"Q")
        return b"\n".join(parts)

    def _text_bounds(
        self,
        font: Font,
        size: float,
        rotation: int,
        lines: list[tuple[str, float, float, float]],
    ) -> list[Segment]:
        """Bound rotated lines, including font side bearings and overhangs."""
        cos, sin = _TURNS[rotation]
        box = font.bbox
        self._charge(len(lines) * 1024)  # Four points and their segments per line.
        segments: list[Segment] = []
        for line, x, y, spacing in lines:
            if font._font.standard_name is not None:  # noqa: SLF001
                with self._scratch(len(line) * 16):
                    advance = font._font.text_width(line, size, None)  # noqa: SLF001
            else:
                glyphs = font._font.shape(line, operation=self._operation)  # noqa: SLF001
                advance = sum(glyph[1] for glyph in glyphs) * size / font._font.units_per_em  # noqa: SLF001
            advance += line.count(" ") * spacing
            for across in (min(0, box.x0 * size), advance + max(0, box.x1 * size)):
                segments.extend(
                    MoveTo(Point(x + across * cos - down * sin, y + across * sin + down * cos))
                    for down in (-max(font.ascender, box.y1) * size, -min(font.descender, box.y0) * size)
                )
        return segments

    def _shown_glyphs(self, font: Font, size: float, lines: list[tuple[str, float, float, float]]) -> list[bytes]:
        """Return the operators that show each line in an embedded face.

        The glyphs are shaped in visual order. One that COLR or a strike
        paints is shown as a one-byte code of a Type 3 font, and any other as
        an Identity-H code, its glyph ID, after their widths and texts are added
        to the fonts. Those writes are one nested transaction, so a refusal
        that the canvas's block catches leaves nothing of them.
        """
        document = self._page.document
        face = font._font  # noqa: SLF001 - the package's own face.
        operation = self._operation
        shaped = [face.shape(line, operation=operation) for line, _x, _y, _spacing in lines]
        drawn = [
            (glyph[0], line[glyph[5] : glyph[6]] or _NO_TEXT)
            for (line, *_), glyphs in zip(lines, shaped, strict=True)
            for glyph in glyphs
            if glyph[0]
        ]
        ids = sorted({glyph for glyph, _ in drawn})
        colour = dict(zip(ids, face.colour_glyphs(ids, size, operation=operation), strict=True))
        outlined = [entry for entry in drawn if colour[entry[0]] is None]
        painted = [(cast("_ColourGlyph", colour[glyph])[0], text) for glyph, text in drawn if colour[glyph] is not None]
        xref = None
        mapped: dict[int, str] = {}
        with document.transaction():
            if outlined:
                xref = embedded_font(document, font, _cache(document).fonts)
                mapped = add_glyphs(document, xref, font, outlined)
            placed = self._colour_glyphs(font, colour, painted) if painted else {}
        codes: dict[int, tuple[bytes, bytes]] = {}
        if xref is not None:
            name = self._font_name(xref).encode()
            codes.update((glyph, (name, b"<%04X>" % glyph)) for glyph, _ in outlined)
        for glyph, entry in colour.items():
            if entry is not None:
                number, code, text = placed[entry[0]]
                codes[glyph] = (self._font_name(number).encode(), b"<%02X>" % code)
                if text is not None:
                    mapped[glyph] = text
        widths = dict(zip(ids, face.glyph_widths(ids), strict=True))
        run = _GlyphRun(size, face.units_per_em, widths, mapped, codes)
        return [run.show(line, glyphs, spacing) for (line, _x, _y, spacing), glyphs in zip(lines, shaped, strict=True)]

    def _colour_glyphs(
        self, font: Font, colour: dict[int, _ColourGlyph | None], painted: list[tuple[str, str]]
    ) -> dict[str, tuple[int, int, str | None]]:
        """Add what ``font``'s Type 3 fonts lack of ``colour``, and return each glyph's font, code, and CMap text.

        ``painted`` holds each drawn glyph's name with its text. A glyph has
        one procedure in the document, however many drawings use it, with one
        image for each of its strikes, or one group of its COLR paints.
        """
        document = self._page.document
        core = document._core()  # noqa: SLF001
        face = font._font  # noqa: SLF001 - the package's own face.
        em = face.units_per_em
        fonts = _cache(document).colour.setdefault(font._key(), [])  # noqa: SLF001
        wanted = [(glyph, entry) for glyph, entry in colour.items() if entry is not None]
        held = _core.type3_codes(core, fonts, em, [entry[0] for _, entry in wanted])
        new: list[tuple[str, float, tuple[float, float, float, float] | None, int | None]] = []
        layered: list[tuple[str, float, int]] = []
        for (glyph, (name, advance, ppem)), found in zip(wanted, held, strict=True):
            if found is not None:
                continue
            if ppem is None:
                layered.append((name, advance, glyph))
                continue
            if not ppem:
                new.append((name, advance, None, None))
                continue
            png, *rect = face.bitmap(glyph, ppem)
            with self._scratch(len(png)):
                image = self._glyph_image(font, glyph, png)
            new.append((name, advance, (rect[0], rect[1], rect[2], rect[3]), image))
        pairs = list(dict.fromkeys(painted))
        shadings = _cache(document).shadings
        placed = _core.type3_draw(core, fonts, em, new, pairs, face=face, shadings=shadings, painted=layered)
        fonts.extend(dict.fromkeys(number for number, _, _ in placed if number not in fonts))
        return {name: result for (name, _), result in zip(pairs, placed, strict=True)}

    def _glyph_image(self, font: Font, glyph: int, png: bytes) -> int:
        """Return the image object of a glyph's PNG, refusing one that does not decode as a damaged font."""
        try:
            return self._stored_image(png)
        except ValueError as error:
            msg = f"{font.name} cannot draw its glyph {glyph}: {error}"
            raise PdfError(msg) from error

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
                data = _core.pixmap_png(document._core(), source)  # noqa: SLF001
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
        fonts and images: its content, geometry and resources are read
        from one version of its document, and its resources are copied
        with the objects they reach as they are when this call is made, so
        a later edit of the source does not change the placed page. ``clip`` is a rectangle
        in the source page's display coordinates, those of its
        :attr:`Page.rect`: only the part of the page inside it is drawn,
        scaled into ``rect`` in place of the whole page. ``rotate`` turns
        the page, or its clipped part, clockwise by 0, 90, 180, or 270
        degrees, and ``keep_aspect`` centres it in ``rect`` at its aspect
        ratio after the turn. A page placed several times, by any canvas of
        this document, is stored once while its content, its geometry and
        every object its resources reach are unchanged.
        Annotations and form fields of the source page are not drawn.
        """
        from pyropdf._page import Page  # noqa: PLC0415 - pyropdf._page imports this module.

        self._check_open()
        if not isinstance(source, Page):
            msg = f"source must be a pyropdf.Page, not {type(source).__name__}"
            raise TypeError(msg)
        box = _rect(rect, "rect")
        rotation = _quarter_turn(rotate)
        document = self._page.document
        core, source_core = document._core(), source.document._core()  # noqa: SLF001
        # One operation reads the page's geometry, content and resources
        # from one version of its document, and writes or reuses its Form.
        operation = _core.begin_canvas(core)

        def read() -> tuple[int, tuple[float, float, float, float], Rect, Rect, int]:
            page = _core.canvas_source_page(source_core, source.xref, source._generation, operation)  # noqa: SLF001
            if page is None:
                raise source._stale()  # noqa: SLF001
            source_rotation, source_box, width, height, _content, snapshot = page
            whole = Rect(0, 0, width, height)
            shown = whole if clip is None else _rect(clip, "clip")
            if not shown.intersects(whole):
                msg = f"clip {tuple(shown)} does not overlap the source page {tuple(whole)}"
                raise ValueError(msg)
            form = _core.canvas_page_form(
                _cache(document).forms,
                core,
                source_core,
                source.xref,
                source._generation,  # noqa: SLF001
                operation,
                snapshot,
            )
            if form is None:
                raise source._stale()  # noqa: SLF001
            return source_rotation, source_box, whole, shown, form

        try:
            source_rotation, source_box, whole, shown, form = read()
        except BaseException:
            _core.abort_canvas(core, operation)
            raise
        _core.end_transaction(core, True)  # noqa: FBT003 - the core's flag
        # Only the part of the clip on the page is drawn.
        shown &= whole
        covered, place = _placement(box, shown.width, shown.height, keep=bool(keep_aspect), rotation=rotation)
        to_display = _display_from_user(source_rotation, source_box)

        def convert(x: float, y: float) -> tuple[float, float]:
            dx, dy = to_display(x, y)
            return place(dx - shown.x0, dy - shown.y0)

        matrix = _affine(convert)
        name = self._add_xobject(form)
        self._emit(
            b"q\n"
            + _numbers(covered.x0, covered.y0, covered.width, covered.height)
            + b" re W n\n"
            + _numbers(*matrix)
            + b" cm\n/"
            + name.encode()
            + b" Do\nQ"
        )

    def draw(self, item: Path | Image) -> None:
        """Paint a :class:`Path` or :class:`Image` item that :meth:`Page.content` returned.

        The item's clips, its groups' opacity and blend modes, and its own
        blend mode are applied; soft masks are not. An axial or radial
        :class:`ShadingPattern` retains the color function read from its
        document version. Its RGB approximation is sampled on the first
        drawing, preserving its matrix, domain, opacity, and extension at
        either end, its clip box, and its background. Sampling is bounded
        to 1,025 stops; features between the probes can be lost.
        Function-based and mesh shadings and :class:`TilingPattern`
        paints cannot be drawn yet. An image is drawn from its object, with the
        image's own masks: a canvas on a page of the item's document draws
        that object again, and one of another document copies it, with the
        objects it reaches but pages, as it is when this call is made. Inline
        images and stencil image masks cannot be drawn yet, and text runs
        follow in a later release; an item that cannot be drawn raises
        ``TypeError`` or ``ValueError`` and draws nothing.

        The round-trip tests allow 16 levels of 255 per channel at 72 dpi,
        outside the boxes of the items that cannot be drawn and of those
        under a soft mask. They cover the bundled corpus and axial/radial
        gradients, including sampled and calculator functions.
        """
        self._check_open()
        if isinstance(item, Image):
            self._draw_image(item)
            return
        if not isinstance(item, Path):
            msg = f"draw() paints Path and Image items, not {type(item).__name__}"
            raise TypeError(msg)
        fill = None if item.fill is None else _paint(item.fill, "fill")
        # Checked before any scope opens, so that a refused stroke draws nothing.
        stroke = _stroke(item.stroke)
        with self._item_context(item):
            self.path(item.segments, fill=fill, stroke=stroke, fill_rule=item.fill_rule)

    def _draw_image(self, item: Image) -> None:
        """Paint an :class:`Image` item from the object it names."""
        if item.is_stencil:
            msg = "a stencil image mask cannot be drawn yet"
            raise ValueError(msg)
        if item.xref == 0:
            msg = "an image without an object number cannot be drawn yet"
            raise ValueError(msg)
        if item._source is None or item._source.document is None:  # noqa: SLF001
            msg = "the image does not name its document; draw an Image that Page.content() returned"
            raise ValueError(msg)
        source = item._source.document  # noqa: SLF001
        document = self._page.document
        if source is document:
            _image_stream(document, item.xref)
            xref = item.xref
        else:
            known = self._copies.get((source, item.xref))
            if known is None:
                copy = _core.canvas_copy_image(document._core(), source._core(), item.xref, self._active)  # noqa: SLF001
                if copy is None:
                    msg = f"object {item.xref} of the item's document is not an image XObject"
                    raise ValueError(msg)
                known = self._copies[source, item.xref] = copy
            xref = known
        # The item's square holds the first row at y = 0, PDF's at y = 1.
        matrix = tuple(Matrix(1, 0, 0, -1, 0, 1) * item.matrix)
        with self._item_context(item):
            name = self._add_xobject(xref)
            self._emit(b"q\n" + _numbers(*matrix) + b" cm\n/" + name.encode() + b" Do\nQ")

    @contextmanager
    def _item_context(self, item: Path | Image) -> Generator[None]:
        """Apply an item's clips, groups, and blend mode around its painting."""
        scopes: list[AbstractContextManager[None]] = [
            self.clip(clip.segments, fill_rule=clip.fill_rule) for clip in item.context.clips
        ]
        for group in item.context.groups:
            if group.blend_mode is not BlendMode.NORMAL:
                scopes.append(self.blend(group.blend_mode))
            if group.opacity < 1:
                scopes.append(self.opacity(group.opacity))
        if item.blend_mode is not BlendMode.NORMAL:
            scopes.append(self.blend(item.blend_mode))
        entered: list[AbstractContextManager[None]] = []
        try:
            for scope in scopes:
                scope.__enter__()
                entered.append(scope)
            yield
        finally:
            for scope in reversed(entered):
                scope.__exit__(None, None, None)

    # Scopes.

    def clip(self, path: PathLike, *, fill_rule: FillRule = FillRule.NONZERO) -> AbstractContextManager[None]:
        """Clip what the block draws to a path."""
        self._check_open()
        construction, points = _path_bytes(_segments(path))
        self._count(points)
        operator = b"W* n" if FillRule(fill_rule) is FillRule.EVEN_ODD else b"W n"
        return _Scope(self, b"q\n" + construction + b"\n" + operator, b"Q")

    def transform(self, matrix: Matrix | Sequence[float]) -> AbstractContextManager[None]:
        """Transform what the block draws by ``matrix``, applied before the enclosing transforms."""
        self._check_open()
        try:
            m = Matrix(*matrix)
        except (TypeError, ValueError, PdfError) as exc:
            msg = f"matrix must be a Matrix or six numbers: {matrix!r}"
            raise ValueError(msg) from exc
        values = tuple(_number(value, "matrix") for value in m)
        # What the content's operator says, to the digit, is what the canvas's patterns follow.
        written = Matrix(*(float(_fmt(value)) for value in values))
        return _Scope(self, b"q\n" + _numbers(*values) + b" cm", b"Q", matrix=written)

    def opacity(self, alpha: float) -> AbstractContextManager[None]:
        """Composite what the block draws as one group, then draw it at ``alpha``."""
        self._check_open()
        return _Scope(self, b"", b"", group=_unit(alpha, "alpha"))

    def blend(self, mode: BlendMode | str) -> AbstractContextManager[None]:
        """Blend what the block draws with the content below it."""
        self._check_open()
        try:
            blend_mode = BlendMode(mode)
        except ValueError as exc:
            msg = f"mode must be a BlendMode: {mode!r}"
            raise ValueError(msg) from exc
        name = self._gstate(blend=_BLEND_NAMES[blend_mode])
        return _Scope(self, b"q\n/" + name.encode() + b" gs", b"Q")

    def layer(self, layer: Layer | LayerMembership) -> AbstractContextManager[None]:
        """Draw the block as optional content of a :class:`Layer` or a :class:`LayerMembership`.

        What the block draws shows only while ``layer``, a group of the
        page's document or a membership of its groups, is visible, in
        rendering, text extraction, and viewers. The block is written as
        ``/OC`` marked content; a membership is written as a new membership
        dictionary. A group that is no longer an optional
        content group when the canvas commits raises
        :class:`StaleObjectError`. Use a :class:`Layer` returned by this
        document's :attr:`Document.layers`: a manually constructed or
        unpickled value has no document of origin and cannot be drawn.
        """
        self._check_open()
        layers = self._page.document.layers
        if isinstance(layer, LayerMembership):
            groups = tuple(layers._group(member) for member in layer.layers)  # noqa: SLF001
            oc: int | tuple[tuple[int, ...], str] = (groups, layer.policy.value)
        elif isinstance(layer, Layer):
            oc = layers._group(layer)  # noqa: SLF001
        else:
            msg = f"layer takes a Layer or a LayerMembership, not {type(layer).__name__}"
            raise TypeError(msg)
        name = self._oc.get(oc)
        if name is None:
            objects = self._page.document.objects
            if isinstance(oc, int):
                xref = oc
            else:
                # hayro reads a membership's policy only from an indirect dictionary.
                groups, policy = oc
                value = {"Type": Name("OCMD"), "OCGs": [objects[group] for group in groups], "P": Name(policy)}
                xref = objects.add(value)
            name = self._oc[oc] = f"PyroL{len(self._oc) + 1}"
            self._properties[name] = xref
        return _Scope(self, b"/OC /" + name.encode() + b" BDC", b"EMC")

    def tag(  # noqa: PLR0913 - the entries of a structure element.
        self,
        role: TagRole | str,
        *,
        alt: str | None = None,
        lang: str | None = None,
        title: str | None = None,
        actual_text: str | None = None,
        scope: TableHeaderScope | str | None = None,
        numbering: ListNumbering | str | None = None,
    ) -> AbstractContextManager[None]:
        """Make what the block draws a structure element of the document's structure tree.

        Scopes nest into the structure tree in the order of the calls.
        :attr:`TagRole.ARTIFACT` marks content outside the logical
        structure, as is everything drawn outside any tag scope of a
        document that already has a structure tree. ``alt`` is the
        alternate description that figures and formulas need, ``title`` the
        element's title, ``lang`` its BCP 47 language, and ``actual_text``
        the text that replaces its content for extraction and assistive
        technology. ``scope`` says what a :attr:`TagRole.TABLE_HEADER` cell
        heads, and ``numbering`` how a :attr:`TagRole.LIST` numbers its
        items; other roles take neither.

        The elements join the document's structure tree, created when it
        has none, under its ``Document`` element when the root holds that
        one element only. The canvas's drawing is then written as a Form
        XObject with a ``/StructParents`` key of its own, in which each
        element names its marked content. A tag scope cannot start inside an
        :meth:`opacity` scope or an artifact.
        """
        self._check_open()
        try:
            tag_role = TagRole(role)
        except ValueError as exc:
            msg = f"role must be a TagRole: {role!r}"
            raise ValueError(msg) from exc
        texts = {"Alt": alt, "Lang": lang, "T": title, "ActualText": actual_text}
        for key, value in texts.items():
            if value is not None and not isinstance(value, str):
                msg = f"{key} must be a string or None: {value!r}"
                raise TypeError(msg)
        if lang is not None and (len(lang) > _MAX_LANGUAGE_TAG or _LANGUAGE_TAG.fullmatch(lang) is None):
            msg = f"lang must be a BCP 47 language tag: {lang!r}"
            raise ValueError(msg)
        header_scope = _tag_attribute(scope, TableHeaderScope, "scope", tag_role, TagRole.TABLE_HEADER)
        list_numbering = _tag_attribute(numbering, ListNumbering, "numbering", tag_role, TagRole.LIST)
        if tag_role is TagRole.ARTIFACT:
            return _TagScope(self, None)
        entries: dict[str, object] = {key: value for key, value in texts.items() if value is not None}
        if header_scope is not None:
            entries["A"] = {"O": Name("Table"), "Scope": Name(header_scope)}
        if list_numbering is not None:
            entries["A"] = {"O": Name("List"), "ListNumbering": Name(list_numbering)}
        return _TagScope(self, _Element(tag_role.value, entries))

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


# The line boundaries of str.splitlines.
_LINE_SEPARATORS = ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
# A bound on the memory of one str object beyond its characters, and of a list slot.
_STR_OVERHEAD = sys.getsizeof("") + 8


def _line_count(text: str) -> int:
    """Return ``len(text.splitlines())`` without splitting ``text``."""
    if not text:
        return 0
    breaks = sum(str.count(text, separator) for separator in _LINE_SEPARATORS) - str.count(text, "\r\n")
    return breaks + (text[-1] not in _LINE_SEPARATORS)


def _lines_size(text: str, count: int) -> int:
    """Return a bound on the memory of ``text``'s lines and of their joined copy."""
    return 2 * sys.getsizeof(text) + count * _STR_OVERHEAD


def _shown_winansi(
    font: Font, lines: list[tuple[str, float, float, float]], operation: _core._OperationBudget | None
) -> list[bytes]:
    """Return the operators that show each line in a Standard 14 font, with its word spacing."""
    shown = []
    word_space = 0.0
    for line, _x, _y, spacing in lines:
        operators = b"<" + font._font.encode(line, None, operation=operation).hex().encode() + b"> Tj"  # noqa: SLF001
        if spacing != word_space:
            operators = _fmt(spacing) + b" Tw\n" + operators
            word_space = spacing
        shown.append(operators)
    return shown


#: A glyph that shaping placed, as ``_Font.shape`` returns it.
_ShapedGlyph = tuple[int, int, int, int, int, int, int, bool, int, int]
#: How far, in thousandths of the em, the last glyph of ``/ActualText`` may end
#: from its cluster's end: readers look for a word gap from about a tenth.
_CLUSTER_END_TOLERANCE = 50


class _GlyphRun:
    """Writes shaped glyphs as ``TJ`` arrays.

    ``TJ`` adjustments move the pen from each glyph's width in ``W`` to its
    shaped advance and by its offset, ``Ts`` raises it, and a line's word
    spacing widens its spaces, since ``Tw`` applies to one-byte codes only.
    A cluster whose glyphs do not read as its text in the ToUnicode CMap is
    shown inside one ``/ActualText``. ``codes`` holds each glyph's font and
    code: ``Tf`` selects a glyph's font where it changes, which leaves the pen
    and the pending adjustment as they are, so a line may mix a Type0 and
    Type 3 fonts.
    """

    def __init__(
        self,
        size: float,
        units_per_em: int,
        widths: dict[int, float],
        mapped: dict[int, str],
        codes: dict[int, tuple[bytes, bytes]],
    ) -> None:
        self.size = size
        self.units_per_em = units_per_em
        self.scale = 1000 / units_per_em
        self.widths = widths
        self.mapped = mapped
        self.codes = codes
        self.font: bytes | None = None
        self.ops: list[bytes] = []
        self.items: list[bytes] = []
        self.pending = 0.0  # From the pen to the text position, in TJ units: positive moves left.
        self.rise = 0

    def flush(self) -> None:
        if self.items:
            self.ops.append(b"[" + b" ".join(self.items) + b"] TJ")
            self.items.clear()

    def show(self, line: str, glyphs: list[_ShapedGlyph], spacing: float) -> bytes:
        self.ops = []
        self.pending = 0.0
        self.rise = 0
        for (start, end), cluster in groupby(glyphs, key=lambda glyph: glyph[8:]):
            self._show_cluster(line, line[start:end], list(cluster), spacing)
        self.flush()
        if self.rise:
            self.ops.append(b"0 Ts")
        return b"\n".join(self.ops)

    def _reads_as(self, text: str, cluster: list[_ShapedGlyph]) -> bool:
        """Return whether the CMap's texts of a cluster's glyphs read as its text, in canonical order of its marks."""
        texts = [self.mapped.get(glyph[0], "") for glyph in cluster if glyph[0]]
        if cluster[0][7]:
            texts.reverse()
        return not texts or unicodedata.normalize("NFD", "".join(texts)) == unicodedata.normalize("NFD", text)

    def _show_cluster(self, line: str, text: str, cluster: list[_ShapedGlyph], spacing: float) -> None:
        spanned = not self._reads_as(text, cluster)
        # Readers take the box of ``/ActualText`` from the glyphs inside: MuPDF
        # from the first glyph, Xpdf and poppler from the first glyph's origin
        # to the last one's end. So a glyph with a width comes first, not the
        # mark that a right-to-left run places before its base, and the last
        # glyph leaves the text position at the cluster's end. Each glyph still
        # draws where the shaper put it (``evidence/cluster-text``).
        lead = next((index for index, glyph in enumerate(cluster) if glyph[0] and glyph[1]), 0) if spanned else 0
        order = [lead, *(index for index in range(len(cluster)) if index != lead)]
        pens = list(accumulate((glyph[1] * self.scale for glyph in cluster), initial=0.0))
        ends_span = spanned and sum(1 for glyph in cluster if glyph[0]) > 1
        if spanned:
            self.flush()
            self.ops.append(b"/Span <</ActualText <FEFF" + _utf16(text) + b">>> BDC")
        at = 0.0
        for index in order:
            glyph, _advance, _y_advance, x_offset, y_offset, start, end, *_ = cluster[index]
            self.pending -= pens[index] - at
            at = pens[index]
            if not glyph:
                # A control character without a glyph only advances.
                continue
            offset = x_offset * self.scale
            follow = pens[-1] - (at + offset + self.widths[glyph]) if ends_span and index == order[-1] else 0.0
            self._show_glyph(glyph, offset, y_offset, follow if abs(follow) > _CLUSTER_END_TOLERANCE else 0.0)
            if spacing and line[start:end] == " ":
                self.pending -= spacing * 1000 / self.size
        self.pending -= pens[-1] - at
        if spanned:
            self.flush()
            self.ops.append(b"EMC")

    def _show_glyph(self, glyph: int, offset: float, y_offset: int, follow: float) -> None:
        """Show one glyph at the pen, and move the text position ``follow`` further through ``Tc``."""
        if y_offset != self.rise:
            self.flush()
            self.rise = y_offset
            self.ops.append(_fmt(self.rise * self.size / self.units_per_em) + b" Ts")
        if follow:
            self.flush()
            self.ops.append(_fmt(follow * self.size / 1000) + b" Tc")
        font, code = self.codes[glyph]
        if font != self.font:
            self.flush()
            self.font = font
            self.ops.append(b"/" + font + b" " + _fmt(self.size) + b" Tf")
        if self.pending - offset:
            self.items.append(_fmt(self.pending - offset))
        self.items.append(code)
        self.pending = self.widths[glyph] + offset + follow
        if follow:
            self.flush()
            self.ops.append(b"0 Tc")


def _font_value(objects: Objects, font: str | int) -> object:
    """Return the resource value of a Standard 14 font's base name or an embedded face's Type0 font."""
    return _font_dict(font) if isinstance(font, str) else objects[font]


def _top_element(root: Dictionary) -> Dictionary:
    """Return the element that new elements join: the root's lone ``Document`` element, or the root."""
    kids = root.get("K")
    if isinstance(kids, Array) and len(kids) == 1:
        kids = kids[0]
    if isinstance(kids, Dictionary) and kids.is_indirect and kids.get("S") == "Document":
        return kids
    return root


def _add_parent_tree_entry(document: Document, root: Dictionary, owners: list[Dictionary | None]) -> int:
    """Add the elements that own a Form's MCIDs to the root's parent tree under a new key; return the key.

    The key follows ``/ParentTreeNextKey`` and every key of the tree, whose
    largest key its last kids hold. A tree whose last kids go deeper than
    32 levels raises :class:`LimitError` (``structure_tree_count``).
    """
    tree = root.get("ParentTree")
    if not isinstance(tree, Dictionary):
        root["ParentTree"] = document.objects[document.objects.add({"Nums": []})]
        tree = cast("Dictionary", root["ParentTree"])
    path = [tree]
    node = tree
    while not isinstance(node.get("Nums"), Array):
        kids = node.get("Kids")
        if not isinstance(kids, Array) or not len(kids) or not isinstance(kids[-1], Dictionary):
            node["Nums"] = []
            break
        if len(path) == _MAX_TREE_DEPTH:
            code = "structure_tree_count"
            msg = f"the parent tree is deeper than {_MAX_TREE_DEPTH} levels"
            raise LimitError(code, msg)
        node = kids[-1]
        path.append(node)
    nums = cast("Array", node["Nums"])
    following = root.get("ParentTreeNextKey")
    key = following if isinstance(following, int) and not isinstance(following, bool) and following >= 0 else 0
    if len(nums) >= 2 and isinstance(nums[-2], int):  # noqa: PLR2004 - a key and its value
        key = max(key, nums[-2] + 1)
    nums.extend([key, owners])
    for held in path[1:]:
        limits = held.get("Limits")
        held["Limits"] = [limits[0] if isinstance(limits, Array) and len(limits) else key, key]
    root["ParentTreeNextKey"] = key + 1
    return key


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


def _merge_resources(  # noqa: PLR0913, PLR0917 - the page, and each kind of resource.
    page: Page,
    xobjects: dict[str, int],
    gstates: dict[tuple[float | None, float | None, str | None], str],
    fonts: dict[str, str | int],
    properties: dict[str, int],
    patterns: dict[str, int] | None = None,
    masks: dict[int, str] | None = None,
) -> dict[str, str]:
    """Add the canvas's resources to the page's own, renaming any that collide.

    The page gets its own direct ``/Resources`` when it inherits or shares
    them, so no other page changes. Returns the renamed names.
    """
    if not xobjects and not gstates and not fonts and not properties and not patterns and not masks:
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
        (
            "ExtGState",
            {
                **{name: _gstate_dict(state) for state, name in gstates.items()},
                **{name: document.objects[xref] for xref, name in (masks or {}).items()},
            },
        ),
        ("Pattern", {name: document.objects[xref] for name, xref in (patterns or {}).items()}),
        ("Font", {name: _font_value(document.objects, font) for name, font in fonts.items()}),
        ("Properties", {name: document.objects[xref] for name, xref in properties.items()}),
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
for _public in (Canvas, ListNumbering, PathBuilder, TableHeaderScope, TagRole, TextFit):
    _public.__module__ = "pyropdf"
