"""Rendered pages: the ``Pixmap`` value and the options of ``page.pixmap``."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pyropdf._core import Pixmap

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Points per inch, the resolution of a scale factor of 1.
_POINTS_PER_INCH = 72.0
#: A matrix's linear part, or its six entries with a translation.
_MATRIX_LENGTHS = (4, 6)


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

__all__ = ["Pixmap"]
