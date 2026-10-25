"""A page of a document, which names its page object."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._errors import StaleObjectError, StalePageError
from pyropdf._pixmap import _render_matrix

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyropdf._document import Document
    from pyropdf._pixmap import Pixmap


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
    def rect(self) -> tuple[float, float, float, float]:
        """Return the display rectangle ``(0, 0, width, height)``, with the rotation applied.

        The visible page is the CropBox clipped to the MediaBox; a box that
        leaves nothing visible gives the A4 page that the renderer draws.
        """
        _, width, height = self._geometry()
        return (0.0, 0.0, width, height)

    def pixmap(
        self,
        *,
        dpi: float | None = None,
        matrix: Sequence[float] | None = None,
        alpha: bool = False,
    ) -> Pixmap:
        """Render the current version of the page to a :class:`Pixmap`.

        ``dpi`` scales the page uniformly, 72 by default; ``matrix`` is a
        linear map ``(a, b, c, d)``, or six entries whose translation is
        dropped, since the raster covers the transformed page. Without
        ``alpha`` the page lies on white. The GIL is released while hayro
        renders.
        """
        linear = _render_matrix(matrix, dpi)
        if not isinstance(alpha, bool):
            msg = f"alpha must be a bool: {alpha!r}"
            raise TypeError(msg)
        try:
            return _core.render_page(self._document._core(), self._number, self._generation, linear, alpha)  # noqa: SLF001
        except StaleObjectError as error:
            raise self._stale() from error

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
