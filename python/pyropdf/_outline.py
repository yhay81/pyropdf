"""The outline (bookmarks): ``Document.outline``, its item handles, and entry records."""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import KW_ONLY, dataclass
from typing import TYPE_CHECKING, overload

from pyropdf import _core
from pyropdf._content import Color
from pyropdf._errors import LimitError, StaleObjectError
from pyropdf._objects import Dictionary
from pyropdf._targets import Destination, Target, _target_from_core, _target_to_core

if TYPE_CHECKING:
    from pyropdf._core import _Document, _OutlineItem, _Target
    from pyropdf._document import Document

#: The outline limits that every read and write shares: items and levels.
#: The core bounds the UTF-8 and encoded bytes of the titles and target
#: texts before it changes anything.
_MAX_OUTLINE_ITEMS = 65_536
_MAX_OUTLINE_LEVELS = 64
_MAX_OUTLINE_TEXT = 16 * 1024 * 1024

#: `/F` flags of an outline item (ISO 32000-2 Table 151).
_ITALIC = 1
_BOLD = 2

#: A page count that admits every page index while a record is validated
#: on its own; writing checks the document's real page count.
_UNBOUNDED_PAGES = 1 << 62


def _input_size(size: int) -> None:
    """Reject oversized character input before the binding creates UTF-8 copies."""
    if size > _MAX_OUTLINE_TEXT:
        code = "toc_input_size"
        msg = f"outline input exceeds the {_MAX_OUTLINE_TEXT}-byte safety limit"
        raise LimitError(code, msg)


def _target(target: object, page_count: int) -> _Target:
    result = _target_to_core(target, page_count)
    _input_size(sum(len(text) for text in result[4:] if text is not None))
    return result


def _color(value: object) -> Color | None:
    """Validate an outline color: an opaque :class:`Color` or ``None``."""
    if value is None:
        return None
    if not isinstance(value, Color):
        msg = f"color must be a Color or None: {value!r}"
        raise TypeError(msg)
    components = (value.red, value.green, value.blue)
    if value.alpha != 1 or not all(math.isfinite(c) and 0 <= c <= 1 for c in components):
        msg = f"an outline color has components from 0 to 1 and no transparency: {value!r}"
        raise ValueError(msg)
    return value


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        msg = f"{name} must be a bool: {value!r}"
        raise TypeError(msg)
    return value


@dataclass(frozen=True, slots=True)
class OutlineEntry:
    """One item of an outline to write, as ``doc.outline = [...]`` takes it.

    Levels start at 1 and grow by at most one from one entry to the next; an
    entry's children are the entries of the next level that follow it.
    ``is_open`` shows the children when the document opens.
    """

    level: int
    title: str
    destination: Target | None = None
    _: KW_ONLY
    color: Color | None = None
    bold: bool = False
    italic: bool = False
    is_open: bool = False

    def __post_init__(self) -> None:
        """Validate the fields."""
        if isinstance(self.level, bool) or not isinstance(self.level, int):
            msg = f"level must be an int: {self.level!r}"
            raise TypeError(msg)
        if not 1 <= self.level <= _MAX_OUTLINE_LEVELS:
            msg = f"level must be from 1 to {_MAX_OUTLINE_LEVELS}: {self.level}"
            raise ValueError(msg)
        if not isinstance(self.title, str):
            msg = f"title must be a str: {self.title!r}"
            raise TypeError(msg)
        _input_size(len(self.title))
        _target(self.destination, _UNBOUNDED_PAGES)
        _color(self.color)
        _bool(self.bold, "bold")
        _bool(self.italic, "italic")
        _bool(self.is_open, "is_open")

    @property
    def page(self) -> int | None:
        """Return the zero-based page of a :class:`Destination`, else ``None``."""
        return self.destination.page if isinstance(self.destination, Destination) else None


def _rgb(color: Color | None) -> tuple[float, float, float] | None:
    return None if color is None else (color.red, color.green, color.blue)


class OutlineItem:
    """A handle on one item of the outline, as ``doc.outline`` yields it.

    Properties read the document on every access, and assigning ``title``,
    ``destination``, ``color``, ``bold``, ``italic``, or ``is_open`` edits the
    item at once. A handle raises :class:`StaleObjectError` while its item no
    longer exists, as after the outline is replaced; it works again if undo
    restores it.
    """

    __slots__ = ("_document", "_id")

    def __init__(self, document: Document, xref: int, generation: int) -> None:
        """Create a handle; :class:`Outline` does this."""
        self._document = document
        self._id = (xref, generation)

    def _core(self) -> _Document:
        return self._document._core()  # noqa: SLF001

    def _read(self) -> _OutlineItem:
        return _core.outline_item(self._core(), self._id)

    @property
    def xref(self) -> int:
        """Return the item's object number."""
        return self._id[0]

    @property
    def obj(self) -> Dictionary:
        """Return the item's dictionary in the low-level object model."""
        self._read()
        return Dictionary(self._document, self._id)

    @property
    def level(self) -> int:
        """Return the item's depth: 1 for a top-level item."""
        return self._read()[0]

    @property
    def title(self) -> str:
        """Return the title shown in the viewer's outline."""
        return self._read()[1]

    @title.setter
    def title(self, title: str) -> None:
        if not isinstance(title, str):
            msg = f"title must be a str: {title!r}"
            raise TypeError(msg)
        _input_size(len(title))
        _core.outline_set_title(self._core(), self._id, title)

    @property
    def destination(self) -> Target | None:
        """Return where the item leads, or ``None``.

        A destination in this document that does not name an existing page
        reads as ``None``, and a named destination reads as the
        :class:`Destination` that it names.
        """
        return _target_from_core(self._read()[2])

    @destination.setter
    def destination(self, target: Target | None) -> None:
        core = self._core()
        _core.outline_set_target(core, self._id, _target(target, _core.page_count(core)))

    @property
    def page(self) -> int | None:
        """Return the zero-based page of a :class:`Destination`, else ``None``."""
        destination = self.destination
        return destination.page if isinstance(destination, Destination) else None

    @property
    def color(self) -> Color | None:
        """Return the title's color, or ``None`` for the viewer's default."""
        color = self._read()[3]
        return None if color is None else Color(*color)

    @color.setter
    def color(self, color: Color | None) -> None:
        _core.outline_set_style(self._core(), self._id, _rgb(_color(color)))

    @property
    def bold(self) -> bool:
        """Return whether the title shows in bold."""
        return bool(self._read()[4] & _BOLD)

    @bold.setter
    def bold(self, bold: bool) -> None:
        self._set_flag(_BOLD, _bool(bold, "bold"))

    @property
    def italic(self) -> bool:
        """Return whether the title shows in italic."""
        return bool(self._read()[4] & _ITALIC)

    @italic.setter
    def italic(self, italic: bool) -> None:
        self._set_flag(_ITALIC, _bool(italic, "italic"))

    @property
    def is_open(self) -> bool:
        """Return whether the item's children show; ``False`` without children.

        Assigning recounts the outline's ``/Count`` entries; an item without
        children has no open state and refuses it.
        """
        _, _, _, _, _, count, has_children = self._read()
        return has_children and count > 0

    @is_open.setter
    def is_open(self, is_open: bool) -> None:
        _core.outline_set_open(self._core(), self._id, _bool(is_open, "is_open"))

    @property
    def children(self) -> tuple[OutlineItem, ...]:
        """Return the item's children in order."""
        ids = _core.outline_children(self._core(), self._id)
        return tuple(OutlineItem(self._document, xref, generation) for xref, generation in ids)

    def _set_flag(self, flag: int, value: bool) -> None:  # noqa: FBT001 - one flag of the setters above.
        _core.outline_set_style(self._core(), self._id, None, None, (flag, value))

    def to_entry(self) -> OutlineEntry:
        """Return the item, without its children, as an :class:`OutlineEntry`."""
        level, title, target, color, flags, count, has_children = self._read()
        return OutlineEntry(
            level,
            title,
            _target_from_core(target),
            color=None if color is None else Color(*color),
            bold=bool(flags & _BOLD),
            italic=bool(flags & _ITALIC),
            is_open=has_children and count > 0,
        )

    def __eq__(self, other: object) -> bool:
        """Compare document and object."""
        if not isinstance(other, OutlineItem):
            return NotImplemented
        return (self._document, self._id) == (other._document, other._id)

    def __hash__(self) -> int:
        """Hash document and object."""
        return hash((id(self._document), self._id))

    def __repr__(self) -> str:
        """Return ``OutlineItem(xref=..., title=...)``, or a stale marker."""
        try:
            return f"OutlineItem(xref={self._id[0]}, title={self.title!r})"
        except (StaleObjectError, ValueError):
            return f"<stale OutlineItem xref={self._id[0]}>"


class Outline(Sequence[OutlineItem]):
    """The document's outline as its top-level items, as ``doc.outline`` returns it.

    Each item's ``children`` give the rest of the tree, and :meth:`walk`
    visits every item in document order. Assigning a list of
    :class:`OutlineEntry` records to ``doc.outline`` replaces the outline,
    and ``del doc.outline`` removes it. The view reads the document on every
    access.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap a document; use ``Document.outline``."""
        self._document = document

    def _items(self) -> list[OutlineItem]:
        document = self._document
        ids = _core.outline_children(document._core())  # noqa: SLF001
        return [OutlineItem(document, xref, generation) for xref, generation in ids]

    @overload
    def __getitem__(self, index: int) -> OutlineItem: ...
    @overload
    def __getitem__(self, index: slice) -> list[OutlineItem]: ...
    def __getitem__(self, index: int | slice) -> OutlineItem | list[OutlineItem]:
        """Return a top-level item, or a list of them for a slice."""
        return self._items()[index]

    def __len__(self) -> int:
        """Return the number of top-level items."""
        return len(self._items())

    def __iter__(self) -> Iterator[OutlineItem]:
        """Iterate over the top-level items."""
        return iter(self._items())

    def walk(self) -> Iterator[OutlineItem]:
        """Iterate over every item in document order, each before its children.

        Items stored as direct dictionaries, which no handle can name, are
        left out; :meth:`entries` lists them.
        """
        document = self._document
        entries = _core.outline_entries(document._core())  # noqa: SLF001
        return iter([OutlineItem(document, *entry[0]) for entry in entries if entry[0][0]])

    def entries(self) -> list[OutlineEntry]:
        """Return every item in document order as :class:`OutlineEntry` records.

        Assigning the list, edited or not, to ``doc.outline`` writes the same
        outline again. Reads share the outline limits: 65,536 items or
        131,072 links between them (``LimitError.code == "toc_entry_count"``),
        16 MiB of title and target text (``"toc_text_size"``), and 64 levels.
        """
        entries = _core.outline_entries(self._document._core())  # noqa: SLF001
        return [
            OutlineEntry(
                level,
                title,
                _target_from_core(target),
                color=None if color is None else Color(*color),
                bold=bool(flags & _BOLD),
                italic=bool(flags & _ITALIC),
                is_open=count > 0,
            )
            for _, level, title, target, color, flags, count in entries
        ]

    def __repr__(self) -> str:
        """Return ``Outline([...])`` with the top-level items."""
        return f"Outline({self._items()!r})"


def _write_outline(document: Document, entries: Iterable[OutlineEntry]) -> None:
    """Replace the outline with ``entries``, all validated before any change.

    The core bounds the titles and target texts, 16 MiB in UTF-8 and as PDF
    text strings (``LimitError.code == "toc_input_size"``).
    """
    core = document._core()  # noqa: SLF001
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Iterable):
        msg = f"the outline is an iterable of OutlineEntry records: {type(entries).__name__}"
        raise TypeError(msg)
    items = list(itertools.islice(entries, _MAX_OUTLINE_ITEMS + 1))
    if len(items) > _MAX_OUTLINE_ITEMS:
        code = "toc_entry_count"
        msg = f"the outline cannot contain more than {_MAX_OUTLINE_ITEMS} items"
        raise LimitError(code, msg)
    page_count = _core.page_count(core)
    specs = []
    previous_level = 0
    characters = 0
    for index, entry in enumerate(items):
        if not isinstance(entry, OutlineEntry):
            msg = f"outline item {index} must be an OutlineEntry: {type(entry).__name__}"
            raise TypeError(msg)
        if entry.level > previous_level + 1:
            msg = f"outline item {index} has level {entry.level}; levels start at 1 and grow by at most one"
            raise ValueError(msg)
        target = _target(entry.destination, page_count)
        characters += len(entry.title) + sum(len(text) for text in target[4:] if text is not None)
        _input_size(characters)
        flags = (_BOLD if entry.bold else 0) | (_ITALIC if entry.italic else 0)
        specs.append((entry.level, entry.title, target, _rgb(entry.color), flags, entry.is_open))
        previous_level = entry.level
    _core.outline_write(core, specs)


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (OutlineEntry, OutlineItem, Outline):
    _public.__module__ = "pyropdf"
