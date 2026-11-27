"""The tables that :meth:`Page.tables` detects."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from pyropdf import _markdown
from pyropdf._errors import LimitError
from pyropdf._geometry import Rect
from pyropdf._limits import _validate_optional_positive_int

if TYPE_CHECKING:
    _Box = tuple[float, float, float, float]
    #: The core's table: ``(bbox, row count, column count, row-major cells,
    #: anchor of every slot, (confidence, alignment, gutter, row gap),
    #: detected header row count)``, where a cell is ``(bbox, text)`` or
    #: ``None`` for a slot that a merged cell covers.
    _CoreTable = tuple[
        _Box,
        int,
        int,
        list[tuple[_Box, str] | None],
        list[int],
        tuple[float, float | None, float | None, float | None],
        int,
    ]
    _TableOptions = tuple[bool, _Box | None, list[float], list[float], float, float, float, int, int]

#: Maximum combined explicit guide coordinates accepted in one ``Page.tables`` call (matches ``text_rule_count``).
_MAX_EXPLICIT_LINES = 4096
#: Maximum finite point tolerance for ``snap_tolerance`` and ``join_tolerance``.
_MAX_POINT_TOLERANCE = 1000.0
#: Maximum em-scaled gutter threshold for ``min_gutter``.
_MAX_GUTTER_EM = 100.0
#: Maximum ``min_words_vertical`` (bounded by ``table_cell_count``).
_MAX_MIN_WORDS_VERTICAL = 4096
#: Maximum ``min_words_horizontal`` (matches ``table_column_count``).
_MAX_MIN_WORDS_HORIZONTAL = 32


def _validate_guide_lines(name: str, lines: Sequence[float] | None) -> list[float]:
    """Validate an optional sequence of finite display-space guide coordinates."""
    if lines is None:
        return []
    if not isinstance(lines, Sequence) or isinstance(lines, (str, bytes, bytearray)):
        msg = f"{name} must be a sequence of finite coordinates or None: {lines!r}"
        raise TypeError(msg)
    if len(lines) > _MAX_EXPLICIT_LINES:
        code = "text_rule_count"
        msg = f"{name} exceeds the {_MAX_EXPLICIT_LINES}-rule limit"
        raise LimitError(code, msg)
    result: list[float] = []
    for value in lines:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            msg = f"{name} coordinates must be real numbers: {value!r}"
            raise TypeError(msg)
        try:
            coord = float(value)
        except OverflowError as error:
            msg = f"{name} coordinates must be finite: {value!r}"
            raise ValueError(msg) from error
        if not math.isfinite(coord):
            msg = f"{name} coordinates must be finite: {value!r}"
            raise ValueError(msg)
        result.append(coord)
    return result


def _validate_tolerance(name: str, value: float, *, positive: bool, maximum: float) -> float:
    """Validate a finite geometric tolerance."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{name} must be a finite number: {value!r}"
        raise TypeError(msg)
    try:
        number = float(value)
    except OverflowError as error:
        msg = f"{name} must be finite: {value!r}"
        raise ValueError(msg) from error
    if not math.isfinite(number):
        msg = f"{name} must be finite: {value!r}"
        raise ValueError(msg)
    if (positive and number <= 0.0) or (not positive and number < 0.0):
        kind = "positive" if positive else "non-negative"
        msg = f"{name} must be {kind}: {value!r}"
        raise ValueError(msg)
    if number > maximum:
        msg = f"{name} must be at most {maximum}: {value!r}"
        raise ValueError(msg)
    return number


def _validate_min_words(name: str, value: int, *, maximum: int, limit_code: str) -> int:
    """Validate a positive integer minimum-word count within its table limit."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be an int: {value!r}"
        raise TypeError(msg)
    if value < 1:
        msg = f"{name} must be at least 1: {value!r}"
        raise ValueError(msg)
    if value > maximum:
        msg = f"{name} exceeds the {maximum} limit: {value!r}"
        raise LimitError(limit_code, msg)
    return value


def table_options(  # noqa: PLR0913 - keyword-only options of Page.tables
    *,
    strategy: Literal["lines", "text"],
    clip: _Box | None,
    horizontal_lines: Sequence[float] | None,
    vertical_lines: Sequence[float] | None,
    snap_tolerance: float,
    join_tolerance: float,
    min_gutter: float,
    min_words_vertical: int,
    min_words_horizontal: int,
) -> _TableOptions:
    """Validate and pack the options of :meth:`Page.tables` for the core."""
    if strategy not in {"lines", "text"}:
        msg = f"strategy must be 'lines' or 'text': {strategy!r}"
        raise ValueError(msg)
    h_lines = _validate_guide_lines("horizontal_lines", horizontal_lines)
    v_lines = _validate_guide_lines("vertical_lines", vertical_lines)
    if len(h_lines) + len(v_lines) > _MAX_EXPLICIT_LINES:
        code = "text_rule_count"
        msg = f"horizontal_lines and vertical_lines together exceed the {_MAX_EXPLICIT_LINES}-rule limit"
        raise LimitError(code, msg)
    snap = _validate_tolerance("snap_tolerance", snap_tolerance, positive=False, maximum=_MAX_POINT_TOLERANCE)
    join = _validate_tolerance("join_tolerance", join_tolerance, positive=False, maximum=_MAX_POINT_TOLERANCE)
    gutter = _validate_tolerance("min_gutter", min_gutter, positive=True, maximum=_MAX_GUTTER_EM)
    min_v = _validate_min_words(
        "min_words_vertical", min_words_vertical, maximum=_MAX_MIN_WORDS_VERTICAL, limit_code="table_cell_count"
    )
    min_h = _validate_min_words(
        "min_words_horizontal",
        min_words_horizontal,
        maximum=_MAX_MIN_WORDS_HORIZONTAL,
        limit_code="table_column_count",
    )
    return (strategy == "text", clip, h_lines, v_lines, snap, join, gutter, min_v, min_h)


def _normalize_header_piece(text: str | None) -> str:
    """Collapse internal whitespace and newlines in one header cell."""
    if not text:
        return ""
    return " ".join(text.split())


def _extract_headers(
    rows: tuple[tuple[str | None, ...], ...],
    header_rows: int,
    anchors: tuple[int, ...] | None,
) -> tuple[str, ...]:
    """Build per-column header names across rows ``0..header_rows``."""
    if header_rows <= 0 or not rows:
        return ()
    col_count = len(rows[0])
    if col_count == 0:
        return ()
    names: list[str] = []
    for col in range(col_count):
        parts: list[str] = []
        seen_anchors: set[int] = set()
        for row_idx in range(min(header_rows, len(rows))):
            slot = row_idx * col_count + col
            if anchors is not None and slot < len(anchors):
                anchor = anchors[slot]
                if anchor in seen_anchors:
                    continue
                seen_anchors.add(anchor)
                anchor_row, anchor_col = divmod(anchor, col_count)
                raw = rows[anchor_row][anchor_col]
            else:
                raw = rows[row_idx][col]
            piece = _normalize_header_piece(raw)
            if piece and (not parts or parts[-1] != piece):
                parts.append(piece)
        names.append(" ".join(parts))
    return tuple(names)


def _unique_column_names(raw_headers: tuple[str, ...], col_count: int) -> list[str]:
    """Turn raw header labels into unique, non-empty column names."""
    used: set[str] = set()
    names: list[str] = []
    for col in range(col_count):
        base = raw_headers[col] if col < len(raw_headers) and raw_headers[col] else f"column_{col}"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        names.append(candidate)
    return names


@dataclass(frozen=True, slots=True, kw_only=True)
class TableDiagnostics:
    """The geometric evidence for one detected table.

    ``confidence`` ranks the results of this deterministic detector from 0
    to 1; it is not a probability. The metrics in ems are present for the
    ``"text"`` strategy and ``None`` for vector grids.
    """

    strategy: Literal["lines", "text"]
    confidence: float
    alignment_error_em: float | None
    minimum_gutter_em: float | None
    row_gap_variation_em: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Table:
    """A table that :meth:`Page.tables` detected, in display space.

    ``rows`` holds the text of every cell and ``cells`` its box, both row by
    row. A merged cell keeps its text and its spanning box in its top-left
    slot; the slots it covers are ``None``.

    ``header_rows`` gives the number of detected leading header rows (``0``
    when the table appears headerless, ``1`` for a single header row, or
    ``>= 2`` for multi-row grouped headers). ``headers`` holds one normalized
    column label per column built from ``rows[:header_rows]``, or ``()`` when
    ``header_rows == 0``.
    """

    bbox: Rect
    rows: tuple[tuple[str | None, ...], ...]
    cells: tuple[tuple[Rect | None, ...], ...]
    diagnostics: TableDiagnostics
    header_rows: int = 0
    headers: tuple[str, ...] = ()
    #: The row-major index of the top-left slot of the cell that covers each
    #: slot, from the detector; Markdown and structured exports fill covered
    #: slots from it when requested.
    _anchors: tuple[int, ...] | None = field(default=None, init=False, repr=False, compare=False)

    def _export_header_count(self, header_rows: int | None) -> int:
        if header_rows is None:
            return self.header_rows
        if isinstance(header_rows, bool) or not isinstance(header_rows, int):
            msg = f"header_rows must be an int or None: {header_rows!r}"
            raise TypeError(msg)
        if header_rows < 0 or header_rows > len(self.rows):
            msg = f"header_rows must be between 0 and {len(self.rows)}: {header_rows!r}"
            raise ValueError(msg)
        return header_rows

    def _export_body_row(self, row_idx: int, col_count: int, *, fill_empty: bool) -> list[str | None]:
        row = self.rows[row_idx]
        anchors = self._anchors
        if not fill_empty or anchors is None:
            return list(row)
        filled: list[str | None] = []
        for col in range(col_count):
            value = row[col]
            if value is None:
                slot = row_idx * col_count + col
                if slot < len(anchors):
                    anchor_row, anchor_col = divmod(anchors[slot], col_count)
                    value = self.rows[anchor_row][anchor_col]
            filled.append(value)
        return filled

    def _resolved_export(
        self, *, header_rows: int | None, fill_empty: bool
    ) -> tuple[list[str], list[list[str | None]]]:
        """Validate export options and return ``(column_names, body_rows)``."""
        if not isinstance(fill_empty, bool):
            msg = f"fill_empty must be True or False: {fill_empty!r}"
            raise TypeError(msg)
        resolved_headers = self._export_header_count(header_rows)
        if not self.rows or not self.rows[0]:
            return [], []
        col_count = len(self.rows[0])
        raw_headers = (
            self.headers
            if header_rows is None and len(self.headers) == col_count
            else _extract_headers(self.rows, resolved_headers, self._anchors)
        )
        columns = _unique_column_names(raw_headers, col_count)
        body = [
            self._export_body_row(row_idx, col_count, fill_empty=fill_empty)
            for row_idx in range(resolved_headers, len(self.rows))
        ]
        return columns, body

    def to_dicts(
        self,
        *,
        header_rows: int | None = None,
        fill_empty: bool = False,
    ) -> list[dict[str, str | None]]:
        """Return each body row as a dictionary keyed by column header names.

        By default ``header_rows`` uses the detected :attr:`header_rows` count.
        Pass an explicit ``header_rows`` integer to override it (``0`` treats
        every row as data using ``column_0``, ``column_1``, ... keys). Empty
        or duplicate header names are disambiguated deterministically so no
        column is silently overwritten. With ``fill_empty=True``, slots covered
        by a merged cell repeat the anchor cell's value instead of ``None``.
        """
        columns, body = self._resolved_export(header_rows=header_rows, fill_empty=fill_empty)
        return [dict(zip(columns, row, strict=True)) for row in body]

    def to_dataframe(
        self,
        *,
        header_rows: int | None = None,
        fill_empty: bool = False,
    ) -> Any:  # noqa: ANN401 - returns optional pandas.DataFrame without a hard pandas dependency
        """Return the table as a ``pandas.DataFrame`` using optional ``pandas``.

        Uses the same ``header_rows`` and ``fill_empty`` rules as
        :meth:`to_dicts`. Raises :class:`ImportError` if ``pandas`` is not
        installed.
        """
        columns, body = self._resolved_export(header_rows=header_rows, fill_empty=fill_empty)
        try:
            import pandas as pd  # ty: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as error:
            msg = "Table.to_dataframe() requires the optional 'pandas' package; install pandas to use it"
            raise ImportError(msg) from error
        return pd.DataFrame(body, columns=columns)

    def markdown(
        self,
        *,
        fill_empty: bool = True,
        max_size: int | None = _markdown._DEFAULT_MAX_MARKDOWN_SIZE,  # noqa: SLF001
    ) -> str:
        """Return the table as Markdown, with the first row as its header.

        With ``fill_empty``, the slots that a merged cell covers repeat its
        text, since Markdown has no row or column spans. More than
        ``max_size`` bytes of UTF-8 raise :class:`LimitError` with code
        ``markdown_output_size`` before the escaped cells are built;
        ``max_size=None`` lifts the limit.
        """
        _validate_optional_positive_int("max_size", max_size)
        try:
            return _markdown.table_to_markdown(
                [list(row) for row in self.rows],
                fill_empty=fill_empty,
                cell_anchors=self._anchors,
                max_size=max_size,
            )
        except _markdown.MarkdownOutputLimitError:
            raise _markdown.markdown_output_limit_error(cast("int", max_size)) from None


def build_tables(raw: Sequence[_CoreTable], strategy: Literal["lines", "text"]) -> list[Table]:
    """Build the tables of :meth:`Page.tables` from the core's row-major tables."""
    tables = []
    for bbox, row_count, col_count, cells, anchors, (confidence, alignment, gutter, row_gap), header_rows in raw:
        boxes = [None if cell is None else Rect(*cell[0]) for cell in cells]
        texts = [None if cell is None else cell[1] for cell in cells]
        starts = range(0, row_count * col_count, col_count)
        rows = tuple(tuple(texts[start : start + col_count]) for start in starts)
        anchor_tuple = tuple(anchors)
        clamped_headers = min(max(header_rows, 0), row_count)
        headers = _extract_headers(rows, clamped_headers, anchor_tuple)
        table = Table(
            bbox=Rect(*bbox),
            rows=rows,
            cells=tuple(tuple(boxes[start : start + col_count]) for start in starts),
            diagnostics=TableDiagnostics(
                strategy=strategy,
                confidence=confidence,
                alignment_error_em=alignment,
                minimum_gutter_em=gutter,
                row_gap_variation_em=row_gap,
            ),
            header_rows=clamped_headers,
            headers=headers,
        )
        object.__setattr__(table, "_anchors", anchor_tuple)
        tables.append(table)
    return tables


# The package re-exports these names; reprs, pickles, and documentation name it.
for _public in (TableDiagnostics, Table):
    _public.__module__ = "pyropdf"
