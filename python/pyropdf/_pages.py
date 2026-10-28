"""``doc.pages``: the pages of a document as a sequence that adds, deletes, and moves pages."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, overload

from pyropdf import _core
from pyropdf._errors import LimitError
from pyropdf._page import Page

if TYPE_CHECKING:
    from pyropdf._document import Document

#: The largest finite PDF real that readers keep (a 32-bit float).
_FLOAT32_MAX = float.fromhex("0x1.fffffep+127")
_A4 = (595.0, 842.0)
_MIN_PAGE_ENTRY_LIMIT = 4096


def _is_int(value: object) -> bool:
    """Return whether ``value`` is an integer other than a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _page_entries(pages: Iterable[int], count: int) -> list[int]:
    """Check page entries against ``count`` pages, reading at most one entry past their limit.

    The limit is the larger of ``count`` and 4,096, so that repeating pages
    never costs more than one pass over a large document.
    """
    limit = max(count, _MIN_PAGE_ENTRY_LIMIT)
    entries: list[int] = []
    for index in pages:
        if len(entries) >= limit:
            code = "page_entry_count"
            msg = (
                f"pages cannot hold more than {limit} entries "
                f"(the larger of the page count and {_MIN_PAGE_ENTRY_LIMIT})"
            )
            raise LimitError(code, msg)
        _resolve(index, count)
        entries.append(index)
    return entries


def _resolve(index: object, count: int) -> int:
    """Resolve a page index, negative ones from the end, against ``count`` pages."""
    if not _is_int(index):
        msg = f"page indexes must be integers, not {type(index).__name__}"
        raise TypeError(msg)
    assert isinstance(index, int)  # noqa: S101 - narrows the type for ty.
    resolved = index + count if index < 0 else index
    if not 0 <= resolved < count:
        msg = f"page index {index} is out of range for {count} pages"
        raise IndexError(msg)
    return resolved


class Pages(Sequence[Page]):
    """The pages of a document, in order.

    Indexing, slicing, and iteration give :class:`Page` handles, which keep
    naming their pages through later edits. :meth:`new`, ``del``, and
    :meth:`move` each change the page tree in one transaction.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap the pages of ``document``; use ``doc.pages`` instead."""
        self._document = document

    def __len__(self) -> int:
        """Return the number of pages."""
        return _core.page_count(self._document._core())  # noqa: SLF001

    @overload
    def __getitem__(self, index: int) -> Page: ...
    @overload
    def __getitem__(self, index: slice) -> list[Page]: ...
    def __getitem__(self, index: int | slice) -> Page | list[Page]:
        """Return page ``index``, or a list of the pages of a slice."""
        doc = self._document._core()  # noqa: SLF001
        if isinstance(index, slice):
            objects = _core.page_objects(doc)
            return [Page(self._document, number, generation) for number, generation in objects[index]]
        number, generation = _core.page_object(doc, _resolve(index, _core.page_count(doc)))
        return Page(self._document, number, generation)

    def __iter__(self) -> Iterator[Page]:
        """Iterate over the pages; a structural edit during the iteration raises ``RuntimeError``."""
        doc = self._document._core()  # noqa: SLF001
        objects = _core.page_objects(doc)
        for at, (number, generation) in enumerate(objects):
            if _core.page_count(doc) != len(objects) or _core.page_object(doc, at) != (number, generation):
                msg = "the pages changed during iteration"
                raise RuntimeError(msg)
            yield Page(self._document, number, generation)

    def __contains__(self, page: object) -> bool:
        """Return whether ``page`` is a current page of this document."""
        if not isinstance(page, Page) or page.document is not self._document:
            return False
        return _core.page_index(self._document._core(), page.xref, page._generation) is not None  # noqa: SLF001

    def index(self, page: object, start: int = 0, stop: int | None = None) -> int:
        """Return the index of ``page``, in time that does not grow with the page count."""
        if page not in self:
            msg = f"{page!r} is not a page of the document"
            raise ValueError(msg)
        assert isinstance(page, Page)  # noqa: S101 - narrows the type for ty.
        found = page.index
        if found < start or (stop is not None and found >= stop):
            msg = f"{page!r} is not in the range"
            raise ValueError(msg)
        return found

    def __delitem__(self, index: int | slice) -> None:
        """Delete page ``index``, or the pages of a slice, in one transaction."""
        doc = self._document._core()  # noqa: SLF001
        objects = _core.page_objects(doc)
        if isinstance(index, slice):
            removed = set(range(len(objects))[index])
            if not removed:
                return
        else:
            removed = {_resolve(index, len(objects))}
        _core.set_pages(doc, [obj for at, obj in enumerate(objects) if at not in removed])

    def new(self, size: tuple[float, float] = _A4, *, index: int | None = None) -> Page:
        """Add a blank page of ``size`` points, A4 by default, where ``list.insert`` would put it.

        Without ``index`` the page goes last. The page has its own MediaBox,
        and no rotation or crop of the page tree's root carries over to it.
        """
        width, height = _validate_size(size)
        doc = self._document._core()  # noqa: SLF001
        count = _core.page_count(doc)
        if index is None:
            at = count
        elif not _is_int(index):
            msg = f"page indexes must be integers, not {type(index).__name__}"
            raise TypeError(msg)
        else:
            at = max(0, index + count) if index < 0 else min(index, count)
        number, generation = _core.new_page(doc, at, width, height)
        return Page(self._document, number, generation)

    def select(self, indexes: Iterable[int], /) -> None:
        """Keep the pages at ``indexes``, in that order, and remove the others, in one transaction.

        Negative indexes count from the end. The navigation that leads to a
        removed page goes, as for ``del``. Each page may appear once.
        """
        doc = self._document._core()  # noqa: SLF001
        objects = _core.page_objects(doc)
        chosen: list[int] = []
        seen: set[int] = set()
        for index in indexes:
            at = _resolve(index, len(objects))
            if at in seen:
                msg = "select keeps each page at most once"
                raise ValueError(msg)
            seen.add(at)
            chosen.append(at)
        _core.set_pages(doc, [objects[at] for at in chosen])

    def move(self, index: int, to: int) -> None:
        """Move page ``index`` so that it ends at index ``to``, in one transaction."""
        doc = self._document._core()  # noqa: SLF001
        objects = _core.page_objects(doc)
        source = _resolve(index, len(objects))
        target = _resolve(to, len(objects))
        if source == target:
            return
        moved = objects.pop(source)
        objects.insert(target, moved)
        _core.set_pages(doc, objects)

    def __repr__(self) -> str:
        """Return a representation naming the document."""
        return f"<Pages of {self._document!r}>"


def _validate_size(size: object) -> tuple[float, float]:
    """Return a page size of two positive finite numbers within PDF's real-number range."""
    if not isinstance(size, (tuple, list)) or len(size) != 2:  # noqa: PLR2004 - a width and a height.
        msg = f"size must be a (width, height) pair: {size!r}"
        raise TypeError(msg)
    values = []
    for value in size:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            msg = f"size must hold numbers: {size!r}"
            raise TypeError(msg)
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            msg = f"size must be positive finite numbers: {size!r}"
            raise ValueError(msg)
        if number > _FLOAT32_MAX:
            msg = f"size must lie within PDF's real-number range: {size!r}"
            raise ValueError(msg)
        values.append(number)
    return values[0], values[1]


__all__ = ["Pages"]
