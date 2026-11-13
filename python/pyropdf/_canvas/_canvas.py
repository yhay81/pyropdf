"""The Canvas context manager and content-stream coordinator."""

from __future__ import annotations

import hashlib
import math
import os
import sys
from collections.abc import Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, cast

from pyropdf import _core
from pyropdf._canvas._path import (
    _BLACK,
    _BLEND_NAMES,
    _IDENTITY,
    _MAX_POINTS,
    _STOP_SIZE,
    _TURNS,
    PathBuilder,
    _affine,
    _bounds,
    _display_from_user,
    _fmt,
    _line_style,
    _number,
    _numbers,
    _paint,
    _Painting,
    _path_bytes,
    _placement,
    _point,
    _quarter_turn,
    _rect,
    _segments,
    _stops,
    _stroke,
    _unit,
    _user_from_display,
)
from pyropdf._canvas._resources import (
    _cache,
    _contents,
    _isolated,
    _merge_resources,
    _page_space,
    _resources_dict,
    _still_image,
)
from pyropdf._canvas._tags import _Scope, _tag_scope, _write_structure
from pyropdf._canvas._text import (
    _canvas_font,
    _GlyphRun,
    _layout_textbox,
    _line_count,
    _line_height,
    _lines_size,
    _shown_winansi,
    _size,
    _text_paint,
)
from pyropdf._content import (
    BlendMode,
    Color,
    FillRule,
    Gradient,
    GradientExtend,
    Image,
    LinearGradient,
    MoveTo,
    Path,
    RadialGradient,
    ShadingPattern,
    Stroke,
)
from pyropdf._errors import LimitError, PdfError, StaleObjectError
from pyropdf._fonts import (
    _DEFAULT_MAX_GENERATED_TEXT_SIZE,
    _NO_TEXT,
    _max_lines,
    _text_input,
    add_glyphs,
    embedded_font,
)
from pyropdf._geometry import Matrix, Point, Rect
from pyropdf._images import PageImage, _image_stream, add_image
from pyropdf._layers import Layer, LayerMembership
from pyropdf._objects import Dictionary, Name
from pyropdf._operations import _DEFAULT_MAX_CONTENT_SIZE, _real

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from contextlib import AbstractContextManager
    from types import TracebackType
    from typing import Self, TypeAlias

    from pyropdf._canvas._path import PathInput
    from pyropdf._canvas._tags import ListNumbering, TableHeaderScope, TagRole, _Element
    from pyropdf._canvas._text import TextFit
    from pyropdf._content import Segment
    from pyropdf._core import Pixmap
    from pyropdf._document import Document
    from pyropdf._fonts import Font
    from pyropdf._page import Page

    # How text draws a glyph in a Type 3 font: its name, advance, and strike, from ``_Font.colour_glyphs``.
    _ColourGlyph: TypeAlias = tuple[str, float, int | None]

#: The deepest nesting of clip, transform, opacity, and blend scopes.
_MAX_SCOPES = 16


def _max_points() -> int:
    return cast("int", getattr(sys.modules.get("pyropdf._canvas"), "_MAX_POINTS", _MAX_POINTS))


def _max_content_size() -> int:
    return cast(
        "int", getattr(sys.modules.get("pyropdf._canvas"), "_DEFAULT_MAX_CONTENT_SIZE", _DEFAULT_MAX_CONTENT_SIZE)
    )


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
    text in a Standard 14 or embedded OpenType :class:`Font`.

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
        limit = _max_content_size()
        if self._buffered + size > limit:
            code = "content_input_size"
            msg = f"the canvas holds over {limit} bytes of content"
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
        limit = _max_points()
        if self._points > limit:
            code = "content_element_count"
            msg = f"the canvas holds more than {limit} path points"
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

    def _resources_value(self) -> dict[str, dict[str, object]]:
        return _resources_dict(
            self._page.document.objects,
            self._xobjects,
            self._gstates,
            self._fonts,
            self._properties,
            self._patterns,
            self._masks,
        )

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
        limit = _max_points()
        if count > limit:
            code = "content_element_count"
            msg = f"the gradient has {count} stops, over the limit of {limit}"
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

    def _write_page_patterns(self, matrix: Matrix) -> None:
        """Write the patterns of the page's content, which reach its default space through its opening ``matrix``."""
        for (shading, inner, in_page), name in self._pattern_names.items():
            if in_page:
                self._patterns[name] = self._write_pattern(shading, inner * matrix)

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
        rotation, box, unit = _page_space(page)
        matrix = _user_from_display(rotation, box, unit)
        self._write_page_patterns(Matrix(*matrix))
        opening = b"q\n" + b" ".join(map(_real, matrix)) + b" cm\n"
        size = len(opening) + self._buffered + 2
        limit = _max_content_size()
        # Check the size and charge the joined content and its framed copy before making them.
        if size > limit:
            code = "content_input_size"
            msg = f"the canvas writes {size} bytes of content, over the {limit}-byte limit"
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
            objects[form]["StructParents"] = _write_structure(page, self._elements, self._mcids, form)
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
            opening_stream = objects[objects.add_stream(b"q\n", filter=None)]
            closing_stream = objects[objects.add_stream(b"\nQ\n", filter=None)]
            streams = [opening_stream, *existing, closing_stream, drawn]
        else:
            streams = [*existing, drawn]
        page.obj["Contents"] = streams

    # Paths.

    def path(
        self,
        path: PathInput,
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
            fit, layout = _layout_textbox(
                box,
                text,
                font=font,
                size=size,
                align=align,
                line_height=line_height,
                paint=(fill, stroke),
                rotation=rotation,
                max_text_size=max_text_size,
                operation=self._operation,
            )
            if layout is not None:
                face, font_size, paint, placed = layout
                self._emit_text(face, font_size, paint, rotation, placed)
            return fit

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

    # Images and pages.

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

        def read() -> tuple[int, tuple[float, float, float, float], float, Rect, Rect, int]:
            page = _core.canvas_source_page(source_core, source.xref, source._generation, operation)  # noqa: SLF001
            if page is None:
                raise source._stale()  # noqa: SLF001
            source_rotation, source_box, source_unit, width, height, _content, snapshot = page
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
            return source_rotation, source_box, source_unit, whole, shown, form

        try:
            source_rotation, source_box, source_unit, whole, shown, form = read()
        except BaseException:
            _core.abort_canvas(core, operation)
            raise
        _core.end_transaction(core, True)  # noqa: FBT003 - the core's flag
        # Only the part of the clip on the page is drawn.
        shown &= whole
        covered, place = _placement(box, shown.width, shown.height, keep=bool(keep_aspect), rotation=rotation)
        to_display = _display_from_user(source_rotation, source_box, source_unit)

        def convert(x: float, y: float) -> tuple[float, float]:
            dx, dy = to_display(x, y)
            return place(dx - shown.x0, dy - shown.y0)

        matrix = _affine(convert)
        name = self._add_xobject(form)
        self._emit(
            b"q\n"
            + _numbers(covered.x0, covered.y0, covered.width, covered.height)
            + b" re W n\n"
            + b" ".join(map(_real, matrix))
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
        images, stencil image masks, and text runs cannot be drawn; an item
        that cannot be drawn raises ``TypeError`` or ``ValueError`` and draws
        nothing.
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

    def clip(self, path: PathInput, *, fill_rule: FillRule = FillRule.NONZERO) -> AbstractContextManager[None]:
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
        return _tag_scope(
            self,
            role,
            alt=alt,
            lang=lang,
            title=title,
            actual_text=actual_text,
            scope=scope,
            numbering=numbering,
        )

    def __repr__(self) -> str:
        """Return the page and whether the canvas is still open."""
        state = "closed" if self._closed else "open"
        return f"<Canvas on {self._page!r}, {state}>"


Canvas.__module__ = "pyropdf"
