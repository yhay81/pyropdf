"""Annotations: ``Page.annotations`` and its ``Annotation`` handles (DESIGN.md §10).

A handle names its page, its annotation's object, and the position where it
last found it in the page's ``/Annots``. An edit that changes how an
annotation looks marks its appearance stale, and settling draws it again
once per transaction (DESIGN.md §7).
"""

from __future__ import annotations

import enum
import math
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, overload

from pyropdf import _core
from pyropdf._content import BlendMode, Color
from pyropdf._errors import LimitError, StaleObjectError, StalePageError
from pyropdf._geometry import Rect, _rect_components
from pyropdf._objects import Dictionary, String

if TYPE_CHECKING:
    from pyropdf._core import _Annotation, _Document
    from pyropdf._page import Page

#: The UTF-8 bytes of the texts that one edit writes (``annotation_input_size``).
_MAX_ANNOTATION_INPUT_TEXT_SIZE = 1024 * 1024
#: The most dash lengths that a border pattern holds.
_MAX_BORDER_DASHES = 32
#: The greatest intensity of a cloudy border (ISO 32000-2 Table 167).
_MAX_CLOUDS = 2
#: The components of a gray, an RGB, and a CMYK color.
_GRAY, _RGB, _CMYK = 1, 3, 4

# The slots of an annotation record's texts.
_CONTENTS, _ICON, _AUTHOR, _CREATED, _MODIFIED, _SUBJECT, _NAME, _INTENT = range(8)


class AnnotationType(enum.IntEnum):
    """Annotation types by ``/Subtype``; the values are pymupdf's ``PDF_ANNOT_*`` codes."""

    UNKNOWN = -1
    TEXT = 0
    LINK = 1
    FREE_TEXT = 2
    LINE = 3
    SQUARE = 4
    CIRCLE = 5
    POLYGON = 6
    POLY_LINE = 7
    HIGHLIGHT = 8
    UNDERLINE = 9
    SQUIGGLY = 10
    STRIKE_OUT = 11
    REDACT = 12
    STAMP = 13
    CARET = 14
    INK = 15
    POPUP = 16
    FILE_ATTACHMENT = 17
    SOUND = 18
    MOVIE = 19
    RICH_MEDIA = 20
    WIDGET = 21
    SCREEN = 22
    PRINTER_MARK = 23
    TRAP_NET = 24
    WATERMARK = 25
    THREE_D = 26
    PROJECTION = 27


#: The annotation type of each PDF subtype.
_SUBTYPE_TYPES: dict[str, AnnotationType] = {
    "Text": AnnotationType.TEXT,
    "Link": AnnotationType.LINK,
    "FreeText": AnnotationType.FREE_TEXT,
    "Line": AnnotationType.LINE,
    "Square": AnnotationType.SQUARE,
    "Circle": AnnotationType.CIRCLE,
    "Polygon": AnnotationType.POLYGON,
    "PolyLine": AnnotationType.POLY_LINE,
    "Highlight": AnnotationType.HIGHLIGHT,
    "Underline": AnnotationType.UNDERLINE,
    "Squiggly": AnnotationType.SQUIGGLY,
    "StrikeOut": AnnotationType.STRIKE_OUT,
    "Redact": AnnotationType.REDACT,
    "Stamp": AnnotationType.STAMP,
    "Caret": AnnotationType.CARET,
    "Ink": AnnotationType.INK,
    "Popup": AnnotationType.POPUP,
    "FileAttachment": AnnotationType.FILE_ATTACHMENT,
    "Sound": AnnotationType.SOUND,
    "Movie": AnnotationType.MOVIE,
    "RichMedia": AnnotationType.RICH_MEDIA,
    "Widget": AnnotationType.WIDGET,
    "Screen": AnnotationType.SCREEN,
    "PrinterMark": AnnotationType.PRINTER_MARK,
    "TrapNet": AnnotationType.TRAP_NET,
    "Watermark": AnnotationType.WATERMARK,
    "3D": AnnotationType.THREE_D,
    "Projection": AnnotationType.PROJECTION,
}


class AnnotationFlags(enum.IntFlag):
    """Annotation ``/F`` flags (ISO 32000-2 Table 167); the values are pymupdf's ``PDF_ANNOT_IS_*``."""

    INVISIBLE = 1 << 0
    HIDDEN = 1 << 1
    PRINT = 1 << 2
    NO_ZOOM = 1 << 3
    NO_ROTATE = 1 << 4
    NO_VIEW = 1 << 5
    READ_ONLY = 1 << 6
    LOCKED = 1 << 7
    TOGGLE_NO_VIEW = 1 << 8
    LOCKED_CONTENTS = 1 << 9


class BorderStyle(enum.StrEnum):
    """The style of an annotation's border, ``/BS /S`` (ISO 32000-2 Table 168)."""

    SOLID = "solid"
    DASHED = "dashed"
    BEVELED = "beveled"
    """Embossed, as a widget's border; other annotations draw it solid."""
    INSET = "inset"
    """Engraved, as a widget's border; other annotations draw it solid."""
    UNDERLINE = "underline"
    """Only the bottom edge, as a widget's border; other annotations draw it solid."""


#: The ``/S`` name of each border style.
_BORDER_STYLE_NAMES = {
    BorderStyle.SOLID: "S",
    BorderStyle.DASHED: "D",
    BorderStyle.BEVELED: "B",
    BorderStyle.INSET: "I",
    BorderStyle.UNDERLINE: "U",
}
_BORDER_STYLES = {name: style for style, name in _BORDER_STYLE_NAMES.items()}


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{name} must be a number: {value!r}"
        raise TypeError(msg)
    if not math.isfinite(value):
        msg = f"{name} must be finite: {value!r}"
        raise ValueError(msg)
    return float(value)


@dataclass(frozen=True, slots=True, init=False)
class Border:
    """The border of an annotation: its line width, style, and dash pattern.

    ``dashes`` lists 1 to 32 dash and gap lengths in points, which a
    :attr:`BorderStyle.DASHED` border needs and no other style takes; giving
    ``dashes`` without a style selects the dashed style. A width of 0 draws
    no border.
    """

    width: float
    style: BorderStyle
    dashes: tuple[float, ...]

    def __init__(
        self,
        width: float = 1.0,
        *,
        style: BorderStyle | None = None,
        dashes: Sequence[float] = (),
    ) -> None:
        """Validate the border."""
        width = _number(width, "a border width")
        if width < 0:
            msg = f"a border width is not negative: {width!r}"
            raise ValueError(msg)
        if isinstance(dashes, (str, bytes)) or not isinstance(dashes, Sequence):
            msg = f"dashes must be a sequence of numbers: {dashes!r}"
            raise TypeError(msg)
        if len(dashes) > _MAX_BORDER_DASHES:
            msg = f"a border has at most {_MAX_BORDER_DASHES} dash lengths"
            raise ValueError(msg)
        lengths = tuple(_number(dash, "a dash length") for dash in dashes)
        if any(dash < 0 for dash in lengths) or (lengths and sum(lengths) <= 0):
            msg = f"dash lengths are not negative and do not sum to 0: {lengths!r}"
            raise ValueError(msg)
        if style is None:
            style = BorderStyle.DASHED if lengths else BorderStyle.SOLID
        style = BorderStyle(style)
        if (style is BorderStyle.DASHED) != bool(lengths):
            msg = "a dashed border needs dash lengths, and only a dashed border takes them"
            raise ValueError(msg)
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "style", style)
        object.__setattr__(self, "dashes", lengths)


class LineEnding(enum.StrEnum):
    """The shape at an end of a line or polyline, ``/LE`` (ISO 32000-2 Table 179).

    Closed shapes are filled with the annotation's interior color.
    """

    NONE = "none"
    SQUARE = "square"
    CIRCLE = "circle"
    DIAMOND = "diamond"
    OPEN_ARROW = "open_arrow"
    CLOSED_ARROW = "closed_arrow"
    BUTT = "butt"
    """A short line across the end."""
    REVERSED_OPEN_ARROW = "reversed_open_arrow"
    REVERSED_CLOSED_ARROW = "reversed_closed_arrow"
    SLASH = "slash"
    """A short line 30 degrees clockwise from across the end."""


class NoteIcon(enum.StrEnum):
    """The icon of a note, ``/Name`` (ISO 32000-2 §12.5.6.4)."""

    COMMENT = "comment"
    HELP = "help"
    INSERT = "insert"
    KEY = "key"
    NEW_PARAGRAPH = "new_paragraph"
    NOTE = "note"
    PARAGRAPH = "paragraph"


class StampIcon(enum.StrEnum):
    """The stamps of ISO 32000-2 §12.5.6.12, ``/Name``; pymupdf's ``STAMP_*`` codes in their order."""

    APPROVED = "approved"
    AS_IS = "as_is"
    CONFIDENTIAL = "confidential"
    DEPARTMENTAL = "departmental"
    EXPERIMENTAL = "experimental"
    EXPIRED = "expired"
    FINAL = "final"
    FOR_COMMENT = "for_comment"
    FOR_PUBLIC_RELEASE = "for_public_release"
    NOT_APPROVED = "not_approved"
    NOT_FOR_PUBLIC_RELEASE = "not_for_public_release"
    SOLD = "sold"
    TOP_SECRET = "top_secret"  # noqa: S105 - a stamp's name, not a password.
    DRAFT = "draft"


class AttachmentIcon(enum.StrEnum):
    """The icon of a file attachment, ``/Name`` (ISO 32000-2 §12.5.6.15)."""

    GRAPH = "graph"
    PUSH_PIN = "push_pin"
    PAPERCLIP = "paperclip"
    TAG = "tag"


def _pdf_name(member: enum.StrEnum) -> str:
    """Return the PDF name of an icon or blend mode member: ``push_pin`` is ``PushPin``."""
    return "".join(part.capitalize() for part in member.value.split("_"))


#: The ``/Name`` of each icon.
_ICON_NAMES: dict[enum.StrEnum, str] = {
    icon: _pdf_name(icon) for kind in (NoteIcon, StampIcon, AttachmentIcon) for icon in kind
}
#: The blend mode of each ``/BM`` name.
_BLEND_MODES = {_pdf_name(mode): mode for mode in BlendMode}


def _color(components: Sequence[float]) -> Color | None:
    """Return a ``/C`` or ``/IC`` array as a color: gray, RGB, or CMYK, or ``None`` when empty."""
    values = [min(max(value, 0.0), 1.0) for value in components]
    if len(values) == _GRAY:
        return Color(values[0], values[0], values[0])
    if len(values) == _RGB:
        return Color(*values)
    if len(values) == _CMYK:  # Converted naively, as MuPDF does.
        cyan, magenta, yellow, black = values
        return Color((1 - cyan) * (1 - black), (1 - magenta) * (1 - black), (1 - yellow) * (1 - black))
    return None


def _opaque_color(value: object, name: str = "color") -> Color:
    if not isinstance(value, Color):
        msg = f"{name} must be a Color: {value!r}"
        raise TypeError(msg)
    components = (value.red, value.green, value.blue)
    if value.alpha != 1 or not all(math.isfinite(c) and 0 <= c <= 1 for c in components):
        msg = f"an annotation color has components from 0 to 1 and no transparency: {value!r}"
        raise ValueError(msg)
    return value


def _opacity(value: object) -> float:
    opacity = _number(value, "opacity")
    if not 0 < opacity <= 1:
        msg = f"opacity must be greater than 0 and at most 1: {value!r}"
        raise ValueError(msg)
    return opacity


def _clouds(clouds: object) -> float:
    intensity = _number(clouds, "clouds")
    if not 0 <= intensity <= _MAX_CLOUDS:
        msg = f"clouds is an intensity from 0 to {_MAX_CLOUDS}: {clouds!r}"
        raise ValueError(msg)
    return intensity


def _text_value(value: object, name: str) -> str:
    """Check a text property's type and its 1 MiB bound before the core copies it."""
    if not isinstance(value, str):
        msg = f"{name} must be a str: {type(value).__name__}"
        raise TypeError(msg)
    if len(value) * 4 > _MAX_ANNOTATION_INPUT_TEXT_SIZE and len(value.encode()) > _MAX_ANNOTATION_INPUT_TEXT_SIZE:
        code = "annotation_input_size"
        msg = f"annotation {name} exceeds the {_MAX_ANNOTATION_INPUT_TEXT_SIZE}-byte UTF-8 limit"
        raise LimitError(code, msg)
    return value


def _rect(value: object) -> tuple[float, float, float, float]:
    """Return a finite, non-empty display rectangle."""
    x0, y0, x1, y1 = _rect_components(value)
    if not all(math.isfinite(c) for c in (x0, y0, x1, y1)) or not (x0 < x1 and y0 < y1):
        msg = f"a rectangle is finite with x0 < x1 and y0 < y1: {value!r}"
        raise ValueError(msg)
    return x0, y0, x1, y1


def _now() -> str:
    """Return the current time as a PDF date string in UTC."""
    return time.strftime("D:%Y%m%d%H%M%SZ", time.gmtime())


class Annotation:
    """A handle on one annotation of a page, as ``page.annotations`` lists it.

    Properties read the document on every access. A handle names the
    annotation's object, so it finds the annotation again after an edit
    moved it; one stored as a direct dictionary in ``/Annots`` is found by
    its position. A handle raises :class:`StaleObjectError` while the
    annotation is not on its page, and :class:`StalePageError` while the
    page is gone; it works again if undo restores them.
    """

    __slots__ = ("_id", "_index", "_page")

    def __init__(self, page: Page, id: tuple[int, int], index: int) -> None:  # noqa: A002 - an object's id.
        """Create a handle; ``page.annotations`` does this."""
        self._page = page
        self._id = id
        self._index = index

    def _location(self) -> tuple[_Document, int, int, int, tuple[int, int]]:
        page = self._page
        return page._document._core(), page._number, page._generation, self._index, self._id  # noqa: SLF001

    def _read(self) -> _Annotation:
        entry = _core.annotation_read(*self._location())
        if entry is None:
            raise self._page._stale()  # noqa: SLF001
        self._index = entry[0]
        return entry

    def _changed(self, result: tuple[int, int] | None) -> None:
        if result is None:
            raise self._page._stale()  # noqa: SLF001
        self._id = result

    def _set(self, *, clear: list[str] | None = None, **entries: object) -> None:
        """Change properties, recording the time of the edit; the appearance settles later."""
        edit: dict[str, object] = {"clear": clear or [], "modified": _now(), **entries}
        self._changed(_core.annotation_edit(*self._location(), edit))

    def _text(self, slot: int) -> str | None:
        return self._read()[5][slot]

    @property
    def page(self) -> Page:
        """Return the page that holds the annotation."""
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
    def type(self) -> AnnotationType:
        """Return the annotation type, :attr:`AnnotationType.UNKNOWN` for a subtype pyropdf does not know."""
        subtype = self._read()[2]
        return AnnotationType.UNKNOWN if subtype is None else _SUBTYPE_TYPES.get(subtype, AnnotationType.UNKNOWN)

    @property
    def rect(self) -> Rect:
        """Return or set the annotation rectangle in display coordinates."""
        return Rect(*self._read()[3])

    @rect.setter
    def rect(self, rect: Sequence[float]) -> None:
        self._set(rect=_rect(rect))

    @property
    def contents(self) -> str | None:
        """Return or set the text that the annotation shows or describes, ``/Contents``."""
        return self._text(_CONTENTS)

    @contents.setter
    def contents(self, value: str) -> None:
        self._set(contents=_text_value(value, "contents"))

    @contents.deleter
    def contents(self) -> None:
        self._set(clear=["contents"])

    @property
    def author(self) -> str | None:
        """Return or set the author, ``/T``."""
        return self._text(_AUTHOR)

    @author.setter
    def author(self, value: str) -> None:
        self._set(author=_text_value(value, "author"))

    @author.deleter
    def author(self) -> None:
        self._set(clear=["author"])

    @property
    def subject(self) -> str | None:
        """Return or set the subject, ``/Subj``."""
        return self._text(_SUBJECT)

    @subject.setter
    def subject(self, value: str) -> None:
        self._set(subject=_text_value(value, "subject"))

    @subject.deleter
    def subject(self) -> None:
        self._set(clear=["subject"])

    @property
    def created(self) -> str | None:
        """Return the creation date as the PDF date string stored, ``/CreationDate``."""
        return self._text(_CREATED)

    @property
    def modified(self) -> str | None:
        """Return the modification date as the PDF date string stored, ``/M``."""
        return self._text(_MODIFIED)

    @property
    def name(self) -> str | None:
        """Return or set the annotation's unique name on its page, ``/NM``."""
        return self._text(_NAME)

    @name.setter
    def name(self, value: str) -> None:
        self._set(name=_text_value(value, "name"))

    @name.deleter
    def name(self) -> None:
        self._set(clear=["name"])

    @property
    def icon(self) -> str | None:
        """Return or set the icon name of a note, stamp, or file attachment, ``/Name``."""
        return self._text(_ICON)

    @icon.setter
    def icon(self, icon: NoteIcon | StampIcon | AttachmentIcon) -> None:
        if icon not in _ICON_NAMES:
            msg = f"icon is a NoteIcon, StampIcon, or AttachmentIcon member: {icon!r}"
            raise ValueError(msg)
        self._set(icon=_ICON_NAMES[icon])

    @property
    def intent(self) -> str | None:
        """Return the intent, ``/IT``, such as ``"FreeTextCallout"``."""
        return self._text(_INTENT)

    @property
    def color(self) -> Color | None:
        """Return or set the color of the border, title bar, or text markup, ``/C``; CMYK converts to RGB."""
        return _color(self._read()[6])

    @color.setter
    def color(self, color: Color | None) -> None:
        if color is None:
            self._set(clear=["color"])
        else:
            self._set(color=_opaque_color(color).rgb)

    @color.deleter
    def color(self) -> None:
        self._set(clear=["color"])

    @property
    def interior_color(self) -> Color | None:
        """Return or set the fill color of a shape or line ending, ``/IC``; CMYK converts to RGB."""
        return _color(self._read()[7])

    @interior_color.setter
    def interior_color(self, color: Color | None) -> None:
        if color is None:
            self._set(clear=["interior_color"])
        else:
            self._set(interior_color=_opaque_color(color, "interior_color").rgb)

    @interior_color.deleter
    def interior_color(self) -> None:
        self._set(clear=["interior_color"])

    @property
    def opacity(self) -> float:
        """Return or set the constant opacity, ``/CA``, 1.0 when absent."""
        opacity = self._read()[8]
        return 1.0 if opacity is None else opacity

    @opacity.setter
    def opacity(self, opacity: float) -> None:
        self._set(opacity=_opacity(opacity))

    @property
    def flags(self) -> AnnotationFlags:
        """Return or set the ``/F`` flags."""
        return AnnotationFlags(self._read()[4] & 0x3FF)

    @flags.setter
    def flags(self, flags: AnnotationFlags) -> None:
        if isinstance(flags, bool) or not isinstance(flags, int):
            msg = f"flags is an AnnotationFlags value: {flags!r}"
            raise TypeError(msg)
        self._set(flags=int(AnnotationFlags(flags)))

    @property
    def border(self) -> Border | None:
        """Return or set the border that ``/BS``, else ``/Border``, describes, or ``None`` without either."""
        border = self._read()[9]
        if border is None:
            return None
        width, style, dashes = border
        return Border(width, style=_BORDER_STYLES[style], dashes=dashes)

    @border.setter
    def border(self, border: Border | None) -> None:
        if border is None:
            self._set(clear=["border"])
            return
        if not isinstance(border, Border):
            msg = f"border must be a Border or None: {border!r}"
            raise TypeError(msg)
        self._set(border=(border.width, _BORDER_STYLE_NAMES[border.style], list(border.dashes)))

    @border.deleter
    def border(self) -> None:
        self._set(clear=["border"])

    @property
    def clouds(self) -> float:
        """Return or set the intensity of a cloudy border, ``/BE``, from 0 (none) to 2.

        Squares, circles, and polygons take one; ``del`` and 0 remove it.
        """
        effect = self.obj.get("BE")
        if not isinstance(effect, Dictionary) or effect.get("S") != "C":
            return 0.0
        intensity = effect.get("I", 0)
        if isinstance(intensity, bool) or not isinstance(intensity, (int, float)) or not math.isfinite(intensity):
            return 0.0
        return min(max(float(intensity), 0.0), float(_MAX_CLOUDS))

    @clouds.setter
    def clouds(self, clouds: float) -> None:
        value = _clouds(clouds)
        if value:
            self._set(clouds=value)
        else:
            self._set(clear=["clouds"])

    @clouds.deleter
    def clouds(self) -> None:
        self._set(clear=["clouds"])

    @property
    def blend_mode(self) -> BlendMode | None:
        """Return or set the blend mode that the appearance is drawn with, ``/BM``, or ``None`` without one."""
        name = self._read()[10]
        return None if name is None else _BLEND_MODES[name]

    @blend_mode.setter
    def blend_mode(self, mode: BlendMode | None) -> None:
        if mode is None:
            self._set(clear=["blend_mode"])
        else:
            self._set(blend_mode=_pdf_name(BlendMode(mode)))

    @blend_mode.deleter
    def blend_mode(self) -> None:
        self._set(clear=["blend_mode"])

    @property
    def rotation(self) -> int:
        """Return or set how far the annotation's content is turned clockwise as the page is displayed.

        That is the page's rotation less the annotation's ``/Rotate``, which
        turns the content counterclockwise in PDF user space. Free text,
        stamps, carets, and file attachments take a multiple of 90.
        """
        stored = self.obj.get("Rotate")
        turn = stored if isinstance(stored, int) and not isinstance(stored, bool) and stored % 90 == 0 else 0
        return (self._page.rotation - turn) % 360

    @rotation.setter
    def rotation(self, rotate: int) -> None:
        if isinstance(rotate, bool) or not isinstance(rotate, int) or rotate % 90:
            msg = f"rotation must be a multiple of 90: {rotate!r}"
            raise ValueError(msg)
        self._set(rotate=rotate % 360)

    @property
    def language(self) -> str | None:
        """Return or set the BCP 47 language of the annotation's text, ``/Lang``; ``del`` removes it."""
        value = self.obj.get("Lang")
        return value.text if isinstance(value, String) else None

    @language.setter
    def language(self, value: str) -> None:
        self._set(language=_text_value(value, "language"))

    @language.deleter
    def language(self) -> None:
        self._set(clear=["language"])

    @property
    def popup_rect(self) -> Rect | None:
        """Return or set the display rectangle of the annotation's popup, ``None`` without one.

        Assigning a rectangle moves the popup, or adds one that the page
        lists after the annotation; ``del`` removes it.
        """
        popup = self._read()[11][1]
        return None if popup is None else Rect(*popup)

    @popup_rect.setter
    def popup_rect(self, rect: Sequence[float]) -> None:
        self._changed(_core.annotation_popup(*self._location(), _rect(rect)))

    @popup_rect.deleter
    def popup_rect(self) -> None:
        self._changed(_core.annotation_popup(*self._location(), None))

    @property
    def is_open(self) -> bool:
        """Return or set whether the popup, or a note without one, opens when the page is shown.

        Assigning needs a popup or a note.
        """
        return self._read()[11][2]

    @is_open.setter
    def is_open(self, value: bool) -> None:
        if not isinstance(value, bool):
            msg = f"is_open is a bool: {value!r}"
            raise TypeError(msg)
        self._changed(_core.annotation_open(*self._location(), value))

    @property
    def in_reply_to(self) -> Annotation | None:
        """Return or set the annotation that this one replies to, ``/IRT``, or ``None``.

        The annotation replied to belongs to the same page; a reply to an
        annotation that is not listed in ``page.annotations``, such as a
        link, raises :class:`StaleObjectError` on use.
        """
        target = self._read()[11][0]
        return None if target is None else Annotation(self._page, target, 0)

    @in_reply_to.setter
    def in_reply_to(self, annotation: Annotation | None) -> None:
        if annotation is None:
            self._changed(_core.annotation_reply(*self._location(), None))
            return
        if not isinstance(annotation, Annotation):
            msg = f"in_reply_to is an Annotation or None: {annotation!r}"
            raise TypeError(msg)
        if annotation._page != self._page:  # noqa: SLF001 - another handle of this class.
            msg = "an annotation replies to an annotation of its own page"
            raise ValueError(msg)
        annotation._read()  # noqa: SLF001
        self._changed(_core.annotation_reply(*self._location(), annotation._id))  # noqa: SLF001

    @in_reply_to.deleter
    def in_reply_to(self) -> None:
        self._changed(_core.annotation_reply(*self._location(), None))

    @property
    def replies(self) -> list[Annotation]:
        """Return the annotations of the page that reply to this one, in ``/Annots`` order."""
        self._read()
        if not self._id[0]:
            return []
        return [
            Annotation(self._page, entry[1], entry[0]) for entry in _entries(self._page) if entry[11][0] == self._id
        ]

    def __eq__(self, other: object) -> bool:
        """Compare the page and the annotation: its object, or its position for a direct dictionary."""
        if not isinstance(other, Annotation):
            return NotImplemented
        return self._page == other._page and self._key() == other._key()

    def __hash__(self) -> int:
        """Hash consistently with equality."""
        return hash((self._page, self._key()))

    def _key(self) -> tuple[int, int]:
        return self._id if self._id[0] else (0, self._index)

    def __repr__(self) -> str:
        """Return ``Annotation(type=..., rect=...)``, or a note for a stale handle."""
        try:
            entry = self._read()
        except (StaleObjectError, StalePageError):
            return "<stale Annotation>"
        kind = AnnotationType.UNKNOWN if entry[2] is None else _SUBTYPE_TYPES.get(entry[2], AnnotationType.UNKNOWN)
        return f"Annotation(type=AnnotationType.{kind.name}, rect={Rect(*entry[3])!r})"


def _entries(page: Page) -> list[_Annotation]:
    """Return the records of the annotations that ``page.annotations`` lists."""
    entries = _core.annotation_list(page._document._core(), page._number, page._generation)  # noqa: SLF001
    if entries is None:
        raise page._stale()  # noqa: SLF001
    return entries


class Annotations(Sequence[Annotation]):
    """The annotations of a page other than links and form widgets, as ``page.annotations`` returns them.

    The sequence lists them in ``/Annots`` order and reads the page on every
    access; popups belong to the annotation that names them and are not
    listed. ``del page.annotations[i]`` removes one, with its popup.

    Reads reject more than 4,096 annotations or 1 MiB of annotation text per
    page.
    """

    __slots__ = ("_page",)

    def __init__(self, page: Page) -> None:
        """Wrap a page; use ``Page.annotations``."""
        self._page = page

    def _handles(self) -> list[Annotation]:
        return [Annotation(self._page, entry[1], entry[0]) for entry in _entries(self._page)]

    @overload
    def __getitem__(self, index: int) -> Annotation: ...
    @overload
    def __getitem__(self, index: slice) -> list[Annotation]: ...
    def __getitem__(self, index: int | slice) -> Annotation | list[Annotation]:
        """Return one annotation, or a list of them for a slice."""
        return self._handles()[index]

    def __len__(self) -> int:
        """Return the number of annotations on the page."""
        return len(_entries(self._page))

    def __iter__(self) -> Iterator[Annotation]:
        """Iterate over the annotations that the page lists now."""
        return iter(self._handles())

    def __delitem__(self, index: int | slice) -> None:
        """Remove annotations and their popups from the page, in one transaction."""
        selected = self._handles()[index]
        annotations = selected if isinstance(selected, list) else [selected]
        page = self._page
        handles = [(annotation._index, annotation._id) for annotation in annotations]  # noqa: SLF001
        if not _core.annotation_remove(page._document._core(), page._number, page._generation, handles):  # noqa: SLF001
            raise page._stale()  # noqa: SLF001

    def __repr__(self) -> str:
        """Return ``Annotations([...])`` with the page's annotations."""
        return f"Annotations({list(self)!r})"


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (
    Annotation,
    Annotations,
    AnnotationType,
    AnnotationFlags,
    AttachmentIcon,
    Border,
    BorderStyle,
    LineEnding,
    NoteIcon,
    StampIcon,
):
    _public.__module__ = "pyropdf"
