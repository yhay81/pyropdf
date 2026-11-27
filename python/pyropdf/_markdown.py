"""Build Markdown from the dictionary of a page's layout.

Ported from pylopdf 036be111 ``src/pylopdf/_markdown.py``.

Initial rules:

- Body size is the font size containing the most characters, rounded to 0.1 pt.
  Sufficiently larger sizes map in descending order to heading levels 1–4.
- Wrapped lines join without a space between characters in scripts that do not
  normally delimit words with spaces.
- Adjacent spans of one line are separated by a space when the gap between
  them exceeds the extractor's word gap, as in ``Page.text()``.
- Leading bullets such as ・, •, and ● plus ``1.``/``1)`` normalize to lists.
- Span flags derived from embedded-font weight and italic metadata become
  emphasis markers in body text. Heading text remains plain to avoid combining
  heading markers with emphasis. Standard 14 Type 1 fonts are excluded because
  hayro does not expose their metadata.
- Multicolumn text follows deterministic whitespace gutters.
- Vertical CJK columns follow top-to-bottom, right-to-left order, and mixed
  horizontal/vertical pages segment into coherent orientation regions before
  ordering blocks and lines. Geometric and tagged ``/Ruby`` (``/RB``/``/RT``)
  furigana runs follow the ``ruby`` option (``"omit"``, ``"parenthesize"``, or
  ``"keep"``).
- Complete bordered tables become Markdown tables in page reading order.
  Conservative borderless detection remains opt-in.
"""

from __future__ import annotations

import math
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pyropdf._content import Path as _ContentPath
from pyropdf._errors import LimitError
from pyropdf._images import PageImage
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Stream
from pyropdf._text import (
    _DEFAULT_MAX_TEXT_GLYPHS,
    _DEFAULT_MAX_TEXT_SIZE,
    RubyMode,
    _validate_ruby,
    build_layout,
    build_words,
    text_options,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pyropdf._geometry import Rect
    from pyropdf._page import Page
    from pyropdf._structure_reading import PageStructureData, StructureBlock
    from pyropdf._tables import Table
    from pyropdf._text import Word

    #: :meth:`Layout.to_dict` and its lines and spans.
    TextPageDict = dict[str, Any]
    TextLine = dict[str, Any]
    TextSpan = dict[str, Any]

_BBox = tuple[float, float, float, float]
_Margins = tuple[float, float, float, float]
_MarkdownTable = tuple[_BBox, str]
_MarkdownImage = tuple[_BBox, str]
_AxisOrientation = Literal["right", "down", "left", "up"]

#: Minimum size ratio relative to body text for a heading.
_HEADING_RATIO = 1.15
#: Maximum number of heading levels.
_MAX_HEADING_LEVELS = 4
#: Leading bullets recognized when followed by whitespace, including ASCII dashes.
_SPACED_BULLETS = "・•●○◦▪‣–—-*"
#: Leading bullets recognized without whitespace, common in CJK documents.
_TIGHT_BULLETS = "・•●○◦▪‣"
#: Numbered lists in ``1.`` or ``23)`` form.
_NUMBERED = re.compile(r"^(\d{1,3})[.)][ 　]+")
#: Inline gap, relative to font size, above which adjacent spans are separate
#: words. Matches ``WORD_GAP`` in ``crates/pyropdf-engine/src/extract/text.rs``.
_WORD_GAP = 0.15
_UTF8_ONE_BYTE_MAX = 0x7F
_UTF8_TWO_BYTE_MAX = 0x7FF
_UTF8_SURROGATE_MIN = 0xD800
_UTF8_SURROGATE_MAX = 0xDFFF
_UTF8_THREE_BYTE_MAX = 0xFFFF


class MarkdownOutputLimitError(Exception):
    """Internal signal that escaped Markdown would exceed its output budget."""


def _round_size(size: float) -> float:
    return round(size, 1)


def _line_inside_bbox(line: TextLine, bbox: _BBox) -> bool:
    """Return whether a line center lies inside a display-coordinate bbox."""
    x0, y0, x1, y1 = line["bbox"]
    center_x = (x0 + x1) * 0.5
    center_y = (y0 + y1) * 0.5
    return bbox[0] <= center_x <= bbox[2] and bbox[1] <= center_y <= bbox[3]


def _word_inside_bbox(word: Word, bbox: _BBox) -> bool:
    """Return whether a word center lies inside a display-coordinate bbox."""
    center_x = (word.bbox.x0 + word.bbox.x1) * 0.5
    center_y = (word.bbox.y0 + word.bbox.y1) * 0.5
    return bbox[0] <= center_x <= bbox[2] and bbox[1] <= center_y <= bbox[3]


def _line_orientation(line: TextLine) -> _AxisOrientation:
    """Return the nearest right-angle direction of one baseline."""
    x, y = line["dir"]
    if abs(x) >= abs(y):
        return "right" if x >= 0 else "left"
    return "down" if y >= 0 else "up"


def _dominant_orientation(lines: list[TextLine]) -> _AxisOrientation:
    """Return the character-weighted baseline direction of a line collection."""
    directions: Counter[_AxisOrientation] = Counter()
    for line in lines:
        directions[_line_orientation(line)] += max(1, len(_line_text(line)))
    return directions.most_common(1)[0][0] if directions else "right"


def table_orientation(layout: TextPageDict, bbox: _BBox) -> _AxisOrientation:
    """Return table text direction, falling back to the surrounding page."""
    lines = [line for block in layout["blocks"] for line in block["lines"]]
    inside = [line for line in lines if _line_inside_bbox(line, bbox)]
    return _dominant_orientation(inside or lines)


def table_to_markdown(
    source_rows: list[list[str | None]],
    *,
    fill_empty: bool = True,
    orientation: _AxisOrientation = "right",
    cell_anchors: Sequence[int] | None = None,
    max_size: int | None = None,
) -> str:
    """Render table rows after bounded normalization of their text direction."""
    rows = [row.copy() for row in source_rows]
    if not rows:
        return ""

    if fill_empty:
        _fill_merged_cells(rows, cell_anchors)
    rows = _orient_rows(rows, orientation)
    _preflight_table_markdown(rows, max_size)

    rendered = ["| " + " | ".join(_escape_table_cell(value) for value in rows[0]) + " |"]
    rendered.append("| " + " | ".join("---" for _ in rows[0]) + " |")
    rendered.extend("| " + " | ".join(_escape_table_cell(value) for value in row) + " |" for row in rows[1:])
    return "\n".join(rendered)


def _preflight_table_markdown(rows: list[list[str | None]], max_size: int | None) -> None:
    """Reject an oversized table before allocating escaped cell strings."""
    if max_size is None:
        return

    header_columns = len(rows[0])
    output_size = len(rows)
    output_size += 4 + max(0, header_columns - 1) * 3 + header_columns * 3
    output_size += sum(4 + max(0, len(row) - 1) * 3 for row in rows)
    if output_size > max_size:
        raise MarkdownOutputLimitError

    for row in rows:
        for value in row:
            if value is None:
                continue
            for character in value:
                if character in {"\\", "|"}:
                    output_size += 2
                elif character == "\n":
                    output_size += 4
                else:
                    output_size += _utf8_character_size(character)
                if output_size > max_size:
                    raise MarkdownOutputLimitError


def _utf8_character_size(character: str) -> int:
    """Return one Unicode scalar's UTF-8 size without allocating encoded bytes."""
    code_point = ord(character)
    if code_point <= _UTF8_ONE_BYTE_MAX:
        return 1
    if code_point <= _UTF8_TWO_BYTE_MAX:
        return 2
    if _UTF8_SURROGATE_MIN <= code_point <= _UTF8_SURROGATE_MAX:
        return len(character.encode())
    if code_point <= _UTF8_THREE_BYTE_MAX:
        return 3
    return 4


def utf8_size(value: str) -> int:
    """Return a string's strict UTF-8 size without materializing encoded bytes."""
    return sum(_utf8_character_size(character) for character in value)


def _fill_merged_cells(
    rows: list[list[str | None]],
    cell_anchors: Sequence[int] | None,
) -> None:
    """Copy merged-cell anchors into covered slots in place."""
    column_count = len(rows[0])
    if cell_anchors is not None and len(cell_anchors) == len(rows) * column_count:
        for index, anchor in enumerate(cell_anchors):
            if not 0 <= anchor < len(cell_anchors):
                continue
            row_index, column_index = divmod(index, column_count)
            anchor_row, anchor_column = divmod(anchor, column_count)
            if rows[row_index][column_index] is None:
                rows[row_index][column_index] = rows[anchor_row][anchor_column]
        return

    # Compatibility fallback for callers that only have extracted values.
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            if value is not None:
                continue
            above = rows[row_index - 1][column_index] if row_index else None
            left = row[column_index - 1] if column_index else None
            row[column_index] = above if above is not None else left


def _orient_rows(
    rows: list[list[str | None]],
    orientation: _AxisOrientation,
) -> list[list[str | None]]:
    """Rotate display-coordinate rows into logical text reading order."""
    if orientation == "down":
        return [list(row) for row in zip(*rows, strict=True)][::-1]
    if orientation == "left":
        return [row[::-1] for row in rows[::-1]]
    if orientation == "up":
        return [list(row) for row in zip(*rows[::-1], strict=True)]
    return rows


def _escape_table_cell(value: str | None) -> str:
    """Escape one Markdown table cell."""
    if value is None:
        return ""
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


_KANA_MIN = 0x3041
_KANA_MAX = 0x30FE
_HALFWIDTH_KANA_MIN = 0xFF66
_HALFWIDTH_KANA_MAX = 0xFF9F
_RUBY_MAX_SIZE_RATIO = 0.68


def _is_parenthesized_ruby_span(span: TextSpan, line: TextLine) -> bool:
    """Return whether ``span`` is a smaller parenthesized Japanese ruby span on a mixed-size line."""
    text = span["text"].strip()
    if len(text) < 3 or not (text.startswith("(") and text.endswith(")")):  # noqa: PLR2004
        return False
    max_size = max((other["size"] for other in line["spans"]), default=0.0)
    if span["size"] > max_size * _RUBY_MAX_SIZE_RATIO:
        return False
    inner = text[1:-1]
    return any(
        _KANA_MIN <= ord(ch) <= _KANA_MAX or _HALFWIDTH_KANA_MIN <= ord(ch) <= _HALFWIDTH_KANA_MAX for ch in inner
    )


def collect_sizes(
    layouts: list[TextPageDict],
    excluded_bboxes: list[list[_BBox]] | None = None,
) -> Counter[float]:
    """Count non-table characters by rounded font size across page dicts."""
    counter: Counter[float] = Counter()
    for page_index, layout in enumerate(layouts):
        bboxes = [] if excluded_bboxes is None else excluded_bboxes[page_index]
        for block in layout["blocks"]:
            for line in block["lines"]:
                if any(_line_inside_bbox(line, bbox) for bbox in bboxes):
                    continue
                for span in line["spans"]:
                    if _is_parenthesized_ruby_span(span, line):
                        continue
                    counter[_round_size(span["size"])] += len(span["text"])
    return counter


def heading_levels(counter: Counter[float]) -> dict[float, int]:
    """Map heading sizes to levels 1–4, excluding body size and below."""
    if not counter:
        return {}
    body = counter.most_common(1)[0][0]
    bigger = sorted((size for size in counter if size > body * _HEADING_RATIO), reverse=True)
    return {size: min(rank + 1, _MAX_HEADING_LEVELS) for rank, size in enumerate(bigger)}


#: Scripts whose adjacent glyphs do not infer inter-word spaces in the extractor.
_NO_SPACE_WORD_SCRIPT_RANGES = (
    (0x0E00, 0x0E7F),
    (0x0E80, 0x0EFF),
    (0x1000, 0x109F),
    (0x1780, 0x17FF),
    (0x19E0, 0x19FF),
    (0xA9E0, 0xA9FF),
    (0xAA60, 0xAA7F),
)

#: Wrapped lines also join CJK punctuation, kana, Han, and width forms directly.
#: This is paragraph joining, not dictionary-based word segmentation.
_NO_SPACE_SCRIPT_RANGES = (
    *_NO_SPACE_WORD_SCRIPT_RANGES,
    (0x3000, 0x30FF),
    (0x3400, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)


def _joins_without_space(ch: str) -> bool:
    """Return whether a character joins a wrapped line without ASCII space."""
    code = ord(ch)
    return any(low <= code <= high for low, high in _NO_SPACE_SCRIPT_RANGES)


def _joins_pair_without_space(previous: str, following: str) -> bool:
    """Return whether wrapped ``previous`` and ``following`` lines join without ASCII space."""
    if not previous or not following or not _joins_without_space(following[0]):
        return False
    if _joins_without_space(previous[-1]):
        return True
    return len(previous) >= 2 and previous[-1] == ")" and _joins_without_space(previous[-2])  # noqa: PLR2004


def _join_lines(lines: list[str]) -> str:
    """Join paragraph lines according to the boundary characters' scripts."""
    parts = [lines[0]]
    previous = lines[0]
    for line in lines[1:]:
        if not _joins_pair_without_space(previous, line):
            parts.append(" ")
        parts.append(line)
        previous = line
    return "".join(parts)


#: pymupdf-compatible span flag bits: italic=2, serif=4, mono=8, bold=16.
_ITALIC = 2
_BOLD = 16


def _span_interval(span: TextSpan, direction: tuple[float, float]) -> tuple[float, float]:
    """Return a span's extent along its line's baseline direction."""
    x0, y0, x1, y1 = span["bbox"]
    projections = [x * direction[0] + y * direction[1] for x in (x0, x1) for y in (y0, y1)]
    # Glyph boxes span at least one em, or the font's larger line height.
    # Their axis-aligned bounds add this cross-axis padding at each end
    # when projected back onto a rotated baseline.
    height = max(1.0, span.get("ascender", 0.8) - span.get("descender", -0.2))
    padding = abs(direction[0] * direction[1]) * span["size"] * height
    return min(projections) + padding, max(projections) - padding


def _needs_span_gap(previous: TextSpan, span: TextSpan, line: TextLine) -> bool:
    """Return whether a word gap separates two adjacent spans of one line.

    Spans contain the inferred spaces between their own glyphs only, so this
    repeats the extractor's symmetric glyph-gap rule at span boundaries.
    """
    before = previous["text"]
    after = span["text"]
    if line["wmode"] == 1 or not before or not after or before[-1].isspace() or after[0].isspace():
        return False
    if all(any(low <= ord(ch) <= high for low, high in _NO_SPACE_WORD_SCRIPT_RANGES) for ch in (before[-1], after[0])):
        return False
    if unicodedata.bidirectional(after[0]) == "NSM":
        return False
    previous_start, previous_end = _span_interval(previous, line["dir"])
    start, end = _span_interval(span, line["dir"])
    gap = start - previous_end if start >= previous_start else previous_start - end
    return gap > max(previous["size"], span["size"], 1.0) * _WORD_GAP


def _join_spans(line: TextLine, render: Callable[[TextSpan], str]) -> str:
    """Join rendered spans, restoring word gaps that fall between two spans."""
    parts: list[str] = []
    previous: TextSpan | None = None
    for span in line["spans"]:
        is_ruby = _is_parenthesized_ruby_span(span, line)
        if not is_ruby and previous is not None and _needs_span_gap(previous, span, line):
            parts.append(" ")
        parts.append(render(span))
        if not is_ruby and not all(unicodedata.bidirectional(ch) == "NSM" for ch in span["text"]):
            previous = span
    return "".join(parts).strip()


def _line_text(line: TextLine) -> str:
    return _join_spans(line, lambda span: span["text"])


def _span_markdown(span: TextSpan) -> str:
    """Convert a span to emphasized Markdown, keeping outer whitespace outside."""
    text: str = span["text"]
    flags = span["flags"]
    bold = bool(flags & _BOLD)
    italic = bool(flags & _ITALIC)
    core = text.strip()
    if not core or not (bold or italic):
        return text
    marker = "***" if bold and italic else ("**" if bold else "*")
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return f"{lead}{marker}{core}{marker}{trail}"


def _line_markdown(line: TextLine) -> str:
    """Build a body line with bold and italic markers."""
    return _join_spans(line, _span_markdown)


def _line_size(line: TextLine) -> float:
    """Return the line's representative size by character count, excluding parenthesized ruby spans."""
    sizes: Counter[float] = Counter()
    for span in line["spans"]:
        if _is_parenthesized_ruby_span(span, line):
            continue
        sizes[_round_size(span["size"])] += len(span["text"])
    if not sizes:
        for span in line["spans"]:
            sizes[_round_size(span["size"])] += len(span["text"])
    return sizes.most_common(1)[0][0] if sizes else 0.0


def _normalize_list_item(text: str) -> str | None:
    """Normalize a bullet or numbered line, returning ``None`` otherwise."""
    if text[:1] and text[0] in _SPACED_BULLETS and text[1:2] in (" ", "　"):
        return "- " + text[2:].lstrip(" 　")
    if text[:1] and text[0] in _TIGHT_BULLETS:
        return "- " + text[1:].lstrip(" 　")
    matched = _NUMBERED.match(text)
    if matched:
        return f"{matched.group(1)}. " + text[matched.end() :]
    return None


def _table_events(
    layout: TextPageDict,
    tables: list[_MarkdownTable],
    words: list[Word] | None,
) -> tuple[dict[int, list[int]], list[set[int]], dict[int, tuple[str, str]]]:
    """Place tables at their first contained line or nearest geometric slot."""
    indexed_lines = [
        ((block_index, line_index), line)
        for block_index, block in enumerate(layout["blocks"])
        for line_index, line in enumerate(block["lines"])
    ]
    lines = [line for _, line in indexed_lines]
    line_positions = {key: index for index, (key, _) in enumerate(indexed_lines)}
    line_words: dict[tuple[int, int], list[Word]] = {}
    for word in [] if words is None else words:
        line_words.setdefault((word.block, word.line), []).append(word)
    memberships: list[set[int]] = [set() for _ in lines]
    events: dict[int, list[int]] = {}
    page_orientation = _dominant_orientation(lines)
    for table_index, (bbox, _) in enumerate(tables):
        contained = _contained_line_positions(
            bbox,
            lines,
            line_positions,
            line_words,
            use_words=words is not None,
        )
        for index in contained:
            memberships[index].add(table_index)
        position = contained[0] if contained else _geometric_event_position(lines, bbox, page_orientation)
        events.setdefault(position, []).append(table_index)
    for table_indices in events.values():
        table_indices.sort(key=lambda index: (tables[index][0][1], tables[index][0][0]))
    residuals = _residual_line_texts(indexed_lines, line_words, memberships, tables)
    return events, memberships, residuals


def _contained_line_positions(
    bbox: _BBox,
    lines: list[TextLine],
    line_positions: dict[tuple[int, int], int],
    line_words: dict[tuple[int, int], list[Word]],
    *,
    use_words: bool,
) -> list[int]:
    """Return layout positions containing text owned by one table."""
    if use_words:
        return sorted(
            {
                line_positions[key]
                for key, words in line_words.items()
                if key in line_positions and any(_word_inside_bbox(word, bbox) for word in words)
            }
        )
    return [index for index, line in enumerate(lines) if _line_inside_bbox(line, bbox)]


def _geometric_event_position(
    lines: list[TextLine],
    bbox: _BBox,
    orientation: _AxisOrientation,
) -> int:
    """Return the nearest reading-order insertion slot for a textless table."""
    has_horiz = any(_line_orientation(line) in {"right", "left"} for line in lines)
    has_vert = any(_line_orientation(line) in {"down", "up"} for line in lines)
    effective = "right" if (has_horiz and has_vert) else orientation
    inline = {
        "right": (1.0, 0.0),
        "down": (0.0, 1.0),
        "left": (-1.0, 0.0),
        "up": (0.0, -1.0),
    }[effective]
    block = (-inline[1], inline[0])
    table_center = ((bbox[0] + bbox[2]) * 0.5, (bbox[1] + bbox[3]) * 0.5)
    table_projection = table_center[0] * block[0] + table_center[1] * block[1]
    return next(
        (
            index
            for index, line in enumerate(lines)
            if (
                (line["bbox"][0] + line["bbox"][2]) * 0.5 * block[0]
                + (line["bbox"][1] + line["bbox"][3]) * 0.5 * block[1]
            )
            >= table_projection
        ),
        len(lines),
    )


def _join_words(words: list[str]) -> str:
    """Join positioned words using the same script-aware paragraph rule."""
    if not words:
        return ""
    return _join_lines(words)


def _residual_line_texts(
    indexed_lines: list[tuple[tuple[int, int], TextLine]],
    line_words: dict[tuple[int, int], list[Word]],
    memberships: list[set[int]],
    tables: list[_MarkdownTable],
) -> dict[int, tuple[str, str]]:
    """Keep words before and after tables when a line crosses a table edge."""
    residuals: dict[int, tuple[str, str]] = {}
    for position, (key, line) in enumerate(indexed_lines):
        if not memberships[position]:
            continue
        bboxes = [tables[index][0] for index in memberships[position]]
        direction = line["dir"]
        projections = [
            corner_x * direction[0] + corner_y * direction[1]
            for bbox in bboxes
            for corner_x in (bbox[0], bbox[2])
            for corner_y in (bbox[1], bbox[3])
        ]
        table_start = min(projections)
        before: list[str] = []
        after: list[str] = []
        for word in line_words.get(key, []):
            if any(_word_inside_bbox(word, bbox) for bbox in bboxes):
                continue
            center_x = (word.bbox.x0 + word.bbox.x1) * 0.5
            center_y = (word.bbox.y0 + word.bbox.y1) * 0.5
            target = before if center_x * direction[0] + center_y * direction[1] < table_start else after
            target.append(word.text)
        residuals[position] = (_join_words(before), _join_words(after))
    return residuals


def _classify_line(
    line: TextLine,
    levels: dict[float, int],
    text_override: str | None = None,
) -> tuple[str, str]:
    """Classify a non-table line as heading, list item, paragraph, or empty."""
    text = _line_text(line) if text_override is None else text_override.strip()
    if not text:
        return "", ""
    level = levels.get(_line_size(line))
    if level is not None:
        # Keep headings plain to avoid stacking # with emphasis markers.
        return "h", "#" * level + " " + text
    item = _normalize_list_item(text)
    if item is not None:
        return "li", item
    return "p", _line_markdown(line) if text_override is None else text


def _join_entries(entries: list[tuple[str, str]]) -> str:
    """Join entries while keeping consecutive list items together."""
    parts: list[str] = []
    previous = ""
    for kind, text in entries:
        if parts:
            parts.append("\n" if kind == "li" and previous == "li" else "\n\n")
        parts.append(text)
        previous = kind
    return "".join(parts)


class _MarkdownBuilder:
    """Accumulate one page without retaining entries beyond its output budget."""

    def __init__(self, max_size: int | None) -> None:
        self.entries: list[tuple[str, str]] = []
        self.paragraph: list[str] = []
        self.max_size = max_size
        self.output_size = 0
        self.paragraph_size = 0

    def _check_size(self, size: int) -> None:
        if self.max_size is not None and size > self.max_size:
            raise MarkdownOutputLimitError

    def _entry_separator_size(self, kind: str) -> int:
        if not self.entries:
            return 0
        return 1 if kind == "li" and self.entries[-1][0] == "li" else 2

    def _append_entry(self, kind: str, text: str, *, text_size: int | None = None) -> None:
        if text_size is None:
            text_size = 0 if self.max_size is None else utf8_size(text)
        next_size = self.output_size + self._entry_separator_size(kind) + text_size
        self._check_size(next_size)
        self.entries.append((kind, text))
        self.output_size = next_size

    def _append_paragraph_line(self, text: str) -> None:
        text_size = 0 if self.max_size is None else utf8_size(text)
        if self.paragraph:
            previous = self.paragraph[-1]
            if not _joins_pair_without_space(previous, text):
                text_size += 1
        pending_separator = 2 if self.entries else 0
        self._check_size(self.output_size + pending_separator + self.paragraph_size + text_size)
        self.paragraph.append(text)
        self.paragraph_size += text_size

    def flush_paragraph(self) -> None:
        """Append and clear a pending paragraph."""
        if not self.paragraph:
            return
        text = _join_lines(self.paragraph)
        self._append_entry("p", text, text_size=self.paragraph_size)
        self.paragraph.clear()
        self.paragraph_size = 0

    def append_classified(self, kind: str, text: str) -> None:
        """Append one classified line, retaining paragraph joining rules."""
        if kind == "p":
            self._append_paragraph_line(text)
        elif kind:
            self.flush_paragraph()
            self._append_entry(kind, text)

    def append_block(self, kind: str, text: str) -> None:
        """Append one complete block without merging it into an earlier paragraph."""
        if not text:
            return
        self.flush_paragraph()
        self._append_entry(kind, text)

    def append_tables(
        self,
        table_data: list[_MarkdownTable],
        table_indices: list[int],
    ) -> None:
        """Append non-empty table renderings for one reading-order event."""
        self.flush_paragraph()
        for table_index in table_indices:
            markdown = table_data[table_index][1]
            if markdown:
                self._append_entry("table", markdown)

    def render(self) -> str:
        """Return the completed page after charging all pending text."""
        self.flush_paragraph()
        return _join_entries(self.entries)


def _image_events(
    layout: TextPageDict,
    images: list[_MarkdownImage],
) -> dict[int, list[_MarkdownImage]]:
    """Place exported image links before the first line whose box starts below theirs."""
    lines = [line for block in layout["blocks"] for line in block["lines"]]
    events: dict[int, list[_MarkdownImage]] = {}
    for bbox, markdown in images:
        position = next((index for index, line in enumerate(lines) if line["bbox"][1] > bbox[1]), len(lines))
        events.setdefault(position, []).append((bbox, markdown))
    for entries in events.values():
        entries.sort(key=lambda item: (item[0][1], item[0][0]))
    return events


def _emit_ordered_media(
    builder: _MarkdownBuilder,
    table_data: list[_MarkdownTable],
    table_indices: list[int],
    images: list[_MarkdownImage],
) -> None:
    """Append pending tables and images in top-to-bottom, left-to-right display order."""
    if not images:
        if table_indices:
            builder.append_tables(table_data, table_indices)
        return
    if not table_indices:
        for _, markdown in images:
            builder.append_block("image", markdown)
        return
    ordered: list[tuple[float, float, str, str]] = [
        (table_data[index][0][1], table_data[index][0][0], "table", table_data[index][1]) for index in table_indices
    ]
    ordered.extend((bbox[1], bbox[0], "image", markdown) for bbox, markdown in images)
    ordered.sort(key=lambda item: (item[0], item[1]))
    for _, _, kind, markdown in ordered:
        if markdown:
            builder.append_block(kind, markdown)


def page_to_markdown(  # noqa: PLR0913 - page conversion with optional tables, words, images, and budget.
    layout: TextPageDict,
    levels: dict[float, int],
    tables: list[_MarkdownTable] | None = None,
    words: list[Word] | None = None,
    max_size: int | None = None,
    *,
    images: list[_MarkdownImage] | None = None,
) -> str:
    """Convert one page while bounding retained Markdown entries and output."""
    table_data = [] if tables is None else tables
    table_events, table_memberships, residual_texts = _table_events(layout, table_data, words)
    image_events = _image_events(layout, images) if images else {}
    builder = _MarkdownBuilder(max_size)
    line_position = 0
    for block in layout["blocks"]:
        for line in block["lines"]:
            pending_tables = table_events.get(line_position, [])
            pending_images = image_events.get(line_position, [])
            if table_memberships[line_position]:
                for _, img_md in pending_images:
                    builder.append_block("image", img_md)
                before, after = residual_texts.get(line_position, ("", ""))
                kind, text = _classify_line(line, levels, before)
                builder.append_classified(kind, text)
                if pending_tables:
                    builder.append_tables(table_data, pending_tables)
                kind, text = _classify_line(line, levels, after)
                builder.append_classified(kind, text)
                line_position += 1
                continue
            if pending_tables or pending_images:
                _emit_ordered_media(builder, table_data, pending_tables, pending_images)
            kind, text = _classify_line(line, levels)
            if not kind:
                line_position += 1
                continue
            builder.append_classified(kind, text)
            line_position += 1
        builder.flush_paragraph()
    _emit_ordered_media(builder, table_data, table_events.get(line_position, []), image_events.get(line_position, []))
    return builder.render()


#: The default bound on the UTF-8 of the Markdown that one call returns.
_DEFAULT_MAX_MARKDOWN_SIZE = 64 * 1024 * 1024
#: The default bound on graphic elements (paths, segments, clips, images) per page.
_DEFAULT_MAX_GRAPHICS = 65_536
#: The default cumulative byte bound on images exported by one call.
_DEFAULT_MAX_IMAGE_EXPORT_SIZE = 64 * 1024 * 1024
#: A text table that covers more than this share of a bordered table's area
#: gives way to it.
_MARKDOWN_TABLE_OVERLAP_LIMIT = 0.5
#: Top and bottom fraction of the body height inspected for repeating headers and footers.
_HEADER_FOOTER_BAND_RATIO = 0.15
#: Minimum share of distinct selected pages a top/bottom band entry must appear on to be suppressed.
_HEADER_FOOTER_MIN_SHARE = 0.4
#: Maximum character length of a running header or footer candidate.
_MAX_HEADER_FOOTER_CHARS = 200
#: Maximum candidate keys recorded per page and across a document (F6 memory bound).
_MAX_BAND_CANDIDATES_PER_PAGE = 64
_MAX_HEADER_FOOTER_KEYS = 8192
_FLOAT32_MAX = 3.4028234663852886e38
_MARGIN_PAIR_LEN = 2
_MARGIN_QUAD_LEN = 4

_WS_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\d+")
_ROMAN = r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_SMALL_ROMAN = r"(?=[clxvi])c{0,3}(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_STANDALONE_ROMAN_RE = re.compile(rf"^([\s\-–—|•·\[\]()]*)(?=[mdclxvi]){_ROMAN}([\s\-–—|•·\[\]()]*)$")
_PAGE_ROMAN_RE = re.compile(
    rf"\b(page|pg\.?|p\.?|pp\.?)\s+(?=[mdclxvi]){_ROMAN}(?:\s*(of|/)\s*(?=[mdclxvi]){_ROMAN})?\b"
)
_RATIO_ROMAN_RE = re.compile(rf"(?<!\w)(?=[mdclxvi]){_ROMAN}\s*(/|of)\s*(?=[mdclxvi]){_ROMAN}(?!\w)")
_BRACKET_ROMAN_RE = re.compile(rf"([\[(]\s*)(?=[mdclxvi]){_ROMAN}(\s*[\])])")
_DELIM_TRAIL_ROMAN_RE = re.compile(rf"([\-–—|•·]\s*)(?=[mdclxvi]){_ROMAN}(?=\s|$)")
_DELIM_LEAD_ROMAN_RE = re.compile(rf"(?:^|(?<=\s))(?=[mdclxvi]){_ROMAN}(\s*[\-–—|•·])")
_EDGE_TRAIL_SMALL_ROMAN_RE = re.compile(rf"(\s+){_SMALL_ROMAN}$")
_EDGE_LEAD_SMALL_ROMAN_RE = re.compile(rf"^({_SMALL_ROMAN})(\s+)")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportedImage:
    """One image prepared for Markdown, JSON, or chunk export.

    Passed to a callable supplied via ``images=...`` on :meth:`Document.markdown`,
    :meth:`Page.markdown`, :meth:`Document.to_json`, or :meth:`Document.to_chunks`.
    ``page`` is the 0-based page index, ``index`` the paint-order image index on
    that page, ``bbox`` its display-space rectangle, ``filename`` its stable
    suggested name (``page-{page}-image-{index}.{ext}``), ``media_type`` its
    MIME type (``"image/png"`` or ``"image/jpeg"``), ``data`` its encoded bytes,
    ``width`` and ``height`` its pixel dimensions, and ``alt_text`` any
    structure-tree ``/Alt`` description when available.
    """

    page: int
    index: int
    bbox: Rect
    filename: str
    media_type: str
    data: bytes
    width: int
    height: int
    alt_text: str | None = None


@dataclass(frozen=True, slots=True)
class _ExportConfig:
    """Normalized options shared across Markdown, JSON, and chunk exports."""

    table_strategy: Literal["lines", "text"] | None = "lines"
    use_structure: Literal["auto", "always", "never"] = "auto"
    expand_abbreviations: bool = False
    ruby: RubyMode = "omit"
    max_size: int | None = _DEFAULT_MAX_MARKDOWN_SIZE
    margins: _Margins | None = None
    ignore_headers_footers: bool = False
    images: Path | Callable[[ExportedImage], object] | None = None
    max_graphics: int | None = _DEFAULT_MAX_GRAPHICS


@dataclass(frozen=True, slots=True)
class _PageGraphics:
    """Bounded graphic inspection result cached once per unique page."""

    has_paths: bool
    images: tuple[PageImage, ...]


def _validate_margin_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"margins values must be non-negative numbers: {value!r}"
        raise TypeError(msg)
    number = float(value)
    if not (math.isfinite(number) and 0.0 <= number <= _FLOAT32_MAX):
        msg = f"margins values must be finite non-negative numbers: {value!r}"
        raise ValueError(msg)
    return number


def normalize_margins(margins: object) -> _Margins | None:
    """Validate and normalize display-space margin insets to ``(left, top, right, bottom)`` or ``None``."""
    if margins is None:
        return None
    if isinstance(margins, (int, float)) and not isinstance(margins, bool):
        inset = _validate_margin_value(margins)
        return None if inset == 0.0 else (inset, inset, inset, inset)
    if isinstance(margins, tuple):
        if len(margins) == _MARGIN_PAIR_LEN:
            horiz = _validate_margin_value(margins[0])
            vert = _validate_margin_value(margins[1])
            return None if horiz == 0.0 and vert == 0.0 else (horiz, vert, horiz, vert)
        if len(margins) == _MARGIN_QUAD_LEN:
            left, top, right, bottom = (_validate_margin_value(v) for v in margins)
            if left == 0.0 and top == 0.0 and right == 0.0 and bottom == 0.0:
                return None
            return (left, top, right, bottom)
        msg = f"margins tuple must have 2 (horizontal, vertical) or 4 (left, top, right, bottom) numbers: {margins!r}"
        raise ValueError(msg)
    msg = (
        "margins must be a non-negative number, (horizontal, vertical), "
        f"(left, top, right, bottom), or None: {margins!r}"
    )
    raise TypeError(msg)


def normalize_images(images: object) -> Path | Callable[[ExportedImage], object] | None:
    """Validate and normalize the ``images`` export target."""
    if images is None:
        return None
    if callable(images):
        return images
    if isinstance(images, (str, os.PathLike)):
        raw = os.fspath(images)
        if not isinstance(raw, str):
            msg = f"images directory path must be a str or os.PathLike[str]: {images!r}"
            raise TypeError(msg)
        if not raw:
            msg = "images directory path must not be empty"
            raise ValueError(msg)
        path = Path(raw)
        if path.exists() and not path.is_dir():
            msg = f"images destination is not a directory: {path}"
            raise ValueError(msg)
        return path
    msg = f"images must be a directory path, a callable, or None: {images!r}"
    raise TypeError(msg)


def _page_body_clip(page: Page, margins: _Margins | None) -> tuple[_BBox | None, bool]:
    """Return ``(clip_bbox, body_empty)`` in display coordinates for ``page``."""
    if margins is None:
        return None, False
    _, width, height = page._geometry()  # noqa: SLF001
    left, top, right, bottom = margins
    x0, y0, x1, y1 = left, top, width - right, height - bottom
    if x0 >= x1 or y0 >= y1:
        return None, True
    return (x0, y0, x1, y1), False


def _rect_center_inside(rect: Rect, bbox: _BBox) -> bool:
    """Return whether ``rect``'s center lies inside display-coordinate ``bbox``."""
    center_x = (rect.x0 + rect.x1) * 0.5
    center_y = (rect.y0 + rect.y1) * 0.5
    return bbox[0] <= center_x <= bbox[2] and bbox[1] <= center_y <= bbox[3]


def _rects_overlap(left: Rect, right: Rect) -> bool:
    """Return whether two display-space rectangles have positive intersection area."""
    return min(left.x1, right.x1) > max(left.x0, right.x0) and min(left.y1, right.y1) > max(left.y0, right.y0)


def _is_zero_rect(rect: Rect) -> bool:
    return rect.x0 == 0.0 and rect.y0 == 0.0 and rect.x1 == 0.0 and rect.y1 == 0.0


def _filter_structure_blocks_by_margins(
    blocks: list[StructureBlock],
    body_clip: _BBox | None,
    *,
    body_empty: bool,
) -> list[StructureBlock]:
    """Exclude structure blocks whose display-space center falls outside ``body_clip``."""
    if body_empty:
        return []
    if body_clip is None:
        return blocks
    return [block for block in blocks if _is_zero_rect(block.bbox) or _rect_center_inside(block.bbox, body_clip)]


def _filter_images_by_margins(
    page_images: Sequence[PageImage],
    body_clip: _BBox | None,
    *,
    body_empty: bool,
) -> list[PageImage]:
    """Keep page images whose display-space center falls inside ``body_clip``."""
    if body_empty:
        return []
    if body_clip is None:
        return list(page_images)
    return [img for img in page_images if _rect_center_inside(img.item.bbox, body_clip)]


class _GraphicsCache:
    """Cache bounded graphic inspection results once per unique page index."""

    __slots__ = ("_entries", "_max_graphics", "_need_images")

    def __init__(self, *, max_graphics: int | None, need_images: bool) -> None:
        self._max_graphics = max_graphics
        self._need_images = need_images
        self._entries: dict[int, _PageGraphics] = {}

    def inspect(self, page: Page) -> _PageGraphics:
        cached = self._entries.get(page.index)
        if cached is not None:
            return cached
        content = page.content(
            text_runs=False,
            paths=True,
            images=True,
            annotations=True,
            max_elements=self._max_graphics,
        )
        has_paths = any(isinstance(item, _ContentPath) for item in content.items)
        imgs = (
            tuple(
                PageImage(page=page, index=idx, item=item, annotations=True) for idx, item in enumerate(content.images)
            )
            if self._need_images
            else ()
        )
        entry = _PageGraphics(has_paths=has_paths, images=imgs)
        self._entries[page.index] = entry
        return entry


def _normalize_band_text(text: str) -> str:
    """Normalize a top/bottom band string for repeated header/footer detection."""
    norm = _WS_RE.sub(" ", text.strip()).lower()
    if not norm or len(norm) > _MAX_HEADER_FOOTER_CHARS:
        return ""
    standalone = _STANDALONE_ROMAN_RE.match(norm)
    if standalone is not None:
        return f"{standalone.group(1)}#{standalone.group(2)}"
    norm = _PAGE_ROMAN_RE.sub(
        lambda m: f"{m.group(1)} # {m.group(2)} #" if m.group(2) is not None else f"{m.group(1)} #",
        norm,
    )
    norm = _RATIO_ROMAN_RE.sub(lambda m: f"# {m.group(1)} #", norm)
    norm = _BRACKET_ROMAN_RE.sub(r"\1#\2", norm)
    norm = _DELIM_TRAIL_ROMAN_RE.sub(r"\1#", norm)
    norm = _DELIM_LEAD_ROMAN_RE.sub(r"#\1", norm)
    norm = _EDGE_TRAIL_SMALL_ROMAN_RE.sub(r"\1#", norm)
    norm = _EDGE_LEAD_SMALL_ROMAN_RE.sub(lambda m: m.group(0) if m.group(1) == "i" else f"#{m.group(2)}", norm)
    return _DIGITS_RE.sub("#", norm)


def _band_for_bbox(bbox: _BBox, y_min: float, y_max: float) -> Literal["top", "bottom"] | None:
    """Return ``"top"`` or ``"bottom"`` if ``bbox``'s vertical center lies in an edge band."""
    span = y_max - y_min
    if span <= 0.0:
        return None
    center_y = (bbox[1] + bbox[3]) * 0.5
    if center_y <= y_min + span * _HEADER_FOOTER_BAND_RATIO:
        return "top"
    if center_y >= y_max - span * _HEADER_FOOTER_BAND_RATIO:
        return "bottom"
    return None


def _band_bounds(height: float, body_clip: _BBox | None) -> tuple[float, float]:
    return (body_clip[1], body_clip[3]) if body_clip is not None else (0.0, height)


@dataclass(frozen=True, slots=True)
class _HeaderFooterRule:
    """Normalized top and bottom band keys to suppress as running headers and footers."""

    top_keys: frozenset[str] = frozenset()
    bottom_keys: frozenset[str] = frozenset()

    @property
    def is_empty(self) -> bool:
        return not self.top_keys and not self.bottom_keys

    def matches(self, text: str, bbox: _BBox, y_min: float, y_max: float) -> bool:
        band = _band_for_bbox(bbox, y_min, y_max)
        if band is None:
            return False
        keys = self.top_keys if band == "top" else self.bottom_keys
        if not keys:
            return False
        norm = _normalize_band_text(text)
        return bool(norm) and norm in keys

    def filter_layout(
        self,
        layout: TextPageDict,
        words: list[Word],
        y_min: float,
        y_max: float,
        table_bboxes: Sequence[_BBox],
    ) -> tuple[TextPageDict, list[Word]]:
        """Drop suppressed header/footer lines outside tables and remap word indices."""
        if self.is_empty:
            return layout, words
        kept_blocks: list[dict[str, Any]] = []
        index_map: dict[tuple[int, int], tuple[int, int]] = {}
        changed = False
        for old_b, block in enumerate(layout["blocks"]):
            kept_lines: list[TextLine] = []
            for old_l, line in enumerate(block["lines"]):
                bbox: _BBox = tuple(line["bbox"])
                inside_table = any(_line_inside_bbox(line, tb) for tb in table_bboxes)
                if not inside_table and self.matches(_line_text(line), bbox, y_min, y_max):
                    changed = True
                    continue
                index_map[(old_b, old_l)] = (len(kept_blocks), len(kept_lines))
                kept_lines.append(line)
            if kept_lines:
                kept_blocks.append({**block, "lines": kept_lines})
        if not changed:
            return layout, words
        new_layout: TextPageDict = {**layout, "blocks": kept_blocks}
        remapped_words = [
            replace(word, block=mapped[0], line=mapped[1])
            for word in words
            if (mapped := index_map.get((word.block, word.line))) is not None
        ]
        return new_layout, remapped_words

    def filter_structure_blocks(
        self,
        blocks: list[StructureBlock],
        y_min: float,
        y_max: float,
    ) -> list[StructureBlock]:
        """Drop non-table, non-image structure blocks matching a repeated header or footer."""
        if self.is_empty:
            return blocks
        kept: list[StructureBlock] = []
        for block in blocks:
            if block.kind in {"table", "image"} or _is_zero_rect(block.bbox):
                kept.append(block)
                continue
            bbox = (block.bbox.x0, block.bbox.y0, block.bbox.x1, block.bbox.y1)
            if not self.matches(block.text, bbox, y_min, y_max):
                kept.append(block)
        return kept


def _collect_band_keys_from_layout(
    layout: TextPageDict,
    y_min: float,
    y_max: float,
    table_bboxes: Sequence[_BBox],
) -> tuple[set[str], set[str]]:
    top: set[str] = set()
    bottom: set[str] = set()
    for block in layout["blocks"]:
        for line in block["lines"]:
            if len(top) + len(bottom) >= _MAX_BAND_CANDIDATES_PER_PAGE:
                return top, bottom
            if any(_line_inside_bbox(line, tb) for tb in table_bboxes):
                continue
            band = _band_for_bbox(tuple(line["bbox"]), y_min, y_max)
            if band is None:
                continue
            norm = _normalize_band_text(_line_text(line))
            if norm:
                (top if band == "top" else bottom).add(norm)
    return top, bottom


def _collect_band_keys_from_blocks(
    blocks: Sequence[StructureBlock],
    y_min: float,
    y_max: float,
) -> tuple[set[str], set[str]]:
    top: set[str] = set()
    bottom: set[str] = set()
    for block in blocks:
        if len(top) + len(bottom) >= _MAX_BAND_CANDIDATES_PER_PAGE:
            return top, bottom
        if block.kind in {"table", "image"} or _is_zero_rect(block.bbox):
            continue
        bbox = (block.bbox.x0, block.bbox.y0, block.bbox.x1, block.bbox.y1)
        band = _band_for_bbox(bbox, y_min, y_max)
        if band is None:
            continue
        norm = _normalize_band_text(block.text)
        if norm:
            (top if band == "top" else bottom).add(norm)
    return top, bottom


def _update_band_counter(counter: Counter[str], keys: set[str]) -> None:
    for key in keys:
        if key in counter or len(counter) < _MAX_HEADER_FOOTER_KEYS:
            counter[key] += 1


def _detect_headers_footers(
    pages: Sequence[Page],
    config: _ExportConfig,
    graphics: _GraphicsCache,
    structure_pages: dict[int, PageStructureData] | None,
) -> _HeaderFooterRule:
    """Detect repeated top/bottom band entries across distinct selected pages."""
    if not config.ignore_headers_footers:
        return _HeaderFooterRule()
    unique_pages: list[Page] = []
    seen_indices: set[int] = set()
    for page in pages:
        if page.index not in seen_indices:
            seen_indices.add(page.index)
            unique_pages.append(page)
    if len(unique_pages) < _MARGIN_PAIR_LEN:
        return _HeaderFooterRule()

    top_counts: Counter[str] = Counter()
    bottom_counts: Counter[str] = Counter()
    for page in unique_pages:
        body_clip, body_empty = _page_body_clip(page, config.margins)
        if body_empty:
            continue
        data = structure_pages[page.index] if structure_pages is not None else None
        if data is not None and data.blocks is not None:
            blocks = _filter_structure_blocks_by_margins(data.blocks, body_clip, body_empty=False)
            y_min, y_max = _band_bounds(data.height, body_clip)
            top, bottom = _collect_band_keys_from_blocks(blocks, y_min, y_max)
        else:
            layout = _fallback_or_clipped_layout(page, data, body_clip, body_empty=False, ruby=config.ruby)[0]
            page_gfx = graphics.inspect(page)
            tables = _page_tables(page, config.table_strategy, clip=body_clip, has_paths=page_gfx.has_paths)
            table_bboxes = [(t.bbox.x0, t.bbox.y0, t.bbox.x1, t.bbox.y1) for t in tables]
            y_min, y_max = _band_bounds(layout["height"], body_clip)
            top, bottom = _collect_band_keys_from_layout(layout, y_min, y_max, table_bboxes)
        _update_band_counter(top_counts, top)
        _update_band_counter(bottom_counts, bottom)

    threshold = max(2, math.ceil(len(unique_pages) * _HEADER_FOOTER_MIN_SHARE))
    return _HeaderFooterRule(
        top_keys=frozenset(key for key, count in top_counts.items() if count >= threshold),
        bottom_keys=frozenset(key for key, count in bottom_counts.items() if count >= threshold),
    )


def _passthrough_jpeg(page_image: PageImage) -> bytes | None:
    """Return raw JPEG bytes when ``page_image`` is a plain RGB/Gray ``DCTDecode`` image."""
    item = page_image.item
    if item.is_stencil or item.mask is not None or item.soft_mask is not None or item.xref == 0:
        return None
    if item.colorspace not in {"DeviceRGB", "DeviceGray"}:
        return None
    stream = page_image.page.document.objects.get(item.xref)
    if not isinstance(stream, Stream) or stream.get("Decode") is not None:
        return None
    stored = page_image.stored()
    if stored is None or stored.filter != "DCTDecode" or not stored.data.startswith(b"\xff\xd8"):
        return None
    return stored.data


class _ImageExporter:
    """Stage bounded image exports for directory writing or invoke a user callback."""

    __slots__ = ("_callback", "_dir", "_exported_refs", "_limit", "_memory_limited", "_staged", "_used")

    def __init__(
        self,
        target: Path | Callable[[ExportedImage], object] | None,
        *,
        memory_limit: int | None,
    ) -> None:
        self._dir = target if isinstance(target, Path) else None
        self._callback = target if callable(target) else None
        self._memory_limited = memory_limit is not None and memory_limit < _DEFAULT_MAX_IMAGE_EXPORT_SIZE
        self._limit = (
            _DEFAULT_MAX_IMAGE_EXPORT_SIZE
            if memory_limit is None
            else min(_DEFAULT_MAX_IMAGE_EXPORT_SIZE, memory_limit)
        )
        self._used = 0
        self._staged: dict[str, bytes] = {}
        self._exported_refs: dict[tuple[int, int], str] = {}

    @property
    def active(self) -> bool:
        return self._dir is not None or self._callback is not None

    def _fail_budget(self) -> None:
        if self._memory_limited:
            code = "memory_size"
            msg = f"exported image data exceeds the {self._limit}-byte memory limit"
            raise LimitError(code, msg)
        code = "export_output_size"
        msg = f"exported image data exceeds the {self._limit}-byte limit"
        raise LimitError(code, msg)

    def _encode(self, page_image: PageImage) -> tuple[str, str, bytes, int, int]:
        remaining = self._limit - self._used
        if remaining <= 0:
            self._fail_budget()
        jpeg = _passthrough_jpeg(page_image)
        if jpeg is not None:
            if len(jpeg) > remaining:
                self._fail_budget()
            return "jpg", "image/jpeg", jpeg, page_image.item.width, page_image.item.height
        pixmap = page_image.pixmap()
        try:
            data = pixmap.to_bytes("png", max_size=remaining)
        except LimitError as error:
            if error.code == "pixmap_output_size" and self._memory_limited:
                self._fail_budget()
            raise
        if len(data) > remaining:
            self._fail_budget()
        return "png", "image/png", data, pixmap.width, pixmap.height

    def export(self, page_image: PageImage, *, alt_text: str | None = None) -> str | None:
        """Export one ``PageImage`` and return its Markdown destination or ``None``."""
        if not self.active:
            return None
        key = (page_image.page.index, page_image.index)
        if self._dir is not None and key in self._exported_refs:
            return self._exported_refs[key]
        ext, media_type, data, width, height = self._encode(page_image)
        self._used += len(data)
        filename = f"page-{page_image.page.index}-image-{page_image.index}.{ext}"
        if self._dir is not None:
            self._staged[filename] = data
            self._exported_refs[key] = filename
            return filename
        assert self._callback is not None  # noqa: S101
        record = ExportedImage(
            page=page_image.page.index,
            index=page_image.index,
            bbox=page_image.item.bbox,
            filename=filename,
            media_type=media_type,
            data=data,
            width=width,
            height=height,
            alt_text=alt_text,
        )
        ref = self._callback(record)
        if ref is None:
            return None
        if not isinstance(ref, str):
            msg = f"images callback must return str or None, got {type(ref).__name__}"
            raise TypeError(msg)
        if "\n" in ref or "\r" in ref:
            msg = "images callback must return a single-line str or None"
            raise ValueError(msg)
        return ref

    def commit(self) -> None:
        """Write all staged directory exports atomically after the export succeeds."""
        if self._dir is None or not self._staged:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        for filename, data in self._staged.items():
            (self._dir / filename).write_bytes(data)


def _match_figure_images(
    block: StructureBlock,
    candidates: Sequence[PageImage],
    claimed: set[int],
) -> list[PageImage]:
    if block.mcids:
        mcid_set = set(block.mcids)
        by_mcid = [
            img
            for img in candidates
            if img.index not in claimed
            and any(mark.mcid in mcid_set for mark in img.item.context.marked_content if mark.mcid is not None)
        ]
        if by_mcid:
            return by_mcid
    if _is_zero_rect(block.bbox):
        return []
    return [img for img in candidates if img.index not in claimed and _rects_overlap(block.bbox, img.item.bbox)]


def _attach_structure_images(
    blocks: list[StructureBlock],
    page_images: Sequence[PageImage],
    exporter: _ImageExporter,
) -> list[StructureBlock]:
    """Export raster images matched to structure ``Figure`` blocks and attach their destinations."""
    if not exporter.active or not page_images:
        return blocks
    from pyropdf._structure_reading import format_markdown_image  # noqa: PLC0415

    candidates = [
        img for img in page_images if not any(mark.tag == "Artifact" for mark in img.item.context.marked_content)
    ]
    if not candidates:
        return blocks
    claimed: set[int] = set()
    updated: list[StructureBlock] = []
    for block in blocks:
        if block.kind != "image":
            updated.append(block)
            continue
        matched = _match_figure_images(block, candidates, claimed)
        if not matched:
            updated.append(block)
            continue
        alt = block.alt_text if block.alt_text is not None else block.text
        alt_arg = block.alt_text if block.alt_text is not None else (block.text or None)
        emitted = False
        for img in matched:
            claimed.add(img.index)
            ref = exporter.export(img, alt_text=alt_arg)
            if ref is None:
                continue
            md = format_markdown_image(alt, ref)
            if not emitted:
                updated.append(replace(block, image=ref, markdown=md))
                emitted = True
            else:
                updated.append(replace(block, bbox=img.item.bbox, image=ref, markdown=md))
        if not emitted:
            updated.append(block)
    return updated


def markdown_output_limit_error(max_size: int) -> LimitError:
    """Build the error of every Markdown conversion that exceeds ``max_size``."""
    return LimitError(
        "markdown_output_size",
        f"Markdown output exceeds the {max_size}-byte UTF-8 limit",
    )


def _table_overlap_ratio(left: Rect, right: Rect) -> float:
    """Return the intersection area divided by the smaller table area."""
    width = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    height = max(0.0, min(left.y1, right.y1) - max(left.y0, right.y0))
    smaller_area = min(left.width * left.height, right.width * right.height)
    return 0.0 if smaller_area <= 0.0 else width * height / smaller_area


def _page_tables(
    page: Page,
    strategy: Literal["lines", "text"] | None,
    *,
    clip: _BBox | None = None,
    body_empty: bool = False,
    has_paths: bool = True,
) -> list[Table]:
    """Return the bordered tables, with ``"text"`` extended by the text tables that overlap none."""
    if strategy is None or body_empty:
        return []
    tables = page.tables(strategy="lines", clip=clip) if has_paths else []
    if strategy == "text":
        for candidate in page.tables(strategy="text", clip=clip):
            if all(
                _table_overlap_ratio(candidate.bbox, table.bbox) < _MARKDOWN_TABLE_OVERLAP_LIMIT for table in tables
            ):
                tables.append(candidate)
    tables.sort(key=lambda table: (table.bbox.y0, table.bbox.x0))
    return tables


def _table_output(
    layout: TextPageDict,
    tables: list[Table],
    *,
    max_size: int | None,
    remaining_size: int | None,
) -> list[_MarkdownTable]:
    """Render the tables of a page without keeping more than the remaining budget."""
    rendered: list[_MarkdownTable] = []
    rendered_bytes = 0
    for table in tables:
        bbox = (table.bbox.x0, table.bbox.y0, table.bbox.x1, table.bbox.y1)
        table_budget = None if remaining_size is None else max(0, remaining_size - rendered_bytes)
        try:
            table_markdown = table_to_markdown(
                [list(row) for row in table.rows],
                orientation=table_orientation(layout, bbox),
                cell_anchors=table._anchors,  # noqa: SLF001
                max_size=table_budget,
            )
        except MarkdownOutputLimitError:
            raise markdown_output_limit_error(max_size or 0) from None
        rendered_bytes += utf8_size(table_markdown)
        rendered.append((bbox, table_markdown))
    return rendered


def _page_layout(page: Page, *, ruby: RubyMode = "omit") -> tuple[TextPageDict, list[Word]]:
    """Return the layout dictionary and the words of a page with the default text options."""
    options = text_options(
        clip=None,
        clipped=False,
        ligatures=True,
        whitespace=True,
        dehyphenate=False,
        unmapped="skip",
        ruby=ruby,
        max_glyphs=_DEFAULT_MAX_TEXT_GLYPHS,
        max_size=_DEFAULT_MAX_TEXT_SIZE,
    )
    width, height, blocks, _ = page._layout(  # noqa: SLF001
        options, chars=False, max_glyphs=_DEFAULT_MAX_TEXT_GLYPHS, max_size=_DEFAULT_MAX_TEXT_SIZE
    )
    return build_layout(width, height, blocks).to_dict(), build_words(blocks)


def _page_layout_clipped(
    page: Page,
    clip: _BBox | None,
    *,
    body_empty: bool = False,
    ruby: RubyMode = "omit",
) -> tuple[TextPageDict, list[Word]]:
    """Return the layout dictionary and words for ``page`` clipped to ``clip``."""
    if body_empty:
        _, width, height = page._geometry()  # noqa: SLF001
        return {"width": width, "height": height, "blocks": []}, []
    if clip is None:
        return _page_layout(page) if ruby == "omit" else _page_layout(page, ruby=ruby)
    options = text_options(
        clip=clip,
        clipped=False,
        ligatures=True,
        whitespace=True,
        dehyphenate=False,
        unmapped="skip",
        ruby=ruby,
        max_glyphs=_DEFAULT_MAX_TEXT_GLYPHS,
        max_size=_DEFAULT_MAX_TEXT_SIZE,
    )
    width, height, blocks, _ = page._layout(  # noqa: SLF001
        options, chars=False, max_glyphs=_DEFAULT_MAX_TEXT_GLYPHS, max_size=_DEFAULT_MAX_TEXT_SIZE
    )
    return build_layout(width, height, blocks).to_dict(), build_words(blocks)


def _fallback_or_clipped_layout(
    page: Page,
    data: PageStructureData | None,
    body_clip: _BBox | None,
    *,
    body_empty: bool,
    ruby: RubyMode = "omit",
) -> tuple[TextPageDict, list[Word]]:
    """Reuse cached structure fallback layout when unclipped and ruby matches, else extract layout."""
    if (
        not body_empty
        and body_clip is None
        and ruby == "keep"
        and data is not None
        and data.layout is not None
        and data.words is not None
    ):
        return data.layout, data.words
    return _page_layout_clipped(page, body_clip, body_empty=body_empty, ruby=ruby)


def validate_options(  # noqa: PLR0913 - validates all shared retrieval export options.
    table_strategy: object,
    max_size: object,
    *,
    use_structure: object = "auto",
    expand_abbreviations: object = False,
    ruby: object = "omit",
    margins: object = None,
    ignore_headers_footers: object = False,
    images: object = None,
    max_graphics: object = _DEFAULT_MAX_GRAPHICS,
) -> None:
    """Refuse options that a Markdown or structured export conversion does not take."""
    if table_strategy not in {"lines", "text", None}:
        msg = f"table_strategy must be 'lines', 'text', or None: {table_strategy!r}"
        raise ValueError(msg)
    if use_structure not in {"auto", "always", "never"}:
        msg = f"use_structure must be 'auto', 'always', or 'never': {use_structure!r}"
        raise ValueError(msg)
    if not isinstance(expand_abbreviations, bool):
        msg = f"expand_abbreviations must be True or False: {expand_abbreviations!r}"
        raise TypeError(msg)
    _validate_ruby(ruby)
    if not isinstance(ignore_headers_footers, bool):
        msg = f"ignore_headers_footers must be True or False: {ignore_headers_footers!r}"
        raise TypeError(msg)
    _validate_optional_positive_int("max_size", max_size)
    _validate_optional_positive_int("max_graphics", max_graphics)
    normalize_margins(margins)
    normalize_images(images)


def _make_config(  # noqa: PLR0913 - constructs internal normalized export config.
    *,
    table_strategy: Literal["lines", "text"] | None,
    max_size: int | None,
    use_structure: Literal["auto", "always", "never"],
    expand_abbreviations: bool,
    ruby: RubyMode = "omit",
    margins: float | tuple[float, float] | tuple[float, float, float, float] | None,
    ignore_headers_footers: bool,
    images: str | os.PathLike[str] | Callable[[ExportedImage], str | None] | None,
    max_graphics: int | None,
) -> _ExportConfig:
    return _ExportConfig(
        table_strategy=table_strategy,
        use_structure=use_structure,
        expand_abbreviations=expand_abbreviations,
        ruby=ruby,
        max_size=max_size,
        margins=normalize_margins(margins),
        ignore_headers_footers=ignore_headers_footers,
        images=normalize_images(images),
        max_graphics=max_graphics,
    )


def _structure_page_markdown(
    blocks: Sequence[StructureBlock],
    *,
    max_size: int | None,
    page_budget: int | None,
) -> str:
    """Render one page's structure blocks to Markdown under ``page_budget`` UTF-8 bytes."""
    builder = _MarkdownBuilder(None if page_budget is None else max(0, page_budget))
    try:
        for block in blocks:
            if block.kind == "table":
                if not block.rows:
                    continue
                sep = builder._entry_separator_size("table")  # noqa: SLF001
                remaining = None if page_budget is None else max(0, page_budget - builder.output_size - sep)
                rendered_table = table_to_markdown(
                    [list(row) for row in block.rows],
                    cell_anchors=block.cell_anchors,
                    max_size=remaining,
                )
                if rendered_table:
                    builder.append_block("table", rendered_table)
                continue
            if not block.markdown:
                continue
            entry_kind = "li" if block.kind == "list_item" else block.kind
            builder.append_block(entry_kind, block.markdown)
        return builder.render()
    except MarkdownOutputLimitError:
        raise markdown_output_limit_error(max_size or 0) from None


def _export_geometric_images(
    page_images: Sequence[PageImage],
    exporter: _ImageExporter,
) -> list[_MarkdownImage]:
    if not exporter.active or not page_images:
        return []
    from pyropdf._structure_reading import format_markdown_image  # noqa: PLC0415

    links: list[_MarkdownImage] = []
    for img in page_images:
        ref = exporter.export(img)
        if ref is not None:
            bbox = (img.item.bbox.x0, img.item.bbox.y0, img.item.bbox.x1, img.item.bbox.y1)
            links.append((bbox, format_markdown_image("", ref)))
    return links


def _render_geometric_page(  # noqa: PLR0913 - renders one geometric page with controls and budget.
    page: Page,
    *,
    config: _ExportConfig,
    graphics: _GraphicsCache,
    hf_rule: _HeaderFooterRule,
    exporter: _ImageExporter,
    levels: dict[float, int],
    data: PageStructureData | None,
    page_budget: int | None,
) -> str:
    body_clip, body_empty = _page_body_clip(page, config.margins)
    page_gfx = graphics.inspect(page)
    if body_empty:
        return ""
    layout, words = _fallback_or_clipped_layout(page, data, body_clip, body_empty=False, ruby=config.ruby)
    tables = _page_tables(page, config.table_strategy, clip=body_clip, has_paths=page_gfx.has_paths)
    table_bboxes = [(t.bbox.x0, t.bbox.y0, t.bbox.x1, t.bbox.y1) for t in tables]
    y_min, y_max = _band_bounds(layout["height"], body_clip)
    layout, words = hf_rule.filter_layout(layout, words, y_min, y_max, table_bboxes)
    markdown_tables = _table_output(layout, tables, max_size=config.max_size, remaining_size=page_budget)
    kept_images = _filter_images_by_margins(page_gfx.images, body_clip, body_empty=False)
    image_links = _export_geometric_images(kept_images, exporter)
    try:
        return page_to_markdown(
            layout,
            levels,
            markdown_tables,
            words if tables else None,
            max_size=None if page_budget is None else max(0, page_budget),
            images=image_links or None,
        )
    except MarkdownOutputLimitError:
        raise markdown_output_limit_error(config.max_size or 0) from None


def _collect_geometric_heading_levels(
    pages: Sequence[Page],
    config: _ExportConfig,
    graphics: _GraphicsCache,
    hf_rule: _HeaderFooterRule,
    structure_pages: dict[int, PageStructureData] | None,
) -> dict[float, int]:
    """Infer geometric heading levels across non-structured pages after applying controls."""
    size_counts: Counter[float] = Counter()
    for page in pages:
        data = structure_pages[page.index] if structure_pages is not None else None
        if data is not None and data.blocks is not None:
            continue
        body_clip, body_empty = _page_body_clip(page, config.margins)
        page_gfx = graphics.inspect(page)
        if body_empty:
            continue
        layout, words = _fallback_or_clipped_layout(page, data, body_clip, body_empty=False, ruby=config.ruby)
        tables = _page_tables(page, config.table_strategy, clip=body_clip, has_paths=page_gfx.has_paths)
        table_bboxes = [(t.bbox.x0, t.bbox.y0, t.bbox.x1, t.bbox.y1) for t in tables]
        y_min, y_max = _band_bounds(layout["height"], body_clip)
        layout, _ = hf_rule.filter_layout(layout, words, y_min, y_max, table_bboxes)
        size_counts.update(collect_sizes([layout], [table_bboxes]))
    return heading_levels(size_counts)


def _render_structure_page_with_controls(  # noqa: PLR0913 - renders one structure page with controls and budget.
    page: Page,
    data: PageStructureData,
    *,
    config: _ExportConfig,
    graphics: _GraphicsCache,
    hf_rule: _HeaderFooterRule,
    exporter: _ImageExporter,
    page_budget: int | None,
) -> str:
    assert data.blocks is not None  # noqa: S101
    body_clip, body_empty = _page_body_clip(page, config.margins)
    page_gfx = graphics.inspect(page)
    if body_empty:
        return ""
    blocks = _filter_structure_blocks_by_margins(data.blocks, body_clip, body_empty=False)
    y_min, y_max = _band_bounds(data.height, body_clip)
    blocks = hf_rule.filter_structure_blocks(blocks, y_min, y_max)
    kept_images = _filter_images_by_margins(page_gfx.images, body_clip, body_empty=False)
    blocks = _attach_structure_images(blocks, kept_images, exporter)
    return _structure_page_markdown(blocks, max_size=config.max_size, page_budget=page_budget)


def pages_markdown(  # noqa: PLR0913 - keyword-only retrieval export options.
    pages: Sequence[Page],
    *,
    table_strategy: Literal["lines", "text"] | None,
    max_size: int | None,
    use_structure: Literal["auto", "always", "never"] = "auto",
    expand_abbreviations: bool = False,
    ruby: RubyMode = "omit",
    margins: float | tuple[float, float] | tuple[float, float, float, float] | None = None,
    ignore_headers_footers: bool = False,
    images: str | os.PathLike[str] | Callable[[ExportedImage], str | None] | None = None,
    max_graphics: int | None = _DEFAULT_MAX_GRAPHICS,
) -> str:
    """Convert ``pages`` to Markdown, using the PDF structure tree when enabled and trustworthy.

    A page with no Markdown adds nothing; the others join with a blank line.
    """
    if not pages:
        return ""
    config = _make_config(
        table_strategy=table_strategy,
        max_size=max_size,
        use_structure=use_structure,
        expand_abbreviations=expand_abbreviations,
        ruby=ruby,
        margins=margins,
        ignore_headers_footers=ignore_headers_footers,
        images=images,
        max_graphics=max_graphics,
    )
    from pyropdf._structure_reading import extract_structure_pages  # noqa: PLC0415

    structure_pages = extract_structure_pages(
        pages,
        table_strategy=config.table_strategy,
        use_structure=config.use_structure,
        expand_abbreviations=config.expand_abbreviations,
        ruby=config.ruby,
        max_graphics=config.max_graphics,
    )
    exporter = _ImageExporter(config.images, memory_limit=pages[0].document.limits.max_memory_size)
    graphics = _GraphicsCache(max_graphics=config.max_graphics, need_images=exporter.active)
    hf_rule = _detect_headers_footers(pages, config, graphics, structure_pages)
    levels = _collect_geometric_heading_levels(pages, config, graphics, hf_rule, structure_pages)

    rendered: list[str] = []
    output_bytes = 0
    for page in pages:
        separator_bytes = 2 if rendered else 0
        page_budget = None if config.max_size is None else config.max_size - output_bytes - separator_bytes
        data = structure_pages[page.index] if structure_pages is not None else None
        if data is not None and data.blocks is not None:
            page_markdown = _render_structure_page_with_controls(
                page,
                data,
                config=config,
                graphics=graphics,
                hf_rule=hf_rule,
                exporter=exporter,
                page_budget=page_budget,
            )
        else:
            page_markdown = _render_geometric_page(
                page,
                config=config,
                graphics=graphics,
                hf_rule=hf_rule,
                exporter=exporter,
                levels=levels,
                data=data,
                page_budget=page_budget,
            )
        if not page_markdown:
            continue
        if page_budget is not None:
            page_bytes = utf8_size(page_markdown)
            if page_bytes > page_budget:
                raise markdown_output_limit_error(config.max_size or 0)
            output_bytes += separator_bytes + page_bytes
        rendered.append(page_markdown)
    exporter.commit()
    return "\n\n".join(rendered)


ExportedImage.__module__ = "pyropdf"
