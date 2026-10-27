"""The options that the text views share, the records of :meth:`Page.words` and :meth:`Page.layout`."""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from pyropdf._content import _RENDER_MODES, Color, TextRenderMode
from pyropdf._errors import PyropdfWarning
from pyropdf._geometry import Point, Quad, Rect
from pyropdf._limits import _validate_optional_positive_int

if TYPE_CHECKING:
    from collections.abc import Sequence

    #: The core's layout tuples: a block is ``(bbox, lines)``; a line is
    #: ``(bbox, spans, words, direction, writing mode, quad)``; a span is
    #: ``(bbox, (text, chars), size, origin, font, flags, style, quad)``, whose
    #: style is ``(color, render mode, clipped, ascender, descender,
    #: (underlined, struck out, superscript))``; a char is ``(text, bbox,
    #: origin, synthetic)``; a word is ``(bbox, text)``; and a quad is four
    #: points.
    _Box = tuple[float, float, float, float]
    _Pt = tuple[float, float]
    _CoreQuad = tuple[_Pt, _Pt, _Pt, _Pt]
    _CoreStyle = tuple[tuple[float, float, float, float] | None, int, bool, float, float, tuple[bool, bool, bool]]
    _CoreChar = tuple[str, _Box, _Pt, bool]
    _CoreSpan = tuple[_Box, tuple[str, list[_CoreChar]], float, _Pt, str, int, _CoreStyle, _CoreQuad]
    _CoreLine = tuple[_Box, list[_CoreSpan], list[tuple[_Box, str]], _Pt, int, _CoreQuad]
    _CoreBlock = tuple[_Box, list[_CoreLine]]

Unmapped: TypeAlias = Literal["skip", "replacement", "glyph_id"]
Unmapped.__doc__ = """What text extraction makes of a glyph without Unicode: ``"skip"`` leaves it
out with a :class:`PyropdfWarning`, ``"replacement"`` gives U+FFFD, and
``"glyph_id"`` gives the character whose code point is the glyph ID."""

_UNMAPPED_CODES = {"skip": 0, "replacement": 1, "glyph_id": 2}

#: Glyphs that one page's text collects by default (``text_glyph_count``).
_DEFAULT_MAX_TEXT_GLYPHS = 262_144
#: UTF-8 bytes of collected and returned text by default (``text_size``).
_DEFAULT_MAX_TEXT_SIZE = 64 * 1024 * 1024

# The core's text options: (clip, keep clipped glyphs, unmapped code, expand
# ligatures, normalize whitespace, dehyphenate, exact boxes, styles).
_TextOptions: TypeAlias = tuple[tuple[float, float, float, float] | None, bool, int, bool, bool, bool, bool, bool]


def text_options(  # noqa: PLR0913 - the shared options of the text views.
    *,
    clip: tuple[float, float, float, float] | None,
    clipped: bool,
    ligatures: bool,
    whitespace: bool,
    dehyphenate: bool,
    unmapped: Unmapped,
    max_glyphs: int | None,
    max_size: int | None,
    exact_bboxes: bool = False,
    styles: bool = False,
) -> _TextOptions:
    """Validate the options of the text views, with ``clip`` already validated."""
    for name, value in (
        ("clipped", clipped),
        ("ligatures", ligatures),
        ("whitespace", whitespace),
        ("dehyphenate", dehyphenate),
        ("exact_bboxes", exact_bboxes),
        ("styles", styles),
    ):
        if not isinstance(value, bool):
            msg = f"{name} must be True or False: {value!r}"
            raise TypeError(msg)
    code = _UNMAPPED_CODES.get(unmapped) if isinstance(unmapped, str) else None
    if code is None:
        msg = f"unmapped must be 'skip', 'replacement', or 'glyph_id': {unmapped!r}"
        raise ValueError(msg)
    _validate_optional_positive_int("max_glyphs", max_glyphs)
    _validate_optional_positive_int("max_size", max_size)
    return (clip, clipped, code, not ligatures, not whitespace, dehyphenate, exact_bboxes, styles)


def _warn_skipped(skipped: int) -> None:
    """Warn once per call that glyphs without a Unicode mapping were left out."""
    if skipped:
        msg = (
            f"text extraction skipped {skipped} glyphs with no Unicode mapping "
            "(for example a Type 3 font without a ToUnicode CMap); "
            "pass unmapped='replacement' or 'glyph_id' to keep them"
        )
        warnings.warn(msg, PyropdfWarning, stacklevel=3)


@dataclass(frozen=True, slots=True, kw_only=True)
class Word:
    """One word of :meth:`Page.words`, in reading order.

    ``block`` and ``line`` are the indexes of the :class:`Block` of
    :meth:`Page.layout` that holds the word and of the :class:`Line` in it, and
    ``index`` is the word's position in that line.
    """

    text: str
    bbox: Rect
    block: int
    line: int
    index: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Char:
    """One glyph of a :class:`Span`, from :meth:`Page.layout` with ``chars=True``.

    ``text`` is the glyph's complete Unicode mapping, which can hold several
    characters, such as the two of an ``fi`` ligature. ``origin`` is its point
    on the baseline; ``bbox`` spans the glyph's advance and the font's
    ascender and descender, or its outline with ``exact_bboxes=True``.
    """

    text: str
    bbox: Rect
    origin: Point
    is_synthetic: bool = False
    """Whether the char is a space that pyropdf inferred from the gap between
    two glyphs, as ``Span.text`` holds it, rather than a glyph; its box spans
    the gap."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Span:
    """A run of text in one font, size, paint, and render mode within a :class:`Line`.

    ``origin`` is the baseline point of the first glyph. ``chars`` holds the
    glyphs when :meth:`Page.layout` was called with ``chars=True`` and is empty
    otherwise.
    """

    text: str
    bbox: Rect
    origin: Point
    size: float
    font: str
    """The PostScript name of the font program, or ``""`` when hayro hands
    out no program, as for a standard font that is not embedded."""
    is_bold: bool
    is_italic: bool
    is_serif: bool
    is_monospaced: bool
    is_superscript: bool
    """Whether the span is set smaller than the line's largest glyphs and
    raised above their baseline by more than a sixth of their size, as a
    superscript is; only horizontal lines have one."""
    is_underlined: bool
    """Whether a vector rule underlines the glyphs; found only by
    ``layout(styles=True)``, and ``False`` otherwise."""
    is_struck: bool
    """Whether a vector rule strikes the glyphs out; found only by
    ``layout(styles=True)``, and ``False`` otherwise."""
    color: Color | None
    """The paint of the glyphs as hayro's sRGB approximation, whose ``alpha``
    is the opacity: the fill of filled text and the stroke of text that is
    only stroked; ``None`` for a pattern paint and for invisible text."""
    render_mode: TextRenderMode
    """How the glyphs are painted. hayro paints the clipping modes 4 to 6 as
    their painting part, and text in mode 7 is not extracted."""
    is_clipped: bool
    """Whether the clip paths leave no part of the glyphs' boxes visible, so
    that a viewer shows none of them."""
    ascender: float
    """The font's ascender in em, as MuPDF reads it from the font program,
    or 0.8 when hayro hands out no program, as for Type 1 fonts."""
    descender: float
    """The font's descender in em, negative below the baseline, or -0.2
    without a font program."""
    chars: tuple[Char, ...]
    quad: Quad
    """The span's box along its baseline: the smallest rectangle in the
    direction of the first glyph's baseline that holds every glyph's box, so
    that it follows rotated text, where ``bbox`` is axis-aligned."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Line:
    """A line of a :class:`Block`.

    ``text`` joins the line's words with single spaces. ``direction`` is the
    unit vector of the baseline in display space, ``(1, 0)`` for horizontal
    text, and ``is_vertical`` is true for vertical writing, in which glyphs
    stack downward.
    """

    text: str
    bbox: Rect
    direction: Point
    is_vertical: bool
    spans: tuple[Span, ...]
    quad: Quad
    """The line's box along its baseline, as :attr:`Span.quad` is."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Block:
    """A block of lines of a :class:`Layout`; ``text`` joins the lines with newlines."""

    text: str
    bbox: Rect
    lines: tuple[Line, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Layout:
    """The text of a page as blocks, lines, spans, and, on request, characters.

    ``width`` and ``height`` are those of the page's display box; every box
    is in display space.
    """

    width: float
    height: float
    blocks: tuple[Block, ...]


# The span flags that pymupdf reports and that the core computes.
_ITALIC = 2
_SERIF = 4
_MONOSPACED = 8
_BOLD = 16


def _rect(box: Sequence[float]) -> Rect:
    """Return a core box of four floats as a :class:`Rect`.

    A sum of floats is finite only when every term is, so a finite sum skips
    the constructor's checks, which cost more than the rest of a record; a
    non-finite one takes the checked path.
    """
    return _new_tuple(Rect, box) if _isfinite(box[0] + box[1] + box[2] + box[3]) else Rect(*box)


def _point(point: Sequence[float]) -> Point:
    """Return a core point of two floats as a :class:`Point`, as :func:`_rect` does."""
    return _new_tuple(Point, point) if _isfinite(point[0] + point[1]) else Point(*point)


def _char(text: str, bbox: Rect, origin: Point, is_synthetic: bool) -> Char:  # noqa: FBT001 - one core tuple.
    """Return a :class:`Char` for every glyph of a layout with characters.

    The ``__init__`` of a frozen dataclass sets each field through
    ``object.__setattr__``, which costs as much as the glyph's two geometry
    values together; the slot descriptors set the same fields directly.
    """
    char = _new_object(Char)
    _set_char_text(char, text)
    _set_char_bbox(char, bbox)
    _set_char_origin(char, origin)
    _set_char_synthetic(char, is_synthetic)
    return char


_isfinite = math.isfinite
_new_tuple = tuple.__new__
_new_object = object.__new__
_set_char_text = Char.text.__set__
_set_char_bbox = Char.bbox.__set__
_set_char_origin = Char.origin.__set__
_set_char_synthetic = Char.is_synthetic.__set__


def _quad(quad: Sequence[Sequence[float]]) -> Quad:
    """Return a core quad of four points as a :class:`Quad`, as :func:`_rect` does."""
    ul, ur, ll, lr = quad
    if _isfinite(ul[0] + ul[1] + ur[0] + ur[1] + ll[0] + ll[1] + lr[0] + lr[1]):
        return _new_tuple(
            Quad, (_new_tuple(Point, ul), _new_tuple(Point, ur), _new_tuple(Point, ll), _new_tuple(Point, lr))
        )
    return Quad(ul, ur, ll, lr)


def build_words(blocks: Sequence[_CoreBlock]) -> list[Word]:
    """Build :meth:`Page.words` from the core's layout."""
    return [
        Word(text=text, bbox=_rect(bbox), block=block_index, line=line_index, index=index)
        for block_index, (_, lines) in enumerate(blocks)
        for line_index, (_, _, words, _, _, _) in enumerate(lines)
        for index, (bbox, text) in enumerate(words)
    ]


def build_layout(width: float, height: float, blocks: Sequence[_CoreBlock]) -> Layout:
    """Build :meth:`Page.layout` from the core's layout.

    Spans of one color share one :class:`Color`, which is immutable.
    """
    colors: dict[tuple[float, float, float, float], Color] = {}
    return Layout(
        width=width,
        height=height,
        blocks=tuple(_build_block(bbox, [_build_line(line, colors) for line in lines]) for bbox, lines in blocks),
    )


def _build_block(bbox: Sequence[float], lines: list[Line]) -> Block:
    """Build one :class:`Block` around its built lines."""
    return Block(text="\n".join(line.text for line in lines), bbox=_rect(bbox), lines=tuple(lines))


def _build_line(line: _CoreLine, colors: dict[tuple[float, float, float, float], Color]) -> Line:
    """Build one :class:`Line` with its spans."""
    bbox, spans, words, direction, writing_mode, quad = line
    return Line(
        text=" ".join(text for _, text in words),
        bbox=_rect(bbox),
        direction=_point(direction),
        is_vertical=writing_mode == 1,
        spans=tuple(_build_span(*span, colors) for span in spans),
        quad=_quad(quad),
    )


def _build_span(  # noqa: PLR0913, PLR0917 - unpacks one core span tuple.
    bbox: Sequence[float],
    content: tuple[str, list[_CoreChar]],
    size: float,
    origin: Sequence[float],
    font: str,
    flags: int,
    style: _CoreStyle,
    quad: _CoreQuad,
    colors: dict[tuple[float, float, float, float], Color],
) -> Span:
    """Build one :class:`Span` from its text and, when asked for, its glyphs."""
    text, glyphs = content
    chars = tuple(
        _char(char, _rect(char_bbox), _point(char_origin), synthetic)
        for char, char_bbox, char_origin, synthetic in glyphs
    )
    components, render_mode, is_clipped, ascender, descender, (is_underlined, is_struck, is_superscript) = style
    if components is None:
        color = None
    elif (color := colors.get(components)) is None:
        color = colors[components] = Color(*components)
    return Span(
        text=text,
        bbox=_rect(bbox),
        origin=_point(origin),
        size=size,
        font=font,
        is_bold=bool(flags & _BOLD),
        is_italic=bool(flags & _ITALIC),
        is_serif=bool(flags & _SERIF),
        is_monospaced=bool(flags & _MONOSPACED),
        is_superscript=is_superscript,
        is_underlined=is_underlined,
        is_struck=is_struck,
        color=color,
        render_mode=_RENDER_MODES[render_mode],
        is_clipped=is_clipped,
        ascender=ascender,
        descender=descender,
        chars=chars,
        quad=_quad(quad),
    )


__all__ = ["Block", "Char", "Layout", "Line", "Span", "Unmapped", "Word"]
