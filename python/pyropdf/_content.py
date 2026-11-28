"""The content model: what hayro paints on a page, as immutable values.

``Page.content()`` returns a :class:`Content` sequence of :class:`TextRun`,
:class:`Path`, and :class:`Image` items in paint order and display space.
Every item carries the :class:`GraphicsContext` that encloses it, and items
painted under the same clips, transparency groups, and marked content share
one context object. ``Page.paths()`` returns the paths alone as
:class:`Paths` columns, without an object per path or segment.
"""

from __future__ import annotations

import enum
import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeAlias, overload

from pyropdf import _core
from pyropdf._geometry import Matrix, Point, Rect
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._pixmap import Colorspace

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Self

    from pyropdf._core import PathColumns, _OperationBudget, _PageSnapshot, _PathPaint, _PathRow, _PathStroke
    from pyropdf._document import Document
    from pyropdf._objects import Array, Dictionary
    from pyropdf._page import _RetainedPage
    from pyropdf._pixmap import Pixmap


class BlendMode(enum.StrEnum):
    """A blend mode of ISO 32000-2 §11.3.5."""

    NORMAL = "normal"
    MULTIPLY = "multiply"
    SCREEN = "screen"
    OVERLAY = "overlay"
    DARKEN = "darken"
    LIGHTEN = "lighten"
    COLOR_DODGE = "color_dodge"
    COLOR_BURN = "color_burn"
    HARD_LIGHT = "hard_light"
    SOFT_LIGHT = "soft_light"
    DIFFERENCE = "difference"
    EXCLUSION = "exclusion"
    HUE = "hue"
    SATURATION = "saturation"
    COLOR = "color"
    LUMINOSITY = "luminosity"


class FillRule(enum.StrEnum):
    """The rule that decides which areas a path fills or clips."""

    NONZERO = "nonzero"
    EVEN_ODD = "even_odd"


class LineCap(enum.StrEnum):
    """The shape at the open ends of a stroke."""

    BUTT = "butt"
    ROUND = "round"
    SQUARE = "square"


class LineJoin(enum.StrEnum):
    """The shape at the corners of a stroke."""

    MITER = "miter"
    ROUND = "round"
    BEVEL = "bevel"


class TextRenderMode(enum.StrEnum):
    """How a text run paints its glyphs.

    hayro paints the clipping modes 4 to 6 as their painting part and adds
    their clip to the context of later items. Text in mode 7, which only
    clips, paints nothing; hayro passes it on as invisible text, so it is an
    ``INVISIBLE`` run.
    """

    FILL = "fill"
    STROKE = "stroke"
    FILL_STROKE = "fill_stroke"
    INVISIBLE = "invisible"


class WritingMode(enum.StrEnum):
    """The writing mode of a text run's font (ISO 32000-2 §9.2.4)."""

    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


class ShadingType(enum.IntEnum):
    """A shading type of ISO 32000-2 §8.7.4.5.

    hayro reads free-form and lattice-form triangle meshes (types 4 and 5)
    into the same triangles, so both report :attr:`TRIANGLE_MESH`.
    """

    FUNCTION = 1
    AXIAL = 2
    RADIAL = 3
    TRIANGLE_MESH = 4
    COONS_PATCH_MESH = 6
    TENSOR_PATCH_MESH = 7


class SoftMaskType(enum.StrEnum):
    """What a soft mask reads from its group: alpha or luminosity."""

    ALPHA = "alpha"
    LUMINOSITY = "luminosity"


class GradientExtend(enum.StrEnum):
    """What a gradient paints beyond the two ends of its line."""

    PAD = "pad"
    """The color at each end continues."""
    PAD_START = "pad_start"
    """Only the color at the start continues."""
    PAD_END = "pad_end"
    """Only the color at the end continues."""
    NONE = "none"
    """Nothing."""
    REPEAT = "repeat"
    """The line starts again."""
    REFLECT = "reflect"
    """The line runs back and forth."""


class ImageMask(enum.StrEnum):
    """The kind of mask that an image dictionary names."""

    SOFT = "soft"
    """``/SMask``: a grayscale soft-mask image."""
    EXPLICIT = "explicit"
    """``/Mask`` with a stencil-mask image."""
    COLOR_KEY = "color_key"
    """``/Mask`` with color ranges that are not painted."""


@dataclass(frozen=True, slots=True)
class MoveTo:
    """Start a new subpath at ``to``."""

    to: Point


@dataclass(frozen=True, slots=True)
class LineTo:
    """Draw a line to ``to``."""

    to: Point


@dataclass(frozen=True, slots=True)
class CurveTo:
    """Draw a cubic Bézier curve to ``to`` with two control points."""

    c1: Point
    c2: Point
    to: Point


@dataclass(frozen=True, slots=True)
class ClosePath:
    """Close the current subpath with a line to its start."""


#: One path construction segment of a `Path` or `PathBuilder`.
Segment: TypeAlias = MoveTo | LineTo | CurveTo | ClosePath


@dataclass(frozen=True, slots=True)
class Color:
    """A solid paint in sRGB with optional stored PDF color space and components.

    ``red``, ``green``, ``blue``, and ``alpha`` range from 0 to 1. Colors
    extracted by :meth:`Page.content` or :meth:`Page.paths` also populate
    ``colorspace`` (such as ``"DeviceGray"``, ``"DeviceRGB"``,
    ``"DeviceCMYK"``, ``"ICCBased"``, ``"CalGray"``, ``"CalRGB"``, ``"Lab"``,
    ``"Indexed"``, ``"Separation"``, ``"DeviceN"``, or ``"Pattern"``) and
    ``components`` with the PDF component values in that color space. Directly
    constructed colors leave both ``None`` unless supplied as keyword
    arguments.

    Equality and hashing compare ``(red, green, blue, alpha)`` so extracted
    colors remain equal to ``Color(red, green, blue, alpha)`` and work
    interchangeably in sets, dictionaries, and :class:`Canvas` drawing.

    Note: hayro canonicalizes ``ICCBased`` color spaces whose profile header
    marks sRGB (``sRGB`` at bytes 52..56) and colors with an active transfer
    function to ``"DeviceRGB"``.
    """

    red: float
    green: float
    blue: float
    alpha: float = 1.0
    colorspace: str | None = field(default=None, kw_only=True, compare=False)
    """The stored PDF color space family name, or ``None`` when unspecified."""
    components: tuple[float, ...] | None = field(default=None, kw_only=True, compare=False)
    """The original PDF color components in :attr:`colorspace`, or ``None`` when unspecified."""

    @property
    def rgb(self) -> tuple[float, float, float]:
        """Return ``(red, green, blue)``."""
        return (self.red, self.green, self.blue)


@dataclass(frozen=True, slots=True, kw_only=True)
class ShadingPattern:
    """A paint that a shading fills, from a shading pattern or ``sh``.

    Read axial and radial paints retain their color function without sampling
    it during extraction. :meth:`Canvas.draw` samples it when repainting.
    """

    shading_type: ShadingType | None
    """The shading type, or ``None`` for a shading that hayro could not
    read, which paints nothing."""
    matrix: Matrix
    """The transform from shading space to display space."""
    opacity: float
    _source: _ShadingSource | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class TilingPattern:
    """A paint that repeats a pattern cell.

    Read tiling patterns retain their captured cell operations in pattern space
    so :meth:`Canvas.draw` can repaint them.
    """

    bbox: Rect
    """The pattern cell in pattern space."""
    x_step: float
    y_step: float
    matrix: Matrix
    """The transform from pattern space to display space."""
    opacity: float = 1.0
    """The opacity of the pattern cell as a whole."""
    _source: _TilingSource | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class PatternPaint:
    """Paint with an indirect tiling or shading pattern in the canvas document.

    Pass the pattern's store handle, for example the ``obj`` of a
    :class:`typed.PatternType1` or :class:`typed.PatternType2`. Its PDF matrix
    positions it in default user space. For an uncolored tiling pattern,
    supply an indirect ``[/Pattern base]`` color space and its color components.
    DeviceN bases support up to 32 components, matching the renderer.
    """

    pattern: Dictionary
    space: Array | None = None
    components: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class ColorStop:
    """A place on a gradient's line, and the color and alpha there."""

    offset: float
    """Where the stop is, from 0 at the line's start to 1 at its end."""
    color: Color
    """The color, whose alpha is the stop's."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LinearGradient:
    """A paint for a :class:`Canvas` that changes color along a straight line.

    The line runs from ``start`` to ``end``, in the space of what it paints,
    so it moves with :meth:`Canvas.transform` as a path does. ``stops`` are
    in order, and two at one offset make a sharp change. Before the first
    stop and after the last, the color does not change. Color and alpha
    interpolate apart, so a color is not premultiplied by its alpha.
    """

    start: Point
    end: Point
    stops: tuple[ColorStop, ...]
    extend: GradientExtend = GradientExtend.PAD


@dataclass(frozen=True, slots=True, kw_only=True)
class RadialGradient:
    """A paint for a :class:`Canvas` that changes color between two circles.

    The line runs from the circle at ``start`` to the circle at ``end``,
    through the circles between them, in the space of what it paints. The
    stops and ``extend`` are those of a :class:`LinearGradient`.
    """

    start: Point
    start_radius: float
    end: Point
    end_radius: float
    stops: tuple[ColorStop, ...]
    extend: GradientExtend = GradientExtend.PAD


#: A linear or radial gradient paint for a `Canvas`.
Gradient: TypeAlias = LinearGradient | RadialGradient
_ReadGradient: TypeAlias = tuple[
    Gradient, tuple[float, float, float, float] | None, tuple[float, float, float, float] | None
]
_InlinePayload: TypeAlias = tuple[
    bool,
    int,
    int,
    bool,
    bytes,
    tuple[int, int, bytes] | None,
    bool,
]
_TilingCellData: TypeAlias = tuple[
    tuple[float, float, float, float],
    float,
    float,
    str | None,
    list[Any],
]
#: A solid color, pattern, shading, or gradient paint; a canvas also repaints
#: axial/radial `ShadingPattern` and `TilingPattern` values.
Paint: TypeAlias = Color | PatternPaint | ShadingPattern | TilingPattern | Gradient


def _sampled_to_gradient(
    raw: tuple[
        bool,
        Sequence[float],
        Sequence[bool],
        Sequence[Sequence[float]],
        tuple[float, float, float, float] | None,
        Sequence[float] | None,
    ],
) -> _ReadGradient:
    """Convert a core sampled-shading tuple into a ``(Gradient, bbox, background)`` triple."""
    axial, coords, ends, samples, bbox, background = raw
    bg: tuple[float, float, float, float] | None = None
    if background is not None:
        r, g, b, a = background
        bg = (r, g, b, a)
    extend = {
        (False, False): GradientExtend.NONE,
        (True, True): GradientExtend.PAD,
        (True, False): GradientExtend.PAD_START,
        (False, True): GradientExtend.PAD_END,
    }[(ends[0], ends[1])]
    stops = tuple(ColorStop(offset, Color(r, g, b, a)) for offset, r, g, b, a in samples)
    if axial:
        gradient: Gradient = LinearGradient(
            start=Point(coords[0], coords[1]), end=Point(coords[2], coords[3]), stops=stops, extend=extend
        )
    else:
        gradient = RadialGradient(
            start=Point(coords[0], coords[1]),
            start_radius=coords[2],
            end=Point(coords[3], coords[4]),
            end_radius=coords[5],
            stops=stops,
            extend=extend,
        )
    return gradient, bbox, bg


class _ShadingSource:
    """A shared, version-stable paint, sampled once on its first drawing."""

    __slots__ = ("_background", "_bbox", "_columns", "_gradient", "_index", "_lock")

    def __init__(
        self,
        columns: PathColumns | None,
        index: int,
        gradient: Gradient | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        background: tuple[float, float, float, float] | None = None,
    ) -> None:
        self._columns = columns
        self._index = index
        self._gradient = gradient
        self._bbox = bbox
        self._background = background
        self._lock = threading.Lock()

    def __reduce__(
        self,
    ) -> tuple[
        type[_ShadingSource],
        tuple[
            None,
            int,
            Gradient | None,
            tuple[float, float, float, float] | None,
            tuple[float, float, float, float] | None,
        ],
    ]:
        # Like an Image's source, the native owner is not a pickle value.
        return _ShadingSource, (None, self._index, self._gradient, self._bbox, self._background)

    def gradient(self, operation: _OperationBudget) -> _ReadGradient:
        """Return its RGB approximation under the active canvas's budget."""
        operation.check("content_element_count", 0)
        with self._lock:
            if self._gradient is not None:
                return self._gradient, self._bbox, self._background
            raw = None if self._columns is None else self._columns.sample_shading(self._index, operation)
            if raw is None:
                msg = "the ShadingPattern does not retain an axial or radial paint; draw one read from Page.content()"
                raise TypeError(msg)
            gradient, self._bbox, self._background = _sampled_to_gradient(raw)
            self._gradient = gradient
            self._columns = None
            return gradient, self._bbox, self._background


class _TilingSource:
    """A shared, version-stable tiling pattern cell, materialized on first drawing."""

    __slots__ = ("_cell", "_columns", "_index", "_lock")

    def __init__(
        self,
        columns: PathColumns | None,
        index: int,
        cell: _TilingCellData | None = None,
    ) -> None:
        self._columns = columns
        self._index = index
        self._cell = cell
        self._lock = threading.Lock()

    def __reduce__(
        self,
    ) -> tuple[type[_TilingSource], tuple[None, int, _TilingCellData | None]]:
        return _TilingSource, (None, self._index, self._cell)

    def cell(self, operation: _OperationBudget) -> _TilingCellData:
        """Return the captured cell under the active canvas's budget."""
        operation.check("content_element_count", 0)
        with self._lock:
            if self._cell is not None:
                return self._cell
            raw = None if self._columns is None else self._columns.tiling_cell(self._index, operation)
            if raw is None:
                msg = "the TilingPattern does not retain its pattern cell; draw one read from Page.content()"
                raise TypeError(msg)
            self._cell = raw
            self._columns = None
            return raw


class _InlineImageSource:
    """A retained inline raster or stencil image payload, materialized on first drawing."""

    __slots__ = ("_columns", "_data", "_index", "_lock")

    def __init__(
        self,
        columns: PathColumns | None,
        index: int,
        data: _InlinePayload | None = None,
    ) -> None:
        self._columns = columns
        self._index = index
        self._data = data
        self._lock = threading.Lock()

    def __reduce__(
        self,
    ) -> tuple[type[_InlineImageSource], tuple[None, int, None]]:
        # Unpickled inline images drop native payloads just like indirect _Source.
        return _InlineImageSource, (None, self._index, None)

    def data(self, operation: _OperationBudget) -> _InlinePayload | None:
        """Return the decoded inline payload under the active canvas's budget."""
        operation.check("content_element_count", 0)
        with self._lock:
            if self._data is not None:
                return self._data
            if self._columns is None:
                return None
            raw = self._columns.inline_image(self._index, operation)
            if raw is None:
                return None
            self._data = raw
            self._columns = None
            return raw


@dataclass(frozen=True, slots=True, kw_only=True)
class Stroke:
    """How a path or text run is stroked, in display space.

    ``width`` and the dash lengths are scaled by the largest scale of the
    transform in force when the stroke was painted.
    """

    paint: Paint
    width: float = 1.0
    cap: LineCap = LineCap.BUTT
    join: LineJoin = LineJoin.MITER
    miter_limit: float = 10.0
    dashes: tuple[float, ...] = ()
    dash_offset: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class SoftMask:
    """A soft mask: the transparency group that masks what it encloses."""

    subtype: SoftMaskType


@dataclass(frozen=True, slots=True, kw_only=True)
class Clip:
    """A clipping path in display space."""

    segments: tuple[Segment, ...]
    fill_rule: FillRule
    bbox: Rect


@dataclass(frozen=True, slots=True, kw_only=True)
class TransparencyGroup:
    """A transparency group and what it applies to everything inside it."""

    opacity: float
    blend_mode: BlendMode
    soft_mask: SoftMask | None


@dataclass(frozen=True, slots=True, kw_only=True)
class MarkedContent:
    """A marked-content sequence (``BMC`` or ``BDC``)."""

    tag: str
    mcid: int | None
    """The marked-content identifier of a ``BDC`` property list."""


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphicsContext:
    """The clips, transparency groups, and marked content around an item.

    Each tuple lists its entries outermost first.
    """

    clips: tuple[Clip, ...]
    groups: tuple[TransparencyGroup, ...]
    marked_content: tuple[MarkedContent, ...]

    @property
    def opacity(self) -> float:
        """Return the product of the groups' opacities."""
        return math.prod(group.opacity for group in self.groups)

    @property
    def clip_bbox(self) -> Rect | None:
        """Return the intersection of the clips' boxes, or ``None`` without clips."""
        if not self.clips:
            return None
        x0, y0, x1, y1 = self.clips[0].bbox
        for clip in self.clips[1:]:
            cx0, cy0, cx1, cy1 = clip.bbox
            x0, y0, x1, y1 = max(x0, cx0), max(y0, cy0), min(x1, cx1), min(y1, cy1)
        return Rect(x0, y0, max(x0, x1), max(y0, y1))


@dataclass(frozen=True, slots=True, kw_only=True)
class FontRef:
    """The font that a text run uses, from its PDF font dictionary and font program.

    A text run separately retains the character codes that selected its glyphs
    for :meth:`Canvas.draw`.
    """

    name: str
    """The font's PostScript or PDF ``/BaseFont`` name (subset prefix stripped),
    falling back to ``/Name`` for Type 3 fonts, or ``""`` when absent."""
    is_type3: bool
    weight: int | None
    is_italic: bool
    is_serif: bool
    is_monospace: bool
    xref: int | None = None
    """The object number of the PDF font dictionary, or ``None`` for a direct
    dictionary or a fallback run without a font dictionary."""
    subtype: str = ""
    """The PDF font ``/Subtype`` (``"Type1"``, ``"TrueType"``, ``"Type0"``,
    ``"Type3"``, or ``"MMType1"``), or ``""`` when absent."""
    is_embedded: bool = False
    """Whether the font dictionary (or a ``Type0`` font's descendant) embeds a
    ``FontFile``, ``FontFile2``, or ``FontFile3`` program stream; ``False``
    for non-embedded, Standard 14, and Type 3 fonts."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Glyph:
    """One glyph of a text run."""

    glyph_id: int | None
    """The glyph ID in the font program, or ``None`` for a Type 3 glyph."""
    text: str | None
    """The Unicode text, or ``None`` when the font maps the glyph to none."""
    matrix: Matrix
    """The transform from text space at a font size of 1 to display space;
    ``matrix.e`` and ``matrix.f`` are the origin."""
    advance: float | None
    """The display-space advance, or ``None`` when hayro does not know it,
    as for Type 3 glyphs."""
    bbox: Rect | None
    """The ink box: the exact box of the outline, or of what a Type 3
    procedure paints; ``None`` for a glyph without ink, such as a space."""

    @property
    def origin(self) -> Point:
        """Return the glyph origin on the baseline."""
        return Point(self.matrix.e, self.matrix.f)

    @property
    def size(self) -> float:
        """Return the display-space font size."""
        return math.hypot(self.matrix.c, self.matrix.d)


@dataclass(frozen=True, slots=True, kw_only=True)
class TextRun:
    """The glyphs that one text-showing operator paints."""

    glyphs: tuple[Glyph, ...]
    font: FontRef
    render_mode: TextRenderMode
    fill: Paint | None
    stroke: Stroke | None
    bbox: Rect
    """The union of the glyphs' ink boxes, or of their origins and advances
    when no glyph has ink."""
    context: GraphicsContext
    blend_mode: BlendMode
    soft_mask: SoftMask | None
    writing_mode: WritingMode = WritingMode.HORIZONTAL
    """The writing mode of the run's font: horizontal or vertical."""
    _replay: _RunReplay | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def text(self) -> str:
        """Return the Unicode text of the glyphs that map to one."""
        return "".join(glyph.text for glyph in self.glyphs if glyph.text is not None)

    @property
    def size(self) -> float:
        """Return the display-space font size of the first glyph."""
        return self.glyphs[0].size


@dataclass(frozen=True, slots=True, kw_only=True)
class Path:
    """A path that is filled, stroked, or both."""

    segments: tuple[Segment, ...]
    fill: Paint | None
    fill_rule: FillRule
    stroke: Stroke | None
    bbox: Rect
    """The exact box of the geometry; a stroke extends half its width
    beyond it."""
    context: GraphicsContext
    blend_mode: BlendMode
    soft_mask: SoftMask | None


@dataclass(frozen=True, slots=True, kw_only=True)
class PathCluster:
    """A display-space cluster of nearby vector paths.

    Returned by :meth:`Paths.clusters` and :meth:`Content.path_clusters`.
    :attr:`path_indices` holds the zero-based paint-order positions of the
    clustered paths in the source :class:`Paths` view or :attr:`Content.paths`.
    """

    bbox: Rect
    """The union of the clustered paths' painted boxes in display coordinates."""
    path_indices: tuple[int, ...]
    """Zero-based paint-order indices of the paths belonging to this cluster."""

    @property
    def count(self) -> int:
        """Return the number of paths in this cluster."""
        return len(self.path_indices)


class _Source:
    """The document that read an item, which a pickle leaves out."""

    __slots__ = ("document", "snapshot")

    def __init__(self, document: Document | None, snapshot: _PageSnapshot | None = None) -> None:
        self.document = document
        self.snapshot = snapshot

    def __reduce__(self) -> tuple[type[_Source], tuple[None]]:
        return _Source, (None,)


@dataclass(frozen=True, slots=True)
class _RunReplay:
    source: _Source | None
    reference: tuple[int, int] | None
    width: int
    codes: list[tuple[int, tuple[float, ...]]]
    refusal: str | None
    pen: tuple[float, ...] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Image:
    """An image painted on the page.

    ``xref`` is the object number, including for stencil masks, or zero for
    an inline image. ``encoded_size`` is the byte length reported by the
    interpreter before decoding image filters; for inline images, it can
    include whitespace before ``EI``.
    """

    xref: int
    """The image object number, or 0 for an inline image."""
    width: int
    height: int
    bits: int
    """Bits per component."""
    colorspace: str
    """The color space name, or ``""`` for an image mask."""
    components: int
    is_stencil: bool
    """Whether the image is a stencil mask painted with the fill paint."""
    mask: ImageMask | None
    encoded_size: int
    """The encoded byte count reported by the interpreter."""
    resolution: tuple[int, int]
    """The resolution that a JPEG's JFIF header states, else 96 by 96."""
    matrix: Matrix
    """The transform from the unit square to display space."""
    bbox: Rect
    context: GraphicsContext
    blend_mode: BlendMode
    soft_mask: SoftMask | None
    fill: Paint | None = None
    """The fill paint of a stencil mask, or ``None`` for a raster image."""
    _source: _Source | None = field(default=None, init=False, repr=False, compare=False)
    _inline: _InlineImageSource | None = field(default=None, init=False, repr=False, compare=False)


#: One painted item yielded by `Page.content`: a `TextRun`, `Path`, or `Image`.
ContentItem: TypeAlias = TextRun | Path | Image


class _ContentOrigin:
    """Keep playback provenance outside the public dataclass fields and pickle."""

    __slots__ = ("_columns", "_page")
    _columns: PathColumns
    _page: _RetainedPage


@dataclass(frozen=True, slots=True, kw_only=True)
class Content(_ContentOrigin, Sequence[ContentItem]):
    """The items that a page paints, in paint order."""

    items: tuple[ContentItem, ...]

    def _retained_page(self) -> _RetainedPage:
        page = getattr(self, "_page", None)
        if page is None:
            msg = "replay requires complete, unfiltered content captured from a page"
            raise ValueError(msg)
        page.document._ensure_open()  # noqa: SLF001 - a captured result follows its source lifetime.
        return page

    def pixmap(  # noqa: PLR0913 - the same keyword rendering options as Page.pixmap.
        self,
        *,
        dpi: float | None = None,
        matrix: Sequence[float] | None = None,
        fit: int | tuple[int, int] | None = None,
        clip: Sequence[float] | None = None,
        alpha: bool = False,
        background: tuple[int, int, int] | tuple[int, int, int, int] | None = None,
        colorspace: Colorspace | Literal["gray", "rgb", "cmyk"] = Colorspace.RGB,
        hinting: bool = False,
        glyph_cache: bool = False,
        antialias: bool = True,
    ) -> Pixmap:
        """Render this complete page capture with the options of :meth:`Page.pixmap`.

        Geometry, paints, resources and annotation visibility are those of the
        capture. Each call has its own budget. Filtered, constructed or unpickled
        content raises :class:`ValueError`; a closed source raises :class:`PdfError`.
        """
        page = self._retained_page()
        return page.pixmap(
            dpi=dpi,
            matrix=matrix,
            fit=fit,
            clip=clip,
            alpha=alpha,
            background=background,
            colorspace=colorspace,
            hinting=hinting,
            glyph_cache=glyph_cache,
            antialias=antialias,
            annotations=page.annotations_included,
        )

    @overload
    def __getitem__(self, index: int) -> ContentItem: ...
    @overload
    def __getitem__(self, index: slice) -> tuple[ContentItem, ...]: ...
    def __getitem__(self, index: int | slice) -> ContentItem | tuple[ContentItem, ...]:
        """Return one item, or a tuple of items for a slice."""
        return self.items[index]

    def __len__(self) -> int:
        """Return the number of items."""
        return len(self.items)

    def __iter__(self) -> Iterator[ContentItem]:
        """Iterate over the items in paint order."""
        return iter(self.items)

    @property
    def text_runs(self) -> tuple[TextRun, ...]:
        """Return the text runs in paint order."""
        return tuple(item for item in self.items if isinstance(item, TextRun))

    @property
    def paths(self) -> tuple[Path, ...]:
        """Return the paths in paint order."""
        return tuple(item for item in self.items if isinstance(item, Path))

    @property
    def images(self) -> tuple[Image, ...]:
        """Return the images in paint order."""
        return tuple(item for item in self.items if isinstance(item, Image))

    def path_clusters(
        self,
        *,
        x_tolerance: float = 3.0,
        y_tolerance: float = 3.0,
        exclude_background: bool = True,
        max_clusters: int | None = 4096,
    ) -> list[PathCluster]:
        """Group nearby vector paths into display-space :class:`PathCluster` regions.

        Each path's painted box is widened by half its stroke width (keeping
        hairline strokes of positive length and dropping zero-area fills).
        Paths whose boxes overlap or come within ``x_tolerance`` points
        horizontally and ``y_tolerance`` points vertically are merged
        transitively until no cluster box reaches another; clusters are sorted
        from top to bottom and left to right, and each cluster's
        :attr:`~PathCluster.path_indices` indexes :attr:`paths` in paint order.

        With ``exclude_background=True`` (the default) on page-captured content,
        axis-aligned full-page background rectangles covering at least 95% of
        both the visible page width and height are omitted before clustering.
        More than ``max_clusters`` resulting clusters raise :class:`LimitError`
        with code ``content_element_count`` (``None`` lifts the cap for trusted
        input). Working buffers and execution honor the source document's
        :class:`DocumentLimits`.
        """
        xt, yt = _validate_cluster_options(
            x_tolerance=x_tolerance,
            y_tolerance=y_tolerance,
            exclude_background=exclude_background,
            max_clusters=max_clusters,
        )
        columns = getattr(self, "_columns", None)
        if columns is not None:
            raw = columns.clusters(0, len(columns), xt, yt, exclude_background, max_clusters)
        else:
            boxes = [
                (
                    item.bbox.x0,
                    item.bbox.y0,
                    item.bbox.x1,
                    item.bbox.y1,
                    item.fill is not None,
                    item.stroke is not None,
                    0.0 if item.stroke is None else item.stroke.width,
                )
                for item in self.items
                if isinstance(item, Path)
            ]
            raw = _core.cluster_path_boxes(boxes, xt, yt, exclude_background, max_clusters)
        return [PathCluster(bbox=_rect(bbox), path_indices=tuple(indices)) for bbox, indices in raw]


class Paths(Sequence[Path]):
    """The paths of a page as columns, from :meth:`Page.paths`.

    Each row is one :class:`Path` of :meth:`Page.content`, in paint order,
    and the row view builds that record on demand: ``len(paths)``,
    ``paths[i]``, and iteration give records equal to ``Content.paths``,
    which share :attr:`contexts` and :attr:`patterns`. A slice with a step of
    1 is a view of the same columns.

    :meth:`column` returns each column without a copy as a read-only
    ``memoryview``, which ``numpy.asarray`` reads; its items are in the
    machine's byte order. The columns are those of :attr:`names`, one item
    per row:

    - ``x0``, ``y0``, ``x1``, ``y1`` (float64): ``Path.bbox``;
    - ``segment_offsets`` (int32, one more than the rows): row ``i``'s
      segments are items ``segment_offsets[i]`` to ``segment_offsets[i + 1]``
      of the segment columns;
    - ``has_fill`` (uint8, 0 or 1): whether ``Path.fill`` is not ``None``;
    - ``fill_red``, ``fill_green``, ``fill_blue``, ``fill_alpha`` (float32):
      the fill's :class:`Color`, which hayro computes in float32, so the
      items hold its components exactly; NaN where the fill is a pattern or
      absent;
    - ``fill_pattern`` (int32): the fill's index in :attr:`patterns`, -1
      where it is a color or absent;
    - ``fill_rule`` (uint8): ``Path.fill_rule`` as its position in
      :class:`FillRule`, 0 for nonzero and 1 for even-odd;
    - ``has_stroke``, ``stroke_red``, ``stroke_green``, ``stroke_blue``,
      ``stroke_alpha``, and ``stroke_pattern``: the same for the paint of
      ``Path.stroke``;
    - ``stroke_width``, ``stroke_miter_limit``, and ``stroke_dash_offset``
      (float64), and ``stroke_cap`` and ``stroke_join`` (uint8, positions in
      :class:`LineCap` and :class:`LineJoin`): the fields of the
      :class:`Stroke`, NaN and 0 without one;
    - ``stroke_dashes_offsets`` (int32, one more than the rows): row ``i``'s
      dash lengths are items ``stroke_dashes_offsets[i]`` to
      ``stroke_dashes_offsets[i + 1]`` of ``stroke_dashes`` (float64);
    - ``context`` (uint32): the index of ``Path.context`` in :attr:`contexts`;
    - ``blend_mode`` (uint8): ``Path.blend_mode`` as its position in
      :class:`BlendMode`;
    - ``soft_mask`` (int8): the ``subtype`` of ``Path.soft_mask`` as its
      position in :class:`SoftMaskType`, 0 for alpha and 1 for luminosity, and
      -1 where it is ``None``.

    The segment columns hold one item per segment of every path of the page,
    and a view shares them, as a view of :class:`Chars` shares ``text_data``:

    - ``path`` (uint32): the row of the segment's path;
    - ``verb`` (uint8): the ASCII code of the segment's PDF operator, ``m``
      for :class:`MoveTo`, ``l`` for :class:`LineTo`, ``c`` for
      :class:`CurveTo`, and ``h`` for :class:`ClosePath`;
    - ``to_x``, ``to_y`` (float64): the segment's ``to`` point, and for ``h``
      the start of its subpath, where the closing line ends, so that every
      segment other than ``m`` runs from the previous segment's point to its
      own;
    - ``c1_x``, ``c1_y``, ``c2_x``, ``c2_y`` (float64): a curve's control
      points, NaN for the other verbs.

    The table is also an Arrow struct array through the Arrow PyCapsule
    interface, which pyarrow, polars, and DuckDB import without a copy. Its
    fields are the columns of the rows, with ``has_fill`` and ``has_stroke``
    as Arrow booleans and nulls where the columns hold NaN or -1, and two
    lists in place of the offsets and the columns they index: ``segments``,
    a list of structs of the segment columns whose control points are null
    for verbs other than ``c``, and ``stroke_dashes``, a list of float64,
    null without a stroke.
    """

    __slots__ = ("_columns", "_start", "_stop", "_values")

    #: The names of the columns, in the order of the Arrow fields.
    names: ClassVar[tuple[str, ...]] = (
        "x0",
        "y0",
        "x1",
        "y1",
        "segment_offsets",
        "path",
        "verb",
        "to_x",
        "to_y",
        "c1_x",
        "c1_y",
        "c2_x",
        "c2_y",
        "has_fill",
        "fill_red",
        "fill_green",
        "fill_blue",
        "fill_alpha",
        "fill_pattern",
        "fill_rule",
        "has_stroke",
        "stroke_red",
        "stroke_green",
        "stroke_blue",
        "stroke_alpha",
        "stroke_pattern",
        "stroke_width",
        "stroke_cap",
        "stroke_join",
        "stroke_miter_limit",
        "stroke_dashes_offsets",
        "stroke_dashes",
        "stroke_dash_offset",
        "context",
        "blend_mode",
        "soft_mask",
    )

    _columns: PathColumns
    _start: int
    _stop: int
    _values: _PathValues

    def __new__(cls) -> Self:
        """Refuse direct construction; read paths with :meth:`Page.paths`."""
        msg = "Paths is returned by Page.paths"
        raise TypeError(msg)

    @classmethod
    def _from_columns(
        cls,
        columns: PathColumns,
        start: int = 0,
        stop: int | None = None,
        values: _PathValues | None = None,
    ) -> Paths:
        paths = object.__new__(cls)
        paths._columns = columns
        paths._start = start
        paths._stop = len(columns) if stop is None else stop
        paths._values = _PathValues(columns) if values is None else values
        return paths

    def __len__(self) -> int:
        return self._stop - self._start

    @overload
    def __getitem__(self, index: int) -> Path: ...
    @overload
    def __getitem__(self, index: slice) -> Paths: ...
    def __getitem__(self, index: int | slice) -> Path | Paths:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step != 1:
                msg = f"a slice of Paths takes a step of 1: {index!r}"
                raise ValueError(msg)
            return Paths._from_columns(
                self._columns,
                self._start + start,
                self._start + max(start, stop),
                self._values,
            )
        row = range(self._start, self._stop)[index]
        return self._values.path(self._columns.rows(row, row + 1)[0])

    def __iter__(self) -> Iterator[Path]:
        for start in range(self._start, self._stop, _PATH_ROWS_PER_READ):
            stop = min(start + _PATH_ROWS_PER_READ, self._stop)
            yield from map(self._values.path, self._columns.rows(start, stop))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Paths):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other, strict=True))

    def __hash__(self) -> int:
        return hash(tuple(self))

    def __repr__(self) -> str:
        return f"<Paths {len(self)} rows>"

    @property
    def contexts(self) -> tuple[GraphicsContext, ...]:
        """Return the graphics contexts of the page's interpretation, which the ``context`` column indexes."""
        return self._values.shared()[0]

    @property
    def patterns(self) -> tuple[ShadingPattern | TilingPattern, ...]:
        """Return the distinct pattern paints, which ``fill_pattern`` and ``stroke_pattern`` index."""
        return self._values.shared()[1]

    def column(self, name: str) -> memoryview:
        """Return the column ``name`` of :attr:`names` as a read-only ``memoryview``, without a copy."""
        return memoryview(self._columns.column(name, self._start, self._stop))

    def clusters(
        self,
        *,
        x_tolerance: float = 3.0,
        y_tolerance: float = 3.0,
        exclude_background: bool = True,
        max_clusters: int | None = 4096,
    ) -> list[PathCluster]:
        """Group nearby vector paths in this view into display-space :class:`PathCluster` regions.

        Clustering runs directly over the columnar buffers in Rust without
        materializing Python :class:`Path` objects, releasing the GIL and
        honoring the source document's :class:`DocumentLimits`. Each path's
        painted box is widened by half its stroke width (keeping hairline
        strokes of positive length and dropping zero-area fills). Paths within
        ``x_tolerance`` points horizontally and ``y_tolerance`` points
        vertically are merged transitively until no cluster box reaches
        another; clusters are sorted from top to bottom and left to right, and
        :attr:`~PathCluster.path_indices` holds zero-based indices into this
        :class:`Paths` view.

        With ``exclude_background=True`` (the default), axis-aligned full-page
        background rectangles covering at least 95% of both the visible page
        width and height are omitted before clustering. More than
        ``max_clusters`` resulting clusters raise :class:`LimitError` with code
        ``content_element_count`` (``None`` lifts the cap for trusted input).
        """
        xt, yt = _validate_cluster_options(
            x_tolerance=x_tolerance,
            y_tolerance=y_tolerance,
            exclude_background=exclude_background,
            max_clusters=max_clusters,
        )
        raw = self._columns.clusters(self._start, self._stop, xt, yt, exclude_background, max_clusters)
        return [PathCluster(bbox=_rect(bbox), path_indices=tuple(indices)) for bbox, indices in raw]

    def __arrow_c_schema__(self) -> object:
        """Return the Arrow schema of the rows as a PyCapsule."""
        return self._columns.arrow_schema()

    def __arrow_c_array__(self, requested_schema: object | None = None) -> tuple[object, object]:
        """Return the rows as the PyCapsules of an Arrow schema and struct array.

        The array's buffers are the columns; ``requested_schema`` is ignored,
        as the interface allows.
        """
        del requested_schema
        return self._columns.arrow_array(self._start, self._stop)


#: The rows that a :class:`Paths` iteration reads from the core at once.
_PATH_ROWS_PER_READ = 4096


def _validate_cluster_options(
    *,
    x_tolerance: float,
    y_tolerance: float,
    exclude_background: bool,
    max_clusters: int | None,
) -> tuple[float, float]:
    """Validate keyword options shared by :meth:`Paths.clusters` and :meth:`Content.path_clusters`."""
    for name, value in (("x_tolerance", x_tolerance), ("y_tolerance", y_tolerance)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            msg = f"{name} must be a finite number of points, at least 0: {value!r}"
            raise ValueError(msg)
    if not isinstance(exclude_background, bool):
        msg = f"exclude_background must be a bool: {exclude_background!r}"
        raise TypeError(msg)
    _validate_optional_positive_int("max_clusters", max_clusters)
    return float(x_tolerance), float(y_tolerance)


_BLEND_MODES = tuple(BlendMode)
_SOFT_MASK_TYPES = tuple(SoftMaskType)
_LINE_CAPS = (LineCap.BUTT, LineCap.ROUND, LineCap.SQUARE)
_LINE_JOINS = (LineJoin.MITER, LineJoin.ROUND, LineJoin.BEVEL)
_RENDER_MODES = tuple(TextRenderMode)
_WRITING_MODES = (WritingMode.HORIZONTAL, WritingMode.VERTICAL)
_COLOR_SPACES: tuple[str | None, ...] = (
    None,
    "DeviceGray",
    "DeviceRGB",
    "DeviceCMYK",
    "ICCBased",
    "CalGray",
    "CalRGB",
    "Lab",
    "Indexed",
    "Separation",
    "DeviceN",
    "Pattern",
)
_IMAGE_MASKS = (None, ImageMask.SOFT, ImageMask.EXPLICIT, ImageMask.COLOR_KEY)
_CLOSE_PATH = ClosePath()
_TEXT_RUN = 0
_PATH = 1
_COLOR = 0
_SHADING = 1
_MOVE = ord("m")
_LINE = ord("l")
_CURVE = ord("c")


def _matrix(values: Sequence[float]) -> Matrix:
    """Build a matrix from six finite floats that the core already checked."""
    return Matrix._make(values)


# The core keeps only finite geometry, so points and boxes skip the checks of
# their constructors, which cost more than the rest of a path's record.
_new_tuple = tuple.__new__


def _point(x: float, y: float) -> Point:
    """Build a point from two finite floats that the core already checked."""
    return _new_tuple(Point, (x, y))


def _rect(values: Sequence[float]) -> Rect:
    """Build a rectangle from four finite floats that the core already checked."""
    return _new_tuple(Rect, values)


def _glyph(glyph_id: int | None, text: str | None, matrix: Matrix, advance: float | None, bbox: Rect | None) -> Glyph:
    """Build a :class:`Glyph` through its slots.

    A page can paint hundreds of thousands of glyphs, and the frozen
    dataclass's ``__init__`` sets each field through ``object.__setattr__``.
    """
    glyph = _new_object(Glyph)
    _set_glyph_id(glyph, glyph_id)
    _set_glyph_text(glyph, text)
    _set_glyph_matrix(glyph, matrix)
    _set_glyph_advance(glyph, advance)
    _set_glyph_bbox(glyph, bbox)
    return glyph


_new_object = object.__new__
_set_glyph_id = Glyph.glyph_id.__set__
_set_glyph_text = Glyph.text.__set__
_set_glyph_matrix = Glyph.matrix.__set__
_set_glyph_advance = Glyph.advance.__set__
_set_glyph_bbox = Glyph.bbox.__set__


def _segments(kinds: bytes, coords: list[float]) -> tuple[Segment, ...]:
    segments: list[Segment] = []
    position = 0
    for kind in kinds:
        if kind == _MOVE:
            segments.append(MoveTo(_point(coords[position], coords[position + 1])))
            position += 2
        elif kind == _LINE:
            segments.append(LineTo(_point(coords[position], coords[position + 1])))
            position += 2
        elif kind == _CURVE:
            x1, y1, x2, y2, x3, y3 = coords[position : position + 6]
            segments.append(CurveTo(_point(x1, y1), _point(x2, y2), _point(x3, y3)))
            position += 6
        else:
            segments.append(_CLOSE_PATH)
    return tuple(segments)


def _soft_mask(code: int | None) -> SoftMask | None:
    return None if code is None else SoftMask(subtype=_SOFT_MASK_TYPES[code])


def _pattern(
    kind: int,
    values: Sequence[float],
    columns: PathColumns | None = None,
    index: int | None = None,
) -> ShadingPattern | TilingPattern:
    """Build the pattern paint of one of the core's paint tuples."""
    if kind == _SHADING:
        shading_type = int(values[0])
        pattern = ShadingPattern(
            shading_type=ShadingType(shading_type) if shading_type else None,
            matrix=_matrix(values[1:7]),
            opacity=values[7],
        )
        if columns is not None and index is not None:
            object.__setattr__(pattern, "_source", _ShadingSource(columns, index))
        return pattern
    tiling = TilingPattern(
        bbox=_rect(values[0:4]),
        x_step=values[4],
        y_step=values[5],
        matrix=_matrix(values[6:12]),
        opacity=values[12],
    )
    if columns is not None and index is not None:
        object.__setattr__(tiling, "_source", _TilingSource(columns, index))
    return tiling


class _Converter:
    """Turn the core's tuples into values, sharing equal paints."""

    def __init__(
        self, fonts: list[Any], states: _Contexts, columns: PathColumns, source: _Source | None = None
    ) -> None:
        self._columns = columns
        self.font_source = source
        self.source = _Source(source.document) if source is not None else None
        self.fonts = [
            FontRef(
                name=name,
                is_type3=is_type3,
                weight=weight,
                is_italic=is_italic,
                is_serif=is_serif,
                is_monospace=is_monospace,
                xref=xref,
                subtype=subtype,
                is_embedded=is_embedded,
            )
            for name, is_type3, weight, is_italic, is_serif, is_monospace, xref, subtype, is_embedded in fonts
        ]
        self.states = states
        self.paints: dict[Any, Paint] = {
            (kind, tuple(values)): _pattern(kind, values, columns, index)
            for index, (kind, values, _) in enumerate(columns.patterns())
        }

    def paint(self, raw: tuple[int, list[float], tuple[int, list[float]] | None]) -> Paint:
        kind, values, stored = raw
        if kind == _COLOR:
            space = None if stored is None else _COLOR_SPACES[stored[0]]
            components = None if stored is None else tuple(stored[1])
            key: Any = (kind, tuple(values), space, components)
            paint = self.paints.get(key)
            if paint is None:
                paint = self.paints[key] = Color(*values, colorspace=space, components=components)
            return paint
        key = (kind, tuple(values))
        paint = self.paints.get(key)
        if paint is None:
            paint = self.paints[key] = _pattern(kind, values)
        return paint

    def stroke(self, raw: Any) -> Stroke | None:  # noqa: ANN401 - a nested core tuple.
        if raw is None:
            return None
        paint, width, cap, join, miter_limit, dashes, dash_offset = raw
        return Stroke(
            paint=self.paint(paint),
            width=width,
            cap=_LINE_CAPS[cap],
            join=_LINE_JOINS[join],
            miter_limit=miter_limit,
            dashes=tuple(dashes),
            dash_offset=dash_offset,
        )

    def text_run(self, raw: Any) -> TextRun:  # noqa: ANN401 - a nested core tuple.
        glyphs, font, render_mode, fill, stroke, bbox, (state, blend, soft_mask), replay, writing_mode = raw
        ids, texts, matrices, advances, boxes = glyphs
        item = TextRun(
            glyphs=tuple(
                _glyph(
                    glyph_id,
                    text,
                    _new_tuple(Matrix, matrices[index * 6 : index * 6 + 6]),
                    advance,
                    None if box is None else _new_tuple(Rect, box),
                )
                for index, (glyph_id, text, advance, box) in enumerate(zip(ids, texts, advances, boxes, strict=True))
            ),
            font=self.fonts[font],
            render_mode=_RENDER_MODES[render_mode],
            fill=None if fill is None else self.paint(fill),
            stroke=self.stroke(stroke),
            bbox=_rect(bbox),
            context=self.states[state],
            blend_mode=_BLEND_MODES[blend],
            soft_mask=_soft_mask(soft_mask),
            writing_mode=_WRITING_MODES[writing_mode],
        )

        object.__setattr__(item, "_replay", _RunReplay(self.font_source, *replay))
        return item

    def path(self, raw: Any) -> Path:  # noqa: ANN401 - a nested core tuple.
        kinds, coords, bbox, fill, even_odd, stroke, (state, blend, soft_mask) = raw
        return Path(
            segments=_segments(kinds, coords),
            fill=None if fill is None else self.paint(fill),
            fill_rule=FillRule.EVEN_ODD if even_odd else FillRule.NONZERO,
            stroke=self.stroke(stroke),
            bbox=_rect(bbox),
            context=self.states[state],
            blend_mode=_BLEND_MODES[blend],
            soft_mask=_soft_mask(soft_mask),
        )

    def image(self, raw: Any) -> Image:  # noqa: ANN401 - a nested core tuple.
        (
            xref,
            (width, height, bits, components, colorspace),
            is_stencil,
            mask,
            encoded_size,
            resolution,
            matrix,
            bbox,
            (state, blend, soft_mask),
            fill,
            inline_index,
        ) = raw
        image = Image(
            xref=xref,
            width=width,
            height=height,
            bits=bits,
            colorspace=colorspace,
            components=components,
            is_stencil=is_stencil,
            mask=_IMAGE_MASKS[mask],
            encoded_size=encoded_size,
            resolution=tuple(resolution),
            matrix=_matrix(matrix),
            bbox=_rect(bbox),
            context=self.states[state],
            blend_mode=_BLEND_MODES[blend],
            soft_mask=_soft_mask(soft_mask),
            fill=None if fill is None else self.paint(fill),
        )
        object.__setattr__(image, "_source", self.source)
        if inline_index is not None:
            object.__setattr__(image, "_inline", _InlineImageSource(self._columns, inline_index))
        return image


class _Contexts:
    """The core's graphics contexts, each built when an item first refers to it."""

    def __init__(self, clips: list[Any], groups: list[Any], marks: list[Any], states: list[Any]) -> None:
        self.raw = (clips, groups, marks)
        self.states = states
        self.clips: dict[int, Clip] = {}
        self.groups: dict[int, TransparencyGroup] = {}
        self.marks: dict[int, MarkedContent] = {}
        self.contexts: dict[int, GraphicsContext] = {}

    def clip(self, index: int) -> Clip:
        value = self.clips.get(index)
        if value is None:
            kinds, coords, even_odd, bbox = self.raw[0][index]
            value = self.clips[index] = Clip(
                segments=_segments(kinds, coords),
                fill_rule=FillRule.EVEN_ODD if even_odd else FillRule.NONZERO,
                bbox=_rect(bbox),
            )
        return value

    def group(self, index: int) -> TransparencyGroup:
        value = self.groups.get(index)
        if value is None:
            opacity, blend, soft_mask = self.raw[1][index]
            value = self.groups[index] = TransparencyGroup(
                opacity=opacity,
                blend_mode=_BLEND_MODES[blend],
                soft_mask=_soft_mask(soft_mask),
            )
        return value

    def mark(self, index: int) -> MarkedContent:
        value = self.marks.get(index)
        if value is None:
            tag, mcid = self.raw[2][index]
            value = self.marks[index] = MarkedContent(tag=tag, mcid=mcid)
        return value

    def __getitem__(self, index: int) -> GraphicsContext:
        value = self.contexts.get(index)
        if value is None:
            clip_indexes, group_indexes, mark_indexes = self.states[index]
            value = self.contexts[index] = GraphicsContext(
                clips=tuple(self.clip(clip) for clip in clip_indexes),
                groups=tuple(self.group(group) for group in group_indexes),
                marked_content=tuple(self.mark(mark) for mark in mark_indexes),
            )
        return value


#: What the columns of :class:`Paths` index: contexts, patterns, and stored colors.
_PathShared: TypeAlias = tuple[
    tuple[GraphicsContext, ...],
    tuple[ShadingPattern | TilingPattern, ...],
    tuple[tuple[str | None, tuple[float, ...]], ...],
]


class _PathValues:
    """The values that the path columns of a page index, built once when first asked for.

    The records of the rows share them, and equal colors share one
    :class:`Color`, as the items of a :class:`Content` do; a lock keeps two
    threads from building them twice.
    """

    __slots__ = ("_colors", "_columns", "_lock", "_shared")

    def __init__(self, columns: PathColumns) -> None:
        self._columns = columns
        self._lock = threading.Lock()
        self._shared: _PathShared | None = None
        self._colors: dict[tuple[tuple[float, float, float, float], int], Color] = {}

    def shared(self) -> _PathShared:
        """Return the contexts, patterns, and stored color metadata."""
        shared = self._shared
        if shared is None:
            with self._lock:
                shared = self._shared
                if shared is None:
                    clips, groups, marks, states = self._columns.contexts()
                    contexts = _Contexts(clips, groups, marks, states)
                    shared = self._shared = (
                        tuple(contexts[index] for index in range(len(states))),
                        tuple(
                            _pattern(kind, values, self._columns, index)
                            for index, (kind, values, _) in enumerate(self._columns.patterns())
                        ),
                        tuple(
                            (_COLOR_SPACES[space], tuple(components))
                            for space, components in self._columns.stored_colors()
                        ),
                    )
        return shared

    def paint(self, raw: _PathPaint) -> Paint:
        """Return the paint of a row: a pattern by its index, or else the color."""
        pattern, rgba, stored_index = raw
        if pattern >= 0:
            return self.shared()[1][pattern]
        key = (rgba, stored_index)
        color = self._colors.get(key)
        if color is None:
            if stored_index >= 0:
                colorspace, components = self.shared()[2][stored_index]
            else:
                colorspace, components = None, None
            color = self._colors.setdefault(key, Color(*rgba, colorspace=colorspace, components=components))
        return color

    def stroke(self, raw: _PathStroke) -> Stroke:
        """Return the stroke of a row."""
        paint, width, cap, join, miter_limit, dashes, dash_offset = raw
        return Stroke(
            paint=self.paint(paint),
            width=width,
            cap=_LINE_CAPS[cap],
            join=_LINE_JOINS[join],
            miter_limit=miter_limit,
            dashes=tuple(dashes),
            dash_offset=dash_offset,
        )

    def path(self, row: _PathRow) -> Path:
        """Return the :class:`Path` of one row of the core's columns."""
        kinds, coords, bbox, fill, even_odd, stroke, context, blend, soft_mask = row
        return Path(
            segments=_segments(kinds, coords),
            fill=None if fill is None else self.paint(fill),
            fill_rule=FillRule.EVEN_ODD if even_odd else FillRule.NONZERO,
            stroke=None if stroke is None else self.stroke(stroke),
            bbox=_rect(bbox),
            context=self.shared()[0][context],
            blend_mode=_BLEND_MODES[blend],
            soft_mask=_soft_mask(soft_mask),
        )


def _content_from_core(
    raw: Any,  # noqa: ANN401 - the core returns nested tuples.
    document: Document | None = None,
    snapshot: _PageSnapshot | None = None,
) -> Content:
    """Build a :class:`Content` from ``_core.page_content``; its images name ``document``."""
    items, fonts, clips, groups, marks, states, columns = raw
    source = None if document is None else _Source(document, snapshot)
    converter = _Converter(fonts, _Contexts(clips, groups, marks, states), columns, source)
    converted: list[ContentItem] = []
    for kind, item in items:
        if kind == _TEXT_RUN:
            converted.append(converter.text_run(item))
        elif kind == _PATH:
            converted.append(converter.path(item))
        else:
            converted.append(converter.image(item))
    content = Content(items=tuple(converted))
    object.__setattr__(content, "_columns", columns)
    return content


# The package re-exports these names; reprs, pickles, and documentation name it.
for _public in (
    BlendMode,
    FillRule,
    LineCap,
    LineJoin,
    TextRenderMode,
    WritingMode,
    ShadingType,
    SoftMaskType,
    GradientExtend,
    ImageMask,
    MoveTo,
    LineTo,
    CurveTo,
    ClosePath,
    Color,
    PatternPaint,
    ShadingPattern,
    TilingPattern,
    ColorStop,
    LinearGradient,
    RadialGradient,
    Stroke,
    SoftMask,
    Clip,
    TransparencyGroup,
    MarkedContent,
    GraphicsContext,
    FontRef,
    Glyph,
    TextRun,
    Path,
    PathCluster,
    Image,
    Content,
    Paths,
):
    _public.__module__ = "pyropdf"
