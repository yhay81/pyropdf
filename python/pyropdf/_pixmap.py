"""Rendered pages: the ``Pixmap`` value and the options of ``page.pixmap``."""

from __future__ import annotations

import enum
import math
from typing import TYPE_CHECKING

from pyropdf._core import Pixmap

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Points per inch, the resolution of a scale factor of 1.
_POINTS_PER_INCH = 72.0
#: The largest R, G, B, or A component of a background.
_COLOR_MAX = 255
#: A matrix's linear part, or its six entries with a translation.
_MATRIX_LENGTHS = (4, 6)


class Colorspace(enum.StrEnum):
    """The color space of a :class:`Pixmap`'s samples."""

    GRAY = "gray"
    """One gray component per pixel."""
    RGB = "rgb"
    """Red, green, and blue components per pixel."""


def _render_gray(colorspace: Colorspace | str) -> bool:
    """Return whether ``colorspace`` asks for gray samples."""
    try:
        return Colorspace(colorspace) is Colorspace.GRAY
    except ValueError:
        msg = f"colorspace must be 'gray' or 'rgb': {colorspace!r}"
        raise ValueError(msg) from None


def _validate_bool(name: str, value: object) -> None:
    """Refuse a rendering flag that is not a bool."""
    if not isinstance(value, bool):
        msg = f"{name} must be a bool: {value!r}"
        raise TypeError(msg)


def _normalize_background(
    background: tuple[int, int, int] | tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int] | None:
    """Validate a ``background`` and normalize it to an RGBA tuple."""
    if background is None:
        return None
    match background:
        case (r, g, b):
            rgba = (r, g, b, _COLOR_MAX)
        case (r, g, b, a):
            rgba = (r, g, b, a)
        case _:
            msg = f"background must be an (R, G, B) or (R, G, B, A) tuple: {background!r}"
            raise ValueError(msg)
    for value in rgba:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _COLOR_MAX:
            msg = f"each background component must be an integer in 0-{_COLOR_MAX}: {background!r}"
            raise ValueError(msg)
    return rgba


def _render_background(
    background: tuple[int, int, int] | tuple[int, int, int, int] | None,
    *,
    alpha: bool,
) -> tuple[int, int, int, int]:
    """Resolve the straight RGBA beneath the page: white without alpha, transparency with it."""
    _validate_bool("alpha", alpha)
    rgba = _normalize_background(background)
    if rgba is None:
        return (0, 0, 0, 0) if alpha else (_COLOR_MAX, _COLOR_MAX, _COLOR_MAX, _COLOR_MAX)
    if not alpha and rgba[3] != _COLOR_MAX:
        msg = f"alpha=False needs an opaque background: {background!r}"
        raise ValueError(msg)
    return rgba


def _render_matrix(matrix: Sequence[float] | None, dpi: float | None) -> tuple[float, float, float, float]:
    """Return the linear part ``(a, b, c, d)`` of ``dpi`` or of ``matrix``.

    The two are exclusive. The raster covers the bounding box of the
    transformed page, so a translation moves no pixels and is dropped.
    """
    if dpi is not None and matrix is not None:
        msg = "pass dpi or matrix, not both"
        raise ValueError(msg)
    if dpi is not None:
        if isinstance(dpi, bool) or not isinstance(dpi, (int, float)) or not math.isfinite(dpi) or dpi <= 0:
            msg = f"dpi must be a positive finite number: {dpi!r}"
            raise ValueError(msg)
        scale = float(dpi) / _POINTS_PER_INCH
        return (scale, 0.0, 0.0, scale)
    if matrix is None:
        return (1.0, 0.0, 0.0, 1.0)
    if isinstance(matrix, (str, bytes)) or len(matrix) not in _MATRIX_LENGTHS:
        msg = f"matrix must hold 4 or 6 numbers (a, b, c, d[, e, f]): {matrix!r}"
        raise ValueError(msg)
    values = []
    for value in matrix:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            msg = f"matrix must hold finite numbers: {matrix!r}"
            raise ValueError(msg)
        values.append(float(value))
    a, b, c, d = values[:4]
    if a * d - b * c == 0:
        msg = f"matrix must be invertible: {tuple(matrix)!r}"
        raise ValueError(msg)
    return (a, b, c, d)


Pixmap.__module__ = "pyropdf"
Colorspace.__module__ = "pyropdf"

__all__ = ["Colorspace", "Pixmap"]
