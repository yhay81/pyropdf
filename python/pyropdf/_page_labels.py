"""Page labels: ``Document.page_labels`` ranges and ``Page.label`` (ISO 32000-2 12.4.2).

The store keeps each page's label when pages are inserted, moved, or
removed (``pyropdf_store::refs::labels``), so this module only reads and
replaces the catalog's ``/PageLabels`` number tree.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyropdf._errors import LimitError
from pyropdf._objects import Array, Dictionary, Name, String

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from pyropdf._document import Document

#: Ranges that one tree holds or that one assignment writes.
_MAX_PAGE_LABEL_RANGES = 4096
#: Levels of the tree below its root that a read follows.
_MAX_TREE_DEPTH = 32
#: Kids that a read follows, over the whole tree.
_MAX_TREE_EDGES = 4096
#: UTF-8 bytes of the styles and prefixes of one tree or one assignment.
_MAX_PAGE_LABEL_TEXT_SIZE = 1024 * 1024


class PageLabelStyle(enum.StrEnum):
    """How a page-label range numbers its pages (``/S``, ISO 32000-2 table 161)."""

    DECIMAL = "D"
    """1, 2, 3"""
    UPPER_ROMAN = "R"
    """I, II, III"""
    LOWER_ROMAN = "r"
    """i, ii, iii"""
    UPPER_LETTERS = "A"
    """A to Z, then AA to ZZ"""
    LOWER_LETTERS = "a"
    """a to z, then aa to zz"""


_PAGE_LABEL_STYLES = frozenset(style.value for style in PageLabelStyle)


@dataclass(frozen=True, slots=True, init=False)
class PageLabelRange:
    """The labels of the pages from ``start`` up to the next range.

    A page is labeled ``prefix`` followed by its number in ``style``,
    counting from ``first`` at ``start``; ``style=None`` labels it with the
    prefix alone.
    """

    start: int
    """The zero-based first page of the range."""
    style: PageLabelStyle | None
    prefix: str
    first: int
    """The number of the range's first page."""

    def __init__(
        self,
        start: int,
        style: PageLabelStyle | str | None = PageLabelStyle.DECIMAL,
        prefix: str = "",
        first: int = 1,
    ) -> None:
        """Validate the fields; ``style`` takes a member or its value, such as ``"r"``."""
        for name, value in (("start", start), ("first", first)):
            if isinstance(value, bool) or not isinstance(value, int):
                msg = f"{name} must be an int: {value!r}"
                raise TypeError(msg)
        if start < 0:
            msg = f"start must not be negative: {start}"
            raise ValueError(msg)
        if first < 1:
            msg = f"first must be at least 1: {first}"
            raise ValueError(msg)
        if style is not None:
            try:
                style = PageLabelStyle(style)
            except ValueError:
                msg = f"style must be a PageLabelStyle or None: {style!r}"
                raise ValueError(msg) from None
        if not isinstance(prefix, str):
            msg = f"prefix must be a str: {prefix!r}"
            raise TypeError(msg)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "style", style)
        object.__setattr__(self, "prefix", prefix)
        object.__setattr__(self, "first", first)

    def label(self, page: int) -> str:
        """Return the label of zero-based ``page``, which the range must cover."""
        number = page - self.start + self.first
        match self.style:
            case PageLabelStyle.DECIMAL:
                digits = str(number)
            case PageLabelStyle.UPPER_ROMAN:
                digits = _roman(number)
            case PageLabelStyle.LOWER_ROMAN:
                digits = _roman(number).lower()
            case PageLabelStyle.UPPER_LETTERS:
                digits = _letters(number)
            case PageLabelStyle.LOWER_LETTERS:
                digits = _letters(number).lower()
            case _:
                digits = ""
        return self.prefix + digits


_ROMAN = (
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)


def _roman(n: int) -> str:
    """Convert a positive integer to uppercase Roman numerals."""
    out = []
    for value, symbol in _ROMAN:
        count, n = divmod(n, value)
        out.append(symbol * count)
    return "".join(out)


def _letters(n: int) -> str:
    """Convert a positive integer to PDF's A..Z, AA..ZZ, AAA... form."""
    letter = chr(ord("A") + (n - 1) % 26)
    return letter * ((n - 1) // 26 + 1)


def _label(ranges: Sequence[PageLabelRange], page: int) -> str:
    """Return the label of zero-based ``page`` under sorted ``ranges``, or ``""``."""
    applicable = None
    for page_range in ranges:
        if page_range.start > page:
            break
        applicable = page_range
    return "" if applicable is None else applicable.label(page)


def _text_size(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _range(start: object, entry: object) -> tuple[PageLabelRange, int] | None:
    """Return the range of one ``/Nums`` pair and its text size, or ``None`` for a pair that is not one."""
    if isinstance(start, bool) or not isinstance(start, int) or start < 0 or not isinstance(entry, Dictionary):
        return None
    style = entry.get("S")
    prefix = entry.get("P")
    first = entry.get("St")
    text = prefix.text if isinstance(prefix, String) else ""
    # An unsupported stored style keeps its prefix without adding a number.
    page_style = str(style) if isinstance(style, Name) and style in _PAGE_LABEL_STYLES else None
    number = first if isinstance(first, int) and not isinstance(first, bool) and first >= 1 else 1
    return PageLabelRange(start, page_style, text, number), _text_size(text) + len(page_style or "")


def _nodes(root: Dictionary) -> Iterator[Dictionary]:
    """Yield the nodes of the tree at ``root`` in order, each indirect one once.

    The walk keeps an explicit stack and raises ``LimitError`` past
    ``_MAX_TREE_DEPTH`` levels or ``_MAX_TREE_EDGES`` kids.
    """
    seen = {root.xref} if root.is_indirect else set()
    stack: list[tuple[Dictionary, int]] = [(root, 0)]
    edges = 0
    while stack:
        node, depth = stack.pop()
        yield node
        kids = node.get("Kids")
        if not isinstance(kids, Array):
            continue
        if depth == _MAX_TREE_DEPTH:
            code = "page_label_tree_depth"
            msg = f"the page label tree is deeper than {_MAX_TREE_DEPTH} levels"
            raise LimitError(code, msg)
        # Reversed, so that the stack visits the kids in order.
        for at in reversed(range(len(kids))):
            edges += 1
            if edges > _MAX_TREE_EDGES:
                code = "page_label_tree_edges"
                msg = f"the page label tree has more than {_MAX_TREE_EDGES} kids"
                raise LimitError(code, msg)
            kid = kids[at]
            if not isinstance(kid, Dictionary) or (kid.is_indirect and kid.xref in seen):
                continue
            if kid.is_indirect:
                seen.add(kid.xref)
            stack.append((kid, depth + 1))


def _read_page_labels(document: Document) -> list[PageLabelRange]:
    """Return the document's page label ranges in page order, the first of each start alone.

    Past the limits of ``_nodes``, ``_MAX_PAGE_LABEL_RANGES`` ranges, or
    ``_MAX_PAGE_LABEL_TEXT_SIZE`` bytes of text, ``LimitError`` is raised
    instead of returning part of the tree.
    """
    root = document.catalog.get("PageLabels")
    if not isinstance(root, Dictionary):
        return []
    ranges: dict[int, PageLabelRange] = {}
    entries = text = 0
    for node in _nodes(root):
        nums = node.get("Nums")
        if not isinstance(nums, Array):
            continue
        for at in range(0, len(nums) - 1, 2):
            entries += 1
            if entries > _MAX_PAGE_LABEL_RANGES:
                code = "page_label_ranges"
                msg = f"the page label tree has more than {_MAX_PAGE_LABEL_RANGES} ranges"
                raise LimitError(code, msg)
            read = _range(nums[at], nums[at + 1])
            if read is None:
                continue
            page_range, size = read
            text += size
            if text > _MAX_PAGE_LABEL_TEXT_SIZE:
                msg = f"the page label tree's text is longer than {_MAX_PAGE_LABEL_TEXT_SIZE} bytes"
                code = "page_label_text_size"
                raise LimitError(code, msg)
            ranges.setdefault(page_range.start, page_range)
    return [ranges[start] for start in sorted(ranges)]


def _validated(ranges: Iterable[PageLabelRange]) -> list[PageLabelRange]:
    """Return ``ranges`` in page order, checked against the limits and ISO 32000-2 12.4.2."""
    items: list[PageLabelRange] = []
    text = 0
    for page_range in ranges:
        if len(items) == _MAX_PAGE_LABEL_RANGES:
            msg = f"page labels cannot hold more than {_MAX_PAGE_LABEL_RANGES} ranges"
            raise ValueError(msg)
        if not isinstance(page_range, PageLabelRange):
            msg = f"page labels are PageLabelRange records: {type(page_range).__name__}"
            raise TypeError(msg)
        text += _text_size(page_range.prefix)
        if text > _MAX_PAGE_LABEL_TEXT_SIZE:
            msg = f"the page label prefixes are longer than {_MAX_PAGE_LABEL_TEXT_SIZE} bytes in UTF-8"
            code = "page_label_input_size"
            raise LimitError(code, msg)
        items.append(page_range)
    starts = [page_range.start for page_range in items]
    if len(set(starts)) != len(starts):
        msg = f"page label ranges start on distinct pages: {sorted(starts)}"
        raise ValueError(msg)
    if items and min(starts) != 0:
        msg = "the first page label range must start at page 0 (ISO 32000-2 12.4.2)"
        raise ValueError(msg)
    return sorted(items, key=lambda page_range: page_range.start)


def _entry(page_range: PageLabelRange) -> dict[str, object]:
    """Return the label dictionary of ``page_range``, without its defaults."""
    entry: dict[str, object] = {}
    if page_range.style is not None:
        entry["S"] = Name(page_range.style.value)
    if page_range.prefix:
        entry["P"] = page_range.prefix
    if page_range.first != 1:
        entry["St"] = page_range.first
    return entry


def _write_page_labels(document: Document, ranges: Iterable[PageLabelRange]) -> None:
    """Replace the page labels with ``ranges``; an empty iterable removes them."""
    items = _validated(ranges)
    catalog = document.catalog
    if items:
        catalog["PageLabels"] = {"Nums": [value for item in items for value in (item.start, _entry(item))]}
    elif "PageLabels" in catalog:
        del catalog["PageLabels"]


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (PageLabelStyle, PageLabelRange):
    _public.__module__ = "pyropdf"
