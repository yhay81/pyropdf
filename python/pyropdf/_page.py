"""A page of a document, which names its page object."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal, overload

from pyropdf import _core, _markdown
from pyropdf._annotations import Annotations
from pyropdf._canvas import Canvas
from pyropdf._content import Content, _content_from_core
from pyropdf._errors import LimitError, StaleObjectError, StalePageError
from pyropdf._geometry import Quad, Rect
from pyropdf._images import PageImage, page_images
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._links import Links
from pyropdf._objects import Dictionary
from pyropdf._ocr import (
    OcrEngine,
    OcrRotation,
    OcrWord,
    _bounded_utf8_size,
    _default_ocr_engine,
    _validate_ocr_rotation,
)
from pyropdf._operations import _DEFAULT_MAX_CONTENT_SIZE, Operations, _from_core
from pyropdf._page_labels import _label, _read_page_labels
from pyropdf._pixmap import Colorspace, _render_background, _render_gray, _render_matrix, _validate_bool
from pyropdf._tables import Table, build_tables
from pyropdf._text import (
    _DEFAULT_MAX_TEXT_GLYPHS,
    _DEFAULT_MAX_TEXT_SIZE,
    Chars,
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
from pyropdf._widgets import Widgets

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyropdf._core import CharColumns
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


def _clip_box(clip: object, *, name: str = "clip") -> tuple[float, float, float, float]:
    """Return an ``(x0, y0, x1, y1)`` clip as floats, refusing one that holds no area."""
    try:
        x0, y0, x1, y1 = (float(value) for value in clip)  # ty: ignore[not-iterable]
    except (TypeError, ValueError) as error:
        msg = f"{name} must be 4 numbers (x0, y0, x1, y1): {clip!r}"
        raise ValueError(msg) from error
    if not all(math.isfinite(v) and abs(v) <= _FLOAT32_MAX for v in (x0, y0, x1, y1)) or x0 >= x1 or y0 >= y1:
        msg = f"{name} must be a finite rect within PDF real-number range with x0 < x1 and y0 < y1: {clip!r}"
        raise ValueError(msg)
    return x0, y0, x1, y1


#: The most words with text, and UTF-8 bytes of their text, of one OCR layer.
_MAX_OCR_LAYER_WORDS = 4096
_MAX_OCR_LAYER_TEXT_BYTES = 1024 * 1024
#: The defaults of the recognition options of :meth:`Page.apply_ocr`, which ``words`` excludes.
_DEFAULT_OCR_RECOGNITION = (300, 1408, 192, 0.5)


def _ocr_layer_words(words: Iterable[OcrWord]) -> list[OcrWord]:
    """Return the words with text of an OCR layer, stopping at the first word over the limits."""
    accepted: list[OcrWord] = []
    text_bytes = 0
    for word in words:
        if not isinstance(word, OcrWord):
            msg = f"words must be OcrWord records: {word!r}"
            raise TypeError(msg)
        _clip_box(word.bbox, name="word bbox")
        if not isinstance(word.text, str):
            msg = f"word text must be a string: {word.text!r}"
            raise TypeError(msg)
        if not word.text:
            continue
        if len(accepted) >= _MAX_OCR_LAYER_WORDS:
            msg = f"words cannot contain more than {_MAX_OCR_LAYER_WORDS} entries with text"
            raise ValueError(msg)
        encoded_bytes = _bounded_utf8_size(word.text, _MAX_OCR_LAYER_TEXT_BYTES - text_bytes)
        if encoded_bytes is None:
            msg = f"word text cannot exceed {_MAX_OCR_LAYER_TEXT_BYTES} UTF-8 bytes per call"
            raise ValueError(msg)
        text_bytes += encoded_bytes
        accepted.append(word)
    return accepted


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
    def annotations(self) -> Annotations:
        """Return the page's annotations other than links and form widgets, in ``/Annots`` order."""
        return Annotations(self)

    @property
    def links(self) -> Links:
        """Return the page's link annotations, in ``/Annots`` order."""
        return Links(self)

    @property
    def widgets(self) -> Widgets:
        """Return the page's form widgets, in ``/Annots`` order."""
        return Widgets(self)

    @property
    def index(self) -> int:
        """Return the zero-based index of the page, in time that does not grow with the page count."""
        index = _core.page_index(self._document._core(), self._number, self._generation)  # noqa: SLF001
        if index is None:
            raise self._stale()
        return index

    def images(self, *, annotations: bool = True) -> list[PageImage]:
        """Return the images that the page paints, in the paint order of :meth:`content`.

        Each :class:`PageImage` decodes its pixels with ``pixmap()`` and
        returns its data as stored with ``stored()``; ``annotations`` counts
        the images of annotation appearances, as in :meth:`content`.
        """
        _validate_bool("annotations", annotations)
        return page_images(self, annotations=annotations)

    @property
    def label(self) -> str:
        """Return the page's label from ``doc.page_labels``, or ``""`` when no range covers it."""
        return _label(_read_page_labels(self._document), self.index)

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

    def _box(self, kind: int) -> Rect:
        boxes = _core.page_boxes(self._document._core(), self._number, self._generation)  # noqa: SLF001
        if boxes is None:
            raise self._stale()
        return Rect(boxes[kind])

    def _set_box(self, kind: int, rect: Rect | Sequence[float]) -> None:
        value = tuple(Rect(rect))
        if not _core.set_page_box(self._document._core(), self._number, self._generation, kind, value):  # noqa: SLF001
            raise self._stale()

    @property
    def media_box(self) -> Rect:
        """Return the page's MediaBox in display space, inherited, or the A4 page that the renderer draws.

        Display space is the visible page's, with ``(0, 0)`` at its top-left,
        so a MediaBox larger than the CropBox has negative coordinates.
        Assigning a :class:`Rect` in the same space sets the page's own
        ``/MediaBox`` in one transaction. A MediaBox that would not hold a
        box the page sets raises ``ValueError``; set that box first.
        """
        return self._box(0)

    @media_box.setter
    def media_box(self, rect: Rect | Sequence[float]) -> None:
        self._set_box(0, rect)

    @property
    def crop_box(self) -> Rect:
        """Return the page's CropBox in display space: inherited, clipped to the MediaBox, and the MediaBox when absent.

        Assigning a :class:`Rect` in the current display space sets the
        page's own ``/CropBox`` in one transaction, and the visible page,
        the renderer, and text coordinates follow it. A box that leaves the
        MediaBox raises ``ValueError``.
        """
        return self._box(1)

    @crop_box.setter
    def crop_box(self, rect: Rect | Sequence[float]) -> None:
        self._set_box(1, rect)

    @property
    def bleed_box(self) -> Rect:
        """Return the page's BleedBox in display space, clipped to the MediaBox, and the CropBox when absent.

        Assigning a :class:`Rect` in the current display space sets the
        page's own ``/BleedBox`` in one transaction; a box that leaves the
        MediaBox raises ``ValueError``.
        """
        return self._box(2)

    @bleed_box.setter
    def bleed_box(self, rect: Rect | Sequence[float]) -> None:
        self._set_box(2, rect)

    @property
    def trim_box(self) -> Rect:
        """Return the page's TrimBox in display space, clipped to the MediaBox, and the CropBox when absent.

        Assigning a :class:`Rect` in the current display space sets the
        page's own ``/TrimBox`` in one transaction; a box that leaves the
        MediaBox raises ``ValueError``.
        """
        return self._box(3)

    @trim_box.setter
    def trim_box(self, rect: Rect | Sequence[float]) -> None:
        self._set_box(3, rect)

    @property
    def art_box(self) -> Rect:
        """Return the page's ArtBox in display space, clipped to the MediaBox, and the CropBox when absent.

        Assigning a :class:`Rect` in the current display space sets the
        page's own ``/ArtBox`` in one transaction; a box that leaves the
        MediaBox raises ``ValueError``.
        """
        return self._box(4)

    @art_box.setter
    def art_box(self, rect: Rect | Sequence[float]) -> None:
        self._set_box(4, rect)

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
        limits of the whole page still apply: a raster is at most 65,408
        pixels wide, 65,532 high, and 64,000,000 in all, and a larger one
        raises ``ValueError``. The GIL is released while hayro renders.
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

    def draw(self, *, underlay: bool = False) -> Canvas:
        """Return a :class:`Canvas` that draws on this page when its ``with`` block ends.

        The drawing is one transaction and one undo step, painted over the
        page's content or, with ``underlay=True``, beneath it.
        """
        return Canvas(self, underlay=underlay)

    def flatten(self, *, annotations: bool = True, widgets: bool = True) -> None:
        """Draw this page's annotation and widget appearances into its content and take them off.

        As :meth:`Document.flatten` does for one page, except that the
        widgets leave their fields, and a field leaves the form with its
        last widget, rather than the form leaving the document.
        """
        _validate_bool("annotations", annotations)
        _validate_bool("widgets", widgets)
        if not _core.flatten(self._document._core(), (self._number, self._generation), annotations, widgets):  # noqa: SLF001
            raise self._stale()

    def ocr(  # noqa: PLR0913 - OCR resource controls are keyword-only.
        self,
        *,
        engine: OcrEngine | None = None,
        dpi: float = 300,
        clip: Sequence[float] | None = None,
        rotation: OcrRotation = 0,
        tile_size: int = 1408,
        overlap: int = 192,
        min_confidence: float = 0.5,
    ) -> list[OcrWord]:
        """Recognize the rendered page's text without changing the document.

        The default engine is loaded lazily from ``pyropdf[ocr]`` and reused.
        Pass an explicit :class:`OcrEngine` to control the model set or its
        lifetime. ``clip`` recognizes one display-space region and returns
        boxes in full-page display space. ``rotation`` corrects sideways input
        clockwise without editing the page.
        """
        resolved_engine = _default_ocr_engine() if engine is None else engine
        if not isinstance(resolved_engine, OcrEngine):
            msg = f"engine must be an OcrEngine or None: {engine!r}"
            raise TypeError(msg)
        return resolved_engine._recognize_page(  # noqa: SLF001
            self,
            dpi=dpi,
            tile_size=tile_size,
            overlap=overlap,
            min_confidence=min_confidence,
            rotation=rotation,
            clip=None if clip is None else _clip_box(clip),
        )

    def apply_ocr(  # noqa: PLR0913 - OCR resource controls are keyword-only.
        self,
        *,
        words: Iterable[OcrWord] | None = None,
        engine: OcrEngine | None = None,
        dpi: float = 300,
        clip: Sequence[float] | None = None,
        rotation: OcrRotation = 0,
        skip_existing: bool = True,
        tile_size: int = 1408,
        overlap: int = 192,
        min_confidence: float = 0.5,
    ) -> list[OcrWord]:
        """Add an invisible, searchable text layer and return the words it holds.

        Without ``words``, the page is recognized as :meth:`ocr` recognizes it.
        ``words`` takes the :class:`OcrWord` records that another engine, such
        as Tesseract or a cloud service, recognized, with boxes in display
        space; the recognition options do not apply to them. The text is not
        drawn and appears only in extraction and search. An Identity-H
        reference font with ToUnicode is used without embedding font data, so
        any language, including CJK, adds almost no file size. One call takes
        at most 4,096 words with text and 1 MiB of UTF-8 text, and adds the
        layer in one transaction.

        By default, a page with extractable text is skipped, which makes
        repeated calls idempotent and avoids duplicate text layers; with
        ``clip``, only text that meets the region counts. Use
        ``skip_existing=False`` to add the layer anyway; existing text is kept.
        ``rotation`` is the clockwise correction that reads sideways input and
        orients the invisible baselines. An empty result changes nothing.
        """
        if not isinstance(skip_existing, bool):
            msg = f"skip_existing must be a bool: {skip_existing!r}"
            raise TypeError(msg)
        resolved_rotation = _validate_ocr_rotation(rotation)
        clip_rect = None if clip is None else _clip_box(clip)
        if words is not None and (
            engine is not None or (dpi, tile_size, overlap, min_confidence) != _DEFAULT_OCR_RECOGNITION
        ):
            msg = "engine, dpi, tile_size, overlap, and min_confidence apply to recognition, not to words"
            raise TypeError(msg)
        if skip_existing and self._has_text(clip_rect):
            return []
        if words is None:
            words = self.ocr(
                engine=engine,
                dpi=dpi,
                clip=clip_rect,
                rotation=resolved_rotation,
                tile_size=tile_size,
                overlap=overlap,
                min_confidence=min_confidence,
            )
        accepted = _ocr_layer_words(words)
        if accepted:
            payload = [(*word.bbox, word.text) for word in accepted]
            try:
                inserted = _core.insert_ocr_layer(
                    self._document._core(),  # noqa: SLF001
                    self._number,
                    self._generation,
                    payload,
                    resolved_rotation,
                )
            except StaleObjectError as error:
                raise self._stale() from error
            if not inserted:
                raise self._stale()
        return accepted

    def _has_text(self, clip: tuple[float, float, float, float] | None) -> bool:
        """Return whether the page has extractable text, inside ``clip`` when given."""
        if clip is None:
            return bool(self.text().strip())
        return any(
            word.text.strip()
            and word.bbox.x0 < clip[2]
            and word.bbox.x1 > clip[0]
            and word.bbox.y0 < clip[3]
            and word.bbox.y1 > clip[1]
            for word in self.words()
        )

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
        _, _, blocks, _ = self._layout(options, chars=False, max_glyphs=max_glyphs, max_size=max_size)
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

    def chars(  # noqa: PLR0913 - the text options are all keyword-only
        self,
        *,
        clip: Sequence[float] | None = None,
        clipped: bool = False,
        ligatures: bool = True,
        whitespace: bool = True,
        dehyphenate: bool = False,
        unmapped: Unmapped = "skip",
        exact_bboxes: bool = False,
        styles: bool = False,
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> Chars:
        """Return the chars of the current version of the page as :class:`Chars` columns, in layout order.

        The rows are the chars of :meth:`layout` with ``chars=True`` and the
        same options, built without a Python object per char: their
        ``block``, ``line``, and ``span`` columns index that layout, and the
        row view builds each :class:`Char` on demand. The options are those
        of :meth:`layout`.
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
            styles=styles,
        )
        try:
            columns, skipped = _core.page_chars(
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
        return Chars(columns)

    def _layout(
        self, options: _TextOptions, *, chars: bool, max_glyphs: int | None, max_size: int | None
    ) -> tuple[float, float, list[_CoreBlock], CharColumns | None]:
        """Return the core's layout of the page and the columns of its chars, and warn of the glyphs it skipped."""
        try:
            width, height, blocks, skipped, columns = _core.page_layout(
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
        return width, height, blocks, columns

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
        # Each character takes 1 to 4 bytes. Reject a long string before
        # encoding, so checking the byte limit also has bounded allocation.
        if len(text) > _MAX_SEARCH_INPUT_BYTES or (
            len(text) * 4 > _MAX_SEARCH_INPUT_BYTES and len(text.encode()) > _MAX_SEARCH_INPUT_BYTES
        ):
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

    def markdown(
        self,
        *,
        table_strategy: Literal["lines", "text"] | None = "lines",
        max_size: int | None = _markdown._DEFAULT_MAX_MARKDOWN_SIZE,  # noqa: SLF001
    ) -> str:
        """Return the current version of the page as Markdown.

        This is :meth:`Document.markdown` of this page alone, which infers
        heading sizes from this page.
        """
        _markdown.validate_options(table_strategy, max_size)
        return _markdown.pages_markdown([self], table_strategy=table_strategy, max_size=max_size)

    def tables(
        self,
        *,
        strategy: Literal["lines", "text"] = "lines",
        clip: Sequence[float] | None = None,
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> list[Table]:
        """Find the tables of the current version of the page from vector rules or aligned text.

        The ``"lines"`` strategy reads the page's stroked rules and thin
        filled rectangles. It needs an outer grid of at least two rows and
        two columns, and rebuilds rectangular merged cells. It splits a
        coarse span of the grid when at least three evenly led text records
        occupy the same cells across it.

        The ``"text"`` strategy finds a borderless table only where at least
        three consecutive rows have the same number of segments, aligned
        column edges, compatible leading, and clear gaps between columns.
        Aligned multicolumn prose can pass for a table, so use it where the
        page is known to hold tabular data.

        ``clip`` is a display-space rectangle: only the tables whose whole
        box lies inside it are returned, and no part of a table is. Each
        table carries :class:`TableDiagnostics`. The page's glyphs count
        against ``max_glyphs`` and ``max_size`` as for :meth:`text`.
        Exceeding 4,096 vector rules, 4,096 cells per candidate table,
        65,536 merged-cell candidates per grid, or 32 text columns raises
        :class:`LimitError`, with no partial table results.
        """
        if strategy not in {"lines", "text"}:
            msg = f"strategy must be 'lines' or 'text': {strategy!r}"
            raise ValueError(msg)
        clip_box = None if clip is None else _clip_box(clip)
        _validate_optional_positive_int("max_glyphs", max_glyphs)
        _validate_optional_positive_int("max_size", max_size)
        try:
            raw, skipped = _core.page_tables(
                self._document._core(),  # noqa: SLF001
                self._number,
                self._generation,
                strategy == "text",
                clip_box,
                max_glyphs,
                max_size,
            )
        except StaleObjectError as error:
            raise self._stale() from error
        _warn_skipped(skipped)
        return build_tables(raw, strategy)

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
