"""Canvas text layout, glyph-run emission, and font dictionary helpers."""

from __future__ import annotations

import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import accumulate, groupby
from typing import TYPE_CHECKING, Literal, TypeAlias

from pyropdf import _core
from pyropdf._canvas._path import _TURNS, _fmt, _number, _numbers, _paint, _stroke
from pyropdf._content import ShadingPattern, TextRenderMode, TilingPattern
from pyropdf._errors import LimitError
from pyropdf._fallback_fonts import FallbackFonts
from pyropdf._fonts import Font, _max_lines, _textbox_text, _utf16
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Name
from pyropdf._targets import Destination, Target, _target_to_core

if TYPE_CHECKING:
    from pyropdf._content import Color, Gradient, PatternPaint, Stroke
    from pyropdf._geometry import Rect
    from pyropdf._objects import Objects
    from pyropdf._page import Page

    _PlacedLine: TypeAlias = tuple[str, float, float, float]
    _TextPaint: TypeAlias = tuple[Color | Gradient | PatternPaint | None, Stroke | None, int]
    _TextboxLayout: TypeAlias = tuple[tuple[Font, ...], float, _TextPaint, list[_PlacedLine]]
    _RichSpanArg: TypeAlias = tuple[str, int, list[int], float, bool, bool, int | None]

TextAlign: TypeAlias = Literal["left", "center", "right", "justify", "start", "end"]
TextAlign.__doc__ = """Horizontal alignment accepted by :meth:`Canvas.textbox`: physical
``"left"``, ``"center"``, ``"right"``, ``"justify"``, or direction-aware
``"start"`` and ``"end"`` (resolved per paragraph against its UAX #9 base
direction)."""

TextDirection: TypeAlias = Literal["auto", "ltr", "rtl"]
TextDirection.__doc__ = """Paragraph base direction for UAX #9 bidirectional layout on
:meth:`Canvas.text` and :meth:`Canvas.textbox`: ``"auto"`` determines the base
direction per paragraph from its first strong directional character (UAX #9
P2/P3), while ``"ltr"`` and ``"rtl"`` force left-to-right or right-to-left."""

#: The alignments of :meth:`Canvas.textbox`.
_ALIGNMENTS = ("left", "center", "right", "justify", "start", "end")
#: The UAX #9 paragraph base directions accepted by :meth:`Canvas.text` and :meth:`Canvas.textbox`.
_BASE_DIRECTIONS = ("auto", "ltr", "rtl")
#: The text progression directions accepted by :meth:`Canvas.text` and :meth:`Canvas.textbox`.
_DIRECTIONS = ("horizontal", "vertical")
#: Maximum number of :class:`TextSpan` entries allowed by default in one rich-text call.
_MAX_TEXT_SPANS = 65_536
#: The Standard 14 fonts that draw their own built-in encoding.
_SYMBOLIC = frozenset({"Symbol", "ZapfDingbats"})
# The line boundaries of str.splitlines.
_LINE_SEPARATORS = ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
# A bound on the memory of one str object beyond its characters, and of a list slot.
_STR_OVERHEAD = sys.getsizeof("") + 8
#: A glyph that fallback shaping placed, as ``_core.shape_text_fallback`` returns it.
_FallbackShapedGlyph: TypeAlias = tuple[int, int, int, int, int, int, int, int, bool, int, int]
#: A glyph that rich layout placed, carrying an optional per-glyph justification extra advance in points.
_RichShapedGlyph: TypeAlias = tuple[int, int, int, int, int, int, int, int, bool, int, int, float]
#: Either a fallback-shaped glyph (11-tuple) or a rich-layout glyph (12-tuple).
_AnyShapedGlyph: TypeAlias = _FallbackShapedGlyph | _RichShapedGlyph
#: Strong RTL or directional embedding/isolate bidi classes that benefit from paragraph-level BiDi layout.
_RTL_BIDI_CLASSES = frozenset({"R", "AL", "RLE", "RLO", "RLI"})
#: How far, in thousandths of the em, the last glyph of ``/ActualText`` may end
#: from its cluster's end: readers look for a word gap from about a tenth.
_CLUSTER_END_TOLERANCE = 50
#: Smallest horizontal GPOS delta (in thousandths of em) that triggers a vertical ``Tm`` shift.
_VERTICAL_X_EPSILON = 0.01
#: PDF text rendering modes (ISO 32000-2 Table 106).
_TR_FILL = 0
_TR_STROKE = 1
_TR_FILL_STROKE = 2
_TR_INVISIBLE = 3
_TR_CLIP_FILL = 4
_TR_CLIP_STROKE = 5
_TR_CLIP_FILL_STROKE = 6
_TR_CLIP = 7


@dataclass(frozen=True, slots=True)
class _TextLayout:
    """Positioned lines and face chain ready to emit as PDF text operators."""

    faces: tuple[Font, ...]
    size: float
    rotation: int
    lines: list[_PlacedLine]
    vertical: bool = False


def _direction(value: object) -> Literal["horizontal", "vertical"]:
    """Return ``"horizontal"`` or ``"vertical"``."""
    if not isinstance(value, str):
        msg = f"direction must be 'horizontal' or 'vertical', not {type(value).__name__}"
        raise TypeError(msg)
    if value == "horizontal":
        return "horizontal"
    if value == "vertical":
        return "vertical"
    msg = f"direction must be one of {', '.join(_DIRECTIONS)}: {value!r}"
    raise ValueError(msg)


def _base_direction(value: object) -> TextDirection:
    """Return ``"auto"``, ``"ltr"``, or ``"rtl"``."""
    if not isinstance(value, str):
        msg = f"base_direction must be 'auto', 'ltr', or 'rtl', not {type(value).__name__}"
        raise TypeError(msg)
    if value == "auto":
        return "auto"
    if value == "ltr":
        return "ltr"
    if value == "rtl":
        return "rtl"
    msg = f"base_direction must be one of {', '.join(_BASE_DIRECTIONS)}: {value!r}"
    raise ValueError(msg)


def _align(value: object) -> TextAlign:
    """Return a checked :data:`TextAlign` value."""
    if not isinstance(value, str):
        msg = f"align must be a str, not {type(value).__name__}"
        raise TypeError(msg)
    if value == "left":
        return "left"
    if value == "center":
        return "center"
    if value == "right":
        return "right"
    if value == "justify":
        return "justify"
    if value == "start":
        return "start"
    if value == "end":
        return "end"
    msg = f"align must be one of {', '.join(_ALIGNMENTS)}: {value!r}"
    raise ValueError(msg)


def _has_rtl(text: str) -> bool:
    """Return whether ``text`` contains strong RTL or directional override/isolate characters."""
    return any(unicodedata.bidirectional(character) in _RTL_BIDI_CLASSES for character in text)


def _validate_span_style(span: TextSpan) -> None:
    """Validate optional paint, stroke, render mode, and decoration flags on a :class:`TextSpan`."""
    if span.fill is not None:
        filled = _paint(span.fill, "fill")
        if isinstance(filled, (ShadingPattern, TilingPattern)):
            msg = f"text needs a Color or a gradient in place of a {type(filled).__name__}"
            raise TypeError(msg)
    if span.stroke is not None:
        stroked = _stroke(span.stroke)
        if stroked is not None and isinstance(stroked.paint, (ShadingPattern, TilingPattern)):
            msg = f"text needs a Color or a gradient in place of a {type(stroked.paint).__name__}"
            raise TypeError(msg)
    if span.render_mode is not None:
        object.__setattr__(span, "render_mode", _render_mode(span.render_mode))
    if type(span.underline) is not bool:
        msg = f"underline must be a bool, not {type(span.underline).__name__}"
        raise TypeError(msg)
    if type(span.strikethrough) is not bool:
        msg = f"strikethrough must be a bool, not {type(span.strikethrough).__name__}"
        raise TypeError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class TextSpan:
    """One inline styled run inside :meth:`Canvas.text` or :meth:`Canvas.textbox`.

    Fields set to ``None`` inherit the enclosing call's defaults (``font``,
    ``size``, ``fill``, ``stroke``, ``render_mode``). To draw a single
    stroke-only or invisible span inside a filled paragraph, set
    ``render_mode=TextRenderMode.STROKE`` (with ``stroke=...``) or
    ``render_mode=TextRenderMode.INVISIBLE``.
    """

    text: str
    font: Font | None = None
    size: float | None = None
    fill: Color | Gradient | PatternPaint | None = None
    stroke: Stroke | Color | Gradient | PatternPaint | None = None
    render_mode: TextRenderMode | str | None = None
    underline: bool = False
    strikethrough: bool = False
    link: Target | None = None

    def __post_init__(self) -> None:
        """Validate every field eagerly on construction."""
        if not isinstance(self.text, str):
            msg = f"text must be a str, not {type(self.text).__name__}"
            raise TypeError(msg)
        if self.font is not None and not isinstance(self.font, Font):
            msg = f"font must be a pyropdf.Font or None, not {type(self.font).__name__}"
            raise TypeError(msg)
        if self.size is not None:
            object.__setattr__(self, "size", _size(self.size))
        _validate_span_style(self)
        if self.link is not None:
            # Validate target shape and non-empty strings; page-count bounds are checked when drawn on a page.
            page_bound = self.link.page + 1 if isinstance(self.link, Destination) else 1
            _target_to_core(self.link, page_bound)


TextSpan.__module__ = "pyropdf"


def _normalize_span_text(text: str, *, textbox: bool) -> str:
    """Normalize line separators (and tabs for ``textbox``) inside one span."""
    normalized = text.replace("\r\n", "\n")
    if textbox:
        return normalized.replace("\r", "\n").replace("\t", " ")
    for separator in _LINE_SEPARATORS:
        if separator != "\n" and separator in normalized:
            normalized = normalized.replace(separator, "\n")
    return normalized


def _coerce_span_sequence(spans_input: str | Sequence[TextSpan], *, limit: int | None) -> Sequence[TextSpan]:
    """Coerce ``str | Sequence[TextSpan]`` to a validated sequence bounded by span count."""
    if isinstance(spans_input, str):
        return (TextSpan(text=spans_input),)
    if isinstance(spans_input, (bytes, bytearray, memoryview)) or not isinstance(spans_input, Sequence):
        msg = f"text must be a str or a sequence of TextSpan, not {type(spans_input).__name__}"
        raise TypeError(msg)
    span_limit = None if limit is None else min(_MAX_TEXT_SPANS, limit)
    if span_limit is not None and len(spans_input) > span_limit:
        code = "content_element_count"
        msg = f"the text has {len(spans_input)} spans, over the limit of {span_limit}"
        raise LimitError(code, msg)
    for item in spans_input:
        if not isinstance(item, TextSpan):
            msg = f"text spans must be TextSpan instances, not {type(item).__name__}"
            raise TypeError(msg)
    return spans_input


def _validate_span_sequence(
    spans_input: str | Sequence[TextSpan],
    *,
    limit: int | None,
    textbox: bool,
) -> tuple[Sequence[TextSpan], list[str], int]:
    """Validate span container, count bound, UTF-8 byte bound, and normalize span text."""
    spans = _coerce_span_sequence(spans_input, limit=limit)
    normalized_texts: list[str] = []
    total_bytes = 0
    for span in spans:
        text = _normalize_span_text(span.text, textbox=textbox)
        total_bytes += len(text.encode("utf-8"))
        if limit is not None and total_bytes > limit:
            code = "text_input_size"
            msg = f"the text exceeds the {limit}-byte UTF-8 limit"
            raise LimitError(code, msg)
        normalized_texts.append(text)

    if not textbox:
        for index in range(len(normalized_texts) - 1, -1, -1):
            if normalized_texts[index]:
                normalized_texts[index] = normalized_texts[index].removesuffix("\n")
                break
    return spans, normalized_texts, total_bytes


def _face_dedup_key(face: Font) -> object:
    """Return a hashable deduplication key for a Standard 14 or embedded :class:`Font`."""
    if face._font.standard_name is not None:  # noqa: SLF001
        return ("standard", face._font.standard_name)  # noqa: SLF001
    return ("embedded", face._key())  # noqa: SLF001


def _prepare_rich_spans(  # noqa: PLR0913 - mirrors Canvas.text/textbox style defaults.
    spans_input: str | Sequence[TextSpan],
    *,
    page: Page,
    font: Font | None,
    fallback_fonts: Sequence[Font] | FallbackFonts,
    size: float,
    fill: Color | Gradient | PatternPaint | None,
    stroke: Stroke | Color | Gradient | PatternPaint | None,
    render_mode: TextRenderMode | None,
    max_text_size: int | None,
    textbox: bool,
) -> tuple[tuple[Font, ...], list[_RichSpanArg], list[_TextPaint], list[Target], int]:
    """Validate and resolve a rich-text span sequence into core layout inputs.

    Returns ``(unique_faces, span_args, span_paints, link_targets, total_utf8_bytes)``.
    """
    from pyropdf._links import _link_target  # noqa: PLC0415 - avoids circular import with _links.

    _validate_optional_positive_int("max_text_size", max_text_size)
    limit = max_text_size
    default_font = Font.standard("Helvetica") if font is None else font
    if not isinstance(default_font, Font):
        msg = f"font must be a pyropdf.Font, not {type(default_font).__name__}"
        raise TypeError(msg)
    font_size = _size(size)
    spans, normalized_texts, total_bytes = _validate_span_sequence(spans_input, limit=limit, textbox=textbox)

    unique_faces: list[Font] = []
    face_index_by_key: dict[object, int] = {}

    def intern_face(face: Font) -> int:
        key = _face_dedup_key(face)
        known = face_index_by_key.get(key)
        if known is None:
            known = len(unique_faces)
            face_index_by_key[key] = known
            unique_faces.append(face)
        return known

    default_chain = _resolve_fonts(default_font, fallback_fonts, direction="horizontal")
    for face in default_chain:
        intern_face(face)

    span_args: list[_RichSpanArg] = []
    span_paints: list[_TextPaint] = []
    link_targets: list[Target] = []

    for span, text in zip(spans, normalized_texts, strict=True):
        chain = (
            default_chain if span.font is None else _resolve_fonts(span.font, fallback_fonts, direction="horizontal")
        )
        primary_idx = intern_face(chain[0])
        fallback_idxs = [intern_face(face) for face in chain[1:]]
        span_size = font_size if span.size is None else span.size
        eff_fill = fill if span.fill is None else span.fill
        eff_stroke = stroke if span.stroke is None else span.stroke
        eff_mode = render_mode if span.render_mode is None else _render_mode(span.render_mode)
        checked_paint = _text_paint(eff_fill, eff_stroke, render_mode=eff_mode)
        link_id: int | None = None
        if span.link is not None:
            _link_target(span.link, page)
            link_id = len(link_targets)
            link_targets.append(span.link)
        span_paints.append(checked_paint)
        span_args.append((text, primary_idx, fallback_idxs, span_size, span.underline, span.strikethrough, link_id))

    return tuple(unique_faces), span_args, span_paints, link_targets, total_bytes


def _render_mode(value: object) -> TextRenderMode | None:
    """Return a checked :class:`TextRenderMode` or ``None``."""
    if value is None:
        return None
    if isinstance(value, TextRenderMode):
        return value
    if not isinstance(value, str):
        msg = f"render_mode must be a TextRenderMode or None, not {type(value).__name__}"
        raise TypeError(msg)
    try:
        return TextRenderMode(value)
    except ValueError as exc:
        modes = ", ".join(mode.value for mode in TextRenderMode)
        msg = f"render_mode must be one of {modes}: {value!r}"
        raise ValueError(msg) from exc


def _expand_fallback_fonts(primary: Font, fallback_fonts: Sequence[Font] | FallbackFonts) -> list[Font]:
    """Validate and flatten ``fallback_fonts`` into an ordered list of :class:`Font`."""
    if isinstance(fallback_fonts, FallbackFonts):
        extras: list[Font] = []
        for _language, entry in fallback_fonts._snapshot():  # noqa: SLF001 - atomic lock snapshot.
            order = (entry.serif, entry.sans) if primary.is_serif else (entry.sans, entry.serif)
            extras.extend(face for face in order if face is not None)
        return extras
    if isinstance(fallback_fonts, (str, bytes)) or not isinstance(fallback_fonts, Sequence):
        msg = f"fallback_fonts must be a sequence of pyropdf.Font or FallbackFonts, not {type(fallback_fonts).__name__}"
        raise TypeError(msg)
    extras = []
    for item in fallback_fonts:
        if not isinstance(item, Font):
            msg = f"fallback_fonts entries must be pyropdf.Font, not {type(item).__name__}"
            raise TypeError(msg)
        extras.append(item)
    return extras


def _resolve_fonts(
    font: Font | None,
    fallback_fonts: Sequence[Font] | FallbackFonts,
    *,
    direction: Literal["horizontal", "vertical"],
) -> tuple[Font, ...]:
    """Return the ordered, deduplicated face chain for drawing or measuring text."""
    if font is None:
        primary = Font.standard("Helvetica")
    elif isinstance(font, Font):
        primary = font
    else:
        msg = f"font must be a pyropdf.Font, not {type(font).__name__}"
        raise TypeError(msg)
    chain: list[Font] = []
    seen: set[object] = set()
    for face in (primary, *_expand_fallback_fonts(primary, fallback_fonts)):
        key: object = (
            ("standard", face._font.standard_name)  # noqa: SLF001
            if face._font.standard_name is not None  # noqa: SLF001
            else ("embedded", face._key())  # noqa: SLF001
        )
        if key not in seen:
            seen.add(key)
            chain.append(face)
    if direction == "vertical" and any(face._font.standard_name is not None for face in chain):  # noqa: SLF001
        msg = "vertical writing requires embedded OpenType fonts; Standard 14 fonts only support horizontal text"
        raise ValueError(msg)
    return tuple(chain)


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


def _clip_text_paint(
    filled: Color | Gradient | PatternPaint | None,
    stroked: Stroke | None,
) -> _TextPaint:
    """Return the fill, stroke, and clip ``Tr`` mode (4..7) for :meth:`Canvas.clip_text`."""
    if filled is not None and stroked is not None:
        return filled, stroked, _TR_CLIP_FILL_STROKE
    if filled is not None:
        return filled, None, _TR_CLIP_FILL
    if stroked is not None:
        return None, stroked, _TR_CLIP_STROKE
    return None, None, _TR_CLIP


def _explicit_mode_paint(
    filled: Color | Gradient | PatternPaint | None,
    stroked: Stroke | None,
    render_mode: TextRenderMode,
) -> _TextPaint:
    """Validate ``filled`` and ``stroked`` against an explicit :class:`TextRenderMode`."""
    if render_mode is TextRenderMode.INVISIBLE:
        return None, None, _TR_INVISIBLE
    if render_mode is TextRenderMode.FILL:
        if filled is None:
            msg = "render_mode='fill' requires a fill"
            raise ValueError(msg)
        return filled, None, _TR_FILL
    if render_mode is TextRenderMode.STROKE:
        if stroked is None:
            msg = "render_mode='stroke' requires a stroke"
            raise ValueError(msg)
        return None, stroked, _TR_STROKE
    if filled is None or stroked is None:
        msg = "render_mode='fill_stroke' requires both a fill and a stroke"
        raise ValueError(msg)
    return filled, stroked, _TR_FILL_STROKE


def _text_paint(
    fill: Color | Gradient | PatternPaint | None,
    stroke: Stroke | Color | Gradient | PatternPaint | None,
    *,
    render_mode: TextRenderMode | None = None,
    clip: bool = False,
) -> _TextPaint:
    """Return the checked fill, stroke, and PDF ``Tr`` mode code (0..7)."""
    stroked = _stroke(stroke)
    filled = None if fill is None else _paint(fill, "fill")
    if isinstance(filled, (ShadingPattern, TilingPattern)):
        msg = f"text needs a Color or a gradient in place of a {type(filled).__name__}"
        raise TypeError(msg)
    if stroked is not None and isinstance(stroked.paint, (ShadingPattern, TilingPattern)):
        msg = f"text needs a Color or a gradient in place of a {type(stroked.paint).__name__}"
        raise TypeError(msg)
    if clip:
        return _clip_text_paint(filled, stroked)
    if render_mode is not None:
        return _explicit_mode_paint(filled, stroked, render_mode)
    if filled is None and stroked is None:
        msg = "text needs a fill or a stroke"
        raise ValueError(msg)
    mode = _TR_FILL_STROKE if filled is not None and stroked is not None else _TR_STROKE if stroked else _TR_FILL
    return filled, stroked, mode


@dataclass(frozen=True, slots=True, kw_only=True)
class TextFit:
    """How text fits a :meth:`Canvas.textbox`: the height left below its last line.

    In horizontal mode, :attr:`spare_height` is the height remaining below the
    last line; in vertical mode, it is the width remaining past the last
    column. A negative :attr:`spare_height` is the shortfall of text that did
    not fit, and was not drawn.
    """

    spare_height: float

    @property
    def fits(self) -> bool:
        """Whether the text fit its box and was drawn."""
        return self.spare_height >= 0


TextFit.__module__ = "pyropdf"


def _line_count(text: str) -> int:
    """Return ``len(text.splitlines())`` without splitting ``text``."""
    if not text:
        return 0
    breaks = sum(str.count(text, separator) for separator in _LINE_SEPARATORS) - str.count(text, "\r\n")
    return breaks + (text[-1] not in _LINE_SEPARATORS)


def _lines_size(text: str, count: int) -> int:
    """Return a bound on the memory of ``text``'s lines and of their joined copy."""
    return 2 * sys.getsizeof(text) + count * _STR_OVERHEAD


def _place_textbox_lines(  # noqa: PLR0913 - box geometry and layout metrics.
    box: Rect,
    lines: list[tuple[str, float, bool]],
    *,
    along_extent: float,
    align: str,
    first_offset: float,
    leading: float,
    vertical: bool,
    rotation: int,
) -> list[_PlacedLine]:
    """Compute display-space line origins and word spacing inside ``box``."""
    cos, sin = _TURNS[rotation]
    if vertical:
        # Columns run along (-sin, cos) and stack right-to-left along (-cos, -sin).
        corner = {0: (box.x1, box.y0), 90: (box.x1, box.y1), 180: (box.x0, box.y1), 270: (box.x0, box.y0)}[rotation]
    else:
        # Lines run along (cos, sin) and stack along (-sin, cos).
        corner = {0: (box.x0, box.y0), 90: (box.x1, box.y0), 180: (box.x1, box.y1), 270: (box.x0, box.y1)}[rotation]
    placed: list[_PlacedLine] = []
    for index, (line, line_length, justify) in enumerate(lines):
        if not line:
            continue
        free = along_extent - line_length
        across = {"center": free / 2, "right": free}.get(align, 0.0)
        spaces = line.count(" ")
        word_space = max(free, 0.0) / spaces if justify and spaces else 0.0
        along = max(across, 0.0)
        stack = first_offset + index * leading
        if vertical:
            x = corner[0] - along * sin - stack * cos
            y = corner[1] + along * cos - stack * sin
        else:
            x = corner[0] + along * cos - stack * sin
            y = corner[1] + along * sin + stack * cos
        placed.append((line, x, y, word_space))
    return placed


def _layout_textbox(  # noqa: PLR0913 - the text style options.
    box: Rect,
    text: str,
    *,
    font: Font | None,
    fallback_fonts: Sequence[Font] | FallbackFonts,
    size: float,
    align: str,
    line_height: float | None,
    paint: tuple[Color | Gradient | PatternPaint | None, Stroke | Color | Gradient | PatternPaint | None],
    render_mode: TextRenderMode | None,
    direction: Literal["horizontal", "vertical"],
    rotation: int,
    max_text_size: int | None,
    operation: _core._OperationBudget | None,
) -> tuple[TextFit, _TextboxLayout | None]:
    """Lay out a text box once the memory of its normalized text is charged."""
    fill, stroke = paint
    normalized = _textbox_text(text, max_text_size)
    checked_align = _align(align)
    physical_align = {"start": "left", "end": "right"}.get(checked_align, checked_align)
    vertical = direction == "vertical"
    horizontal_span = rotation in (0, 180)
    if vertical:
        horizontal_span = not horizontal_span
    along_extent, stack_extent = (
        (box.x1 - box.x0, box.y1 - box.y0) if horizontal_span else (box.y1 - box.y0, box.x1 - box.x0)
    )
    font_size = _size(size)
    checked_paint = _text_paint(fill, stroke, render_mode=render_mode)
    spacing = _line_height(line_height)
    faces = _resolve_fonts(font, fallback_fonts, direction=direction)
    if not normalized:
        return TextFit(spare_height=stack_extent), None
    if len(faces) == 1 and not vertical:
        primary = _canvas_font(normalized, faces[0])
        lines, _needed, spare, ascent, leading = primary._font.layout(  # noqa: SLF001
            normalized,
            along_extent,
            stack_extent,
            font_size,
            spacing,
            align == "justify",
            _max_lines(max_text_size),
            operation=operation,
        )
    else:
        lines, _needed, spare, ascent, leading = _core.layout_text_fallback(
            [face._font for face in faces],  # noqa: SLF001
            normalized,
            along_extent,
            stack_extent,
            font_size,
            spacing,
            align == "justify",
            _max_lines(max_text_size),
            max_text_size,
            False,  # noqa: FBT003 - already normalized above
            vertical,
            operation=operation,
        )
    if spare < 0:
        return TextFit(spare_height=spare), None
    placed = _place_textbox_lines(
        box,
        lines,
        along_extent=along_extent,
        align=physical_align,
        first_offset=ascent * font_size,
        leading=leading,
        vertical=vertical,
        rotation=rotation,
    )
    return TextFit(spare_height=spare), (faces, font_size, checked_paint, placed)


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


class _GlyphRun:
    """Writes shaped glyphs across one or more faces as ``TJ`` arrays.

    Supports both horizontal (``Identity-H`` / Standard 14 / Type 3) and
    vertical (``Identity-V`` with ``/W2``) progression. ``TJ`` adjustments move
    the pen from each glyph's recorded metric in ``W``/``W2`` to its shaped
    advance and offset, ``Ts`` raises horizontal glyphs, and a line's word
    spacing widens its spaces, since ``Tw`` applies to one-byte codes only. A
    cluster whose glyphs do not read as its text in the ToUnicode CMap is shown
    inside one ``/ActualText``. ``codes`` holds each ``(face_index, glyph)``'s
    font resource name and hex code: ``Tf`` selects a glyph's font where it
    changes, leaving the pen and pending adjustment intact.
    """

    def __init__(  # noqa: PLR0913 - per-face glyph tables for emission.
        self,
        size: float,
        scales: list[float],
        widths: dict[tuple[int, int], float],
        mapped: dict[tuple[int, int], str],
        codes: dict[tuple[int, int], tuple[bytes, bytes]],
        *,
        vertical: bool = False,
        v_origins: dict[tuple[int, int], tuple[float, float]] | None = None,
        type3_keys: frozenset[tuple[int, int]] = frozenset(),
    ) -> None:
        self.size = size
        self.scales = scales
        self.widths = widths
        self.mapped = mapped
        self.codes = codes
        self.vertical = vertical
        self.v_origins = v_origins or {}
        self.type3_keys = type3_keys
        self.font: bytes | None = None
        self.ops: list[bytes] = []
        self.items: list[bytes] = []
        self.pending = 0.0
        self.rise_pt = 0.0
        self.pen_y = 0.0
        self.line_origin = (0.0, 0.0)
        self.line_turn = (1, 0)
        self.needs_tm = False

    def flush(self) -> None:
        if self.items:
            self.ops.append(b"[" + b" ".join(self.items) + b"] TJ")
            self.items.clear()

    def _emit_tm(self, tx_units: float, ty_units: float) -> None:
        x0, y0 = self.line_origin
        cos, sin = self.line_turn
        tx_pt = tx_units * self.size / 1000.0
        ty_pt = ty_units * self.size / 1000.0
        x = x0 + tx_pt * cos + ty_pt * sin
        y = y0 + tx_pt * sin - ty_pt * cos
        self.ops.append(_numbers(cos, sin, sin, -cos, x, y) + b" Tm")

    def show(
        self,
        line: str,
        glyphs: Sequence[_AnyShapedGlyph],
        spacing: float,
        *,
        origin: tuple[float, float] = (0.0, 0.0),
        turn: tuple[int, int] = (1, 0),
    ) -> bytes:
        self.ops = []
        self.pending = 0.0
        self.rise_pt = 0.0
        self.pen_y = 0.0
        self.line_origin = origin
        self.line_turn = turn
        self.needs_tm = False
        for (start, end), cluster in groupby(glyphs, key=lambda glyph: glyph[9:11]):
            self._show_cluster(line, line[start:end], list(cluster), spacing)
        self.flush()
        if self.rise_pt:
            self.ops.append(b"0 Ts")
        return b"\n".join(self.ops)

    def _reads_as(self, text: str, cluster: Sequence[_AnyShapedGlyph]) -> bool:
        """Return whether the CMap's texts of a cluster's glyphs read as its text, in canonical order of its marks."""
        texts = [self.mapped.get((glyph[0], glyph[1]), "") for glyph in cluster if glyph[1]]
        if cluster[0][8]:
            texts.reverse()
        return not texts or unicodedata.normalize("NFD", "".join(texts)) == unicodedata.normalize("NFD", text)

    def _apply_horizontal_spacing(self, line: str, start: int, end: int, spacing: float, extra_pt: float) -> None:
        if extra_pt:
            self.pending -= extra_pt * 1000 / self.size
        elif spacing and line[start:end] == " ":
            self.pending -= spacing * 1000 / self.size

    def _show_cluster(
        self,
        line: str,
        text: str,
        cluster: Sequence[_AnyShapedGlyph],
        spacing: float,
    ) -> None:
        spanned = not self._reads_as(text, cluster)
        adv_idx = 3 if self.vertical else 2
        lead = next((index for index, glyph in enumerate(cluster) if glyph[1] and glyph[adv_idx]), 0) if spanned else 0
        order = [lead, *(index for index in range(len(cluster)) if index != lead)]
        pens = list(accumulate((glyph[adv_idx] * self.scales[glyph[0]] for glyph in cluster), initial=0.0))
        ends_span = spanned and sum(1 for glyph in cluster if glyph[1]) > 1
        if spanned:
            self.flush()
            self.ops.append(b"/Span <</ActualText <FEFF" + _utf16(text) + b">>> BDC")
        at = 0.0
        for index in order:
            entry = cluster[index]
            face_idx, glyph, _x_adv, _y_adv, x_offset, y_offset, start, end = entry[:8]
            extra_pt = entry[11] if len(entry) > 11 else 0.0  # noqa: PLR2004 - optional rich extra advance.
            self.pending -= pens[index] - at
            at = pens[index]
            if not glyph:
                # A control character without a glyph only advances.
                continue
            key = (face_idx, glyph)
            scale = self.scales[face_idx]
            if self.vertical:
                self._show_vertical_glyph(
                    key,
                    (x_offset * scale, y_offset * scale),
                    (at, pens[-1]),
                    ends_cluster=ends_span and index == order[-1],
                )
                if spacing and line[start:end] == " ":
                    delta = spacing * 1000 / self.size
                    self.pending += delta
                    self.pen_y -= delta
            else:
                offset = x_offset * scale
                follow = pens[-1] - (at + offset + self.widths[key]) if ends_span and index == order[-1] else 0.0
                rise_pt = y_offset * scale * self.size / 1000.0
                self._show_horizontal_glyph(
                    key, offset, rise_pt, follow if abs(follow) > _CLUSTER_END_TOLERANCE else 0.0
                )
                self._apply_horizontal_spacing(line, start, end, spacing, extra_pt)
        self.pending -= pens[-1] - at
        if self.vertical:
            self.pen_y += pens[-1]
        if spanned:
            self.flush()
            self.ops.append(b"EMC")

    def _select_font(self, font: bytes) -> None:
        if font != self.font:
            self.flush()
            self.font = font
            self.ops.append(b"/" + font + b" " + _fmt(self.size) + b" Tf")

    def _show_horizontal_glyph(self, key: tuple[int, int], offset: float, rise_pt: float, follow: float) -> None:
        """Show one horizontal glyph at the pen, and move the text position ``follow`` further through ``Tc``."""
        if rise_pt != self.rise_pt:
            self.flush()
            self.rise_pt = rise_pt
            self.ops.append(_fmt(self.rise_pt) + b" Ts")
        if follow:
            self.flush()
            self.ops.append(_fmt(follow * self.size / 1000) + b" Tc")
        font, code = self.codes[key]
        self._select_font(font)
        if self.pending - offset:
            self.items.append(_fmt(self.pending - offset))
        self.items.append(code)
        self.pending = self.widths[key] + offset + follow
        if follow:
            self.flush()
            self.ops.append(b"0 Tc")

    def _show_vertical_glyph(
        self,
        key: tuple[int, int],
        offsets: tuple[float, float],
        span: tuple[float, float],
        *,
        ends_cluster: bool,
    ) -> None:
        """Show one vertical glyph along the column using ``W2`` metrics and ``TJ`` / ``Tm``."""
        x_off, y_off = offsets
        at, cluster_end = span
        font, code = self.codes[key]
        if key in self.type3_keys:
            # Type 3 fonts always advance horizontally in PDF and have no W2 origin displacement.
            self.flush()
            self._emit_tm(x_off, self.pen_y + at + y_off)
            self._select_font(font)
            self.ops.append(code + b" Tj")
            self.pending = 0.0
            self.needs_tm = True
            return
        v_x, v_y = self.v_origins[key]
        dx = x_off + v_x
        dy = y_off + v_y
        w1_y = self.widths[key]
        follow = cluster_end - (at + dy + w1_y) if ends_cluster else 0.0
        follow = follow if abs(follow) > _CLUSTER_END_TOLERANCE else 0.0
        if abs(dx) > _VERTICAL_X_EPSILON or self.needs_tm:
            self.flush()
            self._emit_tm(dx, self.pen_y + at + dy)
            self.pending = 0.0
            self.needs_tm = abs(dx) > _VERTICAL_X_EPSILON
            if follow:
                self.ops.append(_fmt(follow * self.size / 1000) + b" Tc")
            self._select_font(font)
            self.items.append(code)
            self.pending = w1_y + dy + follow
            if follow:
                self.flush()
                self.ops.append(b"0 Tc")
            return
        if follow:
            self.flush()
            self.ops.append(_fmt(follow * self.size / 1000) + b" Tc")
        self._select_font(font)
        if self.pending - dy:
            self.items.append(_fmt(self.pending - dy))
        self.items.append(code)
        self.pending = w1_y + dy + follow
        if follow:
            self.flush()
            self.ops.append(b"0 Tc")


def _font_value(objects: Objects, font: str | int) -> object:
    """Return the resource value of a Standard 14 font's base name or an embedded face's Type0 font."""
    return _font_dict(font) if isinstance(font, str) else objects[font]


def _font_dict(base: str) -> dict[str, object]:
    """Return the font dictionary of a Standard 14 font, which needs no embedding."""
    font: dict[str, object] = {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name(base)}
    if base not in _SYMBOLIC:
        font["Encoding"] = Name("WinAnsiEncoding")
    return font
