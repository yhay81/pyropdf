"""Select stored tiling and shading patterns in PDF content streams."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyropdf._canvas._path import _number, _numbers
from pyropdf._objects import Array, Dictionary, Name, Stream

if TYPE_CHECKING:
    from pyropdf._content import PatternPaint
    from pyropdf._document import Document


_MAX_DEVICEN_COMPONENTS = 32
_UNCOLORED = 2
_SHADING_PATTERN = 2
_BASE_SPACE_SIZE = 2

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
