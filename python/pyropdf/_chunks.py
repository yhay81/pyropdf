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

from pyropdf import _core, _markdown
from pyropdf._chunk_sources import PageEvidence, RetrievalSource, RetrievalVersion, clipped, relocated, shifted
from pyropdf._errors import LimitError
from pyropdf._geometry import Rect

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyropdf._markdown import TextLine, TextPageDict
    from pyropdf._page import Page
    from pyropdf._tables import Table
    from pyropdf._text import Word

#: The kind of a structured page :class:`Element`.
ElementKind: TypeAlias = Literal["heading", "paragraph", "list_item", "table", "image"]
ElementKind.__doc__ = "The kind of a structured page :class:`Element`."

#: The characters of one chunk by default.
_DEFAULT_MAX_CHARS = 2000

#: The UTF-8 of all element text that one export returns by default (``export_output_size``).
_DEFAULT_MAX_EXPORT_SIZE = 64 * 1024 * 1024

#: The elements that a chunk never splits.
_INDIVISIBLE = frozenset({"heading", "table", "image"})

_WORDS = re.compile(r"\S+\s*")


@dataclass(frozen=True, slots=True, kw_only=True)
class Element:
    """One piece of a page's structure, with where it is.

    ``text`` is a heading's or paragraph's plain text, a list item with its
    bullet as ``- `` or its number as ``1. ``, a table's Markdown, or empty
    for an image. ``page`` is the page's index and ``bbox`` the box in
    display space; a paragraph's box spans its lines. ``level`` is a
    heading's level, 1 through 4, and ``rows`` a table's cells as
    :attr:`Table.rows` holds them.
    """

    kind: ElementKind
    page: int
    bbox: Rect
    text: str
    level: int | None = None
    rows: tuple[tuple[str | None, ...], ...] | None = None
    sources: tuple[RetrievalSource, ...] = ()
    """Contributing source locations when the caller requests provenance."""

    def to_dict(self) -> dict[str, Any]:
        """Return the element as JSON-safe values: ``kind``, ``page``, ``bbox``, ``text``, and ``level`` or ``rows``."""
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
        if self.sources:
            record["sources"] = [source.to_dict() for source in self.sources]
        return record

    def _markdown(self) -> str:
        return f"{'#' * self.level} {self.text}" if self.kind == "heading" and self.level else self.text


@dataclass(frozen=True, slots=True, kw_only=True)
class Chunk:
    """Consecutive elements within one section, joined as Markdown in :attr:`text`.

    ``index`` is the chunk's position in the export, ``section`` the
    headings above it from the outermost, and ``oversized`` whether its one
    indivisible element passes the budget.
    """

    index: int
    text: str
    elements: tuple[Element, ...]
    section: tuple[str, ...]
    oversized: bool

    @property
    def pages(self) -> tuple[int, ...]:
        """Return the indexes of the pages that the chunk's elements come from, in order."""
        return tuple(sorted({element.page for element in self.elements}))

    def to_dict(self) -> dict[str, Any]:
        """Return the chunk as JSON-safe values, its elements as :meth:`Element.to_dict` gives them."""
        return {
            "index": self.index,
            "text": self.text,
            "pages": list(self.pages),
            "section": list(self.section),
            "oversized": self.oversized,
            "elements": [element.to_dict() for element in self.elements],
        }


def validate_options(table_strategy: object, max_size: object, provenance: object) -> None:
    """Refuse a table strategy or an output bound that an export does not take."""
    _markdown.validate_options(table_strategy, max_size)
    if not isinstance(provenance, bool):
        msg = "provenance must be a bool"
        raise TypeError(msg)


def export_json(
    pages: Sequence[Page], *, table_strategy: Literal["lines", "text"] | None, max_size: int | None, provenance: bool
) -> str:
    """Return ``pages`` as a JSON document of their sizes and elements."""
    records = []
    for page, (width, height), elements in _pages_elements(
        pages, table_strategy=table_strategy, max_size=max_size, provenance=provenance
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


def export_chunks(
    pages: Sequence[Page],
    *,
    max_chars: int,
    provenance: bool,
    table_strategy: Literal["lines", "text"] | None,
    max_size: int | None,
) -> list[Chunk]:
    """Return the chunks of ``pages``' elements within ``max_chars`` characters each."""
    elements = [
        element
        for _, _, page_elements in _pages_elements(
            pages, table_strategy=table_strategy, max_size=max_size, provenance=provenance
        )
        for element in page_elements
    ]
    return _assemble(elements, max_chars)


def _pages_elements(
    pages: Sequence[Page], *, table_strategy: Literal["lines", "text"] | None, max_size: int | None, provenance: bool
) -> list[tuple[Page, tuple[float, float], list[Element]]]:
    """Return each page with its size and elements, with heading sizes inferred from all of them."""
    budget = _Budget(max_size)
    version = None
    if provenance and pages:
        snapshot = pages[0]._reader()  # noqa: SLF001
        if not isinstance(snapshot, _core._PageSnapshot):  # noqa: SLF001
            msg = "retrieval provenance requires pinned export pages"
            raise TypeError(msg)
        version = RetrievalVersion(snapshot, budget.evidence)
    size_counts: Counter[float] = Counter()
    for page in pages:
        layout, _ = _markdown._page_layout(page)  # noqa: SLF001
        tables = _markdown._page_tables(page, table_strategy)  # noqa: SLF001
        bboxes = [(table.bbox.x0, table.bbox.y0, table.bbox.x1, table.bbox.y1) for table in tables]
        size_counts.update(_markdown.collect_sizes([layout], [bboxes]))
    levels = _markdown.heading_levels(size_counts)
    result = []
    for page in pages:
        layout, words = _markdown._page_layout(page)  # noqa: SLF001
        tables = _markdown._page_tables(page, table_strategy)  # noqa: SLF001
        evidence = PageEvidence(page, version) if version is not None else None
        elements = _PageReader(page.index, layout, levels, budget, evidence).read(tables, words if tables else None)
        for image in page.images():
            item = image.item
            sources = (
                (evidence.source("unresolved", "", item.bbox, None, image_xref=item.xref or None),) if evidence else ()
            )
            _place(elements, Element(kind="image", page=page.index, bbox=item.bbox, text="", sources=sources))
        result.append((page, (layout["width"], layout["height"]), elements))
    return result


class _Budget:
    """The UTF-8 that the elements of one export may hold in all."""

    def __init__(self, max_size: int | None) -> None:
        self.max_size = max_size
        self.used = 0

    def evidence(self, text: str) -> None:
        """Reserve a location and its text before adding it to the exported data."""
        self.charge(text, overhead=256)

    def charge(self, text: str, *, overhead: int = 0) -> None:
        if self.max_size is None:
            return
        self.used += overhead + _markdown.utf8_size(text)
        if self.used > self.max_size:
            code = "export_output_size"
            msg = (
                f"retrieval evidence exceeds the {self.max_size}-byte export limit"
                if overhead
                else f"the exported text exceeds the {self.max_size}-byte UTF-8 limit"
            )
            raise LimitError(code, msg)


def _inside(glyph: Rect, box: Rect) -> bool:
    return box.x0 <= (glyph.x0 + glyph.x1) / 2 <= box.x1 and box.y0 <= (glyph.y0 + glyph.y1) / 2 <= box.y1


class _PageReader:
    """Turn one page's layout into elements, as ``page_to_markdown`` turns it into Markdown."""

    def __init__(
        self, page: int, layout: TextPageDict, levels: dict[float, int], budget: _Budget, evidence: PageEvidence | None
    ) -> None:
        self.page = page
        self.layout = layout
        self.levels = levels
        self.budget = budget
        self.elements: list[Element] = []
        self.lines: list[tuple[str, tuple[float, float, float, float], tuple[RetrievalSource, ...]]] = []
        self.evidence = evidence
        self.table_sources: dict[int, list[RetrievalSource]] = {}

    def read(self, tables: list[Table], words: list[Word] | None) -> list[Element]:
        rendered = _markdown._table_output(self.layout, tables, max_size=None, remaining_size=None)  # noqa: SLF001
        events, memberships, residuals = _markdown._table_events(self.layout, rendered, words)  # noqa: SLF001
        if self.evidence is not None:
            # A table may be emitted before all its constituent lines are visited.
            by_line = []
            for block in self.layout["blocks"]:
                for line in block["lines"]:
                    text = _markdown._line_text(line)  # noqa: SLF001
                    sources = self.evidence.line_sources(text, Rect(*line["bbox"]))
                    for table_index in memberships[len(by_line)]:
                        box = tables[table_index].bbox
                        for source in sources:
                            if source.bbox is not None and _inside(source.bbox, box):
                                self.table_sources.setdefault(table_index, []).append(
                                    relocated(source, source.text, None)
                                )
                    by_line.append(sources)
        else:
            by_line = []
        position = 0
        for block in self.layout["blocks"]:
            for line in block["lines"]:
                pending = events.get(position, [])
                sources = by_line[position] if by_line else ()
                if memberships[position]:
                    before, after = residuals.get(position, ("", ""))
                    self._line(line, before, sources)
                    self._tables(tables, rendered, pending)
                    self._line(line, after, sources, trailing=True)
                else:
                    self._tables(tables, rendered, pending)
                    self._line(line, None, sources)
                position += 1
            self._flush()
        self._tables(tables, rendered, events.get(position, []))
        return self.elements

    def _add(self, element: Element) -> None:
        self.budget.charge(element.text)
        self.elements.append(element)

    def _line(
        self, line: TextLine, override: str | None, sources: tuple[RetrievalSource, ...], *, trailing: bool = False
    ) -> None:
        text = _markdown._line_text(line) if override is None else override.strip()  # noqa: SLF001
        if not text:
            return
        bbox = tuple(line["bbox"])
        if self.evidence is not None and override is not None:
            whole = _markdown._line_text(line)  # noqa: SLF001
            at = whole.rfind(text) if trailing else whole.find(text)
            sources = (
                clipped(sources, at, at + len(text))
                if at >= 0
                else (self.evidence.source("unresolved", text, Rect(*bbox), (0, len(text))),)
            )
        level = self.levels.get(_markdown._line_size(line))  # noqa: SLF001
        item = _markdown._normalize_list_item(text)  # noqa: SLF001
        if level is None and item is None:
            self.lines.append((text, bbox, sources))
            return
        self._flush()
        if level is not None:
            self._add(
                Element(kind="heading", page=self.page, bbox=Rect(*bbox), text=text, level=level, sources=sources)
            )
        else:
            normalized = item or text
            if normalized != text and self.evidence is not None:
                suffix = 0
                for before, after in zip(reversed(text), reversed(normalized), strict=False):
                    if before != after:
                        break
                    suffix += 1
                prefix = len(normalized) - suffix
                marker = self.evidence.source("inferred", normalized[:prefix], Rect(*bbox), (0, prefix))
                sources = (marker, *shifted(clipped(sources, len(text) - suffix, len(text)), prefix))
            self._add(Element(kind="list_item", page=self.page, bbox=Rect(*bbox), text=normalized, sources=sources))

    def _tables(self, tables: list[Table], rendered: list[tuple[Any, str]], indexes: list[int]) -> None:
        if not indexes:
            return
        self._flush()
        for index in indexes:
            table = tables[index]
            sources = self.table_sources.pop(index, [])
            if self.evidence is not None:
                sources.append(self.evidence.source("inferred", "", table.bbox, None))
            self._add(
                Element(
                    kind="table",
                    page=self.page,
                    bbox=table.bbox,
                    text=rendered[index][1],
                    rows=table.rows,
                    sources=tuple(sources),
                )
            )

    def _flush(self) -> None:
        if not self.lines:
            return
        boxes = [bbox for _, bbox, _ in self.lines]
        bbox = Rect(
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        text = _markdown._join_lines([text for text, _, _ in self.lines])  # noqa: SLF001
        sources = []
        at = 0
        if self.evidence is not None:
            for part, _, locations in self.lines:
                if not text.startswith(part, at):
                    sources.append(self.evidence.source("inferred", " ", None, (at, at + 1)))
                    at += 1
                sources.extend(shifted(locations, at))
                at += len(part)
        self.lines.clear()
        self._add(Element(kind="paragraph", page=self.page, bbox=bbox, text=text, sources=tuple(sources)))


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
        if element.kind in _INDIVISIBLE or len(element.text) <= max_chars:
            pieces = [element]
        else:
            pieces = []
            at = 0
            for piece in _pieces(element.text, max_chars):
                start = element.text.index(piece, at)
                at = start + len(piece)
                pieces.append(replace(element, text=piece, sources=clipped(element.sources, start, at)))
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
