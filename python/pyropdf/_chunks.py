"""Structured export: pages as elements with provenance, and retrieval chunks of them.

``doc.to_json()`` and ``doc.to_chunks()`` read each page as
:meth:`Document.markdown` does, through the same layout, heading sizes,
list rules, and tables, but keep each piece apart as an :class:`Element`
with the index of its page and its box in display space: a heading with its
level, a paragraph (the lines of one layout block between headings, lists,
and tables), a list item, a table with its rows and its Markdown, and an
image. Elements follow the page's reading order; an image goes before the
first element whose box starts below its own.

A :class:`Chunk` is a run of consecutive elements within a character
budget. A heading starts a new chunk, so a chunk stays in one section, and
:attr:`Chunk.section` holds the headings above it. A paragraph or list item
longer than the budget is split at white space, each piece keeping the
element's page and box; a table, an image, or a heading is never split, and
a chunk holding one that is longer than the budget is marked
:attr:`Chunk.oversized`.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from pyropdf import _markdown
from pyropdf._errors import LimitError
from pyropdf._geometry import Rect

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyropdf._markdown import TextLine, TextPageDict
    from pyropdf._page import Page
    from pyropdf._tables import Table
    from pyropdf._text import Word

#: The kind of a structured page :class:`Element`.
ElementKind: TypeAlias = Literal["heading", "paragraph", "list_item", "table", "image", "blockquote", "code"]
ElementKind.__doc__ = "The kind of a structured page :class:`Element`."

#: The characters of one chunk by default.
_DEFAULT_MAX_CHARS = 2000

#: The UTF-8 of all element text that one export returns by default (``export_output_size``).
_DEFAULT_MAX_EXPORT_SIZE = 64 * 1024 * 1024

#: The elements that a chunk never splits.
_INDIVISIBLE = frozenset({"heading", "table", "image", "code"})

_WORDS = re.compile(r"\S+\s*")


@dataclass(frozen=True, slots=True, kw_only=True)
class Element:
    """One piece of a page's structure, with where it is.

    ``text`` is a heading's, paragraph's, block quote's, or code block's text,
    a list item with its bullet as ``- `` or its number as ``1. ``, a table's
    Markdown, or an image's alternate description when tagged (empty
    otherwise). ``page`` is the page's index and ``bbox`` the box in display
    space; a paragraph's box spans its lines. ``level`` is a heading's level,
    1 through 6 (1 through 4 when inferred geometrically), ``rows`` a table's
    cells as :attr:`Table.rows` holds them, ``alt_text`` an element's ``/Alt``
    description when present, and ``language`` its inherited BCP 47 ``/Lang``
    tag when reading from the structure tree.
    """

    kind: ElementKind
    page: int
    bbox: Rect
    text: str
    level: int | None = None
    rows: tuple[tuple[str | None, ...], ...] | None = None
    alt_text: str | None = None
    language: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the element as JSON-safe values: ``kind``, ``page``, ``bbox``, ``text``, and optional metadata."""
        record: dict[str, Any] = {
            "kind": self.kind,
            "page": self.page,
            "bbox": [self.bbox.x0, self.bbox.y0, self.bbox.x1, self.bbox.y1],
            "text": self.text,
        }
        if self.level is not None:
            record["level"] = self.level
        if self.rows is not None:
            record["rows"] = [list(row) for row in self.rows]
        if self.alt_text is not None:
            record["alt_text"] = self.alt_text
        if self.language is not None:
            record["language"] = self.language
        return record

    def _markdown(self) -> str:
        if self.kind == "heading" and self.level:
            return f"{'#' * self.level} {self.text}"
        if self.kind == "blockquote":
            from pyropdf._structure_reading import format_blockquote  # noqa: PLC0415

            return format_blockquote(self.text)
        if self.kind == "code":
            from pyropdf._structure_reading import format_code_block  # noqa: PLC0415

            return format_code_block(self.text)
        if self.kind == "image":
            alt = self.alt_text if self.alt_text is not None else self.text
            if alt:
                from pyropdf._structure_reading import escape_markdown_alt  # noqa: PLC0415

                return f"![{escape_markdown_alt(alt)}]()"
            return ""
        return self.text


@dataclass(frozen=True, slots=True, kw_only=True)
class Chunk:
    """Consecutive elements within one section, joined as Markdown in :attr:`text`.

    ``index`` is the chunk's position in the export, ``section`` the
    headings above it from the outermost, ``oversized`` whether its one
    indivisible element passes the budget, and ``language`` the dominant
    BCP 47 ``/Lang`` tag of its elements when reading from the structure tree.
    """

    index: int
    text: str
    elements: tuple[Element, ...]
    section: tuple[str, ...]
    oversized: bool
    language: str | None = None

    @property
    def pages(self) -> tuple[int, ...]:
        """Return the indexes of the pages that the chunk's elements come from, in order."""
        return tuple(sorted({element.page for element in self.elements}))

    def to_dict(self) -> dict[str, Any]:
        """Return the chunk as JSON-safe values, its elements as :meth:`Element.to_dict` gives them."""
        record: dict[str, Any] = {
            "index": self.index,
            "text": self.text,
            "pages": list(self.pages),
            "section": list(self.section),
            "oversized": self.oversized,
            "elements": [element.to_dict() for element in self.elements],
        }
        if self.language is not None:
            record["language"] = self.language
        return record


def validate_options(
    table_strategy: object,
    max_size: object,
    *,
    use_structure: object = "auto",
    expand_abbreviations: object = False,
) -> None:
    """Refuse options that a structured export does not take."""
    _markdown.validate_options(
        table_strategy,
        max_size,
        use_structure=use_structure,
        expand_abbreviations=expand_abbreviations,
    )


def export_json(
    pages: Sequence[Page],
    *,
    table_strategy: Literal["lines", "text"] | None,
    max_size: int | None,
    use_structure: Literal["auto", "always", "never"] = "auto",
    expand_abbreviations: bool = False,
) -> str:
    """Return ``pages`` as a JSON document of their sizes and elements."""
    records = []
    for page, (width, height), elements in _pages_elements(
        pages,
        table_strategy=table_strategy,
        max_size=max_size,
        use_structure=use_structure,
        expand_abbreviations=expand_abbreviations,
    ):
        records.append(
            {
                "index": page.index,
                "width": width,
                "height": height,
                "elements": [element.to_dict() for element in elements],
            }
        )
    return json.dumps({"pages": records}, ensure_ascii=False)


def export_chunks(  # noqa: PLR0913 - keyword-only retrieval export options.
    pages: Sequence[Page],
    *,
    max_chars: int,
    table_strategy: Literal["lines", "text"] | None,
    max_size: int | None,
    use_structure: Literal["auto", "always", "never"] = "auto",
    expand_abbreviations: bool = False,
) -> list[Chunk]:
    """Return the chunks of ``pages``' elements within ``max_chars`` characters each."""
    elements = [
        element
        for _, _, page_elements in _pages_elements(
            pages,
            table_strategy=table_strategy,
            max_size=max_size,
            use_structure=use_structure,
            expand_abbreviations=expand_abbreviations,
        )
        for element in page_elements
    ]
    return _assemble(elements, max_chars)


def _structure_elements(blocks: Sequence[Any], budget: _Budget) -> list[Element]:
    """Convert structure blocks for one page into :class:`Element` records within ``budget``."""
    elements: list[Element] = []
    for block in blocks:
        if block.kind == "table":
            if not block.rows:
                continue
            remaining = None if budget.max_size is None else max(0, budget.max_size - budget.used)
            try:
                table_md = _markdown.table_to_markdown(
                    [list(row) for row in block.rows],
                    cell_anchors=block.cell_anchors,
                    max_size=remaining,
                )
            except _markdown.MarkdownOutputLimitError:
                budget.fail()
            budget.charge(table_md)
            elements.append(
                Element(
                    kind="table",
                    page=block.page,
                    bbox=block.bbox,
                    text=table_md,
                    rows=block.rows,
                    language=block.language,
                )
            )
            continue
        budget.charge(block.text)
        elements.append(
            Element(
                kind=block.kind,
                page=block.page,
                bbox=block.bbox,
                text=block.text,
                level=block.level,
                alt_text=block.alt_text,
                language=block.language,
            )
        )
    return elements


def _pages_elements(
    pages: Sequence[Page],
    *,
    table_strategy: Literal["lines", "text"] | None,
    max_size: int | None,
    use_structure: Literal["auto", "always", "never"] = "auto",
    expand_abbreviations: bool = False,
) -> list[tuple[Page, tuple[float, float], list[Element]]]:
    """Return each page with its size and elements, using structure when enabled and trustworthy."""
    if not pages:
        return []
    from pyropdf._structure_reading import extract_structure_pages  # noqa: PLC0415

    structure_pages = extract_structure_pages(
        pages,
        table_strategy=table_strategy,
        use_structure=use_structure,
        expand_abbreviations=expand_abbreviations,
    )
    if structure_pages is None:
        return _geometric_pages_elements(pages, table_strategy=table_strategy, max_size=max_size)

    fallback_levels: dict[float, int] = {}
    fallback_pages = [page for page in pages if structure_pages[page.index].blocks is None]
    if fallback_pages:
        size_counts: Counter[float] = Counter()
        for page in fallback_pages:
            cached = structure_pages[page.index]
            layout = cached.layout if cached.layout is not None else _markdown._page_layout(page)[0]  # noqa: SLF001
            tables = _markdown._page_tables(page, table_strategy)  # noqa: SLF001
            bboxes = [(table.bbox.x0, table.bbox.y0, table.bbox.x1, table.bbox.y1) for table in tables]
            size_counts.update(_markdown.collect_sizes([layout], [bboxes]))
        fallback_levels = _markdown.heading_levels(size_counts)

    budget = _Budget(max_size)
    result = []
    for page in pages:
        data = structure_pages[page.index]
        if data.blocks is not None:
            elements = _structure_elements(data.blocks, budget)
            result.append((page, (data.width, data.height), elements))
        else:
            layout, words = (
                (data.layout, data.words)
                if data.layout is not None and data.words is not None
                else _markdown._page_layout(page)  # noqa: SLF001
            )
            tables = _markdown._page_tables(page, table_strategy)  # noqa: SLF001
            elements = _PageReader(page.index, layout, fallback_levels, budget).read(tables, words if tables else None)
            for image in page.images():
                _place(elements, Element(kind="image", page=page.index, bbox=image.item.bbox, text=""))
            result.append((page, (layout["width"], layout["height"]), elements))
    return result


def _geometric_pages_elements(
    pages: Sequence[Page], *, table_strategy: Literal["lines", "text"] | None, max_size: int | None
) -> list[tuple[Page, tuple[float, float], list[Element]]]:
    """Return each page with its size and geometric elements, with heading sizes inferred from all of them."""
    size_counts: Counter[float] = Counter()
    for page in pages:
        layout, _ = _markdown._page_layout(page)  # noqa: SLF001
        tables = _markdown._page_tables(page, table_strategy)  # noqa: SLF001
        bboxes = [(table.bbox.x0, table.bbox.y0, table.bbox.x1, table.bbox.y1) for table in tables]
        size_counts.update(_markdown.collect_sizes([layout], [bboxes]))
    levels = _markdown.heading_levels(size_counts)
    budget = _Budget(max_size)
    result = []
    for page in pages:
        layout, words = _markdown._page_layout(page)  # noqa: SLF001
        tables = _markdown._page_tables(page, table_strategy)  # noqa: SLF001
        elements = _PageReader(page.index, layout, levels, budget).read(tables, words if tables else None)
        for image in page.images():
            _place(elements, Element(kind="image", page=page.index, bbox=image.item.bbox, text=""))
        result.append((page, (layout["width"], layout["height"]), elements))
    return result


class _Budget:
    """The UTF-8 that the elements of one export may hold in all."""

    def __init__(self, max_size: int | None) -> None:
        self.max_size = max_size
        self.used = 0

    def fail(self) -> None:
        code = "export_output_size"
        msg = f"the exported text exceeds the {self.max_size}-byte UTF-8 limit"
        raise LimitError(code, msg)

    def charge(self, text: str) -> None:
        if self.max_size is None:
            return
        self.used += _markdown.utf8_size(text)
        if self.used > self.max_size:
            self.fail()


class _PageReader:
    """Turn one page's layout into elements, as ``page_to_markdown`` turns it into Markdown."""

    def __init__(self, page: int, layout: TextPageDict, levels: dict[float, int], budget: _Budget) -> None:
        self.page = page
        self.layout = layout
        self.levels = levels
        self.budget = budget
        self.elements: list[Element] = []
        self.lines: list[tuple[str, tuple[float, float, float, float]]] = []

    def read(self, tables: list[Table], words: list[Word] | None) -> list[Element]:
        rendered = _markdown._table_output(self.layout, tables, max_size=None, remaining_size=None)  # noqa: SLF001
        events, memberships, residuals = _markdown._table_events(self.layout, rendered, words)  # noqa: SLF001
        position = 0
        for block in self.layout["blocks"]:
            for line in block["lines"]:
                pending = events.get(position, [])
                if memberships[position]:
                    before, after = residuals.get(position, ("", ""))
                    self._line(line, before)
                    self._tables(tables, rendered, pending)
                    self._line(line, after)
                else:
                    self._tables(tables, rendered, pending)
                    self._line(line, None)
                position += 1
            self._flush()
        self._tables(tables, rendered, events.get(position, []))
        return self.elements

    def _add(self, element: Element) -> None:
        self.budget.charge(element.text)
        self.elements.append(element)

    def _line(self, line: TextLine, override: str | None) -> None:
        text = _markdown._line_text(line) if override is None else override.strip()  # noqa: SLF001
        if not text:
            return
        bbox = tuple(line["bbox"])
        level = self.levels.get(_markdown._line_size(line))  # noqa: SLF001
        item = _markdown._normalize_list_item(text)  # noqa: SLF001
        if level is None and item is None:
            self.lines.append((text, bbox))
            return
        self._flush()
        if level is not None:
            self._add(Element(kind="heading", page=self.page, bbox=Rect(*bbox), text=text, level=level))
        else:
            self._add(Element(kind="list_item", page=self.page, bbox=Rect(*bbox), text=item or text))

    def _tables(self, tables: list[Table], rendered: list[tuple[Any, str]], indexes: list[int]) -> None:
        if not indexes:
            return
        self._flush()
        for index in indexes:
            table = tables[index]
            self._add(Element(kind="table", page=self.page, bbox=table.bbox, text=rendered[index][1], rows=table.rows))

    def _flush(self) -> None:
        if not self.lines:
            return
        boxes = [bbox for _, bbox in self.lines]
        bbox = Rect(
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        text = _markdown._join_lines([text for text, _ in self.lines])  # noqa: SLF001
        self.lines.clear()
        self._add(Element(kind="paragraph", page=self.page, bbox=bbox, text=text))


def _place(elements: list[Element], image: Element) -> None:
    """Insert ``image`` before the first element whose box starts below its own."""
    for position, element in enumerate(elements):
        if element.bbox.y0 > image.bbox.y0:
            elements.insert(position, image)
            return
    elements.append(image)


def _pieces(text: str, limit: int) -> list[str]:
    """Split ``text`` at white space into pieces of at most ``limit`` characters, cutting longer words."""
    pieces: list[str] = []
    current = ""
    for word in _WORDS.findall(text):
        if current and len(current) + len(word.rstrip()) > limit:
            pieces.append(current.rstrip())
            current = ""
        while len(word.rstrip()) > limit:
            pieces.append(word[:limit])
            word = word[limit:]  # noqa: PLW2901 - the rest of a cut word.
        current += word
    if current.strip():
        pieces.append(current.rstrip())
    return pieces


def _separator(previous: Element, element: Element) -> str:
    return "\n" if previous.kind == element.kind == "list_item" else "\n\n"


def _dominant_language(elements: Sequence[Element]) -> str | None:
    """Return the character-weighted dominant BCP 47 language across ``elements``, or ``None``."""
    weights: dict[str, int] = {}
    for element in elements:
        if element.language is not None:
            weights[element.language] = weights.get(element.language, 0) + max(1, len(element.text))
    if not weights:
        return None
    return max(weights, key=lambda lang: weights[lang])


def _assemble(elements: list[Element], max_chars: int) -> list[Chunk]:
    """Group ``elements`` into chunks of at most ``max_chars`` characters, each heading starting one."""
    assembler = _Assembler(max_chars)
    headings: list[tuple[int, str]] = []
    for element in elements:
        if element.kind == "heading":
            assembler.flush()
            level = element.level or 1
            headings = [heading for heading in headings if heading[0] < level]
            headings.append((level, element.text))
        rendered = element._markdown()  # noqa: SLF001
        if element.kind in _INDIVISIBLE or len(rendered) <= max_chars:
            pieces = [element]
        else:
            piece_limit = max(1, max_chars - 2) if element.kind == "blockquote" else max_chars
            pieces = [replace(element, text=piece) for piece in _pieces(element.text, piece_limit)]
        for piece in pieces:
            assembler.add(piece, tuple(text for _, text in headings))
    assembler.flush()
    return assembler.chunks


class _Assembler:
    """The chunks so far and the elements of the one being filled."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self.chunks: list[Chunk] = []
        self.elements: list[Element] = []
        self.parts: list[str] = []
        self.size = 0
        self.last: Element | None = None
        self.section: tuple[str, ...] = ()

    def add(self, element: Element, section: tuple[str, ...]) -> None:
        text = element._markdown()  # noqa: SLF001
        separator = _separator(self.last, element) if self.last is not None and text else ""
        if self.elements and self.size + len(separator) + len(text) > self.max_chars:
            self.flush()
            separator = ""
        if not self.elements:
            self.section = section
        self.elements.append(element)
        if text:
            self.parts += [separator, text]
            self.size += len(separator) + len(text)
            self.last = element

    def flush(self) -> None:
        if not self.elements:
            return
        text = "".join(self.parts)
        oversized = len(text) > self.max_chars
        self.chunks.append(
            Chunk(
                index=len(self.chunks),
                text=text,
                elements=tuple(self.elements),
                section=self.section,
                oversized=oversized,
                language=_dominant_language(self.elements),
            )
        )
        self.elements, self.parts, self.size, self.last = [], [], 0, None


def validate_max_chars(max_chars: object) -> None:
    """Refuse a chunk budget that is not a positive integer."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        msg = f"max_chars must be a positive integer: {max_chars!r}"
        raise TypeError(msg)
    if max_chars < 1:
        msg = f"max_chars must be a positive integer: {max_chars!r}"
        raise ValueError(msg)


for _public in (Chunk, Element):
    _public.__module__ = "pyropdf"
