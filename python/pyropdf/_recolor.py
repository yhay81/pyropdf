"""Recoloring: convert the colors that pages paint to gray, RGB, or CMYK.

``page.recolor(target)`` and ``doc.recolor(target)`` rewrite the color
operators of a page's content, of the Form XObjects that it draws, and of
its annotations' appearances, and convert the images that they paint, so
that every color that they set is in the ``target`` device space.

A color operator becomes ``g``, ``rg``, or ``k``: ``g``, ``rg``, ``k``, and
their stroking forms name their space, and ``sc``, ``scn``, and ``cs`` read
the current space, which a Form XObject inherits where it is drawn (ISO
32000-2 8.10.1). Device, CIE-based gray and RGB, and ICC-based spaces
convert as the device spaces of their component count; Indexed through its
base; Separation through its alternate space when its tint transform is an
exponential function. The conversions are the ones of ISO 32000-2 10.4.2,
with BT.601 luma for gray as rendering to gray uses, and full black
generation and undercolor removal for CMYK.

Nothing is edited in place: a changed Form XObject, appearance, or image
becomes a new object that the drawing which changed draws instead, so a
page that the call leaves out keeps what it drew. Shadings, patterns, Lab,
DeviceN, and what cannot be read are left as they are and reported.
"""

from __future__ import annotations

import base64
import binascii
import struct
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pyropdf import _core
from pyropdf._errors import PdfError
from pyropdf._objects import Array, Dictionary, Name, Real, Stream
from pyropdf._operations import InlineImage, Operation, Operations

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyropdf._document import Document
    from pyropdf._images import PageImage
    from pyropdf._page import Page

#: A target of :meth:`Page.recolor`.
RecolorTarget = Literal["gray", "rgb", "cmyk"]

_COMPONENTS = {"gray": 1, "rgb": 3, "cmyk": 4}
_FAMILIES = {1: "gray", 3: "rgb", 4: "cmyk"}
#: The operators that set a color in a space that they name.
_DEVICE_OPERATORS = {"g": "gray", "rg": "rgb", "k": "cmyk", "G": "gray", "RG": "rgb", "K": "cmyk"}
#: The fill operator that sets a color of each family.
_FILL_OPERATORS = {"gray": "g", "rgb": "rg", "cmyk": "k"}
_DEVICE_SPACES = {"gray": "DeviceGray", "rgb": "DeviceRGB", "cmyk": "DeviceCMYK"}
#: The abbreviations of the device spaces in an inline image (ISO 32000-2 table 91).
_INLINE_SPACES = {"gray": "G", "rgb": "RGB", "cmyk": "CMYK"}
#: The spaces that convert as the device space of a family.
_FAMILY_SPACES = {
    "DeviceGray": "gray",
    "G": "gray",
    "CalGray": "gray",
    "DeviceRGB": "rgb",
    "RGB": "rgb",
    "CalRGB": "rgb",
    "DeviceCMYK": "cmyk",
    "CMYK": "cmyk",
}
#: The ITU-R BT.601 luma weights, which rendering and images use for gray.
_LUMA = (0.299, 0.587, 0.114)
#: Nesting of Form XObjects that a recoloring follows from the page.
_MAX_FORM_DEPTH = 16
#: Drawings of Form XObjects that one call reads; the others are reported.
_MAX_FORMS = 4096
#: The entries that a report keeps; it counts the others.
_MAX_UNCONVERTED = 1024
#: The pixels of an inline image that a recoloring converts.
_MAX_INLINE_PIXELS = 1 << 20
#: Nesting of color spaces that a recoloring follows, as Indexed over Separation.
_MAX_SPACE_DEPTH = 4
#: The JPEG quality when an image's quantization tables cannot be read.
_DEFAULT_QUALITY = 75
#: The luminance quantization table of ITU T.81 annex K, at quality 50.
_LUMINANCE = (
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
)  # fmt: skip
#: The stream entries that a copy sets from its new data.
_STREAM_KEYS = frozenset({"Length", "Filter", "DecodeParms", "DL"})
#: The image entries that describe the samples a conversion replaces.
_SAMPLE_KEYS = frozenset({*_STREAM_KEYS, "ColorSpace", "BitsPerComponent", "Decode", "SMaskInData"})
#: The inline image filters that a recoloring decodes.
_INLINE_FILTERS = {
    "AHx": "hex",
    "ASCIIHexDecode": "hex",
    "A85": "a85",
    "ASCII85Decode": "a85",
    "Fl": "flate",
    "FlateDecode": "flate",
}
#: The annotation entries that hold a color array (ISO 32000-2 12.5.2, 12.5.6.2, and 12.5.6.19).
_ANNOTATION_COLORS = ("C", "IC")
_APPEARANCE_COLORS = ("BC", "BG")


@dataclass(frozen=True, slots=True)
class Unconverted:
    """A color that :meth:`Page.recolor` left as it was."""

    page: int
    """The index of the page that paints it."""
    kind: Literal["shading", "pattern", "colorspace", "image", "form"]
    """What it is: a ``sh`` shading, a pattern, a color space that does not
    convert, an image that does not convert, or a Form XObject past the
    nesting or drawing limit."""
    detail: str
    """What it is in particular, such as ``"Lab"`` or the image's name."""


@dataclass(frozen=True, slots=True)
class RecolorReport:
    """What :meth:`Page.recolor` or :meth:`Document.recolor` converted, and what it could not."""

    target: RecolorTarget
    pages: int
    """The pages recolored."""
    operations: int
    """The color operations rewritten, each drawing of a Form XObject counted once."""
    images: int
    """The image objects and inline images converted."""
    unconverted: tuple[Unconverted, ...]
    """What was left as it was, each kind and detail once per page, in the order met."""
    omitted: int = 0
    """The entries past the first 1,024 of :attr:`unconverted`, which are not listed."""


@dataclass(frozen=True, slots=True)
class _Space:
    """A color space as a recoloring reads it."""

    family: str | None
    """The device family whose colors it converts from, or ``None`` when it does not convert."""
    components: int
    detail: str
    """What a report names it."""
    initial: tuple[float, ...] = ()
    """The color that selecting the space sets (ISO 32000-2 8.6.5)."""
    base: _Space | None = None
    lookup: bytes = b""
    """An Indexed space's colors in its base, or ``base``'s components per index."""
    function: tuple[tuple[float, ...], tuple[float, ...], float] | None = None
    """A Separation's exponential tint transform: C0, C1, and N."""
    pattern: bool = False

    def convert(self, values: tuple[float, ...], target: str) -> tuple[float, ...] | None:
        """Return ``values`` of this space in ``target``, or ``None`` when they do not convert."""
        if len(values) != self.components:
            return None
        if self.function is not None and self.base is not None:
            c0, c1, exponent = self.function
            tint = min(max(values[0], 0.0), 1.0)
            return self.base.convert(tuple(a + tint**exponent * (b - a) for a, b in zip(c0, c1, strict=True)), target)
        if self.lookup and self.base is not None:
            width = self.base.components
            index = int(values[0])
            entry = self.lookup[index * width : (index + 1) * width]
            if index < 0 or len(entry) != width:
                return None
            return self.base.convert(tuple(byte / 255 for byte in entry), target)
        if self.family is None:
            return None
        return _convert(self.family, values, target)


_DEVICE = {
    "gray": _Space("gray", 1, "DeviceGray", (0.0,)),
    "rgb": _Space("rgb", 3, "DeviceRGB", (0.0, 0.0, 0.0)),
    "cmyk": _Space("cmyk", 4, "DeviceCMYK", (0.0, 0.0, 0.0, 1.0)),
}


def _convert(family: str, values: tuple[float, ...], target: str) -> tuple[float, ...]:
    """Convert a color of a device ``family`` to ``target`` (ISO 32000-2 10.4.2)."""
    values = tuple(min(max(value, 0.0), 1.0) for value in values)
    if family == target:
        return values
    if family == "gray":
        rgb = (values[0],) * 3
    elif family == "cmyk":
        c, m, y, k = values
        rgb = (1 - min(1.0, c + k), 1 - min(1.0, m + k), 1 - min(1.0, y + k))
    else:
        rgb = values
    if target == "rgb":
        return rgb
    if target == "gray":
        return (sum(weight * value for weight, value in zip(_LUMA, rgb, strict=True)),)
    c, m, y = (1 - value for value in rgb)
    k = min(c, m, y)
    return (c - k, m - k, y - k, k)


def _number(value: float) -> int | Real:
    """Return a color component as a short operand."""
    rounded = round(value, 4)
    return int(rounded) if rounded == int(rounded) else Real(rounded)


def _numbers(operands: tuple[Any, ...]) -> tuple[float, ...] | None:
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in operands):
        return tuple(float(value) for value in operands)
    return None


@dataclass(slots=True)
class _State:
    """The current fill and stroke spaces, as the source content sets them."""

    fill: _Space = field(default_factory=lambda: _DEVICE["gray"])
    stroke: _Space = field(default_factory=lambda: _DEVICE["gray"])


@dataclass(slots=True)
class _Pending:
    """A new object that the write adds: a Form XObject's or an appearance's copy, or a converted image."""

    source: Stream
    data: bytes
    resources: Dictionary | None = None
    """The resources that a copy draws through, which it owns when ``drawn`` is not empty."""
    drawn: dict[str, int | Stream] = field(default_factory=dict)
    """What a copy draws by a name that changed: a pending object's index, or an object it drew by another name."""
    entries: dict[str, Any] = field(default_factory=dict)
    """An image's entries that its data sets."""
    jpeg: bool = False
    number: int = 0


@dataclass(slots=True)
class _PageEdit:
    page: Page
    operations: list[Operation | InlineImage] | None
    tail: bytes
    drawn: dict[str, int | Stream]
    appearances: list[tuple[Dictionary, str, str | None, int]]
    """The annotation, the appearance's key, its state or ``None``, and the pending copy."""
    colors: list[tuple[Dictionary, str | None, str, list[int | Real]]]
    """The color arrays that change: the annotation, ``"MK"`` for an array of its appearance characteristics, the
    key, and the new array."""


def recolor(document: Document, target: object, indexes: Iterable[int]) -> RecolorReport:
    """Implement :meth:`Document.recolor` and :meth:`Page.recolor` on the pages at ``indexes``, each resolved once."""
    if target not in _COMPONENTS:
        msg = f"target must be 'gray', 'rgb', or 'cmyk': {target!r}"
        raise ValueError(msg)
    all_pages = document.pages
    recoloring = _Recoloring(document, str(target))
    edits = [recoloring.page(all_pages[index]) for index in indexes]
    recoloring.write(edits)
    return RecolorReport(
        target,  # ty: ignore[invalid-argument-type] - checked above.
        len(edits),
        recoloring.operations,
        recoloring.images,
        tuple(recoloring.unconverted),
        recoloring.omitted,
    )


class _Recoloring:
    """One call: reads the pages, then writes what changed in one transaction."""

    def __init__(self, document: Document, target: str) -> None:
        self.document = document
        self.target = target
        self.operations = 0
        self.images = 0
        self.unconverted: list[Unconverted] = []
        self.omitted = 0
        self.seen: set[tuple[int, str, str]] = set()
        #: What the Forms and images being read leave as they were, so that a later drawing reports it again.
        self.recorders: list[dict[tuple[str, str], None]] = []
        self.pending: list[_Pending] = []
        self.forms = 0
        #: The copy of each drawing of a Form XObject, by the form, its inherited resources, and spaces.
        self.copies: dict[tuple[object, ...], tuple[int | None, Any, tuple[tuple[str, str], ...]]] = {}
        #: The conversion of each image object, and what it leaves as it was.
        self.converted: dict[int, tuple[int | None, tuple[tuple[str, str], ...]]] = {}
        self.parsed: dict[int, Operations] = {}
        self.page_index = 0
        self.page_images: dict[int, PageImage] | None = None
        self.current: Page | None = None

    def report(self, kind: str, detail: str) -> None:
        for recorder in self.recorders:
            if len(recorder) < _MAX_UNCONVERTED:
                recorder[kind, detail] = None
        key = (self.page_index, kind, detail)
        if key in self.seen:
            return
        if len(self.unconverted) >= _MAX_UNCONVERTED:
            self.omitted += 1
            return
        self.seen.add(key)
        self.unconverted.append(Unconverted(self.page_index, kind, detail))  # ty: ignore[invalid-argument-type]

    def page(self, page: Page) -> _PageEdit:
        """Plan the recoloring of one page."""
        self.page_index, self.current, self.page_images = page.index, page, None
        self.seen.clear()
        operations = page.operations
        edited = self._edit(operations, _resources(page), _State(), 0)
        edit = _PageEdit(page, None, operations.tail, {}, [], [])
        if edited is not None:
            edit.operations, edit.drawn = edited
        annotations = page.obj.get("Annots")
        for annotation in annotations if isinstance(annotations, Array) else ():
            if isinstance(annotation, Dictionary):
                self._annotation(annotation, edit)
        return edit

    def _annotation(self, annotation: Dictionary, edit: _PageEdit) -> None:
        for key in _ANNOTATION_COLORS:
            self._color_array(annotation, None, key, edit)
        characteristics = annotation.get("MK")
        if isinstance(characteristics, Dictionary):
            for key in _APPEARANCE_COLORS:
                self._color_array(annotation, "MK", key, edit)
        appearances = annotation.get("AP")
        if not isinstance(appearances, Dictionary):
            return
        for key in ("N", "R", "D"):
            entry = appearances.get(key)
            states = [(None, entry)] if isinstance(entry, Stream) else []
            if isinstance(entry, Dictionary) and not isinstance(entry, Stream):
                states = [(str(state), value) for state, value in entry.items() if isinstance(value, Stream)]
            for state, stream in states:
                copy = self._form(stream, None, _State(), 1)
                if copy is not None:
                    edit.appearances.append((annotation, key, state, copy))

    def _color_array(self, annotation: Dictionary, inner: str | None, key: str, edit: _PageEdit) -> None:
        dictionary = annotation if inner is None else annotation[inner]
        value = dictionary.get(key)
        values = _numbers(tuple(value)) if isinstance(value, Array) else None
        family = _FAMILIES.get(len(values)) if values is not None else None
        if values is None or family is None or family == self.target:
            return
        edit.colors.append((annotation, inner, key, [_number(v) for v in _convert(family, values, self.target)]))

    def _edit(
        self, operations: Operations, resources: Dictionary | None, state: _State, depth: int
    ) -> tuple[list[Operation | InlineImage], dict[str, int | Stream]] | None:
        """Return a drawing's recolored operations and the new objects it draws, or ``None`` when nothing changes."""
        xobjects = _sub(resources, "XObject")
        states: list[_State] = []
        out: list[Operation | InlineImage] = []
        # What each name draws in the new operations: a pending object, or the object it names.
        bound: dict[str, int | Stream] = {}
        drawn: dict[str, int | Stream] = {}
        changed = False
        for operation in operations:
            if isinstance(operation, InlineImage):
                image = self._inline(operation, resources)
                changed = changed or image is not operation
                out.append(image)
                continue
            operator = operation.operator
            if operator == "q":
                states.append(_State(state.fill, state.stroke))
            elif operator == "Q":
                if states:
                    state = states.pop()
            elif operator == "sh":
                self.report("shading", str(operation.operands[0]) if operation.operands else "")
            elif operator == "Do":
                drawing = self._draw(operation, (xobjects, resources), state, depth, (bound, drawn))
                changed = changed or drawing is not operation
                operation = drawing  # noqa: PLW2901 - the drawing under its new name.
            else:
                replacement = self._color(operation, resources, state)
                if replacement is not None:
                    out.append(replacement)
                    changed = True
                    continue
            out.append(operation)
        return (out, drawn) if changed or drawn else None

    def _draw(
        self,
        operation: Operation,
        scope: tuple[Dictionary | None, Dictionary | None],
        state: _State,
        depth: int,
        names: tuple[dict[str, int | Stream], dict[str, int | Stream]],
    ) -> Operation:
        """Recolor what a ``Do`` draws, and return the ``Do``, under a new name when the name draws another object.

        ``scope`` holds the drawing's XObjects and resources; ``names`` what
        each name draws in the new operations, and the names that changed.
        """
        xobjects, resources = scope
        bound, drawn = names
        name = str(operation.operands[0]) if operation.operands and isinstance(operation.operands[0], Name) else ""
        xobject = xobjects.get(name) if xobjects is not None and name else None
        if not isinstance(xobject, Stream):
            return operation
        new = self._xobject(xobject, resources, state, depth, name)
        want = xobject if new is None else new
        if name in bound and not _same(bound[name], want):
            # A Form that changes in one drawing and not in another: each draws by a name of its own.
            name = _fresh(name, xobjects, bound)
            operation = Operation("Do", (Name(name),))
            drawn[name] = want
        bound.setdefault(name, want)
        if new is not None:
            drawn[name] = new
        return operation

    def _color(self, operation: Operation, resources: Dictionary | None, state: _State) -> Operation | None:
        """Track a color operation and return the operation that sets its converted color, or ``None``."""
        operator, operands = operation.operator, operation.operands
        stroking = operator.isupper()
        lower = operator.lower()
        if operator in _DEVICE_OPERATORS:
            space, values = _DEVICE[_DEVICE_OPERATORS[operator]], _numbers(operands)
        elif lower == "cs" and len(operands) == 1:
            space = self._space(operands[0], resources, 0)
            values = space.initial
        elif lower in ("sc", "scn"):
            space = state.stroke if stroking else state.fill
            values = _numbers(operands)
            if values is None and space.pattern:
                self.report("pattern", space.detail)
        else:
            return None
        if stroking:
            state.stroke = space
        else:
            state.fill = space
        if space.family == self.target:
            return None
        converted = space.convert(values, self.target) if values is not None else None
        if converted is None:
            if not space.pattern and space.detail != "Separation /None":
                self.report("colorspace", space.detail)
            return None
        self.operations += 1
        fill = _FILL_OPERATORS[self.target]
        return Operation(fill.upper() if stroking else fill, tuple(_number(value) for value in converted))

    def _space(self, value: object, resources: Dictionary | None, depth: int) -> _Space:
        """Read a color space by name or by array (ISO 32000-2 8.6)."""
        if isinstance(value, Name):
            return self._named(str(value), resources, depth)
        if not isinstance(value, (Array, tuple)) or not len(value) or not isinstance(value[0], Name):
            return _Space(None, 0, "unreadable")
        family = str(value[0])
        space = _simple_space(family, value)
        # Indexed and Separation: a base space, then one more entry each (ISO 32000-2 8.6.6).
        if space is None and len(value) == 4 and depth < _MAX_SPACE_DEPTH:  # noqa: PLR2004
            if family in ("Indexed", "I"):
                space = self._indexed(self._space(value[1], resources, depth + 1), value[3])
            elif family == "Separation":
                space = _separation(str(value[1]), self._space(value[2], resources, depth + 1), value[3])
        return _Space(None, 0, family) if space is None else space

    def _named(self, name: str, resources: Dictionary | None, depth: int) -> _Space:
        """Read a color space by name: a family's, or one that ``resources`` define."""
        if name in _FAMILY_SPACES:
            return _DEVICE[_FAMILY_SPACES[name]]
        if name == "Pattern":
            return _Space(None, 0, "Pattern", pattern=True)
        spaces = _sub(resources, "ColorSpace")
        defined = spaces.get(name) if spaces is not None else None
        if defined is None or depth >= _MAX_SPACE_DEPTH:
            return _Space(None, 0, name)
        return self._space(defined, None, depth + 1)

    @staticmethod
    def _indexed(base: _Space, lookup: object) -> _Space:
        """Return an Indexed space over ``base`` with its ``lookup`` table."""
        data = lookup.read() if isinstance(lookup, Stream) else bytes(lookup) if isinstance(lookup, bytes) else b""
        if base.family is None or not data:
            return _Space(None, 1, "Indexed")
        return _Space(None, 1, "Indexed", (0.0,), base, data)

    def _xobject(
        self, xobject: Stream, resources: Dictionary | None, state: _State, depth: int, name: str
    ) -> int | None:
        subtype = xobject.get("Subtype")
        if subtype == "Form":
            return self._form(xobject, resources, state, depth + 1)
        if subtype == "Image":
            return self._image(xobject, name)
        return None

    def _form(self, form: Stream, inherited: Dictionary | None, state: _State, depth: int) -> int | None:
        """Return the pending copy of a drawing of ``form``, or ``None`` when it does not change."""
        own = form.get("Resources")
        resources = own if isinstance(own, Dictionary) else inherited
        number = _xref(form)
        key = (number, None if resources is own else _key(inherited), state.fill, state.stroke)
        cached = self.copies.get(key)
        if cached is not None:
            self._replay(cached[2])
            return cached[0]
        if depth > _MAX_FORM_DEPTH:
            self.report("form", f"Form XObjects nested deeper than {_MAX_FORM_DEPTH}")
            return None
        if self.forms >= _MAX_FORMS:
            self.report("form", f"more than {_MAX_FORMS} drawings of Form XObjects")
            return None
        self.forms += 1
        operations = self.parsed.get(number)
        if operations is None:
            try:
                operations = Operations.parse(form.read())
            except (PdfError, ValueError):
                self.report("form", f"object {number} cannot be read")
                return None
            self.parsed[number] = operations
        notes: dict[tuple[str, str], None] = {}
        self.recorders.append(notes)
        try:
            edited = self._edit(operations, resources, _State(state.fill, state.stroke), depth)
        finally:
            self.recorders.pop()
        copy = None
        if edited is not None:
            out, drawn = edited
            data = Operations(out, tail=operations.tail).to_bytes()
            self.pending.append(_Pending(form, data, resources, drawn))
            copy = len(self.pending) - 1
        # The cache holds the inherited resources, so that their handle stays theirs.
        self.copies[key] = (copy, inherited, tuple(notes))
        return copy

    def _replay(self, notes: tuple[tuple[str, str], ...]) -> None:
        """Report again, for this page, what a Form or image that an earlier drawing read left as it was."""
        for kind, detail in notes:
            self.report(kind, detail)

    def _image(self, stream: Stream, name: str) -> int | None:
        """Return the pending conversion of an image object, or ``None`` when it stays."""
        number = _xref(stream)
        cached = self.converted.get(number)
        if cached is not None:
            self._replay(cached[1])
            return cached[0]
        notes: dict[tuple[str, str], None] = {}
        self.recorders.append(notes)
        try:
            converted = self._convert_image(stream, number, name)
        finally:
            self.recorders.pop()
        self.converted[number] = (converted, tuple(notes))
        return converted

    def _convert_image(self, stream: Stream, number: int, name: str) -> int | None:
        if stream.get("ImageMask") is True:
            return None
        space = self._space(stream.get("ColorSpace"), None, 0) if "ColorSpace" in stream else None
        if space is not None and space.family == self.target:
            return None
        detail = f"/{name} (object {number})"
        if isinstance(stream.get("Mask"), Array) or stream.get("SMaskInData"):
            self.report("image", f"{detail}: its mask reads its samples")
            return None
        if self.page_images is None:
            assert self.current is not None  # noqa: S101 - set for each page.
            images = self.current.images(annotations=True)
            self.page_images = {image.item.xref: image for image in images if image.item.xref}
        image = self.page_images.get(number)
        try:
            pixmap = image.pixmap() if image is not None else None
        except PdfError:
            pixmap = None
        if image is None or pixmap is None or (pixmap.width, pixmap.height) != (image.item.width, image.item.height):
            self.report("image", f"{detail}: not decoded")
            return None
        stored = image.stored()
        jpeg = stored is not None and stored.filter == "DCTDecode" and self.target != "cmyk"
        quality = _jpeg_quality(stored.data) if jpeg and stored is not None else None
        data = _core.recolor_samples(pixmap, _COMPONENTS[self.target], quality)
        entries = {
            "Width": pixmap.width,
            "Height": pixmap.height,
            "BitsPerComponent": 8,
            "ColorSpace": Name(_DEVICE_SPACES[self.target]),
        }
        self.pending.append(_Pending(stream, data, entries=entries, jpeg=jpeg))
        self.images += 1
        return len(self.pending) - 1

    def _inline(self, image: InlineImage, resources: Dictionary | None) -> InlineImage:
        """Return an inline image converted to the target, or ``image`` when it stays."""
        entries = image.dictionary
        if entries.get("IM", entries.get("ImageMask")) is True:
            return image
        space = self._space(entries.get("CS", entries.get("ColorSpace")), resources, 0)
        if space.family == self.target:
            return image
        width, height = entries.get("W", entries.get("Width")), entries.get("H", entries.get("Height"))
        bits = entries.get("BPC", entries.get("BitsPerComponent"))
        data = _inline_data(entries.get("F", entries.get("Filter")), image.data)
        if (
            data is None
            or any(key in entries for key in ("D", "Decode", "DP", "DecodeParms"))
            or space.components == 0
            or bits != 8  # noqa: PLR2004 - 8-bit samples only.
            or not isinstance(width, int)
            or not isinstance(height, int)
            or not 0 < width * height <= _MAX_INLINE_PIXELS
            or len(data) < width * height * space.components
        ):
            self.report("image", f"inline image in {space.detail}")
            return image
        out = bytearray()
        colors: dict[bytes, bytes] = {}
        step = space.components
        for at in range(0, width * height * step, step):
            sample = data[at : at + step]
            color = colors.get(sample)
            if color is None:
                # An Indexed sample is an index; another space's is a component of 0 to 1.
                values = tuple(sample) if space.lookup else tuple(byte / 255 for byte in sample)
                converted = space.convert(values, self.target)
                if converted is None:
                    self.report("image", f"inline image in {space.detail}")
                    return image
                color = bytes(round(value * 255) for value in converted)
                colors[sample] = color
            out += color
        self.images += 1
        dictionary = {"W": width, "H": height, "BPC": 8, "CS": Name(_INLINE_SPACES[self.target]), "F": Name("Fl")}
        dictionary.update({key: value for key, value in entries.items() if key in ("I", "Interpolate", "Intent")})
        return InlineImage(dictionary, zlib.compress(bytes(out)))

    def write(self, edits: list[_PageEdit]) -> None:
        """Add the new objects and point the pages at them, in one transaction."""
        if not self.pending and not any(e.operations or e.drawn or e.appearances or e.colors for e in edits):
            return
        document = self.document
        with document.transaction("recolor"):
            for pending in self.pending:
                pending.number = self._add(pending)
            for edit in edits:
                if edit.operations is not None:
                    edit.page.operations = Operations(edit.operations, tail=edit.tail)
                if edit.drawn:
                    _repoint_page(edit.page, {name: self._object(value) for name, value in edit.drawn.items()})
                for annotation, inner, key, values in edit.colors:
                    _set_color(annotation, inner, key, values)
                for annotation, key, state, at in edit.appearances:
                    _set_appearance(annotation, key, state, self._object(at))

    def _object(self, value: int | Stream) -> Stream:
        """Return a pending object as added, or an object that a drawing draws by a new name."""
        return self.document.objects[self.pending[value].number] if isinstance(value, int) else value

    def _add(self, pending: _Pending) -> int:
        objects = self.document.objects
        dictionary = {key: value for key, value in pending.source.items() if key not in _STREAM_KEYS}
        if pending.entries:
            dictionary = {key: value for key, value in dictionary.items() if key not in _SAMPLE_KEYS}
            dictionary.update(pending.entries)
            if not pending.jpeg:
                return objects.add_stream(pending.data, dictionary)
            number = objects.add_stream(b"", dictionary, filter=None)
            stream = objects[number]
            stream.write(pending.data, filter=None)
            stream["Filter"] = Name("DCTDecode")
            return number
        if pending.drawn:
            own = dict(pending.resources.items()) if pending.resources is not None else {}
            xobjects = own.get("XObject")
            own["XObject"] = dict(xobjects.items()) if isinstance(xobjects, Dictionary) else {}
            for name, value in pending.drawn.items():
                own["XObject"][name] = self._object(value)
            dictionary["Resources"] = own
        return objects.add_stream(pending.data, dictionary)


def _simple_space(family: str, value: Array | tuple[Any, ...]) -> _Space | None:
    """Return a CIE-based, ICC-based, or Pattern space of an array, or ``None`` for another family."""
    if family in ("CalGray", "CalRGB"):
        return _DEVICE[_FAMILY_SPACES[family]]
    if family == "ICCBased" and len(value) > 1 and isinstance(value[1], Stream):
        components = value[1].get("N")
        return _DEVICE[_FAMILIES[components]] if components in _FAMILIES else _Space(None, 0, family)
    if family == "Pattern":
        return _Space(None, 0, "Pattern", pattern=True)
    return None


def _separation(name: str, base: _Space, function: object) -> _Space:
    """Return a Separation space over its alternate ``base``, which converts when its tint transform is exponential."""
    detail = f"Separation /{name}"
    exponential = _exponential(function, base.components)
    if name == "None" or base.family is None or exponential is None:
        return _Space(None, 1, detail, (1.0,) if name == "None" else ())
    return _Space(None, 1, detail, (1.0,), base, function=exponential)


def _exponential(function: object, outputs: int) -> tuple[tuple[float, ...], tuple[float, ...], float] | None:
    """Return C0, C1, and N of a type 2 function of one input and ``outputs`` outputs (ISO 32000-2 7.10.3)."""
    if not isinstance(function, Dictionary) or function.get("FunctionType") != 2:  # noqa: PLR2004 - exponential.
        return None
    exponent = function.get("N")
    c0 = function.get("C0")
    c1 = function.get("C1")
    first = _numbers(tuple(c0)) if isinstance(c0, Array) else (0.0,) if c0 is None else None
    last = _numbers(tuple(c1)) if isinstance(c1, Array) else (1.0,) if c1 is None else None
    if first is None or last is None or len(first) != outputs or len(last) != outputs:
        return None
    if not isinstance(exponent, (int, float)) or isinstance(exponent, bool):
        return None
    return first, last, float(exponent)


def _inline_data(filters: object, data: bytes) -> bytes | None:
    """Return an inline image's samples, decoding the filters that a recoloring reads, or ``None``."""
    names = [filters] if isinstance(filters, Name) else list(filters) if isinstance(filters, tuple) else []
    if filters is not None and not names:
        return None
    try:
        for name in names:
            kind = _INLINE_FILTERS.get(str(name))
            if kind == "hex":
                text = bytes(byte for byte in data.split(b">", 1)[0] if byte not in b" \t\r\n\f\x00")
                data = binascii.unhexlify(text + b"0" * (len(text) % 2))
            elif kind == "a85":
                data = base64.a85decode(data.split(b"~>", 1)[0], ignorechars=b" \t\r\n\f\x00")
            elif kind == "flate":
                data = zlib.decompressobj().decompress(data, _MAX_INLINE_PIXELS * 4)
            else:
                return None
    except (ValueError, zlib.error):
        return None
    return data


def _jpeg_quality(data: bytes) -> int:
    """Estimate the IJG quality of a JPEG from its first quantization table (ITU T.81 B.2.4.1)."""
    at = 2
    while at + 4 <= len(data) and data[at] == 0xFF:  # noqa: PLR2004 - a marker.
        marker = data[at + 1]
        (length,) = struct.unpack(">H", data[at + 2 : at + 4])
        if marker == 0xDB and at + 5 + 64 <= len(data):  # noqa: PLR2004 - DQT.
            precision = data[at + 4] >> 4
            if precision:
                values = struct.unpack(">64H", data[at + 5 : at + 5 + 128]) if at + 5 + 128 <= len(data) else ()
            else:
                values = tuple(data[at + 5 : at + 5 + 64])
            if len(values) == 64:  # noqa: PLR2004 - one table.
                scale = 100 * sum(values) / sum(_LUMINANCE)
                quality = (200 - scale) / 2 if scale <= 100 else 5000 / scale  # noqa: PLR2004 - IJG's scaling.
                return min(max(round(quality), 1), 100)
            break
        if marker == 0xDA:  # noqa: PLR2004 - the scan starts.
            break
        at += 2 + length
    return _DEFAULT_QUALITY


def _set_color(annotation: Dictionary, inner: str | None, key: str, values: list[int | Real]) -> None:
    """Set an annotation's color array, through appearance characteristics of its own, which other widgets may share."""
    dictionary = annotation
    if inner is not None:
        dictionary = annotation[inner]
        if dictionary.is_indirect:
            annotation[inner] = dict(dictionary.items())
            dictionary = annotation[inner]
    dictionary[key] = values


def _set_appearance(annotation: Dictionary, key: str, state: str | None, appearance: Stream) -> None:
    """Make an annotation show ``appearance``, through appearance dictionaries of its own."""
    appearances = annotation["AP"]
    if appearances.is_indirect:
        annotation["AP"] = dict(appearances.items())
        appearances = annotation["AP"]
    if state is None:
        appearances[key] = appearance
        return
    states = appearances[key]
    if states.is_indirect:
        appearances[key] = dict(states.items())
        states = appearances[key]
    states[state] = appearance


def _xref(stream: Stream) -> int:
    """Return a stream's object number: a stream is always an indirect object."""
    return stream.xref or 0


def _same(first: int | Stream, second: int | Stream) -> bool:
    if isinstance(first, Stream) and isinstance(second, Stream):
        return first.xref == second.xref
    return first == second


def _fresh(name: str, xobjects: Dictionary | None, bound: dict[str, int | Stream]) -> str:
    """Return a name that neither the drawing nor its resources use, for one drawing of the XObject ``name``."""
    number = 1
    while f"{name}_{number}" in bound or (xobjects is not None and f"{name}_{number}" in xobjects):
        number += 1
    return f"{name}_{number}"


def _repoint_page(page: Page, drawn: dict[str, Stream]) -> None:
    """Make the page draw the new objects, through resources of its own."""
    own = page.obj.get("Resources")
    if own is None or own.is_indirect:
        inherited = own if own is not None else _resources(page)
        page.obj["Resources"] = {} if inherited is None else dict(inherited.items())
    resources = page.obj["Resources"]
    xobjects = resources.get("XObject")
    if xobjects is None or xobjects.is_indirect:
        resources["XObject"] = {} if xobjects is None else dict(xobjects.items())
    for name, value in drawn.items():
        resources["XObject"][name] = value


def _resources(page: Page) -> Dictionary | None:
    """Return the page's resources, its own or inherited from the page tree."""
    node: Any = page.obj
    for _ in range(64):
        if not isinstance(node, Dictionary):
            return None
        resources = node.get("Resources")
        if isinstance(resources, Dictionary):
            return resources
        node = node.get("Parent")
    return None


def _sub(resources: Dictionary | None, key: str) -> Dictionary | None:
    value = resources.get(key) if resources is not None else None
    return value if isinstance(value, Dictionary) else None


def _key(dictionary: Dictionary | None) -> object:
    if dictionary is None:
        return None
    return dictionary.xref if dictionary.is_indirect else ("direct", id(dictionary))


for _public in (RecolorReport, Unconverted):
    _public.__module__ = "pyropdf"

__all__ = ["RecolorReport", "RecolorTarget", "Unconverted", "recolor"]
