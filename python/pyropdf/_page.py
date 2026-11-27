"""A page of a document, which names its page object."""

from __future__ import annotations

import math
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, overload

from pyropdf import _core, _markdown
from pyropdf._actions import PageTrigger, PageTriggers, _delete_page_triggers, _replace_page_triggers
from pyropdf._annotations import Annotations
from pyropdf._canvas import Canvas
from pyropdf._clean import clean_page
from pyropdf._content import Content, Paths, _content_from_core
from pyropdf._errors import LimitError, StaleObjectError, StalePageError
from pyropdf._fonts import FontInfo, page_fonts
from pyropdf._geometry import Quad, Rect
from pyropdf._images import PageImage, page_images
from pyropdf._interchange import (
    AssociatedFiles,
    PieceData,
    PieceInfo,
    _delete_piece_info,
    _replace_piece_info,
)
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._links import Links
from pyropdf._measurement import (
    Viewport,
    _delete_page_viewports,
    _read_page_viewports,
    _write_page_viewports,
)
from pyropdf._objects import Dictionary
from pyropdf._ocr import (
    OcrEngine,
    OcrMode,
    OcrRotation,
    OcrWord,
    _active_ocr_bounds,
    _bounded_utf8_size,
    _default_ocr_engine,
    _filter_external_hybrid_words,
    _recognize_page_hybrid,
    _validate_ocr_confidence,
    _validate_ocr_dpi,
    _validate_ocr_mode,
    _validate_ocr_rotation,
    _validate_ocr_tiles,
)
from pyropdf._operations import _DEFAULT_MAX_CONTENT_SIZE, Operations, _from_core
from pyropdf._page_labels import _label, _read_page_labels
from pyropdf._pixmap import Colorspace, _render_background, _render_gray, _render_matrix, _validate_bool
from pyropdf._prepress import BoxStyle, SeparationPlate, box_styles, separations, write_box_styles, write_separations
from pyropdf._presentations import (
    NavigationStep,
    Transition,
    _delete_navigation_steps,
    _delete_page_duration,
    _delete_page_thumbnail,
    _delete_page_transition,
    _read_navigation_steps,
    _read_page_duration,
    _read_page_thumbnail,
    _read_page_transition,
    _write_navigation_steps,
    _write_page_duration,
    _write_page_thumbnail,
    _write_page_transition,
)
from pyropdf._recolor import RecolorReport, RecolorTarget, recolor
from pyropdf._tables import Table, build_tables, table_options
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
    text_options,
)
from pyropdf._text_edit import replace_text
from pyropdf._transparency import uses_transparency
from pyropdf._widgets import Widgets

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Mapping, Sequence

    from pyropdf._core import CharColumns
    from pyropdf._document import Document
    from pyropdf._markdown import ExportedImage
    from pyropdf._operations import InlineImage, Operation
    from pyropdf._pixmap import Pixmap
    from pyropdf._targets import Target
    from pyropdf._text import _CoreBlock, _TextOptions

#: Elements that one ``Page.content()`` or ``Page.paths()`` reads by default (``content_element_count``).
_DEFAULT_MAX_CONTENT_ELEMENTS = 262_144
#: UTF-8 bytes of one ``Page.svg()`` by default (``svg_output_size``).
_DEFAULT_MAX_SVG_SIZE = 64 * 1024 * 1024
#: Matches that one ``Page.search()`` returns by default (``search_hit_count``).
_DEFAULT_MAX_SEARCH_HITS = 4096
#: UTF-8 bytes of the text that ``Page.search()`` looks for (``search_input_size``).
_MAX_SEARCH_INPUT_BYTES = 4096
#: The largest magnitude of a PDF real that hayro reads as a 32-bit float.
_FLOAT32_MAX = 3.4028234663852886e38
#: The widest side passed to Rust's ``u32`` fit bound; Rust's raster limits refuse anything above 65,532.
_MAX_FIT_SIDE = 0xFFFF_FFFF
_FIT_PAIR_LENGTH = 2


def _fit_side(value: object) -> int:
    """Validate one side of ``fit`` and clamp huge integers to ``u32`` for Rust's raster-limit check."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"fit must be a positive int or a (width, height) pair of positive ints: {value!r}"
        raise TypeError(msg)
    if value <= 0:
        msg = f"fit dimensions must be positive: {value!r}"
        raise ValueError(msg)
    return min(value, _MAX_FIT_SIDE)


def _render_scale(
    matrix: Sequence[float] | None,
    dpi: float | None,
    fit: int | tuple[int, int] | None,
) -> tuple[tuple[float, float, float, float], tuple[int, int] | None]:
    """Resolve ``dpi``, ``matrix``, or ``fit`` into ``(linear_matrix, fit_box)``."""
    if fit is None:
        return _render_matrix(matrix, dpi), None
    if dpi is not None or matrix is not None:
        msg = "pass at most one of dpi, matrix, and fit"
        raise ValueError(msg)
    if isinstance(fit, bool):
        msg = f"fit must be a positive int or a (width, height) pair of positive ints: {fit!r}"
        raise TypeError(msg)
    if isinstance(fit, int):
        side = _fit_side(fit)
        return (1.0, 0.0, 0.0, 1.0), (side, side)
    if isinstance(fit, (tuple, list)):
        if len(fit) != _FIT_PAIR_LENGTH:
            msg = f"fit must be a positive int or a (width, height) pair of positive ints: {fit!r}"
            raise ValueError(msg)
        return (1.0, 0.0, 0.0, 1.0), (_fit_side(fit[0]), _fit_side(fit[1]))
    msg = f"fit must be a positive int or a (width, height) pair of positive ints: {fit!r}"
    raise TypeError(msg)


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

    def _reader(self) -> _core._Document | _core._PageSnapshot:
        """Return the source for layout, tables and painted content."""
        return self._document._core()  # noqa: SLF001

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

    def fonts(self) -> list[FontInfo]:
        """Return the fonts that the page's resources and its Form XObjects' resources name, one per font dictionary.

        The resources are the page's own or those it inherits from the page
        tree; Form XObjects are followed through their ``/Resources`` to any
        depth, each once. Fonts that only annotation appearances or Type 3
        glyph procedures name are not listed. A page tree deeper than 64
        parents, more than 4096 forms, or more than 65536 font and XObject
        entries over the walk raise :class:`LimitError`.
        :meth:`Document.font_program` returns a font's embedded program.
        """
        return page_fonts(self)

    @property
    def label(self) -> str:
        """Return the page's label from ``doc.page_labels``, or ``""`` when no range covers it."""
        return _label(_read_page_labels(self._document), self.index)

    @property
    def duration(self) -> float | None:
        """Return the page's presentation display duration in seconds (``/Dur``), or ``None`` when absent.

        Assigning a non-negative finite number sets ``/Dur`` in one transaction;
        ``del page.duration`` removes it.
        """
        return _read_page_duration(self)

    @duration.setter
    def duration(self, duration: float) -> None:
        _write_page_duration(self, duration)

    @duration.deleter
    def duration(self) -> None:
        _delete_page_duration(self)

    @property
    def transition(self) -> Transition | None:
        """Return the page's presentation :class:`Transition` (``/Trans``), or ``None`` when absent.

        Assigning a :class:`Transition` writes ``/Trans`` in one transaction,
        promoting the catalog version to PDF 1.5 when the style or options need
        it; ``del page.transition`` removes it.
        """
        return _read_page_transition(self)

    @transition.setter
    def transition(self, transition: Transition) -> None:
        _write_page_transition(self, transition)

    @transition.deleter
    def transition(self) -> None:
        _delete_page_transition(self)

    @property
    def navigation_steps(self) -> tuple[NavigationStep, ...]:
        """Return the page's sub-page presentation sequence (``/PresSteps``) as :class:`NavigationStep` records.

        Reads walk the ``NavNode`` ``/Next`` chain iteratively up to 4,096 steps
        with cycle detection. Assigning an iterable of :class:`NavigationStep`
        records writes indirect ``NavNode`` dictionaries linked with ``/Next``
        and ``/Prev`` in one transaction (or removes ``/PresSteps`` when empty)
        and frees unreferenced retired nodes; ``del page.navigation_steps``
        removes ``/PresSteps`` and frees unreferenced nodes.
        """
        return _read_navigation_steps(self)

    @navigation_steps.setter
    def navigation_steps(self, steps: Iterable[NavigationStep]) -> None:
        _write_navigation_steps(self, steps)

    @navigation_steps.deleter
    def navigation_steps(self) -> None:
        _delete_navigation_steps(self)

    @property
    def triggers(self) -> PageTriggers:
        """Return the page's ``/AA`` additional actions as a live :class:`PageTriggers` mutable mapping.

        Assigning a mapping replaces ``Page['AA']`` in one transaction, and
        ``del page.triggers`` removes it.
        """
        _ = self.index
        return PageTriggers(self)

    @triggers.setter
    def triggers(self, triggers: Mapping[PageTrigger, Target] | Iterable[tuple[PageTrigger, Target]]) -> None:
        _replace_page_triggers(self, triggers)

    @triggers.deleter
    def triggers(self) -> None:
        _delete_page_triggers(self)

    @property
    def associated_files(self) -> AssociatedFiles:
        """Return the page's ``/AF`` associated files as a live :class:`AssociatedFiles` sequence."""
        _ = self.index
        return AssociatedFiles(self._document, self._page_host)

    @property
    def piece_info(self) -> PieceInfo:
        """Return, replace, or delete the page's ``/PieceInfo`` application data as a live :class:`PieceInfo`."""
        _ = self.index
        return PieceInfo(self._document, self._page_host, is_page=True)

    @piece_info.setter
    def piece_info(self, values: Mapping[str, PieceData] | Iterable[tuple[str, PieceData]]) -> None:
        _replace_piece_info(self._document, self._page_host, values, is_page=True)

    @piece_info.deleter
    def piece_info(self) -> None:
        _delete_piece_info(self._document, self._page_host, is_page=True)

    def _page_host(self) -> Dictionary:
        _ = self.index
        return self.obj

    @property
    def thumbnail(self) -> Pixmap | None:
        """Return the page's embedded thumbnail image (``/Thumb``) decoded via hayro, or ``None``.

        Assigning a :class:`Pixmap` writes an indirect Flate-compressed
        ``DeviceGray`` or ``DeviceRGB`` thumbnail stream (flattening alpha onto
        white) in one transaction and frees any unreferenced retired thumbnail
        stream; ``del page.thumbnail`` removes ``/Thumb`` and frees its stream
        when unreferenced.
        """
        return _read_page_thumbnail(self)

    @thumbnail.setter
    def thumbnail(self, pixmap: Pixmap) -> None:
        _write_page_thumbnail(self, pixmap)

    @thumbnail.deleter
    def thumbnail(self) -> None:
        _delete_page_thumbnail(self)

    @property
    def box_styles(self) -> dict[str, BoxStyle]:
        """Local guideline styles keyed by CropBox, BleedBox, TrimBox or ArtBox.

        An absent entry leaves the display choice to the reader. Assign a mapping
        to replace the known styles or delete the property to remove them.
        """
        return _core.read_operation(self.document._core(), lambda _: box_styles(self))  # noqa: SLF001

    @box_styles.setter
    def box_styles(self, styles: Mapping[str, BoxStyle]) -> None:
        write_box_styles(self, styles)

    @box_styles.deleter
    def box_styles(self) -> None:
        write_box_styles(self, {})

    @property
    def separations(self) -> dict[Page, SeparationPlate]:
        """The ordered pages of this pre-separated group and their colorants.

        Assign a mapping including this page to create or replace the group.
        Omitted members become ordinary pages. Deleting dissolves the group
        in one transaction. A page already in another group must leave it first.
        """
        return _core.read_operation(self.document._core(), lambda _: separations(self))  # noqa: SLF001

    @separations.setter
    def separations(self, plates: Mapping[Page, SeparationPlate]) -> None:
        write_separations(self, plates)

    @separations.deleter
    def separations(self) -> None:
        write_separations(self, {})

    @property
    def viewports(self) -> tuple[Viewport, ...]:
        """Return the page's measurement viewports (``/VP``) as :class:`Viewport` records in display coordinates.

        Each :attr:`Viewport.bbox` is converted between PDF user space and the
        page's display space across ``/CropBox``, ``/Rotate``, and ``/UserUnit``,
        and numeric measurement values (:class:`NumberFormat`,
        :class:`RectilinearMeasure`, :class:`GeospatialMeasure`, and
        :class:`PointData`) are preserved as :class:`~decimal.Decimal` values.
        Assigning an iterable of :class:`Viewport` records replaces ``Page['VP']``
        in one transaction (promoting the catalog version to ``1.6`` or ``2.0``
        as required, or removing ``/VP`` when empty) and frees unreferenced
        retired objects; ``del page.viewports`` removes ``/VP`` and frees
        unreferenced objects.
        """
        return _read_page_viewports(self)

    @viewports.setter
    def viewports(self, viewports: Iterable[Viewport]) -> None:
        _write_page_viewports(self, viewports)

    @viewports.deleter
    def viewports(self) -> None:
        _delete_page_viewports(self)

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

    def remove_rotation(self) -> None:
        """Set the rotation to 0 while keeping what the page displays, in one transaction.

        The page's user space turns with it: the content draws through one
        more matrix, and the page boxes and the geometry of the annotations,
        links, and widgets move with it. Annotation appearances turn through
        copies of their streams, so other pages that share a stream keep it as
        it was; an annotation with the NoRotate flag stays upright, its
        rectangle's upper-left corner where it was displayed. Display space is
        the same before and after, so every display coordinate stays valid.
        A widget's ``/MK /R`` turns too, so that a regenerated appearance is as
        upright as the present one.

        An annotation or ``/Annots`` array that another page also lists raises
        ``ValueError`` and changes nothing. Explicit destinations that name the
        page, in links, actions, the outline, named destinations, or anywhere
        else in the document, move with it; a horizontal fit becomes a vertical
        one on a quarter turn, and a null coordinate stays null. A page without
        rotation is left as it is.
        """
        if not _core.remove_page_rotation(self._document._core(), self._number, self._generation):  # noqa: SLF001
            raise self._stale()

    @property
    def rect(self) -> Rect:
        """Return the display :class:`Rect` ``(0, 0, width, height)``, with the rotation applied.

        The visible page is the CropBox clipped to the MediaBox; a box that
        leaves nothing visible gives the A4 page that the renderer draws.
        Display coordinates are points of 1/72 inch: a page's ``/UserUnit``
        sets the size of its user space unit, so a 400 by 300 MediaBox with
        ``/UserUnit 2`` is 800 by 600 points.
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
        fit: int | tuple[int, int] | None = None,
        clip: Sequence[float] | None = None,
        alpha: bool = False,
        background: tuple[int, int, int] | tuple[int, int, int, int] | None = None,
        annotations: bool = True,
        colorspace: Colorspace | Literal["gray", "rgb"] = Colorspace.RGB,
        hinting: bool = False,
        glyph_cache: bool = False,
        antialias: bool = True,
    ) -> Pixmap:
        """Render the current version of the page to a :class:`Pixmap`.

        ``dpi`` scales the page uniformly, 72 by default; ``matrix`` is a
        :class:`Matrix` or six entries, whose translation is dropped, since
        the raster covers the transformed page, or a linear map
        ``(a, b, c, d)``; ``fit`` scales the page (or ``clip`` when given)
        uniformly to fit within ``(width, height)`` pixels, with an integer
        ``n`` meaning ``(n, n)``. At most one of ``dpi``, ``matrix``, and
        ``fit`` may be given. Without ``alpha`` the page lies on white.
        ``background`` is an RGB or RGBA tuple with components from 0 to 255
        painted beneath the page, which must be opaque without ``alpha``.
        ``annotations=False`` leaves out the appearances of annotations and
        form widgets. ``colorspace="gray"`` converts the rendered colors to
        gray with ITU-R BT.601 luma weights.

        ``hinting=True`` hints text, as FreeType's light hinting does, which
        sharpens small text: horizontal text filled at 4 to 128 pixels per
        em, and not stroked or clipping text. The fonts that
        :meth:`Document.subset_fonts` subsets and that a save compacts can
        draw differently hinted than their originals, so hinted renders of a
        document before and after either may differ.

        ``glyph_cache=True`` draws text through hayro's glyph cache, which
        rasterizes each glyph once at each size and reuses it, so that text
        renders faster. The cache places a glyph at a whole pixel vertically
        and a quarter of a pixel horizontally, so text moves by up to half a
        pixel vertically. It holds horizontal text filled in one color, in
        glyphs up to 128 pixels wide and high; other text is filled where it
        lies. Its memory is outside :class:`DocumentLimits`: it grows with
        each glyph that the document draws at another size, and lasts as
        long as the thread's render cache for the document's version, which
        the thread keeps until it renders another version.

        ``antialias=False`` disables anti-aliasing for fills, strokes, and
        text by passing hayro's aliasing threshold, producing hard-edged
        vector and text output while still drawing raster images normally.
        When ``antialias=False``, hayro bypasses the glyph cache.

        ``clip`` is a display rectangle ``(x0, y0, x1, y1)``: its bounding
        box under the matrix, rounded outward to whole pixels and
        intersected with the page, is all that is rasterized. A clip that
        leaves no pixel of the page raises :class:`PdfError`.

        A raster is at most 65,408 pixels wide, 65,532 high, and 64,000,000
        in all, and a larger one raises ``ValueError``. With ``clip``, the
        limits apply to the clipped raster, so a small region renders at
        scales at which the whole page would not. If either side of the
        transformed page exceeds 524,288 pixels, clipped rendering raises
        :class:`LimitError` with code ``clip_page_side``. The GIL is
        released while hayro renders.

        These dimensions bound the output raster. Image decoding and
        rasterization inside hayro can exceed the document's memory and
        time limits. See [Reading and rendering](../limits.md#reading-and-rendering).
        """
        linear, fit_box = _render_scale(matrix, dpi, fit)
        rgba = _render_background(background, alpha=alpha)
        _validate_bool("annotations", annotations)
        _validate_bool("hinting", hinting)
        _validate_bool("glyph_cache", glyph_cache)
        _validate_bool("antialias", antialias)
        gray = _render_gray(colorspace)
        box = None if clip is None else _clip_box(clip)
        try:
            return _core.render_page(
                self._reader(),
                self._number,
                self._generation,
                linear,
                fit_box,
                alpha,
                gray,
                rgba,
                box,
                annotations,
                hinting,
                glyph_cache,
                antialias,
            )
        except StaleObjectError as error:
            raise self._stale() from error

    def draw(self, *, underlay: bool = False) -> Canvas:
        """Return a :class:`Canvas` that draws on this page when its ``with`` block ends.

        The drawing is one transaction and one undo step, painted over the
        page's content or, with ``underlay=True``, beneath it.
        """
        return Canvas(self, underlay=underlay)

    def replace_text(self, old: str, new: str, *, count: int | None = None) -> int:
        """Replace ``old`` with ``new`` where simple fonts draw it, and return the number of replacements.

        ``old`` is found in the strings of each text-showing operation of
        the page's content and of the Form XObjects that it draws, decoded
        through the operation's Type 1, TrueType, or Type 3 font as text
        extraction reads it, across the strings and kerning of a ``TJ``
        array but not across operations. ``new`` is encoded through the
        same font, and a kerning number after it makes up the difference in
        advance, so the text that follows keeps its position. A Form
        XObject's text is read with the text state where it is drawn.
        ``count`` bounds the replacements, in drawing order; ``None``
        replaces all.

        A character of ``new`` that the font has no code for, and ``old``
        drawn in a composite (Type 0) font of the page's content, raise
        :class:`ValueError` and leave the document unchanged. Form XObjects
        nested deeper than 16, or more than 4,096 drawings of them, raise
        :class:`LimitError`. A font subset is not extended, so a code
        without a glyph in the program shows as the program's missing glyph.
        A drawing of a Form XObject that changes draws a copy, so the other
        drawings, pages, and Forms that share it keep the old text. The
        replacement is one transaction.
        """
        return replace_text(self, old, new, count=count)

    def recolor(self, target: RecolorTarget) -> RecolorReport:
        """Convert the colors that the page paints to ``target``: ``"gray"``, ``"rgb"``, or ``"cmyk"``.

        Rewrites the color operators of the page's content, of the Form
        XObjects that it draws, and of its annotations' appearances, and
        converts its images and the color arrays of its annotations; see
        :meth:`Document.recolor`. Returns what it converted and what it
        left as it was.
        """
        return recolor(self._document, target, [self.index])

    def clean_contents(self, *, annotations: bool = True) -> None:
        """Normalize the page's content and drop the resources that it never names, in one transaction.

        The content becomes one new stream written from the values of its
        operations, one per line, without comments; a ``Q`` without its
        ``q`` is dropped, an open ``q`` is closed at the end, and empty
        ``q Q`` and ``BT ET`` pairs go, so what renders and the text stay as
        they were. (hayro draws annotation appearances in the graphics state
        that an open ``q`` leaves; after cleaning it draws them from the
        initial state, as Ghostscript does before and after.) The page gets its own ``/Resources``, so a dictionary
        that other pages share or that the page inherits stays theirs,
        holding only the fonts, XObjects, graphics states, color spaces,
        patterns, shadings, and property lists that the content names, the
        default color spaces, and every other entry. Resources stay whole
        when the content draws a Form XObject or a Type 3 font that has no
        resources of its own, since it draws with the page's. With
        ``annotations``, every annotation and widget appearance stream is
        cleaned in place the same way, with its own resources.

        Content with bytes that hayro stops reading at raises
        :class:`ValueError`, and content past the reading limits of
        :attr:`operations` raises :class:`LimitError`; either leaves the
        document unchanged.
        """
        _validate_bool("annotations", annotations)
        clean_page(self, annotations=annotations)

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
        mode: OcrMode = "full",
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

        ``mode`` selects what to recognize: ``"full"`` (the default) recognizes
        the whole page or ``clip`` regardless of existing text;
        ``"skip_existing"`` returns an empty list when extractable text already
        exists on the page or in ``clip``; and ``"hybrid"`` locates placed
        raster-image regions not already covered by legible native text,
        recognizes only those regions under the document's limits, and drops
        any recognized words that overlap existing native text.
        """
        resolved_mode = _validate_ocr_mode(mode)
        clip_rect = None if clip is None else _clip_box(clip)
        if engine is not None and not isinstance(engine, OcrEngine):
            msg = f"engine must be an OcrEngine or None: {engine!r}"
            raise TypeError(msg)
        if resolved_mode == "hybrid":
            return _recognize_page_hybrid(
                self,
                engine=engine,
                dpi=dpi,
                tile_size=tile_size,
                overlap=overlap,
                min_confidence=min_confidence,
                rotation=rotation,
                clip=clip_rect,
            )
        if resolved_mode == "skip_existing":
            _validate_ocr_dpi(dpi)
            _validate_ocr_tiles(tile_size, overlap)
            _validate_ocr_confidence(min_confidence)
            _validate_ocr_rotation(rotation)
            _active_ocr_bounds(self, clip_rect)
            if self._has_text(clip_rect):
                return []
        resolved_engine = _default_ocr_engine() if engine is None else engine
        return resolved_engine._recognize_page(  # noqa: SLF001
            self,
            dpi=dpi,
            tile_size=tile_size,
            overlap=overlap,
            min_confidence=min_confidence,
            rotation=rotation,
            clip=clip_rect,
        )

    def apply_ocr(  # noqa: PLR0913 - OCR resource controls are keyword-only.
        self,
        *,
        words: Iterable[OcrWord] | None = None,
        engine: OcrEngine | None = None,
        dpi: float = 300,
        clip: Sequence[float] | None = None,
        rotation: OcrRotation = 0,
        mode: OcrMode = "skip_existing",
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

        ``mode`` selects how existing text is handled: ``"skip_existing"``
        (the default) skips a page or ``clip`` that already holds extractable
        text; ``"hybrid"`` recognizes only raster-image regions not covered by
        legible native text (or filters supplied ``words`` against existing
        native text) so mixed pages gain OCR text without duplicating native
        words; and ``"full"`` adds the layer regardless of existing text.
        ``rotation`` is the clockwise correction that reads sideways input and
        orients the invisible baselines. An empty result changes nothing.
        """
        resolved_mode = _validate_ocr_mode(mode)
        resolved_rotation = _validate_ocr_rotation(rotation)
        clip_rect = None if clip is None else _clip_box(clip)
        if words is not None and (
            engine is not None or (dpi, tile_size, overlap, min_confidence) != _DEFAULT_OCR_RECOGNITION
        ):
            msg = "engine, dpi, tile_size, overlap, and min_confidence apply to recognition, not to words"
            raise TypeError(msg)
        if resolved_mode == "skip_existing" and self._has_text(clip_rect):
            return []
        if words is None:
            recognized = self.ocr(
                engine=engine,
                dpi=dpi,
                clip=clip_rect,
                rotation=resolved_rotation,
                mode=resolved_mode,
                tile_size=tile_size,
                overlap=overlap,
                min_confidence=min_confidence,
            )
            accepted = _ocr_layer_words(recognized)
        else:
            accepted = _ocr_layer_words(words)
            if resolved_mode == "hybrid":
                accepted = _filter_external_hybrid_words(self, accepted, clip_rect, resolved_rotation)
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

        A complete result can be rendered again with :meth:`Content.pixmap` or
        placed with :meth:`Canvas.page` while this document is open, even after a
        change to the original page. Filtering by kind or clip yields items only.

        Items come in paint order, in display coordinates, as hayro
        interprets the current version of the page, with optional-content
        visibility applied. Each carries its paints, its blend mode and soft
        mask, and the :class:`GraphicsContext` of clips, transparency groups,
        and marked content that encloses it. Glyph boxes are the exact boxes
        of the glyph outlines. A text run describes a CJK font that the
        document does not embed as the face of
        :attr:`Document.fallback_fonts` that draws it, in its glyph IDs, its
        glyph and run boxes, and its :class:`FontRef`; a glyph's matrix and
        advance stay what the page gives without a face. Without a face the
        glyphs have no box, and the run's box spans their origins and advances.
        hayro paints every image inside a transparency group that holds the
        graphics state's opacity, blend mode, and soft mask, so an image's
        context ends with that group. A shading painted with ``sh`` is a
        :class:`Path` that covers the clip region and fills with a
        :class:`ShadingPattern`.

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

        The element count bounds what pyropdf collects. hayro's stream
        decoding and interpretation can exceed the
        [document limits](../limits.md#reading-and-rendering).
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
            reader = self._reader()
            snapshot = None
            if text_runs:
                snapshot = reader if isinstance(reader, _core._PageSnapshot) else _core.snapshot_pages(reader)  # noqa: SLF001
            raw = _core.page_content(
                snapshot if snapshot is not None else reader,
                self._number,
                self._generation,
                (text_runs, paths, images),
                annotations,
                clip_box,
                max_elements,
            )
        except StaleObjectError as error:
            raise self._stale() from error
        content = _content_from_core(raw, self._document, snapshot)
        if snapshot is not None and paths and images and clip_box is None:
            object.__setattr__(content, "_page", _RetainedPage(self, snapshot, annotations=annotations))
        return content

    def paths(
        self,
        *,
        clip: Sequence[float] | None = None,
        annotations: bool = True,
        max_elements: int | None = _DEFAULT_MAX_CONTENT_ELEMENTS,
    ) -> Paths:
        """Return the paths that the page paints as :class:`Paths` columns, in paint order.

        The rows are the paths of :meth:`content` with ``text_runs=False``,
        ``images=False``, and the same options, built from the same
        interpretation without a Python object per path or segment:
        ``max_elements`` counts what it counts there, and above it the call
        raises :class:`LimitError` with code ``content_element_count``. The
        row view builds each :class:`Path` on demand. The GIL is released
        while hayro interprets the page and the columns are built.

        Requesting only paths still runs hayro's interpreter. Its stream
        decoding and working memory can exceed the
        [document limits](../limits.md#reading-and-rendering).
        """
        _validate_bool("annotations", annotations)
        _validate_optional_positive_int("max_elements", max_elements)
        clip_box = None if clip is None else _clip_box(clip)
        try:
            columns = _core.page_paths(
                self._document._core(),  # noqa: SLF001
                self._number,
                self._generation,
                annotations,
                clip_box,
                max_elements,
            )
        except StaleObjectError as error:
            raise self._stale() from error
        return Paths._from_columns(columns)  # noqa: SLF001 - package constructor.

    def text(  # noqa: PLR0913 - the text options are all keyword-only
        self,
        *,
        clip: Sequence[float] | None = None,
        clipped: bool = False,
        ligatures: bool = True,
        whitespace: bool = True,
        dehyphenate: bool = False,
        unmapped: Unmapped = "skip",
        actual_text: bool = True,
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> str:
        """Return the text of the current version of the page in reading order.

        Reading order follows the geometry and script heuristics described in
        the documentation rather than the document's semantic order. Spaces
        between words are inferred from glyph positions, and lines end with a
        newline. Annotation appearances count as page content. The
        ``/ActualText`` of a marked-content sequence replaces the text of the
        glyphs inside it unless ``actual_text=False``, which extracts the
        underlying glyph Unicode directly.

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
        without Unicode becomes (see :data:`Unmapped`). With
        ``unmapped="type3_char_code"``, an unmapped Type 3 glyph's original
        character code is interpreted as a Unicode code point. This guess
        can produce unrelated text for a custom font encoding.

        ``max_glyphs`` bounds the glyphs collected, 262,144 by default, with
        :class:`LimitError` code ``text_glyph_count``, and ``max_size`` the
        UTF-8 of their text and of the returned text, 64 MiB by default, with
        code ``text_size``; ``None`` lifts either. The GIL is released while
        hayro interprets the page.

        A page with little text can still take substantial memory and time
        for hayro to decode and interpret. See the
        [document limits for page reading](../limits.md#reading-and-rendering).
        """
        options = text_options(
            clip=None if clip is None else _clip_box(clip),
            clipped=clipped,
            ligatures=ligatures,
            whitespace=whitespace,
            dehyphenate=dehyphenate,
            unmapped=unmapped,
            actual_text=actual_text,
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
        actual_text: bool = True,
        exact_bboxes: bool = False,
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> list[Word]:
        """Return the words of the current version of the page in reading order, as :class:`Word` records.

        Boxes are in display space and span the glyphs' advances and the
        font's ascender and descender, or their outlines with
        ``exact_bboxes=True``, which depend on :attr:`Document.fallback_fonts`
        for a CJK font that the document does not embed: they are the outlines
        of the face that draws it. The other options are those of :meth:`text`; a
        word that dehyphenation joins is one :class:`Word` on its first line
        whose box spans both parts.

        As with :meth:`text`, the glyph and text limits bound what is collected.
        hayro can exceed the [document limits](../limits.md#reading-and-rendering)
        while decoding and interpreting the page.
        """
        options = text_options(
            clip=None if clip is None else _clip_box(clip),
            clipped=clipped,
            ligatures=ligatures,
            whitespace=whitespace,
            dehyphenate=dehyphenate,
            unmapped=unmapped,
            actual_text=actual_text,
            max_glyphs=max_glyphs,
            max_size=max_size,
            exact_bboxes=exact_bboxes,
        )
        try:
            words, skipped = _core.page_words(
                self._reader(),
                self._number,
                self._generation,
                options,
                max_glyphs,
                max_size,
            )
        except StaleObjectError as error:
            raise self._stale() from error
        _warn_skipped(skipped)
        return words

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
        actual_text: bool = True,
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
        ``exact_bboxes=True``, which depend on :attr:`Document.fallback_fonts`
        for a CJK font that the document does not embed: they are the outlines
        of the face that draws it. Dehyphenation removes the hyphen from the end
        of the line and keeps both lines. ``styles=True`` also reads the
        page's vector rules and sets :attr:`Span.is_underlined` and
        :attr:`Span.is_struck` where a horizontal rule underlines or strikes
        out horizontal text. The other options are those of :meth:`text`, and
        ``max_size`` also bounds the UTF-8 of the layout's strings.
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
            actual_text=actual_text,
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
        actual_text: bool = True,
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
            actual_text=actual_text,
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
        return Chars._from_columns(columns)  # noqa: SLF001 - package constructor.

    def _layout(
        self, options: _TextOptions, *, chars: bool, max_glyphs: int | None, max_size: int | None
    ) -> tuple[float, float, list[_CoreBlock], CharColumns | None]:
        """Return the core's layout of the page and the columns of its chars, and warn of the glyphs it skipped."""
        try:
            width, height, blocks, skipped, columns = _core.page_layout(
                self._reader(),
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
        actual_text: bool = ...,
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
        actual_text: bool = ...,
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
        actual_text: bool = True,
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
            actual_text=actual_text,
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

    @contextmanager
    def _export_single(self) -> Generator[Page]:
        """Pin this page and its document to one version for a single-page export."""
        if isinstance(self, _ExportPage):
            yield self
            return
        from pyropdf._fallback_fonts import _set_fallback  # noqa: PLC0415

        pinned = self._document._import_source(_core.attachment_reader(self._document._core()))  # noqa: SLF001
        try:
            if self._document._fallback is not None:  # noqa: SLF001
                _set_fallback(pinned, self._document._fallback)  # noqa: SLF001
            idx = _core.page_index(pinned._core(), self._number, self._generation)  # noqa: SLF001
            if idx is None:
                raise self._stale()
            snapshot = _core.snapshot_pages(pinned._core())  # noqa: SLF001
            yield _ExportPage(pinned, snapshot, idx)
        finally:
            pinned.close()

    def markdown(  # noqa: PLR0913 - keyword-only retrieval export options.
        self,
        *,
        table_strategy: Literal["lines", "text"] | None = "lines",
        use_structure: Literal["auto", "always", "never"] = "auto",
        expand_abbreviations: bool = False,
        margins: float | tuple[float, float] | tuple[float, float, float, float] | None = None,
        images: str | os.PathLike[str] | Callable[[ExportedImage], str | None] | None = None,
        max_graphics: int | None = _markdown._DEFAULT_MAX_GRAPHICS,  # noqa: SLF001
        max_size: int | None = _markdown._DEFAULT_MAX_MARKDOWN_SIZE,  # noqa: SLF001
    ) -> str:
        """Return the current version of the page as Markdown.

        This is :meth:`Document.markdown` of this page alone, using the
        document's structure tree when enabled and trustworthy, or inferring
        heading sizes from this page when falling back to geometric extraction.
        ``margins``, ``images``, and ``max_graphics`` work as on
        :meth:`Document.markdown`.
        """
        _markdown.validate_options(
            table_strategy,
            max_size,
            use_structure=use_structure,
            expand_abbreviations=expand_abbreviations,
            margins=margins,
            images=images,
            max_graphics=max_graphics,
        )
        with self._export_single() as export_page:
            return _markdown.pages_markdown(
                [export_page],
                table_strategy=table_strategy,
                max_size=max_size,
                use_structure=use_structure,
                expand_abbreviations=expand_abbreviations,
                margins=margins,
                images=images,
                max_graphics=max_graphics,
            )

    def tables(  # noqa: PLR0913 - table detection options are all keyword-only
        self,
        *,
        strategy: Literal["lines", "text"] = "lines",
        clip: Sequence[float] | None = None,
        horizontal_lines: Sequence[float] | None = None,
        vertical_lines: Sequence[float] | None = None,
        snap_tolerance: float = 1.0,
        join_tolerance: float = 1.0,
        min_gutter: float = 2.0,
        min_words_vertical: int = 3,
        min_words_horizontal: int = 2,
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> list[Table]:
        """Find the tables of the current version of the page from vector rules or aligned text.

        The ``"lines"`` strategy reads the page's stroked rules and thin
        filled rectangles. It needs an outer grid of at least two rows and
        two columns, and rebuilds rectangular merged cells. It splits a
        coarse span of the grid when at least ``min_words_vertical`` evenly led
        text records occupy the same cells across it.

        The ``"text"`` strategy finds a borderless table only where at least
        ``min_words_vertical`` consecutive rows have the same number of
        segments (at least ``min_words_horizontal``), aligned column edges,
        compatible leading, and column gaps wider than ``min_gutter`` ems.
        Aligned multicolumn prose can pass for a table, so use it where the
        page is known to hold tabular data.

        ``horizontal_lines`` and ``vertical_lines`` supply explicit
        display-space ``y`` and ``x`` coordinates (in points, after rotation
        and crop box) to define or augment table grid boundaries on borderless
        or partially ruled pages. ``snap_tolerance`` (points, 0 to 1,000) sets
        the coordinate clustering and alignment tolerance, and
        ``join_tolerance`` (points, 0 to 1,000) sets the gap across which
        near-miss rule corners and collinear fragments join.

        ``clip`` is a display-space rectangle: only the tables whose whole
        box lies inside it are returned, and no part of a table is. Each
        table carries :class:`TableDiagnostics`, detected :attr:`Table.header_rows`
        and :attr:`Table.headers`, and structured exports (:meth:`Table.to_dicts`,
        :meth:`Table.to_dataframe`, :meth:`Table.markdown`). The page's glyphs
        count against ``max_glyphs`` and ``max_size`` as for :meth:`text`.
        Exceeding 4,096 vector rules (including explicit guides), 4,096 cells
        per candidate table, 65,536 merged-cell candidates per grid, or 32 text
        columns raises :class:`LimitError`, with no partial table results.
        """
        clip_box = None if clip is None else _clip_box(clip)
        options = table_options(
            strategy=strategy,
            clip=clip_box,
            horizontal_lines=horizontal_lines,
            vertical_lines=vertical_lines,
            snap_tolerance=snap_tolerance,
            join_tolerance=join_tolerance,
            min_gutter=min_gutter,
            min_words_vertical=min_words_vertical,
            min_words_horizontal=min_words_horizontal,
        )
        _validate_optional_positive_int("max_glyphs", max_glyphs)
        _validate_optional_positive_int("max_size", max_size)
        try:
            raw, skipped = _core.page_tables(
                self._reader(),
                self._number,
                self._generation,
                options,
                max_glyphs,
                max_size,
            )
        except StaleObjectError as error:
            raise self._stale() from error
        _warn_skipped(skipped)
        return build_tables(raw, strategy)

    def uses_transparency(self) -> bool:
        """Return whether the current page uses transparent imaging under ISO 32000-2 Annex Q.

        Check paint operations, selected Forms, images, patterns, Type 3
        character procedures and annotation appearances. The query follows only
        the resources reached by painting. Cyclic resources and exhausted
        limits raise an error.
        """
        return uses_transparency(self)

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


class _RetainedPage(Page):
    """Geometry and paints from the same version as a complete Content result."""

    __slots__ = ("_annotations_included", "_snapshot")

    def __init__(self, page: Page, snapshot: _core._PageSnapshot, *, annotations: bool) -> None:
        super().__init__(page.document, page.xref, page._generation)  # noqa: SLF001
        self._snapshot = snapshot
        self._annotations_included = annotations

    @property
    def annotations_included(self) -> bool:
        return self._annotations_included

    def _reader(self) -> _core._PageSnapshot:
        return self._snapshot


class _ExportPage(Page):
    """The page at a fixed index in the version captured for a document export."""

    __slots__ = ("_index", "_snapshot")

    def __init__(self, document: Document, snapshot: _core._PageSnapshot, index: int) -> None:
        super().__init__(document, *snapshot.page_object(index))
        self._snapshot = snapshot
        self._index = index

    @property
    def index(self) -> int:
        return self._index

    def _reader(self) -> _core._PageSnapshot:
        return self._snapshot


__all__ = ["Page"]
