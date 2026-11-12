"""Canvas text layout, glyph-run emission, and font dictionary helpers."""

from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass
from itertools import accumulate, groupby
from typing import TYPE_CHECKING

from pyropdf._canvas._path import _TURNS, _fmt, _number, _paint, _stroke
from pyropdf._content import ShadingPattern
from pyropdf._fonts import Font, _max_lines, _textbox_text, _utf16
from pyropdf._objects import Name

if TYPE_CHECKING:
    from typing import TypeAlias

    from pyropdf import _core
    from pyropdf._content import Color, Gradient, Stroke
    from pyropdf._geometry import Rect
    from pyropdf._objects import Objects

    _PlacedLine: TypeAlias = tuple[str, float, float, float]
    _TextboxLayout: TypeAlias = tuple[Font, float, tuple[Color | Gradient | None, Stroke | None], list[_PlacedLine]]

#: The alignments of :meth:`Canvas.textbox`, in the order of the core's codes.
_ALIGNMENTS = ("left", "center", "right", "justify")
#: The Standard 14 fonts that draw their own built-in encoding.
_SYMBOLIC = frozenset({"Symbol", "ZapfDingbats"})
# The line boundaries of str.splitlines.
_LINE_SEPARATORS = ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
# A bound on the memory of one str object beyond its characters, and of a list slot.
_STR_OVERHEAD = sys.getsizeof("") + 8
#: A glyph that shaping placed, as ``_Font.shape`` returns it.
_ShapedGlyph = tuple[int, int, int, int, int, int, int, bool, int, int]
#: How far, in thousandths of the em, the last glyph of ``/ActualText`` may end
#: from its cluster's end: readers look for a word gap from about a tenth.
_CLUSTER_END_TOLERANCE = 50


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


def _text_paint(
    fill: Color | Gradient | None, stroke: Stroke | Color | Gradient | None
) -> tuple[Color | Gradient | None, Stroke | None]:
    """Return the checked fill and stroke of text, which needs at least one."""
    if fill is None and stroke is None:
        msg = "text needs a fill or a stroke"
        raise ValueError(msg)
    stroked = _stroke(stroke)
    filled = None if fill is None else _paint(fill, "fill")
    if isinstance(filled, ShadingPattern) or (stroked is not None and isinstance(stroked.paint, ShadingPattern)):
        msg = "text needs a Color or a gradient in place of a ShadingPattern"
        raise TypeError(msg)
    return filled, stroked


@dataclass(frozen=True, slots=True)
class TextFit:
    """How text fits a :meth:`Canvas.textbox`: the height left below its last line.

    A negative :attr:`spare_height` is the shortfall of text that did not
    fit, and was not drawn.
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


def _layout_textbox(  # noqa: PLR0913 - the text style options.
    box: Rect,
    text: str,
    *,
    font: Font | None,
    size: float,
    align: str,
    line_height: float | None,
    paint: tuple[Color | Gradient | None, Stroke | Color | Gradient | None],
    rotation: int,
    max_text_size: int | None,
    operation: _core._OperationBudget | None,
) -> tuple[TextFit, _TextboxLayout | None]:
    """Lay out a text box once the memory of its normalized text is charged."""
    fill, stroke = paint
    normalized = _textbox_text(text, max_text_size)
    if align not in _ALIGNMENTS:
        msg = f"align must be one of {', '.join(_ALIGNMENTS)}: {align!r}"
        raise ValueError(msg)
    upright = rotation in (0, 180)
    width, height = (box.x1 - box.x0, box.y1 - box.y0) if upright else (box.y1 - box.y0, box.x1 - box.x0)
    font_size = _size(size)
    filled, stroked = _text_paint(fill, stroke)
    spacing = _line_height(line_height)
    if not normalized:
        return TextFit(spare_height=height), None
    face = _canvas_font(normalized, font)
    lines, _needed, spare, ascent, leading = face._font.layout(  # noqa: SLF001
        normalized,
        width,
        height,
        font_size,
        spacing,
        align == "justify",
        _max_lines(max_text_size),
        operation=operation,
    )
    if spare < 0:
        return TextFit(spare_height=spare), None
    cos, sin = _TURNS[rotation]
    # The turned box's top-left corner, from which lines run along (cos, sin) and stack along (-sin, cos).
    corner = {0: (box.x0, box.y0), 90: (box.x1, box.y0), 180: (box.x1, box.y1), 270: (box.x0, box.y1)}[rotation]
    placed: list[_PlacedLine] = []
    for index, (line, line_width, justify) in enumerate(lines):
        if not line:
            continue
        free = width - line_width
        across = {"center": free / 2, "right": free}.get(align, 0.0)
        spaces = line.count(" ")
        word_space = max(free, 0.0) / spaces if justify and spaces else 0.0
        along = max(across, 0.0)
        down = ascent * font_size + index * leading
        placed.append((line, corner[0] + along * cos - down * sin, corner[1] + along * sin + down * cos, word_space))
    return TextFit(spare_height=spare), (face, font_size, (filled, stroked), placed)


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
    """Writes shaped glyphs as ``TJ`` arrays.

    ``TJ`` adjustments move the pen from each glyph's width in ``W`` to its
    shaped advance and by its offset, ``Ts`` raises it, and a line's word
    spacing widens its spaces, since ``Tw`` applies to one-byte codes only.
    A cluster whose glyphs do not read as its text in the ToUnicode CMap is
    shown inside one ``/ActualText``. ``codes`` holds each glyph's font and
    code: ``Tf`` selects a glyph's font where it changes, which leaves the pen
    and the pending adjustment as they are, so a line may mix a Type0 and
    Type 3 fonts.
    """

    def __init__(
        self,
        size: float,
        units_per_em: int,
        widths: dict[int, float],
        mapped: dict[int, str],
        codes: dict[int, tuple[bytes, bytes]],
    ) -> None:
        self.size = size
        self.units_per_em = units_per_em
        self.scale = 1000 / units_per_em
        self.widths = widths
        self.mapped = mapped
        self.codes = codes
        self.font: bytes | None = None
        self.ops: list[bytes] = []
        self.items: list[bytes] = []
        self.pending = 0.0  # From the pen to the text position, in TJ units: positive moves left.
        self.rise = 0

    def flush(self) -> None:
        if self.items:
            self.ops.append(b"[" + b" ".join(self.items) + b"] TJ")
            self.items.clear()

    def show(self, line: str, glyphs: list[_ShapedGlyph], spacing: float) -> bytes:
        self.ops = []
        self.pending = 0.0
        self.rise = 0
        for (start, end), cluster in groupby(glyphs, key=lambda glyph: glyph[8:]):
            self._show_cluster(line, line[start:end], list(cluster), spacing)
        self.flush()
        if self.rise:
            self.ops.append(b"0 Ts")
        return b"\n".join(self.ops)

    def _reads_as(self, text: str, cluster: list[_ShapedGlyph]) -> bool:
        """Return whether the CMap's texts of a cluster's glyphs read as its text, in canonical order of its marks."""
        texts = [self.mapped.get(glyph[0], "") for glyph in cluster if glyph[0]]
        if cluster[0][7]:
            texts.reverse()
        return not texts or unicodedata.normalize("NFD", "".join(texts)) == unicodedata.normalize("NFD", text)

    def _show_cluster(self, line: str, text: str, cluster: list[_ShapedGlyph], spacing: float) -> None:
        spanned = not self._reads_as(text, cluster)
        # Readers take the box of ``/ActualText`` from the glyphs inside: MuPDF
        # from the first glyph, Xpdf and poppler from the first glyph's origin
        # to the last one's end. So a glyph with a width comes first, not the
        # mark that a right-to-left run places before its base, and the last
        # glyph leaves the text position at the cluster's end. Each glyph still
        # draws where the shaper put it (``evidence/cluster-text``).
        lead = next((index for index, glyph in enumerate(cluster) if glyph[0] and glyph[1]), 0) if spanned else 0
        order = [lead, *(index for index in range(len(cluster)) if index != lead)]
        pens = list(accumulate((glyph[1] * self.scale for glyph in cluster), initial=0.0))
        ends_span = spanned and sum(1 for glyph in cluster if glyph[0]) > 1
        if spanned:
            self.flush()
            self.ops.append(b"/Span <</ActualText <FEFF" + _utf16(text) + b">>> BDC")
        at = 0.0
        for index in order:
            glyph, _advance, _y_advance, x_offset, y_offset, start, end, *_ = cluster[index]
            self.pending -= pens[index] - at
            at = pens[index]
            if not glyph:
                # A control character without a glyph only advances.
                continue
            offset = x_offset * self.scale
            follow = pens[-1] - (at + offset + self.widths[glyph]) if ends_span and index == order[-1] else 0.0
            self._show_glyph(glyph, offset, y_offset, follow if abs(follow) > _CLUSTER_END_TOLERANCE else 0.0)
            if spacing and line[start:end] == " ":
                self.pending -= spacing * 1000 / self.size
        self.pending -= pens[-1] - at
        if spanned:
            self.flush()
            self.ops.append(b"EMC")

    def _show_glyph(self, glyph: int, offset: float, y_offset: int, follow: float) -> None:
        """Show one glyph at the pen, and move the text position ``follow`` further through ``Tc``."""
        if y_offset != self.rise:
            self.flush()
            self.rise = y_offset
            self.ops.append(_fmt(self.rise * self.size / self.units_per_em) + b" Ts")
        if follow:
            self.flush()
            self.ops.append(_fmt(follow * self.size / 1000) + b" Tc")
        font, code = self.codes[glyph]
        if font != self.font:
            self.flush()
            self.font = font
            self.ops.append(b"/" + font + b" " + _fmt(self.size) + b" Tf")
        if self.pending - offset:
            self.items.append(_fmt(self.pending - offset))
        self.items.append(code)
        self.pending = self.widths[glyph] + offset + follow
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
