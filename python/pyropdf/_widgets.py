"""Form widgets: ``Page.widgets`` and its ``Widget`` handles (DESIGN.md §10).

A widget is where a form field appears on a page. A handle names its page,
its widget's object, and the position where it last found it in the page's
``/Annots``, as an annotation handle does.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, overload

from pyropdf import _core
from pyropdf._annotations import (
    _BORDER_STYLE_NAMES,
    _BORDER_STYLES,
    AnnotationFlags,
    Border,
    _color,
    _opaque_color,
    _rect,
    _text_value,
)
from pyropdf._content import Color
from pyropdf._errors import StaleObjectError
from pyropdf._geometry import Rect
from pyropdf._objects import Dictionary

if TYPE_CHECKING:
    from pyropdf._core import _Widget, _WidgetStyle
    from pyropdf._form import Field, FieldSpecification
    from pyropdf._page import Page


class Widget:
    """A handle on one form widget of a page, as ``page.widgets`` lists it.

    Properties read the document on every access, and assigning one changes
    the widget and draws it again for its field's value, in one
    transaction. A handle finds its widget again after an edit moved it; it
    raises :class:`StaleObjectError` while the widget is not on its page and
    :class:`StalePageError` while the page is gone, and it works again if
    undo restores them.
    """

    __slots__ = ("_id", "_index", "_page")

    def __init__(self, page: Page, id: tuple[int, int], index: int) -> None:  # noqa: A002 - an object's id.
        """Create a handle; ``page.widgets`` and :attr:`Field.widgets` do this."""
        self._page = page
        self._id = id
        self._index = index

    def _read(self) -> _Widget:
        page = self._page
        entry = _core.widget_read(page._document._core(), page._number, page._generation, self._index, self._id)  # noqa: SLF001
        if entry is None:
            raise page._stale()  # noqa: SLF001
        self._index = entry[0]
        return entry

    def _style(self) -> _WidgetStyle:
        page = self._page
        style = _core.widget_style(page._document._core(), page._number, page._generation, self._index, self._id)  # noqa: SLF001
        if style is None:
            raise page._stale()  # noqa: SLF001
        return style

    def _set(self, *, clear: list[str] | None = None, **entries: object) -> None:
        """Change the widget and draw it again for its field's value."""
        page = self._page
        self._read()
        edit: dict[str, object] = {"clear": clear or [], **entries}
        result = _core.widget_edit(page._document._core(), page._number, page._generation, self._index, self._id, edit)  # noqa: SLF001
        if result is None:
            raise page._stale()  # noqa: SLF001
        self._id = result

    @property
    def page(self) -> Page:
        """Return the page that shows the widget."""
        return self._page

    @property
    def xref(self) -> int:
        """Return the widget's object number, or 0 for a dictionary stored in ``/Annots`` itself."""
        self._read()
        return self._id[0]

    @property
    def obj(self) -> Dictionary:
        """Return the widget dictionary as a live low-level handle."""
        self._read()
        if self._id[0]:
            return Dictionary(self._page._document, self._id)  # noqa: SLF001
        return self._page.obj["Annots"][self._index]

    @property
    def field(self) -> Field | None:
        """Return the terminal form field that the widget shows, or ``None`` when no field owns it."""
        from pyropdf._form import Field  # noqa: PLC0415 - the form module imports this one.

        field = self._read()[2]
        return None if field is None else Field(self._page._document, *field)  # noqa: SLF001

    @property
    def rect(self) -> Rect:
        """Return or set the widget rectangle in display coordinates."""
        return Rect(*self._read()[3])

    @rect.setter
    def rect(self, rect: Sequence[float]) -> None:
        self._set(rect=_rect(rect))

    @property
    def flags(self) -> AnnotationFlags:
        """Return or set the widget's ``/F`` flags, such as :attr:`AnnotationFlags.HIDDEN`."""
        return AnnotationFlags(self._read()[4] & 0x3FF)

    @flags.setter
    def flags(self, flags: AnnotationFlags) -> None:
        if isinstance(flags, bool) or not isinstance(flags, int):
            msg = f"flags is an AnnotationFlags value: {flags!r}"
            raise TypeError(msg)
        self._set(flags=int(AnnotationFlags(flags)))

    @property
    def border(self) -> Border | None:
        """Return or set the border, ``/BS`` else ``/Border``; ``del`` removes it."""
        border = self._style()[1]
        if border is None:
            return None
        width, style, dashes = border
        return Border(width, style=_BORDER_STYLES[style], dashes=dashes)

    @border.setter
    def border(self, border: Border) -> None:
        if not isinstance(border, Border):
            msg = f"border must be a Border: {border!r}"
            raise TypeError(msg)
        self._set(border=(border.width, _BORDER_STYLE_NAMES[border.style], list(border.dashes)))

    @border.deleter
    def border(self) -> None:
        self._set(clear=["border"])

    @property
    def border_color(self) -> Color | None:
        """Return or set the border color, ``/MK /BC``; ``del`` removes it."""
        return _color(self._style()[0][0])

    @border_color.setter
    def border_color(self, color: Color) -> None:
        self._set(border_color=_opaque_color(color, "border_color").rgb)

    @border_color.deleter
    def border_color(self) -> None:
        self._set(clear=["border_color"])

    @property
    def background_color(self) -> Color | None:
        """Return or set the background color, ``/MK /BG``; ``del`` removes it."""
        return _color(self._style()[0][1])

    @background_color.setter
    def background_color(self, color: Color) -> None:
        self._set(background_color=_opaque_color(color, "background_color").rgb)

    @background_color.deleter
    def background_color(self) -> None:
        self._set(clear=["background_color"])

    @property
    def caption(self) -> str | None:
        """Return or set the caption of a pushbutton, ``/MK /CA``; ``del`` removes it.

        A caption with a character outside WinAnsi is kept but not drawn.
        """
        return self._style()[0][2]

    @caption.setter
    def caption(self, caption: str) -> None:
        self._set(caption=_text_value(caption, "caption"))

    @caption.deleter
    def caption(self) -> None:
        self._set(clear=["caption"])

    @property
    def text_color(self) -> Color:
        """Return or set the color of the widget's text, from the ``/DA`` that it draws with."""
        appearance = self._style()[2]
        return Color(0, 0, 0) if appearance is None else Color(*appearance[2])

    @text_color.setter
    def text_color(self, color: Color) -> None:
        self._set(text_color=_opaque_color(color, "text_color").rgb)

    @property
    def font_size(self) -> float:
        """Return or set the size of the widget's text in points, 0 to fit the text to the widget."""
        appearance = self._style()[2]
        return 0.0 if appearance is None else appearance[1]

    @font_size.setter
    def font_size(self, size: float) -> None:
        if isinstance(size, bool) or not isinstance(size, (int, float)) or not 0 <= size <= _MAX_FONT_SIZE:
            msg = f"font_size is a number from 0, which fits the text, to 10000: {size!r}"
            raise ValueError(msg)
        self._set(font_size=float(size))

    @property
    def button_states(self) -> tuple[str, ...]:
        """Return the appearance states of a check box or radio button, such as ``("Off", "Yes")``."""
        return tuple(self._read()[5])

    @property
    def on_state(self) -> str | None:
        """Return the state that selects a check box or radio button, or ``None`` without one."""
        return next((state for state in self._read()[5] if state != "Off"), None)

    def __eq__(self, other: object) -> bool:
        """Compare the page and the widget: its object, or its position for a direct dictionary."""
        if not isinstance(other, Widget):
            return NotImplemented
        return self._page == other._page and self._key() == other._key()

    def __hash__(self) -> int:
        """Hash consistently with equality."""
        return hash((self._page, self._key()))

    def _key(self) -> tuple[tuple[int, int], int]:
        return (self._id, 0 if self._id[0] else self._index)

    def __repr__(self) -> str:
        """Return ``Widget(rect=...)``, or a note for a stale handle."""
        try:
            entry = self._read()
        except StaleObjectError:
            return "<stale Widget>"
        return f"Widget(rect={Rect(*entry[3])!r})"


#: The largest ``/DA`` font size that a widget edit writes.
_MAX_FONT_SIZE = 10_000


class Widgets(Sequence[Widget]):
    """The form widgets of a page, as ``page.widgets`` returns them, in ``/Annots`` order.

    Reads reject more than 4,096 annotations or 1 MiB of state names per
    page.
    """

    __slots__ = ("_page",)

    def __init__(self, page: Page) -> None:
        """Wrap a page; use ``Page.widgets``."""
        self._page = page

    def _entries(self) -> list[_Widget]:
        page = self._page
        entries = _core.widget_list(page._document._core(), page._number, page._generation)  # noqa: SLF001
        if entries is None:
            raise page._stale()  # noqa: SLF001
        return entries

    def _handles(self) -> list[Widget]:
        return [Widget(self._page, entry[1], entry[0]) for entry in self._entries()]

    @overload
    def __getitem__(self, index: int) -> Widget: ...
    @overload
    def __getitem__(self, index: slice) -> list[Widget]: ...
    def __getitem__(self, index: int | slice) -> Widget | list[Widget]:
        """Return one widget, or a list of them for a slice."""
        return self._handles()[index]

    def __delitem__(self, index: int | slice) -> None:
        """Remove widgets from the page and from the form, in one transaction.

        A widget of a field leaves every page that lists it and the field's
        ``/Kids``; the field leaves the form with its last widget, as
        ``del doc.form[name]`` removes it. The widget objects stay in the
        document.
        """
        selected = self._handles()[index]
        widgets = selected if isinstance(selected, list) else [selected]
        page = self._page
        handles = [(widget._index, widget._id) for widget in widgets]  # noqa: SLF001
        if not _core.widget_remove(page._document._core(), page._number, page._generation, handles):  # noqa: SLF001
            raise page._stale()  # noqa: SLF001

    def add(self, specification: FieldSpecification) -> Widget:
        """Add the widget that ``specification`` describes, and return it.

        A new name creates the field with this widget; the name of an
        existing field of the same kind adds another widget to it, and a
        field merged with its only widget becomes their parent. The widget
        is drawn as setting the field's value draws it, and the edit is one
        transaction. A name with dots, the name of a field that has fields
        below it, and an existing field of another kind raise
        :class:`PdfError`.
        """
        from pyropdf._form import _FIELD_SPECIFICATIONS  # noqa: PLC0415 - _form imports this module.

        if not isinstance(specification, _FIELD_SPECIFICATIONS):
            msg = f"page.widgets.add takes a field specification: {type(specification).__name__}"
            raise TypeError(msg)
        page = self._page
        payload = specification._payload()  # noqa: SLF001
        result = _core.widget_add(page._document._core(), page._number, page._generation, payload)  # noqa: SLF001
        if result is None:
            raise page._stale()  # noqa: SLF001
        widget, index = result
        return Widget(page, widget, index)

    def __len__(self) -> int:
        """Return the number of widgets on the page."""
        return len(self._entries())

    def __iter__(self) -> Iterator[Widget]:
        """Iterate over the widgets that the page lists now."""
        return iter(self._handles())

    def __repr__(self) -> str:
        """Return ``Widgets([...])`` with the page's widgets."""
        return f"Widgets({list(self)!r})"


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (Widget, Widgets):
    _public.__module__ = "pyropdf"
