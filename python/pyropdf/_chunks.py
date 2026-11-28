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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from pyropdf import _markdown
from pyropdf._errors import LimitError
from pyropdf._geometry import Rect

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Sequence

    from pyropdf._markdown import ExportedImage, TextLine, TextPageDict, _ExportConfig
    from pyropdf._page import Page
    from pyropdf._structure_reading import StructureBlock
    from pyropdf._tables import Table
    from pyropdf._text import RubyMode, Word

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
    description when present, ``language`` its inherited BCP 47 ``/Lang``
    tag when reading from the structure tree, and ``image`` its exported
    image filename or callback reference when ``images=...`` is configured.
    """

    kind: ElementKind
    page: int
    bbox: Rect
    text: str
    level: int | None = None
    rows: tuple[tuple[str | None, ...], ...] | None = None
    alt_text: str | None = None
    language: str | None = None
    image: str | None = None

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
        if self.image is not None:
            record["image"] = self.image
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
            if self.image is not None or alt:
                from pyropdf._structure_reading import format_markdown_image  # noqa: PLC0415

                return format_markdown_image(alt, self.image or "")
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
    max_graphics: object = _markdown._DEFAULT_MAX_GRAPHICS,  # noqa: SLF001
) -> None:
    """Refuse options that a structured export does not take."""
    _markdown.validate_options(
        table_strategy,
        max_size,
        use_structure=use_structure,
        expand_abbreviations=expand_abbreviations,
        ruby=ruby,
        margins=margins,
        ignore_headers_footers=ignore_headers_footers,
        images=images,
        max_graphics=max_graphics,
    )


def export_json(  # noqa: PLR0913 - keyword-only retrieval export options.
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
    max_graphics: int | None = _markdown._DEFAULT_MAX_GRAPHICS,  # noqa: SLF001
) -> str:
    """Return ``pages`` as a JSON document of their sizes and elements."""
    config = _markdown._make_config(  # noqa: SLF001
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
    page_records, exporter = _pages_elements(pages, config)
    records = [
        {
            "index": page.index,
            "width": width,
            "height": height,
            "elements": [element.to_dict() for element in elements],
        }
        for page, (width, height), elements in page_records
    ]
    payload = json.dumps({"pages": records}, ensure_ascii=False)
    exporter.commit()
    return payload


def export_chunks(  # noqa: PLR0913 - keyword-only retrieval export options.
    pages: Sequence[Page],
    *,
    max_chars: int,
    table_strategy: Literal["lines", "text"] | None,
    max_size: int | None,
    use_structure: Literal["auto", "always", "never"] = "auto",
    expand_abbreviations: bool = False,
    ruby: RubyMode = "omit",
    margins: float | tuple[float, float] | tuple[float, float, float, float] | None = None,
    ignore_headers_footers: bool = False,
    images: str | os.PathLike[str] | Callable[[ExportedImage], str | None] | None = None,
    max_graphics: int | None = _markdown._DEFAULT_MAX_GRAPHICS,  # noqa: SLF001
) -> list[Chunk]:
    """Return the chunks of ``pages``' elements within ``max_chars`` characters each."""
    config = _markdown._make_config(  # noqa: SLF001
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
    page_records, exporter = _pages_elements(pages, config)
    elements = [element for _, _, page_elements in page_records for element in page_elements]
    chunks = _assemble(elements, max_chars)
    exporter.commit()
    return chunks


def _structure_elements(blocks: Sequence[StructureBlock], budget: _Budget) -> list[Element]:
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
        if block.image is not None:
            budget.charge(block.image)
        elements.append(
            Element(
                kind=block.kind,
                page=block.page,
                bbox=block.bbox,
                text=block.text,
                level=block.level,
                alt_text=block.alt_text,
                language=block.language,
                image=block.image,
            )
        )
    return elements


def _geometric_page_elements(  # noqa: PLR0913 - extracts one geometric page's elements with controls.
    page: Page,
    *,
    config: _ExportConfig,
    graphics: _markdown._GraphicsCache,
    hf_rule: _markdown._HeaderFooterRule,
    exporter: _markdown._ImageExporter,
    levels: dict[float, int],
    budget: _Budget,
    data: Any,  # noqa: ANN401 - optional PageStructureData from _structure_reading.
) -> tuple[tuple[float, float], list[Element]]:
    body_clip, body_empty = _markdown._page_body_clip(page, config.margins)  # noqa: SLF001
    page_gfx = graphics.inspect(page)
    if body_empty:
        _, width, height = page._geometry()  # noqa: SLF001
        return (width, height), []
    layout, words = _markdown._fallback_or_clipped_layout(  # noqa: SLF001
        page, data, body_clip, body_empty=False, ruby=config.ruby
    )
    tables = graphics.tables(page, config.table_strategy, clip=body_clip)
    table_bboxes = [(t.bbox.x0, t.bbox.y0, t.bbox.x1, t.bbox.y1) for t in tables]
    y_min, y_max = _markdown._band_bounds(layout["height"], body_clip)  # noqa: SLF001
    layout, words = hf_rule.filter_layout(layout, words, y_min, y_max, table_bboxes)
    elements = _PageReader(page.index, layout, levels, budget).read(tables, words if tables else None)
    kept_images = _markdown._filter_images_by_margins(page_gfx.images, body_clip, body_empty=False)  # noqa: SLF001
    for page_image in kept_images:
        ref = exporter.export(page_image)
        if ref is not None:
            budget.charge(ref)
        _place(elements, Element(kind="image", page=page.index, bbox=page_image.item.bbox, text="", image=ref))
    return (layout["width"], layout["height"]), elements


def _pages_elements(
    pages: Sequence[Page],
    config: _ExportConfig,
) -> tuple[list[tuple[Page, tuple[float, float], list[Element]]], _markdown._ImageExporter]:
    """Return each page with its size and elements plus the uncommitted image exporter."""
    if not pages:
        return [], _markdown._ImageExporter(None, memory_limit=None)  # noqa: SLF001
    from pyropdf._structure_reading import extract_structure_pages  # noqa: PLC0415

    structure_pages = extract_structure_pages(
        pages,
        table_strategy=config.table_strategy,
        use_structure=config.use_structure,
        expand_abbreviations=config.expand_abbreviations,
        ruby=config.ruby,
        max_graphics=config.max_graphics,
    )
    exporter = _markdown._ImageExporter(  # noqa: SLF001
        config.images, memory_limit=pages[0].document.limits.max_memory_size
    )
    graphics = _markdown._GraphicsCache(max_graphics=config.max_graphics, need_images=True)  # noqa: SLF001
    graphics.seed_from_structure(structure_pages)
    hf_rule = _markdown._detect_headers_footers(pages, config, graphics, structure_pages)  # noqa: SLF001
    levels = _markdown._collect_geometric_heading_levels(  # noqa: SLF001
        pages, config, graphics, hf_rule, structure_pages
    )

    budget = _Budget(config.max_size)
    result: list[tuple[Page, tuple[float, float], list[Element]]] = []
    for page in pages:
        data = structure_pages[page.index] if structure_pages is not None else None
        if data is not None and data.blocks is not None:
            body_clip, body_empty = _markdown._page_body_clip(page, config.margins)  # noqa: SLF001
            page_gfx = graphics.inspect(page)
            if body_empty:
                result.append((page, (data.width, data.height), []))
                continue
            blocks = _markdown._filter_structure_blocks_by_margins(  # noqa: SLF001
                data.blocks, body_clip, body_empty=False
            )
            y_min, y_max = _markdown._band_bounds(data.height, body_clip)  # noqa: SLF001
            blocks = hf_rule.filter_structure_blocks(blocks, y_min, y_max)
            kept_images = _markdown._filter_images_by_margins(  # noqa: SLF001
                page_gfx.images, body_clip, body_empty=False
            )
            blocks = _markdown._attach_structure_images(blocks, kept_images, exporter)  # noqa: SLF001
            elements = _structure_elements(blocks, budget)
            result.append((page, (data.width, data.height), elements))
        else:
            size, elements = _geometric_page_elements(
                page,
                config=config,
                graphics=graphics,
                hf_rule=hf_rule,
                exporter=exporter,
                levels=levels,
                budget=budget,
                data=data,
            )
            result.append((page, size, elements))
    return result, exporter


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
        remaining = None if self.budget.max_size is None else max(0, self.budget.max_size - self.budget.used)
        try:
            rendered = _markdown._table_output(  # noqa: SLF001
                self.layout,
                tables,
                max_size=self.budget.max_size,
                remaining_size=remaining,
            )
        except LimitError as error:
            if error.code == "markdown_output_size":
                self.budget.fail()
            raise
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
