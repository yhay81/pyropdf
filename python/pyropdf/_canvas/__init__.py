"""Canvas: drawing paths, images, text, and other pages on a page, in paint order."""

from __future__ import annotations

from pyropdf._canvas._canvas import Canvas
from pyropdf._canvas._path import _MAX_POINTS as _MAX_POINTS
from pyropdf._canvas._path import PathBuilder
from pyropdf._canvas._resources import _cache as _cache
from pyropdf._canvas._resources import _Images as _Images
from pyropdf._canvas._tags import ListNumbering, TableHeaderScope, TagRole
from pyropdf._canvas._text import TextFit
from pyropdf._canvas._text import _line_count as _line_count
from pyropdf._fonts import _max_lines as _max_lines
from pyropdf._operations import _DEFAULT_MAX_CONTENT_SIZE as _DEFAULT_MAX_CONTENT_SIZE

__all__ = [
    "Canvas",
    "ListNumbering",
    "PathBuilder",
    "TableHeaderScope",
    "TagRole",
    "TextFit",
]
