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
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast, overload

from pyropdf import _core
from pyropdf._content import BlendMode, Color
from pyropdf._errors import LimitError, MalformedPdfError, PdfError, StaleObjectError, StalePageError
from pyropdf._fonts import Font
from pyropdf._geometry import Point, Quad, Rect, _rect_components
from pyropdf._limits import _validate_optional_positive_int
from pyropdf._objects import _DEFAULT_MAX_STREAM_DATA_SIZE, Dictionary, Name, Stream, String
from pyropdf._pixmap import Colorspace, _render_background, _render_gray, _render_matrix
from pyropdf._text import _DEFAULT_MAX_TEXT_GLYPHS, _DEFAULT_MAX_TEXT_SIZE, Unmapped, _warn_skipped, text_options

if TYPE_CHECKING:
    from pyropdf._core import Pixmap, _Annotation, _Document
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


#: The ``/LE`` name of each line ending.
_LINE_ENDING_NAMES = {
    LineEnding.NONE: "None",
    LineEnding.SQUARE: "Square",
    LineEnding.CIRCLE: "Circle",
    LineEnding.DIAMOND: "Diamond",
    LineEnding.OPEN_ARROW: "OpenArrow",
    LineEnding.CLOSED_ARROW: "ClosedArrow",
    LineEnding.BUTT: "Butt",
    LineEnding.REVERSED_OPEN_ARROW: "ROpenArrow",
    LineEnding.REVERSED_CLOSED_ARROW: "RClosedArrow",
    LineEnding.SLASH: "Slash",
}
_LINE_ENDINGS = {name: ending for ending, name in _LINE_ENDING_NAMES.items()}


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


class SoundEncoding(enum.StrEnum):
    """How a sound's samples encode amplitudes, ``/E`` (ISO 32000-2 Table 208)."""

    RAW = "Raw"
    """Unsigned values from 0 through 2**bits - 1."""
    SIGNED = "Signed"
    """Two's-complement values."""
    MU_LAW = "muLaw"
    """μ-law encoded samples."""
    A_LAW = "ALaw"
    """A-law encoded samples."""


@dataclass(frozen=True, slots=True)
class Sound:
    """The sound of a sound annotation, ``/Sound``, as :meth:`Annotation.sound` reads it.

    ``data`` holds the samples as the stream stores them after its filters,
    or the stream's bytes before them when read with ``decode=False``; the
    samples stay in ``encoding`` and, for a compressed sound, in the format
    that ``compression`` names.
    """

    data: bytes
    """The sample data."""
    rate: float
    """Samples per second per channel, ``/R``."""
    channels: int
    """The sound channels, ``/C``: 1 when absent."""
    bits: int
    """The bits per sample value per channel, ``/B``: 8 when absent."""
    encoding: SoundEncoding
    """The encoding of the sample values, ``/E``: :attr:`SoundEncoding.RAW` when absent."""
    compression: str | None
    """The sound compression format, ``/CO``, or ``None`` for uncompressed samples."""


def _sound_number(value: object, name: str, *, integer: bool) -> float:
    """Return a positive, finite ``/R``, ``/C``, or ``/B``, or raise :class:`MalformedPdfError`."""
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value <= 0:
        expected = "a positive integer" if integer else "a positive number"
        msg = f"the sound's /{name} must be {expected}: {value!r}"
        raise MalformedPdfError(msg)
    return value


#: Attached files stop at 64 MiB unless the caller passes another bound.
_DEFAULT_MAX_FILE_SIZE = 64 * 1024 * 1024
#: The longest date string that a file's ``created`` and ``modified`` take.
_MAX_DATE_SIZE = 64


def _attached_data(data: object, max_size: int | None) -> bytes:
    """Check attached data against ``max_size`` before copying it (``embedded_file_size``)."""
    _validate_optional_positive_int("max_size", max_size)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        msg = f"data must be bytes: {type(data).__name__}"
        raise TypeError(msg)
    size = data.nbytes if isinstance(data, memoryview) else len(data)
    if max_size is not None and size > max_size:
        code = "embedded_file_size"
        msg = f"attachment input is {size} bytes, exceeding the {max_size}-byte limit"
        raise LimitError(code, msg)
    return bytes(data)


def _file_texts(texts: Iterable[str], label: str) -> None:
    """Refuse texts that are longer together in UTF-8 than 1 MiB (``embedded_file_input_size``)."""
    size = 0
    for text in texts:
        # Each character needs at least one UTF-8 byte. Reject oversized
        # inputs before allocating an encoded copy of them.
        if len(text) > _MAX_ANNOTATION_INPUT_TEXT_SIZE - size:
            code = "embedded_file_input_size"
            msg = f"the {label} is longer than the {_MAX_ANNOTATION_INPUT_TEXT_SIZE}-byte limit in UTF-8"
            raise LimitError(code, msg)
        size += len(text.encode("utf-8", "surrogatepass"))
        if size > _MAX_ANNOTATION_INPUT_TEXT_SIZE:
            code = "embedded_file_input_size"
            msg = f"the {label} is longer than the {_MAX_ANNOTATION_INPUT_TEXT_SIZE}-byte limit in UTF-8"
            raise LimitError(code, msg)


def _file_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        msg = f"{name} must be a str: {type(value).__name__}"
        raise TypeError(msg)
    _file_texts([value], name)
    return value


def _file_date(value: object, name: str) -> str:
    if not isinstance(value, str):
        msg = f"{name} must be a str: {type(value).__name__}"
        raise TypeError(msg)
    if not value.startswith("D:") or not value.isascii() or len(value) > _MAX_DATE_SIZE:
        msg = f"{name} must be a PDF date string such as 'D:20260101120000Z': {value!r}"
        raise ValueError(msg)
    return value


def _string_text(value: object) -> str | None:
    return value.text if isinstance(value, String) else None


def _embedded_stream(filespec: Dictionary) -> Stream | None:
    """Return the embedded file stream that a file specification's ``/EF`` names, if any."""
    files = filespec.get("EF")
    if not isinstance(files, Dictionary):
        return None
    for key in ("F", "UF"):
        stream = files.get(key)
        if isinstance(stream, Stream):
            return stream
    return None


class AnnotationFile:
    """The file that a file attachment annotation embeds, as :attr:`Annotation.file` returns it.

    It reads the document on every access and has the members of
    :class:`Attachment`, which describes the document's own attachments:
    assigning ``filename``, ``description``, ``created``, or ``modified``
    edits the file specification at once. A handle raises
    :class:`StaleObjectError` once its annotation no longer embeds a file.
    """

    __slots__ = ("_annotation",)

    def __init__(self, annotation: Annotation) -> None:
        """Wrap an annotation; :attr:`Annotation.file` does this."""
        self._annotation = annotation

    def _filespec(self) -> Dictionary:
        filespec = self._annotation._filespec()  # noqa: SLF001
        if filespec is None:
            msg = "the annotation no longer embeds a file"
            raise StaleObjectError(msg)
        return filespec

    def _params(self) -> Dictionary | None:
        stream = _embedded_stream(self._filespec())
        params = stream.get("Params") if stream is not None else None
        return params if isinstance(params, Dictionary) else None

    def _edit(self, name: str, edit: Callable[[Dictionary], None]) -> None:
        """Apply ``edit`` to the file specification in one transaction that records the annotation's change."""
        self._filespec()
        annotation = self._annotation
        old_id, old_index = annotation._id, annotation._index  # noqa: SLF001
        try:
            with annotation._page._document.transaction(name):  # noqa: SLF001
                # The edit stores a direct annotation as an object of its own.
                annotation._set()  # noqa: SLF001
                edit(self._filespec())
        except BaseException:
            # A failed edit (including a failed commit) discards the new object.
            annotation._id, annotation._index = old_id, old_index  # noqa: SLF001
            raise

    def _edit_params(self, key: str, value: str | None) -> None:
        def edit(filespec: Dictionary) -> None:
            stream = _embedded_stream(filespec)
            if stream is None:
                msg = "the attached file has no embedded file stream"
                raise PdfError(msg)
            if not isinstance(stream.get("Params"), Dictionary):
                stream["Params"] = {}
            params = stream["Params"]
            if value is not None:
                params[key] = value
            elif key in params:
                del params[key]

        self._edit("annotation file edit", edit)

    @property
    def filename(self) -> str:
        """Return the viewer-facing file name (``/UF``, else ``/F``), or ``""``; assignable."""
        filespec = self._filespec()
        return _string_text(filespec.get("UF")) or _string_text(filespec.get("F")) or ""

    @filename.setter
    def filename(self, filename: str) -> None:
        filename = _file_text(filename, "filename")
        if not filename:
            msg = "a file name cannot be empty"
            raise ValueError(msg)

        def edit(filespec: Dictionary) -> None:
            filespec["F"] = filename
            filespec["UF"] = filename

        self._edit("annotation file edit", edit)

    @property
    def description(self) -> str | None:
        """Return the description (``/Desc``), or ``None``; assignable, and ``del`` removes it."""
        return _string_text(self._filespec().get("Desc")) or None

    @description.setter
    def description(self, description: str) -> None:
        description = _file_text(description, "description")

        def edit(filespec: Dictionary) -> None:
            filespec["Desc"] = description

        self._edit("annotation file edit", edit)

    @description.deleter
    def description(self) -> None:
        def edit(filespec: Dictionary) -> None:
            if "Desc" in filespec:
                del filespec["Desc"]

        self._edit("annotation file edit", edit)

    @property
    def size(self) -> int:
        """Return the uncompressed size that the file declares (``/Params /Size``), or -1, as pymupdf does."""
        params = self._params()
        size = params.get("Size") if params is not None else None
        return size if isinstance(size, int) and not isinstance(size, bool) else -1

    @property
    def created(self) -> str | None:
        """Return the creation date (``/Params /CreationDate``) as stored, or ``None``; assignable and deletable.

        Dates are PDF date strings such as ``D:20260101120000Z``.
        """
        params = self._params()
        return (_string_text(params.get("CreationDate")) or None) if params is not None else None

    @created.setter
    def created(self, date: str) -> None:
        self._edit_params("CreationDate", _file_date(date, "created"))

    @created.deleter
    def created(self) -> None:
        self._edit_params("CreationDate", None)

    @property
    def modified(self) -> str | None:
        """Return the modification date (``/Params /ModDate``) as stored, or ``None``; assignable and deletable.

        :meth:`write` sets it to the current time.
        """
        params = self._params()
        return (_string_text(params.get("ModDate")) or None) if params is not None else None

    @modified.setter
    def modified(self, date: str) -> None:
        self._edit_params("ModDate", _file_date(date, "modified"))

    @modified.deleter
    def modified(self) -> None:
        self._edit_params("ModDate", None)

    def read(self, *, max_size: int | None = _DEFAULT_MAX_FILE_SIZE) -> bytes:
        """Return the file's contents; ``max_size`` bounds the decoded data (``embedded_file_size``).

        The document's limits bound the decoding; ``None`` lifts ``max_size``
        for trusted input.
        """
        _validate_optional_positive_int("max_size", max_size)
        stream = _embedded_stream(self._filespec())
        if stream is None:
            msg = "the attached file has no embedded file stream"
            raise PdfError(msg)
        try:
            return stream.read(max_size=max_size)
        except LimitError as error:
            if error.code != "stream_data_size":
                raise
            code = "embedded_file_size"
            msg = f"the attached file is larger than the {max_size}-byte limit"
            raise LimitError(code, msg) from error

    def write(self, data: bytes | bytearray | memoryview, /, *, max_size: int | None = _DEFAULT_MAX_FILE_SIZE) -> None:
        """Replace the file's contents, as pymupdf's ``embfile_upd`` does.

        The file name, description, and creation date stay; ``size`` becomes
        the new length, ``modified`` the current time, and a stale
        ``/CheckSum`` is dropped. ``max_size`` bounds ``data`` before it is
        copied and defaults to 64 MiB (``embedded_file_size``).
        """
        self._filespec()
        payload = _attached_data(data, max_size)
        document = self._annotation._page._document  # noqa: SLF001

        def edit(filespec: Dictionary) -> None:
            stream = _embedded_stream(filespec)
            if stream is None:
                objects = document.objects
                stream = objects[objects.add_stream(payload, {"Type": Name("EmbeddedFile")})]
                filespec["EF"] = {"F": stream, "UF": stream}
            else:
                stream.write(payload)
            if not isinstance(stream.get("Params"), Dictionary):
                stream["Params"] = {}
            params = stream["Params"]
            params["Size"] = len(payload)
            params["ModDate"] = _now()
            if "CheckSum" in params:
                del params["CheckSum"]

        self._edit("annotation file write", edit)

    def __repr__(self) -> str:
        """Return ``AnnotationFile('name')``."""
        try:
            return f"AnnotationFile({self.filename!r})"
        except (StaleObjectError, StalePageError):
            return "<stale AnnotationFile>"


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
    if len(value) > _MAX_ANNOTATION_INPUT_TEXT_SIZE or (
        len(value) * 4 > _MAX_ANNOTATION_INPUT_TEXT_SIZE and len(value.encode()) > _MAX_ANNOTATION_INPUT_TEXT_SIZE
    ):
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

    def _geometry(self) -> tuple[list[list[tuple[float, float]]] | None, tuple[str, str] | None]:
        geometry = _core.annotation_geometry(*self._location())
        if geometry is None:
            raise self._page._stale()  # noqa: SLF001
        return geometry

    @property
    def vertices(self) -> tuple[Point, ...] | None:
        """Return or set the display points of a line (its two ends), polygon, or polyline.

        ``None`` for another type or a missing or malformed geometry. Reads
        refuse more than 65,536 points (``annotation_point_count``).
        """
        if self.type not in (AnnotationType.LINE, AnnotationType.POLYGON, AnnotationType.POLY_LINE):
            return None
        points, _ = self._geometry()
        return None if points is None else tuple(Point(x, y) for x, y in points[0])

    @vertices.setter
    def vertices(self, vertices: Iterable[Point | Sequence[float]]) -> None:
        minimum = 2
        points = _points(vertices, "vertices", minimum, [0])
        if self.type is AnnotationType.LINE and len(points) != minimum:
            msg = "a line has two vertices"
            raise ValueError(msg)
        self._set(points=[[tuple(point) for point in points]])

    @property
    def strokes(self) -> tuple[tuple[Point, ...], ...] | None:
        """Return or set the display points of each stroke of an ink annotation, ``None`` for another type."""
        if self.type is not AnnotationType.INK:
            return None
        points, _ = self._geometry()
        return None if points is None else tuple(tuple(Point(x, y) for x, y in stroke) for stroke in points)

    @strokes.setter
    def strokes(self, strokes: Iterable[Iterable[Point | Sequence[float]]]) -> None:
        self._set(points=[[tuple(point) for point in stroke] for stroke in _strokes(strokes)])

    @property
    def line_endings(self) -> tuple[LineEnding, LineEnding] | None:
        """Return or set the start and end shapes of a line or polyline, ``None`` for another type."""
        _, endings = self._geometry()
        if endings is None:
            return None
        start, end = endings
        return _LINE_ENDINGS[start], _LINE_ENDINGS[end]

    @line_endings.setter
    def line_endings(self, endings: tuple[LineEnding, LineEnding]) -> None:
        start, end = _line_endings(endings)
        self._set(line_endings=(_LINE_ENDING_NAMES[start], _LINE_ENDING_NAMES[end]))

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

    def pixmap(
        self,
        *,
        dpi: float | None = None,
        matrix: Sequence[float] | None = None,
        alpha: bool = False,
        background: tuple[int, int, int] | tuple[int, int, int, int] | None = None,
        colorspace: Colorspace | Literal["gray", "rgb"] = Colorspace.RGB,
    ) -> Pixmap:
        """Render the annotation's appearance alone, over its display rectangle :attr:`rect`.

        The page's content and its other annotations and widgets are left
        out. The appearance is the current version's, as a render draws it:
        its normal appearance, or the one a render synthesizes for an
        annotation without one; a hidden annotation renders as the bare
        background. The options are those of :meth:`Page.pixmap`, and so
        are its size limits; a rectangle that leaves no pixel of the page
        raises :class:`PdfError`, and an empty one ``ValueError``. The GIL
        is released while hayro renders.
        """
        linear = _render_matrix(matrix, dpi)
        rgba = _render_background(background, alpha=alpha)
        gray = _render_gray(colorspace)
        page = self._page
        pixmap = _core.annotation_pixmap(*self._location(), linear, alpha, gray, rgba)
        if pixmap is None:
            raise page._stale()  # noqa: SLF001
        return pixmap

    def text(  # noqa: PLR0913 - the text options are all keyword-only
        self,
        *,
        clipped: bool = False,
        ligatures: bool = True,
        whitespace: bool = True,
        dehyphenate: bool = False,
        unmapped: Unmapped = "skip",
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> str:
        """Return the text that the annotation's appearance draws, in reading order.

        The appearance is the one that :meth:`pixmap` renders, read as
        :meth:`Page.text` reads a page that draws nothing else, with its
        options; an appearance that draws no text, or a hidden annotation,
        gives ``""``. ``/Contents`` is not read; :attr:`contents` returns
        it.
        """
        options = text_options(
            clip=None,
            clipped=clipped,
            ligatures=ligatures,
            whitespace=whitespace,
            dehyphenate=dehyphenate,
            unmapped=unmapped,
            max_glyphs=max_glyphs,
            max_size=max_size,
        )
        page = self._page
        result = _core.annotation_text(*self._location(), options, max_glyphs, max_size)
        if result is None:
            raise page._stale()  # noqa: SLF001
        text, skipped = result
        _warn_skipped(skipped)
        return text

    def _filespec(self) -> Dictionary | None:
        """Return the file specification of a file attachment, ``/FS``, or ``None``."""
        if self.type is not AnnotationType.FILE_ATTACHMENT:
            return None
        filespec = self.obj.get("FS")
        return filespec if isinstance(filespec, Dictionary) else None

    @property
    def file(self) -> AnnotationFile | None:
        """Return the file that a file attachment embeds, or ``None`` for another annotation.

        A file attachment whose ``/FS`` is not a file specification
        dictionary embeds no file either.
        """
        return None if self._filespec() is None else AnnotationFile(self)

    def sound(self, *, decode: bool = True, max_size: int | None = _DEFAULT_MAX_STREAM_DATA_SIZE) -> Sound | None:
        """Return the sound that a sound annotation plays, or ``None`` for another kind of annotation.

        ``decode`` and ``max_size`` read the ``/Sound`` stream as
        :meth:`Stream.read` reads it, with :class:`LimitError` code
        ``stream_data_size`` past ``max_size``. A sound annotation without a
        ``/Sound`` stream, and a sound whose ``/R``, ``/C``, ``/B``, ``/E``,
        or ``/CO`` is malformed, raise :class:`MalformedPdfError`.
        """
        _validate_optional_positive_int("max_size", max_size)
        if not isinstance(decode, bool):
            msg = f"decode must be a bool: {decode!r}"
            raise TypeError(msg)
        if self.type is not AnnotationType.SOUND:
            return None
        stream = self.obj.get("Sound")
        if not isinstance(stream, Stream):
            msg = f"the sound annotation's /Sound must be a stream: {stream!r}"
            raise MalformedPdfError(msg)
        rate = _sound_number(stream.get("R"), "R", integer=False)
        channels = int(_sound_number(stream.get("C", 1), "C", integer=True))
        bits = int(_sound_number(stream.get("B", 8), "B", integer=True))
        encoding = stream.get("E", Name("Raw"))
        if not isinstance(encoding, Name) or encoding not in SoundEncoding._value2member_map_:
            msg = f"the sound's /E must be Raw, Signed, muLaw, or ALaw: {encoding!r}"
            raise MalformedPdfError(msg)
        compression = stream.get("CO")
        if compression is not None and not isinstance(compression, Name):
            msg = f"the sound's /CO must be a name: {compression!r}"
            raise MalformedPdfError(msg)
        return Sound(
            data=stream.read(decode=decode, max_size=max_size),
            rate=float(rate),
            channels=channels,
            bits=bits,
            encoding=SoundEncoding(encoding),
            compression=None if compression is None else str(compression),
        )

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


#: The most rectangles of one text-markup annotation.
_MAX_MARKUP_RECTS = 4096
#: The side of a note's icon, in points.
_NOTE_SIZE = 20
#: A PDF date as created annotations write it twice, for /CreationDate and
#: /M, which the input budget charges so that the annotation reads back
#: under the same budget.
_DATE_PLACEHOLDER = "D:00000000000000Z"


def _contents(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        msg = f"contents must be a str or None: {type(value).__name__}"
        raise TypeError(msg)
    return value


def _bound_input(subtype: str, contents: str | None) -> None:
    """Bound the subtype, contents, and dates that an added annotation writes to 1 MiB of UTF-8."""
    texts = (subtype, contents or "", _DATE_PLACEHOLDER, _DATE_PLACEHOLDER)
    characters = sum(len(text) for text in texts)
    if characters > _MAX_ANNOTATION_INPUT_TEXT_SIZE or (
        characters * 4 > _MAX_ANNOTATION_INPUT_TEXT_SIZE
        and sum(len(text.encode()) for text in texts) > _MAX_ANNOTATION_INPUT_TEXT_SIZE
    ):
        code = "annotation_input_size"
        msg = f"annotation subtype and contents input exceeds the {_MAX_ANNOTATION_INPUT_TEXT_SIZE}-byte UTF-8 limit"
        raise LimitError(code, msg)


def _markup_rects(rects: object) -> tuple[Rect, ...]:
    """Validate 1 to 4,096 rectangles or quads, stopping an iterator at the first one too many."""
    if isinstance(rects, (str, bytes)) or not isinstance(rects, Iterable):
        msg = f"rects must be a rectangle, a quad, or a sequence of them: {rects!r}"
        raise TypeError(msg)
    if isinstance(rects, Quad):
        return (Rect(*_rect(rects.rect)),)
    if isinstance(rects, (list, tuple)) and rects and isinstance(rects[0], (int, float)):
        return (Rect(*_rect(rects)),)
    validated: list[Rect] = []
    for rect in rects:
        if len(validated) >= _MAX_MARKUP_RECTS:
            msg = f"rects cannot contain more than {_MAX_MARKUP_RECTS} rectangles"
            raise ValueError(msg)
        validated.append(Rect(*_rect(rect.rect if isinstance(rect, Quad) else rect)))
    if not validated:
        msg = "rects must contain at least one rectangle or quad"
        raise ValueError(msg)
    return tuple(validated)


#: Rectangles or quads that text markup covers: one, or a sequence of them.
MarkupRects: TypeAlias = Sequence[float] | Quad | Iterable[Sequence[float] | Quad]


class _TextMarkup:
    """The fields and validation that the text-markup specifications share."""

    __slots__ = ()

    rects: tuple[Rect, ...]
    color: Color
    opacity: float
    contents: str | None

    def _init(self, rects: object, color: object, opacity: object, contents: object) -> None:
        """Validate the specification; the contents are bounded before the rectangles are read."""
        contents = _contents(contents)
        _bound_input(type(self).__name__, contents)
        validated = _markup_rects(rects)
        if isinstance(opacity, bool) or not isinstance(opacity, (int, float)):
            msg = f"opacity must be a number: {opacity!r}"
            raise TypeError(msg)
        object.__setattr__(self, "rects", validated)
        object.__setattr__(self, "color", _opaque_color(color))
        object.__setattr__(self, "opacity", _opacity(opacity))
        object.__setattr__(self, "contents", contents)

    def _payload(self) -> dict[str, Any]:
        return {
            "subtype": type(self).__name__,
            "rects": [(rect.x0, rect.y0, rect.x1, rect.y1) for rect in self.rects],
            "color": (self.color.red, self.color.green, self.color.blue),
            "opacity": self.opacity,
            "contents": self.contents,
            "created": _now(),
        }


@dataclass(frozen=True, slots=True, init=False)
class Highlight(_TextMarkup):
    """A highlight over text, as ``page.annotations.add`` takes it.

    ``rects`` is one rectangle or quad, or a sequence of them, in display
    coordinates, such as the result of ``page.search``; a :class:`Quad`
    contributes its bounding rectangle, and one annotation holds at most
    4,096. ``color`` fills them in multiply blending at ``opacity``, and
    ``contents`` is the text of its note. The annotation is written with its
    appearance, so every viewer draws it alike.
    """

    rects: tuple[Rect, ...]
    color: Color
    opacity: float
    contents: str | None

    def __init__(
        self,
        rects: MarkupRects,
        *,
        color: Color = Color(1.0, 1.0, 0.0),  # noqa: B008 - Color is immutable.
        opacity: float = 0.4,
        contents: str | None = None,
    ) -> None:
        """Validate the specification; the contents are bounded before the rectangles are read."""
        self._init(rects, color, opacity, contents)


@dataclass(frozen=True, slots=True, init=False)
class Underline(_TextMarkup):
    """A line under text, as ``page.annotations.add`` takes it.

    The fields are those of :class:`Highlight`; ``color`` draws a one-point
    line just above the bottom edge of each rectangle.
    """

    rects: tuple[Rect, ...]
    color: Color
    opacity: float
    contents: str | None

    def __init__(
        self,
        rects: MarkupRects,
        *,
        color: Color = Color(0.0, 0.0, 1.0),  # noqa: B008 - Color is immutable.
        opacity: float = 1.0,
        contents: str | None = None,
    ) -> None:
        """Validate the specification; the contents are bounded before the rectangles are read."""
        self._init(rects, color, opacity, contents)


@dataclass(frozen=True, slots=True, init=False)
class StrikeOut(_TextMarkup):
    """A line through text, as ``page.annotations.add`` takes it.

    The fields are those of :class:`Highlight`; ``color`` draws a one-point
    line through the middle of each rectangle.
    """

    rects: tuple[Rect, ...]
    color: Color
    opacity: float
    contents: str | None

    def __init__(
        self,
        rects: MarkupRects,
        *,
        color: Color = Color(1.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        opacity: float = 1.0,
        contents: str | None = None,
    ) -> None:
        """Validate the specification; the contents are bounded before the rectangles are read."""
        self._init(rects, color, opacity, contents)


@dataclass(frozen=True, slots=True, init=False)
class Squiggly(_TextMarkup):
    """A wavy line under text, as ``page.annotations.add`` takes it.

    The fields are those of :class:`Highlight`; ``color`` draws a zigzag
    two points wide along the bottom of each rectangle, at most 65,536 points
    in all (``annotation_point_count``).
    """

    rects: tuple[Rect, ...]
    color: Color
    opacity: float
    contents: str | None

    def __init__(
        self,
        rects: MarkupRects,
        *,
        color: Color = Color(1.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        opacity: float = 1.0,
        contents: str | None = None,
    ) -> None:
        """Validate the specification; the contents are bounded before the rectangles are read."""
        self._init(rects, color, opacity, contents)


def _point(value: object) -> Point:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:  # noqa: PLR2004
        msg = f"a point is a Point or a pair of numbers: {value!r}"
        raise TypeError(msg)
    return Point(_number(value[0], "a point coordinate"), _number(value[1], "a point coordinate"))


@dataclass(frozen=True, slots=True, init=False)
class Note:
    """A note shown as an icon, as ``page.annotations.add`` takes it.

    ``at`` is the display point of the icon's top-left corner; the icon is
    20 points square, and its flags ask viewers to keep it upright on a
    turned page. ``contents`` is the note's text, ``icon`` its picture,
    ``color`` the icon's fill, and ``is_open`` whether a viewer opens the
    note when it shows the page. Add a popup by assigning
    :attr:`Annotation.popup_rect`.
    """

    at: Point
    contents: str | None
    icon: NoteIcon
    color: Color
    opacity: float
    is_open: bool

    def __init__(  # noqa: PLR0913 - the note's options.
        self,
        at: Point | Sequence[float],
        *,
        contents: str | None = None,
        icon: NoteIcon = NoteIcon.NOTE,
        color: Color = Color(1.0, 1.0, 0.0),  # noqa: B008 - Color is immutable.
        opacity: float = 1.0,
        is_open: bool = False,
    ) -> None:
        """Validate the specification."""
        contents = _contents(contents)
        _bound_input("Text", contents)
        if not isinstance(is_open, bool):
            msg = f"is_open is a bool: {is_open!r}"
            raise TypeError(msg)
        object.__setattr__(self, "at", _point(at))
        object.__setattr__(self, "contents", contents)
        object.__setattr__(self, "icon", NoteIcon(icon))
        object.__setattr__(self, "color", _opaque_color(color))
        object.__setattr__(self, "opacity", _opacity(opacity))
        object.__setattr__(self, "is_open", is_open)

    def _payload(self) -> dict[str, Any]:
        x, y = self.at
        return {
            "subtype": "Text",
            "rect": (x, y, x + _NOTE_SIZE, y + _NOTE_SIZE),
            "color": (self.color.red, self.color.green, self.color.blue),
            "opacity": self.opacity,
            "contents": self.contents,
            "created": _now(),
            "icon": _ICON_NAMES[self.icon],
            "open": self.is_open,
        }


@dataclass(frozen=True, slots=True, init=False)
class Caret:
    """A caret that marks where text goes, as ``page.annotations.add`` takes it.

    ``rect`` is in display coordinates, and the caret fills it pointing up
    on the display, on a turned page too: its ``/Rotate`` takes the page's
    rotation. ``color`` fills the caret, ``opacity`` applies to it, and
    ``contents`` is the text of the annotation's note. Turn it later with
    :attr:`Annotation.rotation`.
    """

    rect: Rect
    color: Color
    opacity: float
    contents: str | None

    def __init__(
        self,
        rect: Sequence[float],
        *,
        color: Color = Color(0.0, 0.0, 1.0),  # noqa: B008 - Color is immutable.
        opacity: float = 1.0,
        contents: str | None = None,
    ) -> None:
        """Validate the specification."""
        contents = _contents(contents)
        _bound_input("Caret", contents)
        object.__setattr__(self, "rect", Rect(*_rect(rect)))
        object.__setattr__(self, "color", _opaque_color(color))
        object.__setattr__(self, "opacity", _opacity(opacity))
        object.__setattr__(self, "contents", contents)

    def _payload(self) -> dict[str, Any]:
        return {
            "subtype": "Caret",
            "rect": tuple(self.rect),
            "color": (self.color.red, self.color.green, self.color.blue),
            "opacity": self.opacity,
            "contents": self.contents,
            "created": _now(),
        }


@dataclass(frozen=True, slots=True, init=False)
class FileAttachment:
    """A file embedded in the page behind an icon, as ``page.annotations.add`` takes it.

    ``at`` is the display point of the top-left corner of the 20-point
    icon, which stands upright on a turned page too: its ``/Rotate`` takes
    the page's rotation. ``data`` is the file and ``filename`` its name,
    which with ``description`` stops at 1 MiB (``embedded_file_input_size``);
    ``max_size`` bounds ``data`` before it is copied and defaults to 64 MiB
    (``embedded_file_size``). ``color`` fills the icon. Read the file back
    through :attr:`Annotation.file`; the document's own attachments, in
    :attr:`Document.attachments`, do not list it.
    """

    at: Point
    data: bytes
    filename: str
    description: str | None
    icon: AttachmentIcon
    color: Color
    opacity: float
    contents: str | None

    def __init__(  # noqa: PLR0913 - the attachment's options.
        self,
        at: Point | Sequence[float],
        data: bytes | bytearray | memoryview,
        *,
        filename: str,
        description: str | None = None,
        icon: AttachmentIcon = AttachmentIcon.PUSH_PIN,
        color: Color = Color(1.0, 1.0, 0.0),  # noqa: B008 - Color is immutable.
        opacity: float = 1.0,
        contents: str | None = None,
        max_size: int | None = _DEFAULT_MAX_FILE_SIZE,
    ) -> None:
        """Validate the specification; texts and data are bounded before they are copied."""
        contents = _contents(contents)
        if not isinstance(filename, str) or (description is not None and not isinstance(description, str)):
            msg = "filename must be a str and description a str or None"
            raise TypeError(msg)
        _file_texts((filename, description or ""), "attached file name and description")
        _bound_input("FileAttachment", contents)
        object.__setattr__(self, "at", _point(at))
        object.__setattr__(self, "data", _attached_data(data, max_size))
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "icon", AttachmentIcon(icon))
        object.__setattr__(self, "color", _opaque_color(color))
        object.__setattr__(self, "opacity", _opacity(opacity))
        object.__setattr__(self, "contents", contents)

    def _payload(self) -> dict[str, Any]:
        x, y = self.at
        return {
            "subtype": "FileAttachment",
            "rect": (x, y, x + _NOTE_SIZE, y + _NOTE_SIZE),
            "data": self.data,
            "filename": self.filename,
            "description": self.description,
            "icon": _ICON_NAMES[self.icon],
            "color": (self.color.red, self.color.green, self.color.blue),
            "opacity": self.opacity,
            "contents": self.contents,
            "created": _now(),
            # The data was bounded when the specification was made.
            "max_size": None,
        }


def _turn(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value % 90:
        msg = f"rotate must be a multiple of 90: {value!r}"
        raise ValueError(msg)
    return value % 360


@dataclass(frozen=True, slots=True, init=False)
class Stamp:
    """A rubber stamp, as ``page.annotations.add`` takes it.

    ``rect`` is in display coordinates; the stamp's label is drawn as large
    as fits, centred in a rounded frame, in ``color`` or the stamp's own
    color (green for approval, red for restriction, blue for the rest), and
    ``rotate`` turns it clockwise by a multiple of 90 degrees as the page is
    displayed.
    """

    rect: Rect
    icon: StampIcon
    color: Color | None
    opacity: float
    rotate: int
    contents: str | None

    def __init__(  # noqa: PLR0913 - the stamp's options.
        self,
        rect: Sequence[float],
        *,
        icon: StampIcon = StampIcon.APPROVED,
        color: Color | None = None,
        opacity: float = 1.0,
        rotate: int = 0,
        contents: str | None = None,
    ) -> None:
        """Validate the specification."""
        contents = _contents(contents)
        _bound_input("Stamp", contents)
        object.__setattr__(self, "rect", Rect(*_rect(rect)))
        object.__setattr__(self, "icon", StampIcon(icon))
        object.__setattr__(self, "color", _optional_color(color, "color"))
        object.__setattr__(self, "opacity", _opacity(opacity))
        object.__setattr__(self, "rotate", _turn(rotate))
        object.__setattr__(self, "contents", contents)

    def _payload(self) -> dict[str, Any]:
        return {
            "subtype": "Stamp",
            "rect": tuple(self.rect),
            "icon": _ICON_NAMES[self.icon],
            "color": None if self.color is None else (self.color.red, self.color.green, self.color.blue),
            "opacity": self.opacity,
            "rotate": self.rotate,
            "contents": self.contents,
            "created": _now(),
        }


#: The ``/Q`` code of each free-text alignment.
_FREE_TEXT_ALIGNMENTS = ("left", "center", "right")
#: The largest font size of free text, in points.
_MAX_FREE_TEXT_SIZE = 10_000


@dataclass(frozen=True, slots=True, init=False)
class FreeText:
    """Text written on the page in a box, as ``page.annotations.add`` takes it.

    ``rect`` is the box in display coordinates and ``text`` its text,
    wrapped as :meth:`Canvas.textbox` wraps it, ``align``-ed, and in the
    Standard 14 ``font``, Helvetica by default, at ``size`` points and
    ``text_color``. The font is named in the annotation's ``/DA``, so other
    viewers can redraw it; it prints WinAnsi characters only. ``color``
    fills the box, ``border`` outlines it in the text color, and ``rotate``
    turns the text clockwise by a multiple of 90 degrees as the page is
    displayed. ``callout`` draws a line from the display point that the
    text explains, through an optional knee, to the box, with
    ``callout_ending`` at the explained point. Text that does not fit
    raises :class:`PdfError`.
    """

    rect: Rect
    text: str
    font: Font | None
    size: float
    text_color: Color
    color: Color | None
    border: Border
    align: Literal["left", "center", "right"]
    rotate: int
    callout: tuple[Point, ...] | None
    callout_ending: LineEnding
    opacity: float

    def __init__(  # noqa: PLR0913 - the text's style options.
        self,
        rect: Sequence[float],
        text: str,
        *,
        font: Font | None = None,
        size: float = 11.0,
        text_color: Color = Color(0.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        color: Color | None = None,
        border: Border = Border(0),  # noqa: B008 - Border is immutable.
        align: Literal["left", "center", "right"] = "left",
        rotate: int = 0,
        callout: Sequence[Point | Sequence[float]] | None = None,
        callout_ending: LineEnding = LineEnding.NONE,
        opacity: float = 1.0,
    ) -> None:
        """Validate the specification; the text is bounded before anything else is read."""
        if not isinstance(text, str):
            msg = f"text must be a str: {type(text).__name__}"
            raise TypeError(msg)
        _bound_input("FreeText", text)
        if font is not None and not isinstance(font, Font):
            msg = f"font must be a pyropdf.Font or None, not {type(font).__name__}"
            raise TypeError(msg)
        if font is not None and font._font.standard_name is None:  # noqa: SLF001 - the package's own face.
            msg = "free text draws in a Standard 14 font; an embedded face is not supported"
            raise ValueError(msg)
        size = _number(size, "size")
        if not 0 < size <= _MAX_FREE_TEXT_SIZE:
            msg = f"size must be greater than 0 and at most {_MAX_FREE_TEXT_SIZE}: {size!r}"
            raise ValueError(msg)
        if not isinstance(border, Border):
            msg = f"border must be a Border: {border!r}"
            raise TypeError(msg)
        if align not in _FREE_TEXT_ALIGNMENTS:
            msg = f"align must be one of {', '.join(_FREE_TEXT_ALIGNMENTS)}: {align!r}"
            raise ValueError(msg)
        points = None
        if callout is not None:
            if isinstance(callout, (str, bytes)) or not isinstance(callout, Sequence) or not 2 <= len(callout) <= 3:  # noqa: PLR2004
                msg = "callout must hold 2 or 3 points: the point it explains, an optional knee, and the box"
                raise ValueError(msg)
            points = tuple(_point(point) for point in callout)
        object.__setattr__(self, "rect", Rect(*_rect(rect)))
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "font", font)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "text_color", _opaque_color(text_color, "text_color"))
        object.__setattr__(self, "color", _optional_color(color, "color"))
        object.__setattr__(self, "border", border)
        object.__setattr__(self, "align", align)
        object.__setattr__(self, "rotate", _turn(rotate))
        object.__setattr__(self, "callout", points)
        object.__setattr__(self, "callout_ending", LineEnding(callout_ending))
        object.__setattr__(self, "opacity", _opacity(opacity))

    def _payload(self) -> dict[str, Any]:
        try:
            self.text.encode("cp1252")
        except UnicodeEncodeError as exc:
            msg = "Standard 14 fonts can only print WinAnsi (Latin-1-equivalent) characters"
            raise ValueError(msg) from exc
        border = self.border
        color = self.color
        text_color = self.text_color
        spec: dict[str, Any] = {
            "subtype": "FreeText",
            "rect": tuple(self.rect),
            "contents": self.text,
            "created": _now(),
            "color": None if color is None else (color.red, color.green, color.blue),
            "border": (border.width, _BORDER_STYLE_NAMES[border.style], list(border.dashes)),
            "opacity": self.opacity,
            "text_color": (text_color.red, text_color.green, text_color.blue),
            "font_size": self.size,
            "font_name": "Helvetica" if self.font is None else self.font._font.standard_name,  # noqa: SLF001
            "align": _FREE_TEXT_ALIGNMENTS.index(self.align),
            "rotate": self.rotate,
        }
        if self.callout is not None:
            spec["callout"] = [tuple(point) for point in self.callout]
            spec["callout_ending"] = _LINE_ENDING_NAMES[self.callout_ending]
        return spec


#: The most points that one annotation's geometry holds (``annotation_point_count``).
_MAX_ANNOTATION_POINTS = 65_536


def _optional_color(value: object, name: str) -> Color | None:
    return None if value is None else _opaque_color(value, name)


def _points(values: object, name: str, minimum: int, budget: list[int]) -> tuple[Point, ...]:
    """Validate a sequence of points, stopping an iterable at the first point over the budget."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        msg = f"{name} must be a sequence of points: {values!r}"
        raise TypeError(msg)
    points: list[Point] = []
    for value in values:
        if budget[0] >= _MAX_ANNOTATION_POINTS:
            code = "annotation_point_count"
            msg = f"an annotation holds at most {_MAX_ANNOTATION_POINTS} points"
            raise LimitError(code, msg)
        budget[0] += 1
        points.append(_point(value))
    if len(points) < minimum:
        msg = f"{name} must hold at least {minimum} points"
        raise ValueError(msg)
    return tuple(points)


def _strokes(strokes: object) -> tuple[tuple[Point, ...], ...]:
    """Validate the strokes of ink: one or more, each of one or more points, 65,536 points in all."""
    if isinstance(strokes, (str, bytes)) or not isinstance(strokes, Iterable):
        msg = f"strokes must be a sequence of point sequences: {strokes!r}"
        raise TypeError(msg)
    budget = [0]
    validated = tuple(_points(stroke, "a stroke", 1, budget) for stroke in strokes)
    if not validated:
        msg = "strokes must hold at least one stroke"
        raise ValueError(msg)
    return validated


def _line_endings(value: object) -> tuple[LineEnding, LineEnding]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:  # noqa: PLR2004
        msg = f"line_endings is a pair of LineEnding members: {value!r}"
        raise TypeError(msg)
    return LineEnding(value[0]), LineEnding(value[1])


class _Shape:
    """The fields that the specifications of shapes and lines share."""

    __slots__ = ()

    color: Color | None
    border: Border
    opacity: float
    blend_mode: BlendMode | None
    contents: str | None

    def _init_style(  # noqa: PLR0913, PLR0917 - the shared fields, as each __init__ received them.
        self,
        subtype: str,
        color: object,
        interior_color: object,
        border: object,
        opacity: object,
        blend_mode: object,
        contents: object,
    ) -> None:
        """Validate the shared fields; the contents are bounded before any point is read."""
        contents = _contents(contents)
        _bound_input(subtype, contents)
        if not isinstance(border, Border):
            msg = f"border must be a Border: {border!r}"
            raise TypeError(msg)
        object.__setattr__(self, "color", _optional_color(color, "color"))
        if not isinstance(self, Ink):
            object.__setattr__(self, "interior_color", _optional_color(interior_color, "interior_color"))
        object.__setattr__(self, "border", border)
        object.__setattr__(self, "opacity", _opacity(opacity))
        object.__setattr__(self, "blend_mode", None if blend_mode is None else BlendMode(blend_mode))
        object.__setattr__(self, "contents", contents)

    def _style(self, subtype: str) -> dict[str, Any]:
        border = self.border
        interior = getattr(self, "interior_color", None)
        endings = getattr(self, "line_endings", None)
        return {
            "subtype": subtype,
            "color": None if self.color is None else (self.color.red, self.color.green, self.color.blue),
            "interior_color": None if interior is None else (interior.red, interior.green, interior.blue),
            "border": (border.width, _BORDER_STYLE_NAMES[border.style], list(border.dashes)),
            "opacity": self.opacity,
            "blend_mode": None if self.blend_mode is None else _pdf_name(self.blend_mode),
            "contents": self.contents,
            "created": _now(),
            "clouds": getattr(self, "clouds", 0.0) or None,
            "line_endings": None if endings is None else tuple(_LINE_ENDING_NAMES[ending] for ending in endings),
        }


@dataclass(frozen=True, slots=True, init=False)
class Square(_Shape):
    """A rectangle, as ``page.annotations.add`` takes it.

    ``rect`` is in display coordinates. ``color`` strokes the border and
    ``interior_color`` fills the inside; either may be ``None``. The border
    lies inside ``rect``, and ``clouds`` from 0 to 2 draws it as a cloud
    whose bumps grow with it, inside ``rect`` too. ``opacity`` and
    ``blend_mode`` apply to both paints, and ``contents`` is the text of the
    annotation's note.
    """

    rect: Rect
    color: Color | None
    interior_color: Color | None
    border: Border
    clouds: float
    opacity: float
    blend_mode: BlendMode | None
    contents: str | None

    def __init__(  # noqa: PLR0913 - the annotation's style options.
        self,
        rect: Sequence[float],
        *,
        color: Color | None = Color(1.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        interior_color: Color | None = None,
        border: Border = Border(),  # noqa: B008 - Border is immutable.
        clouds: float = 0,
        opacity: float = 1.0,
        blend_mode: BlendMode | None = None,
        contents: str | None = None,
    ) -> None:
        """Validate the specification."""
        self._init_style(type(self).__name__, color, interior_color, border, opacity, blend_mode, contents)
        object.__setattr__(self, "rect", Rect(*_rect(rect)))
        object.__setattr__(self, "clouds", _clouds(clouds))

    def _payload(self) -> dict[str, Any]:
        return {**self._style(type(self).__name__), "rect": tuple(self.rect)}


@dataclass(frozen=True, slots=True, init=False)
class Circle(_Shape):
    """An ellipse inscribed in a rectangle, as ``page.annotations.add`` takes it.

    The fields are those of :class:`Square`; the ellipse and its border lie
    inside ``rect``.
    """

    rect: Rect
    color: Color | None
    interior_color: Color | None
    border: Border
    clouds: float
    opacity: float
    blend_mode: BlendMode | None
    contents: str | None

    def __init__(  # noqa: PLR0913 - the annotation's style options.
        self,
        rect: Sequence[float],
        *,
        color: Color | None = Color(1.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        interior_color: Color | None = None,
        border: Border = Border(),  # noqa: B008 - Border is immutable.
        clouds: float = 0,
        opacity: float = 1.0,
        blend_mode: BlendMode | None = None,
        contents: str | None = None,
    ) -> None:
        """Validate the specification."""
        self._init_style(type(self).__name__, color, interior_color, border, opacity, blend_mode, contents)
        object.__setattr__(self, "rect", Rect(*_rect(rect)))
        object.__setattr__(self, "clouds", _clouds(clouds))

    def _payload(self) -> dict[str, Any]:
        return {**self._style(type(self).__name__), "rect": tuple(self.rect)}


@dataclass(frozen=True, slots=True, init=False)
class LineAnnotation(_Shape):
    """A straight line between two display points, as ``page.annotations.add`` takes it.

    ``color`` strokes the line with the ``border`` width and dashes, and
    ``line_endings`` gives the shapes at its start and end, whose closed
    shapes ``interior_color`` fills. The annotation's rectangle is the line's
    bounds, widened for the stroke and the endings.
    """

    start: Point
    end: Point
    color: Color | None
    interior_color: Color | None
    border: Border
    line_endings: tuple[LineEnding, LineEnding]
    opacity: float
    blend_mode: BlendMode | None
    contents: str | None

    def __init__(  # noqa: PLR0913 - the annotation's style options.
        self,
        start: Point | Sequence[float],
        end: Point | Sequence[float],
        *,
        color: Color | None = Color(1.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        interior_color: Color | None = None,
        border: Border = Border(),  # noqa: B008 - Border is immutable.
        line_endings: tuple[LineEnding, LineEnding] = (LineEnding.NONE, LineEnding.NONE),
        opacity: float = 1.0,
        blend_mode: BlendMode | None = None,
        contents: str | None = None,
    ) -> None:
        """Validate the specification."""
        self._init_style("Line", color, interior_color, border, opacity, blend_mode, contents)
        object.__setattr__(self, "start", _point(start))
        object.__setattr__(self, "end", _point(end))
        object.__setattr__(self, "line_endings", _line_endings(line_endings))

    def _payload(self) -> dict[str, Any]:
        return {**self._style("Line"), "points": [[tuple(self.start), tuple(self.end)]]}


@dataclass(frozen=True, slots=True, init=False)
class Polygon(_Shape):
    """A closed polygon through two or more display points, as ``page.annotations.add`` takes it.

    ``color`` strokes the outline and ``interior_color`` fills it; ``clouds``
    from 0 to 2 draws the outline as a cloud whose bumps grow with it. One
    annotation holds at most 65,536 points.
    """

    vertices: tuple[Point, ...]
    color: Color | None
    interior_color: Color | None
    border: Border
    clouds: float
    opacity: float
    blend_mode: BlendMode | None
    contents: str | None

    def __init__(  # noqa: PLR0913 - the annotation's style options.
        self,
        vertices: Iterable[Point | Sequence[float]],
        *,
        color: Color | None = Color(1.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        interior_color: Color | None = None,
        border: Border = Border(),  # noqa: B008 - Border is immutable.
        clouds: float = 0,
        opacity: float = 1.0,
        blend_mode: BlendMode | None = None,
        contents: str | None = None,
    ) -> None:
        """Validate the specification; the contents are bounded before the points are read."""
        self._init_style("Polygon", color, interior_color, border, opacity, blend_mode, contents)
        object.__setattr__(self, "clouds", _clouds(clouds))
        object.__setattr__(self, "vertices", _points(vertices, "vertices", 2, [0]))

    def _payload(self) -> dict[str, Any]:
        return {**self._style("Polygon"), "points": [[tuple(point) for point in self.vertices]]}


@dataclass(frozen=True, slots=True, init=False)
class PolyLine(_Shape):
    """An open line through two or more display points, as ``page.annotations.add`` takes it.

    The fields are those of :class:`LineAnnotation`, with ``line_endings`` at
    the first and last points; one annotation holds at most 65,536 points.
    """

    vertices: tuple[Point, ...]
    color: Color | None
    interior_color: Color | None
    border: Border
    line_endings: tuple[LineEnding, LineEnding]
    opacity: float
    blend_mode: BlendMode | None
    contents: str | None

    def __init__(  # noqa: PLR0913 - the annotation's style options.
        self,
        vertices: Iterable[Point | Sequence[float]],
        *,
        color: Color | None = Color(1.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        interior_color: Color | None = None,
        border: Border = Border(),  # noqa: B008 - Border is immutable.
        line_endings: tuple[LineEnding, LineEnding] = (LineEnding.NONE, LineEnding.NONE),
        opacity: float = 1.0,
        blend_mode: BlendMode | None = None,
        contents: str | None = None,
    ) -> None:
        """Validate the specification; the contents are bounded before the points are read."""
        self._init_style("PolyLine", color, interior_color, border, opacity, blend_mode, contents)
        object.__setattr__(self, "line_endings", _line_endings(line_endings))
        object.__setattr__(self, "vertices", _points(vertices, "vertices", 2, [0]))

    def _payload(self) -> dict[str, Any]:
        return {**self._style("PolyLine"), "points": [[tuple(point) for point in self.vertices]]}


@dataclass(frozen=True, slots=True, init=False)
class Ink(_Shape):
    """Freehand strokes, each through one or more display points, as ``page.annotations.add`` takes it.

    ``color`` draws every stroke with round caps and joins at the ``border``
    width; one annotation holds at most 65,536 points in all.
    """

    strokes: tuple[tuple[Point, ...], ...]
    color: Color | None
    border: Border
    opacity: float
    blend_mode: BlendMode | None
    contents: str | None

    def __init__(  # noqa: PLR0913 - the annotation's style options.
        self,
        strokes: Iterable[Iterable[Point | Sequence[float]]],
        *,
        color: Color | None = Color(1.0, 0.0, 0.0),  # noqa: B008 - Color is immutable.
        border: Border = Border(),  # noqa: B008 - Border is immutable.
        opacity: float = 1.0,
        blend_mode: BlendMode | None = None,
        contents: str | None = None,
    ) -> None:
        """Validate the specification; the contents are bounded before the points are read."""
        self._init_style("Ink", color, None, border, opacity, blend_mode, contents)
        object.__setattr__(self, "strokes", _strokes(strokes))

    def _payload(self) -> dict[str, Any]:
        return {**self._style("Ink"), "points": [[tuple(point) for point in stroke] for stroke in self.strokes]}


#: The specifications that ``page.annotations.add`` takes.
AnnotationSpecification: TypeAlias = (
    Highlight
    | Underline
    | StrikeOut
    | Squiggly
    | Note
    | Square
    | Circle
    | LineAnnotation
    | Polygon
    | PolyLine
    | Ink
    | Caret
    | FileAttachment
    | FreeText
    | Stamp
)
_SPECIFICATIONS = (
    Highlight,
    Underline,
    StrikeOut,
    Squiggly,
    Note,
    Square,
    Circle,
    LineAnnotation,
    Polygon,
    PolyLine,
    Ink,
    Caret,
    FileAttachment,
    FreeText,
    Stamp,
)


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

    def add(self, specification: AnnotationSpecification) -> Annotation:
        """Add the annotation that ``specification`` describes, and return it.

        The annotation is drawn when its transaction commits, or before a
        read in the transaction, so that every viewer draws it alike.
        """
        if not isinstance(specification, _SPECIFICATIONS):
            msg = f"page.annotations.add takes an annotation specification: {type(specification).__name__}"
            raise TypeError(msg)
        page = self._page
        payload = cast("dict[str, object]", specification._payload())  # noqa: SLF001
        result = _core.annotation_add(page._document._core(), page._number, page._generation, payload)  # noqa: SLF001
        if result is None:
            raise page._stale()  # noqa: SLF001
        # The handle finds the new annotation by its object.
        return Annotation(page, result, 0)

    def __repr__(self) -> str:
        """Return ``Annotations([...])`` with the page's annotations."""
        return f"Annotations({list(self)!r})"


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (
    Annotation,
    AnnotationFile,
    Annotations,
    AnnotationType,
    AnnotationFlags,
    AttachmentIcon,
    Border,
    BorderStyle,
    Caret,
    Circle,
    FileAttachment,
    FreeText,
    Highlight,
    Ink,
    LineAnnotation,
    LineEnding,
    Note,
    NoteIcon,
    Polygon,
    PolyLine,
    Square,
    Squiggly,
    Stamp,
    StampIcon,
    StrikeOut,
    Underline,
):
    _public.__module__ = "pyropdf"
