"""A page of a document, which names its page object."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal, overload

from pyropdf import _core
from pyropdf._content import Content, _content_from_core
from pyropdf._errors import LimitError, StaleObjectError, StalePageError
from pyropdf._geometry import Quad, Rect
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Dictionary
from pyropdf._operations import _DEFAULT_MAX_CONTENT_SIZE, Operations, _from_core
from pyropdf._pixmap import Colorspace, _render_background, _render_gray, _render_matrix, _validate_bool
from pyropdf._text import (
    _DEFAULT_MAX_TEXT_GLYPHS,
    _DEFAULT_MAX_TEXT_SIZE,
    Layout,
    Unmapped,
    Word,
    _quad,
    _rect,
    _warn_skipped,
    build_layout,
    build_words,
    text_options,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyropdf._document import Document
    from pyropdf._operations import InlineImage, Operation
    from pyropdf._pixmap import Pixmap
    from pyropdf._text import _CoreBlock, _TextOptions

#: Elements that one ``Page.content()`` reads by default (``content_element_count``).
_DEFAULT_MAX_CONTENT_ELEMENTS = 262_144
#: UTF-8 bytes of one ``Page.svg()`` by default (``svg_output_size``).
_DEFAULT_MAX_SVG_SIZE = 64 * 1024 * 1024
#: Matches that one ``Page.search()`` returns by default (``search_hit_count``).
_DEFAULT_MAX_SEARCH_HITS = 4096
#: UTF-8 bytes of the text that ``Page.search()`` looks for (``search_input_size``).
_MAX_SEARCH_INPUT_BYTES = 4096
#: The largest magnitude of a PDF real that hayro reads as a 32-bit float.
_FLOAT32_MAX = 3.4028234663852886e38


def _clip_box(clip: object) -> tuple[float, float, float, float]:
    """Return an ``(x0, y0, x1, y1)`` clip as floats, refusing one that holds no area."""
    try:
        x0, y0, x1, y1 = (float(value) for value in clip)  # ty: ignore[not-iterable]
    except (TypeError, ValueError) as error:
        msg = f"clip must be 4 numbers (x0, y0, x1, y1): {clip!r}"
        raise ValueError(msg) from error
    if not all(math.isfinite(v) and abs(v) <= _FLOAT32_MAX for v in (x0, y0, x1, y1)) or x0 >= x1 or y0 >= y1:
        msg = f"clip must be a finite rect within PDF real-number range with x0 < x1 and y0 < y1: {clip!r}"
        raise ValueError(msg)
    return x0, y0, x1, y1


class Page:
    """A page of a document, named by its page object.

    A page stays the same page when other pages are inserted, moved, or
    deleted; :attr:`index` follows it. Once the page itself is deleted, its
    reads raise :class:`StalePageError`, and they work again if undo
    restores it. Two pages are equal when they name the same page object of
    the same document.
    """

    __slots__ = ("_document", "_generation", "_number")

    def __init__(self, document: Document, number: int, generation: int) -> None:
        """Name page object ``number`` of ``generation``; use ``doc.pages[i]`` instead."""
        self._document = document
        self._number = number
        self._generation = generation

    @property
    def document(self) -> Document:
        """Return the document that holds the page."""
        return self._document

    @property
    def xref(self) -> int:
        """Return the object number of the page dictionary."""
        return self._number

    @property
    def obj(self) -> Dictionary:
        """Return the page dictionary as a handle of the low-level object model."""
        return Dictionary(self._document, (self._number, self._generation))

    @property
    def index(self) -> int:
        """Return the zero-based index of the page, in time that does not grow with the page count."""
        index = _core.page_index(self._document._core(), self._number, self._generation)  # noqa: SLF001
        if index is None:
            raise self._stale()
        return index

    @property
    def rotation(self) -> int:
        """Return the page's ``/Rotate``, inherited and normalized to 0, 90, 180, or 270.

        Assigning a multiple of 90 sets the page's own ``/Rotate``, in one
        transaction; another value raises ``ValueError``.
        """
        return self._geometry()[0]

    @rotation.setter
    def rotation(self, rotation: int) -> None:
        if isinstance(rotation, bool) or not isinstance(rotation, int):
            msg = f"rotation must be an int: {rotation!r}"
            raise TypeError(msg)
        if rotation % 90 != 0:
            msg = f"rotation must be a multiple of 90: {rotation!r}"
            raise ValueError(msg)
        if not _core.set_page_rotation(self._document._core(), self._number, self._generation, rotation % 360):  # noqa: SLF001
            raise self._stale()

    @property
    def rect(self) -> Rect:
        """Return the display :class:`Rect` ``(0, 0, width, height)``, with the rotation applied.

        The visible page is the CropBox clipped to the MediaBox; a box that
        leaves nothing visible gives the A4 page that the renderer draws.
        """
        _, width, height = self._geometry()
        return Rect(0.0, 0.0, width, height)

    def pixmap(  # noqa: PLR0913 - the rendering options are all keyword-only
        self,
        *,
        dpi: float | None = None,
        matrix: Sequence[float] | None = None,
        clip: Sequence[float] | None = None,
        alpha: bool = False,
        background: tuple[int, int, int] | tuple[int, int, int, int] | None = None,
        annotations: bool = True,
        colorspace: Colorspace | Literal["gray", "rgb"] = Colorspace.RGB,
    ) -> Pixmap:
        """Render the current version of the page to a :class:`Pixmap`.

        ``dpi`` scales the page uniformly, 72 by default; ``matrix`` is a
        :class:`Matrix` or six entries, whose translation is dropped, since
        the raster covers the transformed page, or a linear map
        ``(a, b, c, d)``. Without ``alpha`` the page lies on white.
        ``background`` is an RGB or RGBA tuple with components from 0 to 255
        painted beneath the page, which must be opaque without ``alpha``.
        ``annotations=False`` leaves out the appearances of annotations and
        form widgets. ``colorspace="gray"`` converts the rendered colors to
        gray with ITU-R BT.601 luma weights.

        ``clip`` is a display rectangle ``(x0, y0, x1, y1)``: its bounding
        box under the matrix, rounded outward to whole pixels and
        intersected with the page, is all that is rasterized. A clip that
        leaves no pixel of the page raises :class:`PdfError`, and the size
        limits of the whole page still apply. The GIL is released while
        hayro renders.
        """
        linear = _render_matrix(matrix, dpi)
        rgba = _render_background(background, alpha=alpha)
        _validate_bool("annotations", annotations)
        gray = _render_gray(colorspace)
        box = None if clip is None else _clip_box(clip)
        try:
            return _core.render_page(
                self._document._core(),  # noqa: SLF001
                self._number,
                self._generation,
                linear,
                alpha,
                gray,
                rgba,
                box,
                annotations,
            )
        except StaleObjectError as error:
            raise self._stale() from error

    def svg(self, *, annotations: bool = True, max_size: int | None = _DEFAULT_MAX_SVG_SIZE) -> str:
        """Render the current version of the page to an SVG string at 72 dpi, with text drawn as paths.

        ``annotations=False`` leaves out the appearances of annotations and
        form widgets. ``max_size`` caps the UTF-8 output and defaults to
        64 MiB, raising :class:`LimitError` with code ``svg_output_size``;
        ``None`` opts out. hayro-svg builds its whole string before the limit
        is checked, so the limit keeps an oversized result from becoming a
        Python string rather than bounding the converter's memory. The GIL
        is released while hayro-svg converts.
        """
        _validate_bool("annotations", annotations)
        _validate_optional_positive_int("max_size", max_size)
        try:
            return _core.render_page_svg(
                self._document._core(),  # noqa: SLF001
                self._number,
                self._generation,
                annotations,
                max_size,
            )
        except StaleObjectError as error:
            raise self._stale() from error

    def content(  # noqa: PLR0913 - the item kinds and the reading options are all keyword-only
        self,
        *,
        text_runs: bool = True,
        paths: bool = True,
        images: bool = True,
        clip: Sequence[float] | None = None,
        annotations: bool = True,
        max_elements: int | None = _DEFAULT_MAX_CONTENT_ELEMENTS,
    ) -> Content:
        """Return the text runs, paths, and images that the page paints.

        Items come in paint order, in display coordinates, as hayro
        interprets the current version of the page, with optional-content
        visibility applied. Each carries its paints, its blend mode and soft
        mask, and the :class:`GraphicsContext` of clips, transparency groups,
        and marked content that encloses it. Glyph boxes are the exact boxes
        of the glyph outlines. hayro paints every image inside a transparency
        group that holds the graphics state's opacity, blend mode, and soft
        mask, so an image's context ends with that group. A shading painted
        with ``sh`` is a :class:`Path` that covers the clip region and fills
        with a :class:`ShadingPattern`.

        ``text_runs=False``, ``paths=False``, and ``images=False`` leave out
        that kind of item without building it. ``clip`` keeps the items whose
        box meets a display-coordinate rectangle. ``annotations=False`` leaves
        out annotation appearances, which otherwise follow the page content.
        Items whose geometry or paint is not finite, which hayro cannot paint
        either, are left out.

        ``max_elements`` bounds the glyphs, path and clip segments, stroke
        dash lengths, and items of one interpretation, before ``clip``
        applies; above it the call raises :class:`LimitError` with code
        ``content_element_count``. ``None`` removes the bound for trusted
        input. The GIL is released while hayro interprets the page.
        """
        for name, value in (
            ("text_runs", text_runs),
            ("paths", paths),
            ("images", images),
            ("annotations", annotations),
        ):
            if not isinstance(value, bool):
                msg = f"{name} must be a bool: {value!r}"
                raise TypeError(msg)
        _validate_optional_positive_int("max_elements", max_elements)
        clip_box = None if clip is None else _clip_box(clip)
        try:
            raw = _core.page_content(
                self._document._core(),  # noqa: SLF001
                self._number,
                self._generation,
                (text_runs, paths, images),
                annotations,
                clip_box,
                max_elements,
            )
        except StaleObjectError as error:
            raise self._stale() from error
        return _content_from_core(raw)

    def text(  # noqa: PLR0913 - the text options are all keyword-only
        self,
        *,
        clip: Sequence[float] | None = None,
        clipped: bool = False,
        ligatures: bool = True,
        whitespace: bool = True,
        dehyphenate: bool = False,
        unmapped: Unmapped = "skip",
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> str:
        """Return the text of the current version of the page in reading order.

        Reading order follows the geometry and script heuristics described in
        the documentation rather than the document's semantic order. Spaces
        between words are inferred from glyph positions, and lines end with a
        newline. Annotation appearances count as page content.

        ``clip`` keeps the glyphs whose box's center lies inside a
        display-space rectangle, in the page's reading order, which gives the
        text in a rectangle. Glyphs that the page boundary or the clip paths
        hide entirely are left out unless ``clipped=True``; invisible text,
        such as an OCR layer, is kept. ``ligatures=False`` expands the
        ligature characters U+FB00 to U+FB06 to their letters;
        ``whitespace=False`` makes every whitespace character a space;
        ``dehyphenate=True`` removes a hyphen that ends a line between two
        letters of a word that continues in lowercase on the next line of the
        block, and joins the two lines; and ``unmapped`` decides what a glyph
        without Unicode becomes (see :data:`Unmapped`).

        ``max_glyphs`` bounds the glyphs collected, 262,144 by default, with
        :class:`LimitError` code ``text_glyph_count``, and ``max_size`` the
        UTF-8 of their text and of the returned text, 64 MiB by default, with
        code ``text_size``; ``None`` lifts either. The GIL is released while
        hayro interprets the page.
        """
        options = text_options(
            clip=None if clip is None else _clip_box(clip),
            clipped=clipped,
            ligatures=ligatures,
            whitespace=whitespace,
            dehyphenate=dehyphenate,
            unmapped=unmapped,
            max_glyphs=max_glyphs,
            max_size=max_size,
        )
        try:
            text, skipped = _core.page_text(
                self._document._core(),  # noqa: SLF001
                self._number,
                self._generation,
                options,
                max_glyphs,
                max_size,
            )
        except StaleObjectError as error:
            raise self._stale() from error
        _warn_skipped(skipped)
        return text

    def words(  # noqa: PLR0913 - the text options are all keyword-only
        self,
        *,
        clip: Sequence[float] | None = None,
        clipped: bool = False,
        ligatures: bool = True,
        whitespace: bool = True,
        dehyphenate: bool = False,
        unmapped: Unmapped = "skip",
        exact_bboxes: bool = False,
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> list[Word]:
        """Return the words of the current version of the page in reading order, as :class:`Word` records.

        Boxes are in display space and span the glyphs' advances and the
        font's ascender and descender, or their outlines with
        ``exact_bboxes=True``. The other options are those of :meth:`text`; a
        word that dehyphenation joins is one :class:`Word` on its first line
        whose box spans both parts.
        """
        options = text_options(
            clip=None if clip is None else _clip_box(clip),
            clipped=clipped,
            ligatures=ligatures,
            whitespace=whitespace,
            dehyphenate=dehyphenate,
            unmapped=unmapped,
            max_glyphs=max_glyphs,
            max_size=max_size,
            exact_bboxes=exact_bboxes,
        )
        _, _, blocks = self._layout(options, chars=False, max_glyphs=max_glyphs, max_size=max_size)
        return build_words(blocks)

    def layout(  # noqa: PLR0913 - the text options are all keyword-only
        self,
        *,
        clip: Sequence[float] | None = None,
        chars: bool = False,
        clipped: bool = False,
        ligatures: bool = True,
        whitespace: bool = True,
        dehyphenate: bool = False,
        unmapped: Unmapped = "skip",
        exact_bboxes: bool = False,
        styles: bool = False,
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> Layout:
        """Return the text of the current version of the page as blocks, lines, and spans, in reading order.

        With ``chars=True``, every span also lists its characters as
        :class:`Char` records: each glyph keeps its complete Unicode mapping,
        ligatures included, and each space that pyropdf infers from a gap is a
        char with ``is_synthetic`` set, so that a span's chars join to its
        text. A glyph of the page's own content streams names the stream and
        the index in :attr:`operations` of the operation that showed it, in
        ``Char.stream`` and ``Char.operation``, which a second interpretation
        of the content finds. Boxes are in display space and span the glyphs' advances and the
        font's ascender and descender, or their outlines with
        ``exact_bboxes=True``. Dehyphenation removes the hyphen from the end
        of the line and keeps both lines. ``styles=True`` also reads the
        page's vector rules and sets :attr:`Span.is_underlined` and
        :attr:`Span.is_struck` where a horizontal rule underlines or strikes
        out horizontal text, as pymupdf's ``TEXT_COLLECT_STYLES`` does. The
        other options are those of :meth:`text`, and ``max_size`` also bounds
        the UTF-8 of the layout's strings.
        """
        if not isinstance(chars, bool):
            msg = f"chars must be True or False: {chars!r}"
            raise TypeError(msg)
        options = text_options(
            clip=None if clip is None else _clip_box(clip),
            clipped=clipped,
            ligatures=ligatures,
            whitespace=whitespace,
            dehyphenate=dehyphenate,
            unmapped=unmapped,
            max_glyphs=max_glyphs,
            max_size=max_size,
            exact_bboxes=exact_bboxes,
            styles=styles,
        )
        return build_layout(*self._layout(options, chars=chars, max_glyphs=max_glyphs, max_size=max_size))

    def _layout(
        self, options: _TextOptions, *, chars: bool, max_glyphs: int | None, max_size: int | None
    ) -> tuple[float, float, list[_CoreBlock]]:
        """Return the core's layout of the page and warn of the glyphs it skipped."""
        try:
            width, height, blocks, skipped = _core.page_layout(
                self._document._core(),  # noqa: SLF001
                self._number,
                self._generation,
                options,
                chars,
                max_glyphs,
                max_size,
            )
        except StaleObjectError as error:
            raise self._stale() from error
        _warn_skipped(skipped)
        return width, height, blocks

    @overload
    def search(
        self,
        text: str,
        *,
        clip: Sequence[float] | None = ...,
        quads: Literal[False] = ...,
        max_hits: int | None = ...,
        clipped: bool = ...,
        ligatures: bool = ...,
        whitespace: bool = ...,
        dehyphenate: bool = ...,
        unmapped: Unmapped = ...,
        exact_bboxes: bool = ...,
        max_glyphs: int | None = ...,
        max_size: int | None = ...,
    ) -> list[Rect]: ...

    @overload
    def search(
        self,
        text: str,
        *,
        clip: Sequence[float] | None = ...,
        quads: Literal[True],
        max_hits: int | None = ...,
        clipped: bool = ...,
        ligatures: bool = ...,
        whitespace: bool = ...,
        dehyphenate: bool = ...,
        unmapped: Unmapped = ...,
        exact_bboxes: bool = ...,
        max_glyphs: int | None = ...,
        max_size: int | None = ...,
    ) -> list[Quad]: ...

    def search(  # noqa: PLR0913 - the text options are all keyword-only
        self,
        text: str,
        *,
        clip: Sequence[float] | None = None,
        quads: bool = False,
        max_hits: int | None = _DEFAULT_MAX_SEARCH_HITS,
        clipped: bool = False,
        ligatures: bool = True,
        whitespace: bool = True,
        dehyphenate: bool = False,
        unmapped: Unmapped = "skip",
        exact_bboxes: bool = False,
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> list[Rect] | list[Quad]:
        """Search the text of the current version of the page case-insensitively for ``text``.

        Return one :class:`Rect` per match, or with ``quads=True`` one
        :class:`Quad` per line of each match: a quad follows the baseline,
        so that it covers rotated text tightly where a rectangle is
        axis-aligned. Search reads the lines of :meth:`layout` and finds no
        match across lines, except that ``dehyphenate=True`` joins a
        hyphenated word's lines; a match across that join gives one
        rectangle that spans both parts, or one quad per line. A space that
        pyropdf infers from a gap matches a space of ``text``.

        ``text`` is at most 4,096 bytes in UTF-8, or the call raises
        :class:`LimitError` with code ``search_input_size``. More matches
        than ``max_hits`` raise :class:`LimitError` with code
        ``search_hit_count`` instead of returning some of them;
        ``max_hits=None`` lifts the limit for trusted input. The other
        options are those of :meth:`layout`.
        """
        if not isinstance(text, str):
            msg = f"text must be a str: {text!r}"
            raise TypeError(msg)
        if not text:
            msg = "text must be at least 1 character"
            raise ValueError(msg)
        # A character takes at most 4 bytes: short text needs no encoding to check.
        if len(text) * 4 > _MAX_SEARCH_INPUT_BYTES and len(text.encode()) > _MAX_SEARCH_INPUT_BYTES:
            code = "search_input_size"
            msg = f"the search text exceeds the {_MAX_SEARCH_INPUT_BYTES}-byte UTF-8 limit"
            raise LimitError(code, msg)
        if not isinstance(quads, bool):
            msg = f"quads must be True or False: {quads!r}"
            raise TypeError(msg)
        _validate_optional_positive_int("max_hits", max_hits)
        options = text_options(
            clip=None if clip is None else _clip_box(clip),
            clipped=clipped,
            ligatures=ligatures,
            whitespace=whitespace,
            dehyphenate=dehyphenate,
            unmapped=unmapped,
            max_glyphs=max_glyphs,
            max_size=max_size,
            exact_bboxes=exact_bboxes,
        )
        try:
            hits, skipped = _core.page_search(
                self._document._core(),  # noqa: SLF001
                self._number,
                self._generation,
                text,
                quads,
                options,
                max_hits,
                max_glyphs,
                max_size,
            )
        except StaleObjectError as error:
            raise self._stale() from error
        _warn_skipped(skipped)
        if quads:
            return [_quad(hit) for hit in hits]
        return [_rect(hit) for hit in hits]

    def selection(
        self,
        start: Sequence[float],
        end: Sequence[float],
        *,
        clip: Sequence[float] | None = None,
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> str:
        """Return the text that a viewer selects by dragging from ``start`` to ``end``.

        The selection runs in reading order from the character nearest
        ``start`` to the one nearest ``end``, whichever comes first, with the
        first and last lines in part and the lines between them whole, as
        :meth:`text` assembles them, and without a final line break. Both
        points are in display space; ``clip`` limits the characters as
        :meth:`text` does, and so do the budgets. A page without text
        selects ``""``.
        """
        points = []
        for name, point in (("start", start), ("end", end)):
            try:
                x, y = (float(value) for value in point)
            except (TypeError, ValueError) as error:
                msg = f"{name} must be a point (x, y): {point!r}"
                raise ValueError(msg) from error
            if not (math.isfinite(x) and math.isfinite(y)):
                msg = f"{name} must be a finite point: {point!r}"
                raise ValueError(msg)
            points.append((x, y))
        options = text_options(
            clip=None if clip is None else _clip_box(clip),
            clipped=False,
            ligatures=True,
            whitespace=True,
            dehyphenate=False,
            unmapped="skip",
            max_glyphs=max_glyphs,
            max_size=max_size,
        )
        try:
            text, skipped = _core.page_selection(
                self._document._core(),  # noqa: SLF001
                self._number,
                self._generation,
                points[0],
                points[1],
                options,
                max_glyphs,
                max_size,
            )
        except StaleObjectError as error:
            raise self._stale() from error
        _warn_skipped(skipped)
        return text

    @property
    def operations(self) -> Operations:
        """Return the page's content as :class:`Operations`, as hayro reads it.

        The content streams of ``/Contents`` are decoded and joined as the
        renderer joins them, each stream of an array followed by one space,
        and tokenized with hayro's parser, so the operations are the ones
        that rendering interprets. Each operation keeps its bytes, so
        ``page.operations.to_bytes()`` is that content.

        Assigning a sequence of :class:`Operation` and :class:`InlineImage`
        values writes them as one new Flate-compressed content stream that
        replaces ``/Contents``, in one transaction, so a stream that other
        pages share stays theirs. Operations that a read produced write
        their bytes back, so ``page.operations = page.operations`` keeps
        what renders. More than 64 MiB of written content raises
        :class:`LimitError` code ``content_input_size``.

        Reading refuses more than 1,048,576 operations
        (``content_operation_count``) and more than 64 MiB of joined content
        (``content_stream_size``), besides the decoding limits of
        :class:`DocumentLimits`; use :meth:`Operations.parse` with
        ``max_operations=None`` on ``page.obj["Contents"].read()`` for larger
        trusted content. The GIL is released while the content is read.
        """
        raw = _core.page_operations(self._document._core(), self._number, self._generation)  # noqa: SLF001
        if raw is None:
            raise self._stale()
        return _from_core(raw)

    @operations.setter
    def operations(self, operations: Iterable[Operation | InlineImage]) -> None:
        if not isinstance(operations, Operations):
            if isinstance(operations, (str, bytes, bytearray, memoryview)) or not isinstance(operations, Iterable):
                msg = f"assign a sequence of Operation and InlineImage values: {type(operations).__name__}"
                raise TypeError(msg)
            operations = Operations(operations)
        data = operations.to_bytes()
        if len(data) > _DEFAULT_MAX_CONTENT_SIZE:
            code = "content_input_size"
            msg = f"page content of {len(data)} bytes exceeds the {_DEFAULT_MAX_CONTENT_SIZE}-byte limit"
            raise LimitError(code, msg)
        document = self._document
        with document.transaction():
            # The page must still be a page when its content changes.
            _ = self.index
            number = document.objects.add_stream(data)
            self.obj["Contents"] = document.objects[number]

    def _geometry(self) -> tuple[int, float, float]:
        """Return the rotation, width, and height, or raise for a deleted page."""
        geometry = _core.page_geometry(self._document._core(), self._number, self._generation)  # noqa: SLF001
        if geometry is None:
            raise self._stale()
        return geometry

    def _stale(self) -> StalePageError:
        """Return the error of a page that the page tree no longer lists."""
        msg = f"page object {self._number} {self._generation} is no longer a page of the document; use doc.pages[i]"
        return StalePageError(msg)

    def __eq__(self, other: object) -> bool:
        """Return whether ``other`` names the same page object of the same document."""
        if not isinstance(other, Page):
            return NotImplemented
        return (
            other._document is self._document
            and other._number == self._number
            and other._generation == self._generation
        )

    def __hash__(self) -> int:
        """Hash the document and the page object."""
        return hash((id(self._document), self._number, self._generation))

    def __repr__(self) -> str:
        """Return a representation naming the page object and the document."""
        return f"<Page {self._number} {self._generation} R of {self._document!r}>"


__all__ = ["Page"]
