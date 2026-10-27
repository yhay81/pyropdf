"""Links: ``Page.links``, its ``Link`` handles, and ``Document.destinations``."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, overload

from pyropdf import _core
from pyropdf._annotations import _rect
from pyropdf._errors import LimitError, StaleObjectError, StalePageError
from pyropdf._geometry import Rect
from pyropdf._objects import Dictionary
from pyropdf._targets import Destination, Target, _destination, _target_from_core, _target_to_core

if TYPE_CHECKING:
    from pyropdf._core import _Document, _Link, _Target
    from pyropdf._document import Document
    from pyropdf._page import Page

#: The texts that one link edit may write: its subtype and target texts.
_MAX_LINK_INPUT_TEXT_SIZE = 1024 * 1024


def _link_target(target: object, page: Page) -> _Target:
    """Return the core's tuple for a link target, with its texts bounded."""
    if target is None:
        msg = "a link needs a target: a Destination, RemoteDestination, Launch, NamedAction, or URI string"
        raise TypeError(msg)
    spec = _target_to_core(target, len(page._document.pages))  # noqa: SLF001
    texts = ("Link", spec[4] or "", spec[5] or "")
    if sum(len(text) for text in texts) * 4 > _MAX_LINK_INPUT_TEXT_SIZE and (
        sum(len(text.encode()) for text in texts) > _MAX_LINK_INPUT_TEXT_SIZE
    ):
        code = "annotation_input_size"
        msg = f"link subtype and target input exceeds the {_MAX_LINK_INPUT_TEXT_SIZE}-byte UTF-8 limit"
        raise LimitError(code, msg)
    return spec


class Link:
    """A handle on one link annotation of a page, as ``page.links`` lists it.

    ``rect`` is the area that follows the link, in the page's display
    coordinates, and ``target`` where it leads; assigning either edits the
    annotation. ``target`` is ``None`` for an action that pyropdf does not
    read, such as JavaScript. A handle reads the document on every access and
    names the link's object, so it finds the link again after an edit moved
    it; one stored as a direct dictionary in ``/Annots`` is found by its
    position. A handle raises :class:`StaleObjectError` while the link is not
    on its page, and :class:`StalePageError` while the page is gone; it works
    again if undo restores them.
    """

    __slots__ = ("_id", "_index", "_page")

    def __init__(self, page: Page, id: tuple[int, int], index: int) -> None:  # noqa: A002 - an object's id.
        """Create a handle; ``page.links`` does this."""
        self._page = page
        self._id = id
        self._index = index

    def _location(self) -> tuple[_Document, int, int, int, tuple[int, int]]:
        page = self._page
        return page._document._core(), page._number, page._generation, self._index, self._id  # noqa: SLF001

    def _read(self) -> _Link:
        entry = _core.link_read(*self._location())
        if entry is None:
            raise self._page._stale()  # noqa: SLF001
        self._index = entry[0]
        return entry

    def _edit(self, rect: tuple[float, float, float, float] | None, target: _Target | None) -> None:
        result = _core.link_edit(*self._location(), rect, target)
        if result is None:
            raise self._page._stale()  # noqa: SLF001
        self._id = result

    @property
    def page(self) -> Page:
        """Return the page that holds the link."""
        return self._page

    @property
    def xref(self) -> int:
        """Return the annotation's object number, or 0 for a dictionary stored in ``/Annots`` itself."""
        self._read()
        return self._id[0]

    @property
    def obj(self) -> Dictionary:
        """Return the annotation dictionary as a live low-level handle."""
        self._read()
        if self._id[0]:
            return Dictionary(self._page._document, self._id)  # noqa: SLF001
        return self._page.obj["Annots"][self._index]

    @property
    def rect(self) -> Rect:
        """Return the area that follows the link, in display coordinates."""
        return Rect(*self._read()[2])

    @rect.setter
    def rect(self, rect: Sequence[float]) -> None:
        self._edit(_rect(rect), None)

    @property
    def target(self) -> Target | None:
        """Return where the link leads, or ``None`` for an action that pyropdf does not read."""
        return _target_from_core(self._read()[3])

    @target.setter
    def target(self, target: Target) -> None:
        self._edit(None, _link_target(target, self._page))

    def __eq__(self, other: object) -> bool:
        """Compare the page and the link: its object, or its position for a direct dictionary."""
        if not isinstance(other, Link):
            return NotImplemented
        return self._page == other._page and self._key() == other._key()

    def __hash__(self) -> int:
        """Hash consistently with equality."""
        return hash((self._page, self._key()))

    def _key(self) -> tuple[int, int]:
        return self._id if self._id[0] else (0, self._index)

    def __repr__(self) -> str:
        """Return ``Link(rect=..., target=...)``, or a note for a stale handle."""
        try:
            _, _, rect, target = self._read()
        except (StaleObjectError, StalePageError):
            return "<stale Link>"
        return f"Link(rect={Rect(*rect)!r}, target={_target_from_core(target)!r})"


def _entries(page: Page) -> list[_Link]:
    """Return the records of the links that ``page.links`` lists."""
    entries = _core.link_list(page._document._core(), page._number, page._generation)  # noqa: SLF001
    if entries is None:
        raise page._stale()  # noqa: SLF001
    return entries


class Links(Sequence[Link]):
    """The link annotations of a page, in ``/Annots`` order, as ``page.links`` returns them.

    The sequence reads the page on every access. ``add(rect, target)`` adds
    a link and ``del page.links[i]`` removes one from the page.

    Reads reject more than 4,096 annotations or 1 MiB of target text per
    page. A named destination resolves through the ``/Names /Dests`` tree,
    which is indexed once until an edit changes what indexing it read, or
    the legacy ``/Dests`` dictionary; a tree beyond 65,536 entries, 16 MiB of
    names, or 32 levels raises rather than leaving the link unresolved.
    """

    __slots__ = ("_page",)

    def __init__(self, page: Page) -> None:
        """Wrap a page; use ``Page.links``."""
        self._page = page

    def _handles(self) -> list[Link]:
        return [Link(self._page, entry[1], entry[0]) for entry in _entries(self._page)]

    @overload
    def __getitem__(self, index: int) -> Link: ...
    @overload
    def __getitem__(self, index: slice) -> list[Link]: ...
    def __getitem__(self, index: int | slice) -> Link | list[Link]:
        """Return one link, or a list of them for a slice."""
        return self._handles()[index]

    def __len__(self) -> int:
        """Return the number of links on the page."""
        return len(_entries(self._page))

    def __iter__(self) -> Iterator[Link]:
        """Iterate over the links that the page lists now."""
        return iter(self._handles())

    def __delitem__(self, index: int | slice) -> None:
        """Remove links from the page, in one transaction."""
        selected = self._handles()[index]
        links = selected if isinstance(selected, list) else [selected]
        page = self._page
        handles = [(link._index, link._id) for link in links]  # noqa: SLF001
        if not _core.annotation_remove(page._document._core(), page._number, page._generation, handles):  # noqa: SLF001
            raise page._stale()  # noqa: SLF001

    def add(self, rect: Sequence[float], target: Target) -> Link:
        """Add a link over ``rect``, in display coordinates, that leads to ``target``, and return it.

        ``target`` is a URI string, a :class:`Destination` in this document, a
        :class:`RemoteDestination`, a :class:`Launch`, or a
        :class:`NamedAction`. The link draws no border. Its texts share a
        1 MiB budget, checked before any change with
        ``LimitError.code == "annotation_input_size"``.
        """
        page = self._page
        area = _rect(rect)
        spec = _link_target(target, page)
        result = _core.link_add(page._document._core(), page._number, page._generation, area, spec)  # noqa: SLF001
        if result is None:
            raise page._stale()  # noqa: SLF001
        # The handle finds the new link by its object.
        return Link(page, result, 0)

    def __repr__(self) -> str:
        """Return ``Links([...])`` with the page's links."""
        return f"Links({list(self)!r})"


class Destinations(Mapping[str, Destination | None]):
    """The named destinations of a document, as ``doc.destinations`` returns them.

    Keys are the names of the catalog's ``/Names /Dests`` tree, the first
    entry of a repeated name winning, then those of the legacy ``/Dests``
    dictionary that the tree lacks; a value is the :class:`Destination` the
    name leads to, or ``None`` when it names no page. Links already read
    named destinations as the destinations they name. The mapping is
    read-only and reads the document on every access, under the
    named-destination limits: 65,536 entries or nodes, 131,072 edges
    (``named_destination_count``), 32 levels, and 16 MiB of names
    (``named_destination_text_size``).
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap a document; use ``Document.destinations``."""
        self._document = document

    def _items(self) -> dict[str, Destination | None]:
        return {
            name: None if place is None else _destination(*place)
            for name, place in _core.named_destinations(self._document._core())  # noqa: SLF001
        }

    def __getitem__(self, name: str) -> Destination | None:
        """Return where ``name`` leads; an unknown name raises :class:`KeyError`."""
        items = self._items()
        if not isinstance(name, str) or name not in items:
            raise KeyError(name)
        return items[name]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the names, tree entries first."""
        return iter(list(self._items()))

    def __len__(self) -> int:
        """Return the number of names."""
        return len(self._items())

    def __repr__(self) -> str:
        """Return ``Destinations({...})``."""
        return f"Destinations({self._items()!r})"


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (Destinations, Link, Links):
    _public.__module__ = "pyropdf"
