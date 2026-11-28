"""Select stored tiling and shading patterns and compile captured tiling cells."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyropdf._canvas._path import (
    _BLEND_NAMES,
    _CAPS,
    _JOINS,
    _line_style,
    _number,
    _numbers,
    _Painting,
)
from pyropdf._canvas._resources import _gstate_dict
from pyropdf._content import _BLEND_MODES, _LINE_CAPS, _LINE_JOINS, BlendMode, Color, Stroke, _sampled_to_gradient
from pyropdf._errors import LimitError, PdfError
from pyropdf._geometry import Matrix, Rect
from pyropdf._metadata import _require_version
from pyropdf._objects import Array, Dictionary, Name, Stream
from pyropdf._operations import _real

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyropdf._canvas._canvas import Canvas
    from pyropdf._content import PatternPaint, _TilingCellData
    from pyropdf._document import Document


_MAX_DEVICEN_COMPONENTS = 32
_UNCOLORED = 2
_SHADING_PATTERN = 2
_BASE_SPACE_SIZE = 2
_OP_PUSH_CLIP = 0
_OP_POP_CLIP = 1
_OP_PUSH_GROUP = 2
_OP_POP_GROUP = 3
_OP_PATH = 4
_OP_RASTER_IMAGE = 5
_OP_STENCIL_IMAGE = 6
_PAINT_COLOR = 0
_PAINT_SHADING = 1
_MOVE = ord("m")
_LINE = ord("l")
_CURVE = ord("c")
_SINGULAR_DET = 1e-18
_FLOAT32_MAX = float.fromhex("0x1.fffffep+127")
_RESOURCE_ENTRY_BYTES = 64

_COMPONENTS = {
    "DeviceGray": 1,
    "CalGray": 1,
    "DeviceRGB": 3,
    "CalRGB": 3,
    "Lab": 3,
    "DeviceCMYK": 4,
    "Indexed": 1,
    "Separation": 1,
}


def _components(base: object) -> int:
    """Return the number of operands that the underlying color space takes."""
    family = base[0] if isinstance(base, Array) and len(base) else base
    if (
        isinstance(family, Name)
        and family in _COMPONENTS
        and (isinstance(base, Array) or family in ("DeviceGray", "DeviceRGB", "DeviceCMYK"))
    ):
        return _COMPONENTS[family]
    if isinstance(family, Name) and isinstance(base, Array) and len(base) >= _BASE_SPACE_SIZE:
        detail = base[1]
        if family == "ICCBased" and isinstance(detail, Stream):
            count = detail.get("N")
            if type(count) is int and count in (1, 3, 4):
                return count
        if family == "DeviceN" and isinstance(detail, Array) and 1 <= len(detail) <= _MAX_DEVICEN_COMPONENTS:
            return len(detail)
    msg = "an uncolored pattern needs a base color space with a defined component count"
    raise ValueError(msg)


def _pattern_operands(document: Document, paint: PatternPaint) -> tuple[str, bytes]:
    """Check the pattern and return the resource name of its space and its operands."""
    pattern = paint.pattern
    if not isinstance(pattern, Dictionary) or pattern.xref is None or pattern._document is not document:  # noqa: SLF001
        msg = "pattern must be an indirect pattern in the canvas document"
        raise ValueError(msg)
    raw_kind = pattern.get("PatternType")
    kind = raw_kind if type(raw_kind) is int else None
    paint_type = pattern.get("PaintType")
    if isinstance(pattern, Stream) and kind == 1 and type(paint_type) is int and paint_type in (1, 2):
        uncolored = paint_type == _UNCOLORED
    elif (
        not isinstance(pattern, Stream) and kind == _SHADING_PATTERN and isinstance(pattern.get("Shading"), Dictionary)
    ):
        uncolored = False
    else:
        msg = "pattern must be a tiling pattern stream or a shading pattern dictionary"
        raise ValueError(msg)
    if not uncolored:
        if paint.space is not None or paint.components:
            msg = "a colored tiling or shading pattern takes no base space or components"
            raise ValueError(msg)
        return "Pattern", b""
    space = paint.space
    if not isinstance(space, Array) or space.xref is None or space._document is not document:  # noqa: SLF001
        msg = "an uncolored pattern needs an indirect Pattern color space in the canvas document"
        raise ValueError(msg)
    if len(space) != _BASE_SPACE_SIZE or not isinstance(space[0], Name) or space[0] != "Pattern":
        msg = "an uncolored pattern needs a [/Pattern base] color space"
        raise ValueError(msg)
    count = _components(space[1])
    if len(paint.components) != count:
        msg = f"the pattern base color space requires {count} component values"
        raise ValueError(msg)
    operands = _numbers(*(_number(paint.components[i], "pattern component") for i in range(count)))
    return f"PyroPCS{space.xref}G{space.generation}", operands + b" "


def _raw_path_bytes(kinds: bytes, coords: Sequence[float]) -> tuple[bytes, int, tuple[float, float, float, float]]:
    """Build PDF path operators, point count, and bounding box from raw segment arrays."""
    parts: list[bytes] = []
    pos = 0
    points = 0
    x0 = y0 = math.inf
    x1 = y1 = -math.inf
    for kind in kinds:
        if kind == _MOVE:
            x, y = coords[pos], coords[pos + 1]
            pos += 2
            points += 1
            x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
            parts.append(_numbers(x, y) + b" m")
        elif kind == _LINE:
            x, y = coords[pos], coords[pos + 1]
            pos += 2
            points += 1
            x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
            parts.append(_numbers(x, y) + b" l")
        elif kind == _CURVE:
            c1x, c1y, c2x, c2y, tx, ty = coords[pos : pos + 6]
            pos += 6
            points += 3
            x0 = min(x0, c1x, c2x, tx)
            y0 = min(y0, c1y, c2y, ty)
            x1 = max(x1, c1x, c2x, tx)
            y1 = max(y1, c1y, c2y, ty)
            parts.append(_numbers(c1x, c1y, c2x, c2y, tx, ty) + b" c")
        else:
            parts.append(b"h")
    return b"\n".join(parts), points, (x0, y0, x1, y1)


def _matrix_bbox(matrix: Matrix) -> tuple[float, float, float, float]:
    """Return the bounding box of the unit square under ``matrix``."""
    corners = [
        (matrix.e, matrix.f),
        (matrix.a + matrix.e, matrix.b + matrix.f),
        (matrix.c + matrix.e, matrix.d + matrix.f),
        (matrix.a + matrix.c + matrix.e, matrix.b + matrix.d + matrix.f),
    ]
    xs = [pt[0] for pt in corners]
    ys = [pt[1] for pt in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _valid_area(area: tuple[float, float, float, float]) -> bool:
    """Return whether ``area`` has finite PDF-range coordinates in non-inverted order."""
    x0, y0, x1, y1 = area
    return all(math.isfinite(v) and abs(v) <= _FLOAT32_MAX for v in area) and x0 <= x1 and y0 <= y1


def _shading_area(
    area: tuple[float, float, float, float],
    matrix: Matrix,
    fallback: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Map ``area`` into shading space through ``matrix`` when invertible and in PDF real range."""
    safe_fallback = fallback if _valid_area(fallback) else (0.0, 0.0, 1.0, 1.0)
    if not _valid_area(area):
        area = safe_fallback
    det = matrix.a * matrix.d - matrix.b * matrix.c
    if not math.isfinite(det) or abs(det) <= _SINGULAR_DET:
        return safe_fallback
    try:
        x0, y0, x1, y1 = Rect(*area) / matrix
    except (ZeroDivisionError, PdfError, OverflowError, ValueError):
        return safe_fallback
    mapped = (x0, y0, x1, y1)
    if not _valid_area(mapped):
        return safe_fallback
    return mapped


@dataclass(slots=True)
class _TilingLayer:
    """One transparency group layer inside a compiled tiling pattern cell."""

    parts: list[bytes] = field(default_factory=list)
    opacity: float = 1.0
    blend_name: str | None = None
    has_non_normal_blend: bool = False
    xobjects: set[str] = field(default_factory=set)
    gstates: set[tuple[float | None, float | None, str | None]] = field(default_factory=set)
    patterns: set[str] = field(default_factory=set)
    masks: set[int] = field(default_factory=set)


class _TilingBuilder:
    """Compiles a captured tiling pattern cell into PDF stream bytes and resources."""

    __slots__ = ("_bbox", "_bytes", "_canvas", "_gstates", "_layers", "_limit", "_masks", "_patterns", "_xobjects")

    def __init__(self, canvas: Canvas, bbox: tuple[float, float, float, float], limit: int) -> None:
        self._canvas = canvas
        self._bbox = bbox
        self._limit = limit
        self._bytes = 0
        self._xobjects: dict[str, int] = {}
        self._gstates: dict[tuple[float | None, float | None, str | None], str] = {}
        self._patterns: dict[str, int] = {}
        self._masks: dict[int, str] = {}
        self._layers: list[_TilingLayer] = [_TilingLayer()]

    def _emit(self, data: bytes) -> None:
        size = len(data) + 1
        if self._bytes + size > self._limit:
            code = "content_input_size"
            msg = f"the tiling pattern cell holds over {self._limit} bytes of content"
            raise LimitError(code, msg)
        self._canvas._charge(size)  # noqa: SLF001 - charge cell assembly to active canvas operation.
        self._bytes += size
        self._layers[-1].parts.append(data)

    def _gstate(
        self, *, fill_alpha: float | None = None, stroke_alpha: float | None = None, blend: str | None = None
    ) -> str:
        if fill_alpha is not None or stroke_alpha is not None or blend is not None:
            document = self._canvas._page.document  # noqa: SLF001
            _require_version(document, document.catalog, (1, 4))
        layer = self._layers[-1]
        if blend is not None:
            layer.has_non_normal_blend = True
        key = (fill_alpha, stroke_alpha, blend)
        name = self._gstates.get(key)
        if name is None:
            name = f"PyroTG{len(self._gstates) + 1}"
            self._gstates[key] = name
        layer.gstates.add(key)
        return name

    def _xobject(self, xref: int) -> str:
        for name, known in self._xobjects.items():
            if known == xref:
                self._layers[-1].xobjects.add(name)
                return name
        name = f"PyroTX{len(self._xobjects) + 1}"
        self._xobjects[name] = xref
        self._layers[-1].xobjects.add(name)
        return name

    def _pattern(self, xref: int) -> str:
        for name, known in self._patterns.items():
            if known == xref:
                self._layers[-1].patterns.add(name)
                return name
        name = f"PyroTP{len(self._patterns) + 1}"
        self._patterns[name] = xref
        self._layers[-1].patterns.add(name)
        return name

    def _mask(self, xref: int) -> str:
        name = self._masks.get(xref)
        if name is None:
            name = self._masks[xref] = f"PyroTM{len(self._masks) + 1}"
        self._layers[-1].masks.add(xref)
        return name

    def _resources_for(self, layer: _TilingLayer) -> dict[str, dict[str, object]]:
        objects = self._canvas._page.document.objects  # noqa: SLF001
        resources: dict[str, dict[str, object]] = {}
        if layer.xobjects:
            resources["XObject"] = {
                name: objects[xref] for name, xref in self._xobjects.items() if name in layer.xobjects
            }
        if layer.gstates or layer.masks:
            resources["ExtGState"] = {
                **{name: _gstate_dict(key) for key, name in self._gstates.items() if key in layer.gstates},
                **{name: objects[xref] for xref, name in self._masks.items() if xref in layer.masks},
            }
        if layer.patterns:
            resources["Pattern"] = {
                name: objects[xref] for name, xref in self._patterns.items() if name in layer.patterns
            }
        return resources

    def _paint(
        self,
        raw_paint: Any,  # noqa: ANN401 - nested core cell paint tuple.
        area: tuple[float, float, float, float],
        *,
        is_stroke: bool,
    ) -> _Painting:
        kind = raw_paint[0]
        if kind == _PAINT_COLOR:
            r, g, b, a = raw_paint[1]
            alpha = min(max(a, 0.0), 1.0)
            operator = b" RG" if is_stroke else b" rg"
            return _Painting((_numbers(r, g, b) + operator,), None if alpha >= 1 else alpha, None)
        if kind == _PAINT_SHADING:
            sampled, matrix_coeffs = raw_paint[1], raw_paint[2]
            gradient, bbox, background = _sampled_to_gradient(sampled)
            matrix = Matrix(*matrix_coeffs)
            shaded_area = _shading_area(area, matrix, self._bbox)
            shading, mask, alpha = self._canvas._gradient(  # noqa: SLF001
                gradient,
                shaded_area,
                bbox,
                background,
                sampled=True,
                mask_matrix=(matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f),
            )
            pattern_xref = self._canvas._write_pattern(shading, matrix)  # noqa: SLF001
            pname = self._pattern(pattern_xref).encode("ascii")
            space, operator = (b"/Pattern CS", b" SCN") if is_stroke else (b"/Pattern cs", b" scn")
            masked = None if mask is None else self._mask(mask)
            return _Painting((space, b"/" + pname + operator), None if alpha >= 1 else alpha, masked)
        nested_cell, matrix_coeffs, raw_opacity = raw_paint[1], raw_paint[2], raw_paint[3]
        opacity = min(max(raw_opacity, 0.0), 1.0)
        nested_xref = _write_tiling_cell_object(self._canvas, nested_cell, Matrix(*matrix_coeffs), self._limit)
        pname = self._pattern(nested_xref).encode("ascii")
        space, operator = (b"/Pattern CS", b" SCN") if is_stroke else (b"/Pattern cs", b" scn")
        return _Painting((space, b"/" + pname + operator), None if opacity >= 1 else opacity, None)

    def _emit_path(
        self,
        construction: bytes,
        paints: tuple[_Painting | None, _Painting | None, Stroke | None],
        *,
        even_odd: bool,
        blend_name: str | None,
    ) -> None:
        filling, stroking, stroke = paints
        parts = [b"q"]
        fill_alpha = None if filling is None else filling.alpha
        stroke_alpha = None if stroking is None else stroking.alpha
        if fill_alpha is not None or stroke_alpha is not None or blend_name is not None:
            gname = self._gstate(fill_alpha=fill_alpha, stroke_alpha=stroke_alpha, blend=blend_name)
            parts.append(b"/" + gname.encode("ascii") + b" gs")
        for painting in (filling, stroking):
            if painting is not None and painting.mask is not None:
                parts.append(b"/" + painting.mask.encode("ascii") + b" gs")
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
        self._emit(b"\n".join(parts))

    def _op_path(self, op: Any) -> None:  # noqa: ANN401 - core op tuple.
        _, kinds, coords, raw_fill, even_odd, raw_stroke, blend_code = op
        construction, points, box = _raw_path_bytes(kinds, coords)
        self._canvas._count(points)  # noqa: SLF001
        blend = _BLEND_MODES[blend_code]
        blend_name = None if blend is BlendMode.NORMAL else _BLEND_NAMES[blend]
        filling = None
        if raw_fill is not None:
            padded = (box[0] - 1.0, box[1] - 1.0, box[2] + 1.0, box[3] + 1.0)
            filling = self._paint(raw_fill, padded, is_stroke=False)
        stroking = None
        stroke_obj: Stroke | None = None
        if raw_stroke is not None:
            spaint, width, cap, join, miter_limit, dashes, dash_offset = raw_stroke
            pad = max(width, 1.0) / 2.0 * max(miter_limit, math.sqrt(2)) + 1.0
            padded = (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
            stroking = self._paint(spaint, padded, is_stroke=True)
            stroke_obj = Stroke(
                paint=Color(0, 0, 0),
                width=width,
                cap=_LINE_CAPS[cap],
                join=_LINE_JOINS[join],
                miter_limit=miter_limit,
                dashes=tuple(dashes),
                dash_offset=dash_offset,
            )
            _ = (_CAPS[stroke_obj.cap], _JOINS[stroke_obj.join])
        if filling is None and stroking is None:
            return
        if filling is not None and stroking is not None and (filling.mask is not None or stroking.mask is not None):
            self._emit_path(construction, (filling, None, None), even_odd=even_odd, blend_name=blend_name)
            self._emit_path(construction, (None, stroking, stroke_obj), even_odd=even_odd, blend_name=blend_name)
        else:
            self._emit_path(construction, (filling, stroking, stroke_obj), even_odd=even_odd, blend_name=blend_name)

    def _op_push_clip(self, op: Any) -> None:  # noqa: ANN401 - core op tuple.
        _, kinds, coords, even_odd = op
        if kinds:
            construction, points, _ = _raw_path_bytes(kinds, coords)
            self._canvas._count(points)  # noqa: SLF001
            self._emit(b"q\n" + construction + (b"\nW* n" if even_odd else b"\nW n"))
        else:
            self._emit(b"q\n0 0 0 0 re W n")

    def _op_raster_image(self, op: Any) -> None:  # noqa: ANN401 - core op tuple.
        _, width, height, gray, color, alpha, interpolate, matrix_coeffs, blend_code = op
        xref = self._canvas._write_captured_raster(  # noqa: SLF001
            (width, height, gray, color, alpha, interpolate)
        )
        xname = self._xobject(xref).encode("ascii")
        pdf_matrix = tuple(Matrix(1, 0, 0, -1, 0, 1) * Matrix(*matrix_coeffs))
        blend = _BLEND_MODES[blend_code]
        parts = [b"q"]
        if blend is not BlendMode.NORMAL:
            gname = self._gstate(blend=_BLEND_NAMES[blend]).encode("ascii")
            parts.append(b"/" + gname + b" gs")
        parts.append(b" ".join(map(_real, pdf_matrix)) + b" cm")
        parts.append(b"/" + xname + b" Do")
        parts.append(b"Q")
        self._emit(b"\n".join(parts))

    def _op_stencil_image(self, op: Any) -> None:  # noqa: ANN401 - core op tuple.
        _, width, height, mask_bits, interpolate, raw_fill, matrix_coeffs, blend_code = op
        xref = self._canvas._write_captured_stencil(width, height, mask_bits, interpolate)  # noqa: SLF001
        xname = self._xobject(xref).encode("ascii")
        area = _matrix_bbox(Matrix(*matrix_coeffs))
        painting = self._paint(raw_fill, area, is_stroke=False)
        pdf_matrix = tuple(Matrix(1, 0, 0, -1, 0, 1) * Matrix(*matrix_coeffs))
        blend = _BLEND_MODES[blend_code]
        blend_name = None if blend is BlendMode.NORMAL else _BLEND_NAMES[blend]
        parts = [b"q"]
        if painting.alpha is not None or blend_name is not None:
            gname = self._gstate(fill_alpha=painting.alpha, blend=blend_name).encode("ascii")
            parts.append(b"/" + gname + b" gs")
        if painting.mask is not None:
            parts.append(b"/" + painting.mask.encode("ascii") + b" gs")
        parts.extend(painting.operators)
        parts.append(b" ".join(map(_real, pdf_matrix)) + b" cm")
        parts.append(b"/" + xname + b" Do")
        parts.append(b"Q")
        self._emit(b"\n".join(parts))

    def _op_pop_group(self) -> None:
        if len(self._layers) <= 1:
            return
        layer = self._layers.pop()
        if not layer.parts:
            return
        opacity = min(max(layer.opacity, 0.0), 1.0)
        if opacity >= 1.0 and layer.blend_name is None and not layer.has_non_normal_blend:
            parent = self._layers[-1]
            parent.xobjects.update(layer.xobjects)
            parent.gstates.update(layer.gstates)
            parent.patterns.update(layer.patterns)
            parent.masks.update(layer.masks)
            self._emit(b"q\n" + b"\n".join(layer.parts) + b"\nQ")
            return
        document = self._canvas._page.document  # noqa: SLF001
        _require_version(document, document.catalog, (1, 4))
        body = b"\n".join(layer.parts)
        resources = self._resources_for(layer)
        res_entries = sum(len(table) for table in resources.values())
        with self._canvas._scratch(len(body) + 128 + res_entries * _RESOURCE_ENTRY_BYTES):  # noqa: SLF001
            form = document.objects.add_stream(
                body,
                {
                    "Type": Name("XObject"),
                    "Subtype": Name("Form"),
                    "BBox": list(self._bbox),
                    "Group": {"Type": Name("Group"), "S": Name("Transparency"), "I": True},
                    "Resources": resources,
                },
            )
        fname = self._xobject(form).encode("ascii")
        gname = self._gstate(
            fill_alpha=None if opacity >= 1.0 else opacity,
            stroke_alpha=None if opacity >= 1.0 else opacity,
            blend=layer.blend_name,
        ).encode("ascii")
        self._emit(b"q\n/" + gname + b" gs\n/" + fname + b" Do\nQ")

    def _op_push_group(self, op: Any) -> None:  # noqa: ANN401 - core op tuple.
        _, opacity, blend_code = op
        blend = _BLEND_MODES[blend_code]
        blend_name = None if blend is BlendMode.NORMAL else _BLEND_NAMES[blend]
        self._layers.append(_TilingLayer(opacity=min(max(opacity, 0.0), 1.0), blend_name=blend_name))

    def _apply_op(self, op: Any) -> None:  # noqa: ANN401 - core op tuple.
        code = op[0]
        if code == _OP_PUSH_CLIP:
            self._op_push_clip(op)
        elif code == _OP_POP_CLIP:
            self._emit(b"Q")
        elif code == _OP_PUSH_GROUP:
            self._op_push_group(op)
        elif code == _OP_POP_GROUP:
            self._op_pop_group()
        elif code == _OP_PATH:
            self._op_path(op)
        elif code == _OP_RASTER_IMAGE:
            self._op_raster_image(op)
        elif code == _OP_STENCIL_IMAGE:
            self._op_stencil_image(op)

    def build(self, ops: list[Any]) -> tuple[bytes, dict[str, dict[str, object]]]:
        try:
            for index, op in enumerate(ops):
                if index % 128 == 0:
                    self._canvas._active.poll()  # noqa: SLF001
                self._apply_op(op)
            while len(self._layers) > 1:
                self._op_pop_group()
            root = self._layers[0]
            stream_bytes = b"\n".join(root.parts)
            resources = self._resources_for(root)
        finally:
            self._canvas._release(self._bytes)  # noqa: SLF001
            self._bytes = 0
        return stream_bytes, resources


@dataclass(frozen=True, slots=True)
class _CompiledTilingCell:
    """A compiled tiling pattern stream and its pattern-space geometry."""

    stream_bytes: bytes
    resources: dict[str, dict[str, object]]
    bbox: tuple[float, float, float, float]
    x_step: float
    y_step: float


def _compile_tiling_cell(canvas: Canvas, cell: _TilingCellData, limit: int) -> _CompiledTilingCell:
    """Validate and compile a captured tiling cell into stream bytes and resources."""
    bbox, x_step, y_step, refusal, ops = cell
    if refusal is not None:
        raise ValueError(refusal)
    builder = _TilingBuilder(canvas, bbox, limit)
    stream_bytes, resources = builder.build(ops)
    return _CompiledTilingCell(stream_bytes, resources, bbox, x_step, y_step)


def _write_tiling_stream(canvas: Canvas, compiled: _CompiledTilingCell, matrix: Matrix) -> int:
    """Write one PDF ``/PatternType 1`` stream object in the canvas document."""
    document = canvas._page.document  # noqa: SLF001
    res_entries = sum(len(table) for table in compiled.resources.values())
    with canvas._scratch(len(compiled.stream_bytes) + 256 + res_entries * _RESOURCE_ENTRY_BYTES):  # noqa: SLF001
        return document.objects.add_stream(
            compiled.stream_bytes,
            {
                "Type": Name("Pattern"),
                "PatternType": 1,
                "PaintType": 1,
                "TilingType": 1,
                "BBox": list(compiled.bbox),
                "XStep": compiled.x_step,
                "YStep": compiled.y_step,
                "Matrix": [matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f],
                "Resources": compiled.resources,
            },
        )


def _write_tiling_cell_object(canvas: Canvas, cell: _TilingCellData, matrix: Matrix, limit: int) -> int:
    """Compile and write a nested tiling cell directly into the canvas document."""
    return _write_tiling_stream(canvas, _compile_tiling_cell(canvas, cell, limit), matrix)
