"""A page of a document, which names its page object."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

from pyropdf import _core
from pyropdf._content import Content, _content_from_core
from pyropdf._errors import StaleObjectError, StalePageError
from pyropdf._geometry import Rect
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import Dictionary
from pyropdf._operations import Operations, _from_core
from pyropdf._pixmap import Colorspace, _render_background, _render_gray, _render_matrix, _validate_bool

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyropdf._document import Document
    from pyropdf._pixmap import Pixmap

#: Elements that one ``Page.content()`` reads by default (``content_element_count``).
_DEFAULT_MAX_CONTENT_ELEMENTS = 262_144
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

    @property
    def operations(self) -> Operations:
        """Return the page's content as :class:`Operations`, as hayro reads it.

        The content streams of ``/Contents`` are decoded and joined as the
        renderer joins them, each stream of an array followed by one space,
        and tokenized with hayro's parser, so the operations are the ones
        that rendering interprets. Each operation keeps its bytes, so
        ``page.operations.to_bytes()`` is that content.

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
