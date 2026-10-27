"""The tables that :meth:`Page.tables` detects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from pyropdf import _markdown
from pyropdf._geometry import Rect
from pyropdf._limits import _validate_optional_positive_int

if TYPE_CHECKING:
    from collections.abc import Sequence

    _Box = tuple[float, float, float, float]
    #: The core's table: ``(bbox, row count, column count, row-major cells,
    #: anchor of every slot, (confidence, alignment, gutter, row gap))``, where
    #: a cell is ``(bbox, text)`` or ``None`` for a slot that a merged cell covers.
    _CoreTable = tuple[
        _Box,
        int,
        int,
        list[tuple[_Box, str] | None],
        list[int],
        tuple[float, float | None, float | None, float | None],
    ]


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
    """

    bbox: Rect
    rows: tuple[tuple[str | None, ...], ...]
    cells: tuple[tuple[Rect | None, ...], ...]
    diagnostics: TableDiagnostics
    #: The row-major index of the top-left slot of the cell that covers each
    #: slot, from the detector; Markdown fills a covered slot from it.
    _anchors: tuple[int, ...] | None = field(default=None, repr=False, compare=False)

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
    for bbox, row_count, col_count, cells, anchors, (confidence, alignment, gutter, row_gap) in raw:
        boxes = [None if cell is None else Rect(*cell[0]) for cell in cells]
        texts = [None if cell is None else cell[1] for cell in cells]
        starts = range(0, row_count * col_count, col_count)
        tables.append(
            Table(
                bbox=Rect(*bbox),
                rows=tuple(tuple(texts[start : start + col_count]) for start in starts),
                cells=tuple(tuple(boxes[start : start + col_count]) for start in starts),
                diagnostics=TableDiagnostics(
                    strategy=strategy,
                    confidence=confidence,
                    alignment_error_em=alignment,
                    minimum_gutter_em=gutter,
                    row_gap_variation_em=row_gap,
                ),
                _anchors=tuple(anchors),
            )
        )
    return tables


# The package re-exports these names; reprs, pickles, and documentation name it.
for _public in (TableDiagnostics, Table):
    _public.__module__ = "pyropdf"
