"""The options that the text views share, the records of :meth:`Page.words` and :meth:`Page.layout`."""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeAlias, overload

from pyropdf._content import _RENDER_MODES, Color, TextRenderMode
from pyropdf._errors import PyropdfWarning
from pyropdf._geometry import Point, Quad, Rect
from pyropdf._limits import _validate_optional_positive_int

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyropdf._core import CharColumns

    #: The core's layout tuples: a block is ``(bbox, lines)``; a line is
    #: ``(bbox, spans, words, direction, writing mode, quad)``; a span is
    #: ``(bbox, (text, chars), size, origin, font, flags, style, quad)``, whose
    #: style is ``(color, render mode, clipped, ascender, descender,
    #: (underlined, struck out, superscript))``; a char is ``(text, bbox,
    #: origin, synthetic, (stream, operation) or None)``; a word is ``(bbox,
    #: text)``; and a quad is four points.
    _Box = tuple[float, float, float, float]
    _Pt = tuple[float, float]
    _CoreQuad = tuple[_Pt, _Pt, _Pt, _Pt]
    _CoreStyle = tuple[tuple[float, float, float, float] | None, int, bool, float, float, tuple[bool, bool, bool]]
    _CoreChar = tuple[str, _Box, _Pt, bool, tuple[int, int] | None]
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
    stream: int | None = None
    """The object number of the content stream that holds the operator that
    showed the glyph, or ``None``, as for ``operation``."""
    operation: int | None = None
    """The index in :attr:`Page.operations` of the operation that showed the
    glyph, or ``None`` for a synthetic char and for a glyph that a Form
    XObject or an annotation's appearance shows. It is also ``None`` for
    every char of a page whose content :attr:`Page.operations` cannot read,
    or that pyropdf cannot match to hayro's interpretation."""


class Chars(Sequence[Char]):
    """The chars of a page as columns, from :meth:`Page.chars` and :attr:`Layout.chars`.

    Each row is one :class:`Char` of the layout with chars, in layout order,
    and the row view builds that record on demand: ``len(chars)``,
    ``chars[i]``, and iteration give records equal to the spans' ``chars``.
    A slice with a step of 1 is a view of the same columns.

    :meth:`column` returns each column without a copy as a read-only
    ``memoryview``, which ``numpy.frombuffer`` reads; its items are in the
    machine's byte order. The columns are those of :attr:`names`:

    - ``x0``, ``y0``, ``x1``, ``y1`` (float64): ``Char.bbox``;
    - ``origin_x``, ``origin_y`` (float64): ``Char.origin``;
    - ``text_offsets`` (int32, one more than the rows) and ``text_data``
      (bytes): the UTF-8 of every ``Char.text``, which row ``i`` holds from
      ``text_offsets[i]`` to ``text_offsets[i + 1]`` of ``text_data``, as
      Arrow's ``utf8`` holds it. A view shares the ``text_data`` of the
      whole page;
    - ``block``, ``line``, ``span`` (uint32): the row's place in
      ``Layout.blocks``, its block's ``lines``, and its line's ``spans``;
    - ``is_synthetic`` (uint8, 0 or 1): ``Char.is_synthetic``;
    - ``stream``, ``operation`` (uint32): ``Char.stream`` and
      ``Char.operation``, both 0 where they are ``None``. No object has
      the number 0, so ``stream == 0`` marks those rows.

    The table is also an Arrow struct array through the Arrow PyCapsule
    interface, which pyarrow, polars, and DuckDB import without a copy. Its
    fields are those of the columns, with one ``text`` field (``utf8``) for
    ``text_offsets`` and ``text_data``, ``is_synthetic`` as Arrow
    booleans, and ``stream`` and ``operation`` null where they are ``None``.
    """

    __slots__ = ("_columns", "_start", "_stop")

    #: The names of the columns, in the order of the Arrow fields.
    names: ClassVar[tuple[str, ...]] = (
        "x0",
        "y0",
        "x1",
        "y1",
        "origin_x",
        "origin_y",
        "text_offsets",
        "text_data",
        "block",
        "line",
        "span",
        "is_synthetic",
        "stream",
        "operation",
    )

    def __init__(self, columns: CharColumns, start: int = 0, stop: int | None = None) -> None:
        self._columns = columns
        self._start = start
        self._stop = len(columns) if stop is None else stop

    def __len__(self) -> int:
        return self._stop - self._start

    @overload
    def __getitem__(self, index: int) -> Char: ...
    @overload
    def __getitem__(self, index: slice) -> Chars: ...
    def __getitem__(self, index: int | slice) -> Char | Chars:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step != 1:
                msg = f"a slice of Chars takes a step of 1: {index!r}"
                raise ValueError(msg)
            return Chars(self._columns, self._start + start, self._start + max(start, stop))
        row = range(self._start, self._stop)[index]
        return _row_char(self._columns.rows(row, row + 1)[0])

    def __iter__(self) -> Iterator[Char]:
        for start in range(self._start, self._stop, _ROWS_PER_READ):
            stop = min(start + _ROWS_PER_READ, self._stop)
            yield from map(_row_char, self._columns.rows(start, stop))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Chars):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other, strict=True))

    def __hash__(self) -> int:
        return hash(tuple(self))

    def __repr__(self) -> str:
        return f"<Chars {len(self)} rows>"

    def column(self, name: str) -> memoryview:
        """Return the column ``name`` of :attr:`names` as a read-only ``memoryview``, without a copy."""
        return memoryview(self._columns.column(name, self._start, self._stop))

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


#: The rows that a :class:`Chars` iteration reads from the core at once.
_ROWS_PER_READ = 4096


def _row_char(row: _CoreChar) -> Char:
    """Return the :class:`Char` of one row of a :class:`Chars`."""
    text, bbox, origin, is_synthetic, source = row
    return _char(text, _rect(bbox), _point(origin), is_synthetic, source)


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
    chars: Chars | None = None
    """The chars of every span as columns when :meth:`Page.layout` was
    called with ``chars=True``, and ``None`` otherwise."""

    def to_dict(self) -> dict[str, Any]:
        """Return the layout as dictionaries, lists, and tuples for JSON.

        The shape is that of pymupdf's ``get_text("dict")``: blocks with
        ``number``, ``type`` (0 for text), ``bbox``, and ``lines``; lines with
        ``bbox``, ``wmode``, ``dir``, and ``spans``; and spans with ``bbox``,
        ``origin``, ``size``, ``font``, ``flags``, ``char_flags``, ``color``,
        ``alpha``, ``ascender``, ``descender``, and ``text``. When the layout
        holds characters, each span has ``chars`` in place of ``text``, a list
        of ``c``, ``bbox``, ``origin``, and ``synthetic``, as in pymupdf's
        ``"rawdict"``. ``Char.stream`` and ``Char.operation`` stay on the
        records, out of pymupdf's shape.
        """
        return {
            "width": self.width,
            "height": self.height,
            "blocks": [
                {
                    "number": number,
                    "type": 0,
                    "bbox": tuple(block.bbox),
                    "lines": [
                        {
                            "bbox": tuple(line.bbox),
                            "wmode": int(line.is_vertical),
                            "dir": tuple(line.direction),
                            "spans": [_span_dict(span) for span in line.spans],
                        }
                        for line in block.lines
                    ],
                }
                for number, block in enumerate(self.blocks)
            ],
        }

    def to_html(self) -> str:
        """Return the layout as a positioned HTML fragment, as pymupdf's ``"html"`` output.

        The fragment is one ``<div>`` the size of the page, in points, with one
        absolutely positioned ``<p>`` per line at the top-left corner of its
        box, turned along its baseline, and one ``<span>`` per span in its
        font family, size, weight, style, and color. Invisible text keeps its
        place with ``opacity:0``.
        """
        parts = [f'<div style="position:relative;width:{_pt(self.width)};height:{_pt(self.height)}">']
        parts.extend(_html_line(line) for block in self.blocks for line in block.lines)
        parts.append("</div>\n")
        return "\n".join(parts)

    def to_xml(self) -> str:
        """Return the layout as XML, as pymupdf's ``"xml"`` output.

        A ``<page>`` holds ``<block>``, ``<line>``, and ``<font>`` elements
        with their boxes, writing mode, baseline direction, and font, size,
        and color. A layout with characters gives one ``<char>`` per
        character with its box, origin, and text, and marks inferred spaces
        with ``synthetic="1"``; a layout without characters gives each span's
        text.
        """
        parts = [f'<page width="{_num(self.width)}" height="{_num(self.height)}">']
        for block in self.blocks:
            parts.append(f'<block bbox="{_nums(block.bbox)}">')
            for line in block.lines:
                parts.append(
                    f'<line bbox="{_nums(line.bbox)}" wmode="{int(line.is_vertical)}" dir="{_nums(line.direction)}">'
                )
                parts.extend(_xml_span(span) for span in line.spans)
                parts.append("</line>")
            parts.append("</block>")
        parts.append("</page>\n")
        return "\n".join(parts)


# The span flags that pymupdf reports; the core passes all but the
# superscript as flags.
_SUPERSCRIPT = 1
_ITALIC = 2
_SERIF = 4
_MONOSPACED = 8
_BOLD = 16
# The char flags of pymupdf's span dictionaries.
_STRIKEOUT = 1
_UNDERLINE = 2
_FILLED = 16
_STROKED = 32
_CLIPPED = 64
_FILLED_MODES = frozenset({TextRenderMode.FILL, TextRenderMode.FILL_STROKE})
_STROKED_MODES = frozenset({TextRenderMode.STROKE, TextRenderMode.FILL_STROKE})


def _num(value: float) -> str:
    """Return a coordinate with at most three decimals and no trailing zeros."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _nums(values: Sequence[float]) -> str:
    return " ".join(_num(value) for value in values)


def _pt(value: float) -> str:
    return f"{_num(value)}pt"


def _srgb(component: float) -> int:
    """Return a color component from 0 to 1 as a byte."""
    return round(min(max(component, 0.0), 1.0) * 255)


def _hex(color: Color | None) -> str:
    """Return a color as ``#rrggbb``; text without a color paint is black."""
    if color is None:
        return "#000000"
    return f"#{_srgb(color.red):02x}{_srgb(color.green):02x}{_srgb(color.blue):02x}"


def _flags(span: Span) -> int:
    """Return pymupdf's span flags of a span."""
    return (
        (_SUPERSCRIPT if span.is_superscript else 0)
        | (_ITALIC if span.is_italic else 0)
        | (_SERIF if span.is_serif else 0)
        | (_MONOSPACED if span.is_monospaced else 0)
        | (_BOLD if span.is_bold else 0)
    )


def _html_line(line: Line) -> str:
    """Return one line of :meth:`Layout.to_html`."""
    style = f"position:absolute;margin:0;white-space:pre;left:{_pt(line.bbox.x0)};top:{_pt(line.bbox.y0)}"
    if line.is_vertical:
        style += ";writing-mode:vertical-rl"
    elif line.direction != (1, 0):
        # Turn the line about the start of its quad, along its baseline.
        ul = line.quad.ul
        angle = math.degrees(math.atan2(line.direction.y, line.direction.x))
        style = (
            f"position:absolute;margin:0;white-space:pre;left:{_pt(ul.x)};top:{_pt(ul.y)};"
            f"transform-origin:0 0;transform:rotate({_num(angle)}deg)"
        )
    spans = "".join(_html_span(span) for span in line.spans)
    return f'<p style="{style}">{spans}</p>'


def _html_span(span: Span) -> str:
    """Return one span of :meth:`Layout.to_html`."""
    family = "serif" if span.is_serif else "monospace" if span.is_monospaced else "sans-serif"
    font = f"{escape(_css_string(span.font))},{family}" if span.font else family
    style = f"font-family:{font};font-size:{_pt(span.size)};line-height:1;color:{_hex(span.color)}"
    if span.is_bold:
        style += ";font-weight:bold"
    if span.is_italic:
        style += ";font-style:italic"
    if span.render_mode is TextRenderMode.INVISIBLE:
        style += ";opacity:0"
    elif span.color is not None and span.color.alpha < 1:
        style += f";opacity:{_num(span.color.alpha)}"
    return f'<span style="{style}">{escape(span.text, quote=False)}</span>'


def _css_string(value: str) -> str:
    """Quote a CSS string before the enclosing HTML attribute is escaped."""
    quoted = "".join(f"\\{ord(char):x} " if char in '\\"' or char < " " or char == "\x7f" else char for char in value)
    return f'"{quoted}"'


_SYNTHETIC = ' synthetic="1"'


def _xml_span(span: Span) -> str:
    """Return one span of :meth:`Layout.to_xml` as a ``<font>`` element."""
    head = (
        f'<font name="{escape(span.font)}" size="{_num(span.size)}" flags="{_flags(span)}" '
        f'color="{_hex(span.color)}" bbox="{_nums(span.bbox)}">'
    )
    if not span.chars:
        return f"{head}{escape(span.text, quote=False)}</font>"
    chars = "".join(
        f'<char bbox="{_nums(char.bbox)}" x="{_num(char.origin.x)}" y="{_num(char.origin.y)}" '
        f'c="{escape(char.text)}"{_SYNTHETIC if char.is_synthetic else ""}/>'
        for char in span.chars
    )
    return f"{head}{chars}</font>"


def _span_dict(span: Span) -> dict[str, Any]:
    """Return one span of :meth:`Layout.to_dict`."""
    char_flags = (
        (_STRIKEOUT if span.is_struck else 0)
        | (_UNDERLINE if span.is_underlined else 0)
        | (_FILLED if span.render_mode in _FILLED_MODES else 0)
        | (_STROKED if span.render_mode in _STROKED_MODES else 0)
        | (_CLIPPED if span.is_clipped else 0)
    )
    color = span.color
    result: dict[str, Any] = {
        "bbox": tuple(span.bbox),
        "origin": tuple(span.origin),
        "size": span.size,
        "font": span.font,
        "flags": _flags(span),
        "char_flags": char_flags,
        "color": 0 if color is None else _srgb(color.red) << 16 | _srgb(color.green) << 8 | _srgb(color.blue),
        "alpha": 255 if color is None else _srgb(color.alpha),
        "ascender": span.ascender,
        "descender": span.descender,
    }
    if not span.chars:
        result["text"] = span.text
    else:
        result["chars"] = [
            {"c": char.text, "bbox": tuple(char.bbox), "origin": tuple(char.origin), "synthetic": char.is_synthetic}
            for char in span.chars
        ]
    return result


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


def _char(text: str, bbox: Rect, origin: Point, is_synthetic: bool, source: tuple[int, int] | None) -> Char:  # noqa: FBT001 - one core tuple.
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
    if source is None:
        _set_char_stream(char, None)
        _set_char_operation(char, None)
    else:
        _set_char_stream(char, source[0])
        _set_char_operation(char, source[1])
    return char


_isfinite = math.isfinite
_new_tuple = tuple.__new__
_new_object = object.__new__
_set_char_text = Char.text.__set__
_set_char_bbox = Char.bbox.__set__
_set_char_origin = Char.origin.__set__
_set_char_synthetic = Char.is_synthetic.__set__
_set_char_stream = Char.stream.__set__
_set_char_operation = Char.operation.__set__


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


def build_layout(
    width: float, height: float, blocks: Sequence[_CoreBlock], columns: CharColumns | None = None
) -> Layout:
    """Build :meth:`Page.layout` from the core's layout and the columns of its chars, if any.

    Spans of one color share one :class:`Color`, which is immutable.
    """
    colors: dict[tuple[float, float, float, float], Color] = {}
    return Layout(
        width=width,
        height=height,
        blocks=tuple(_build_block(bbox, [_build_line(line, colors) for line in lines]) for bbox, lines in blocks),
        chars=None if columns is None else Chars(columns),
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
        _char(char, _rect(char_bbox), _point(char_origin), synthetic, source)
        for char, char_bbox, char_origin, synthetic, source in glyphs
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


__all__ = ["Block", "Char", "Chars", "Layout", "Line", "Span", "Unmapped", "Word"]
