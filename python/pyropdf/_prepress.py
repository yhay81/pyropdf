"""Stored page separations, boundary guidelines, and print annotation appearances."""

from __future__ import annotations

import math
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyropdf import _core, typed
from pyropdf._errors import LimitError, PdfError
from pyropdf._geometry import Matrix, Rect
from pyropdf._media import _require_media_version
from pyropdf._objects import Array, Dictionary, Name, Stream, String

if TYPE_CHECKING:
    from collections.abc import Generator

    from pyropdf._document import Document
    from pyropdf._page import Page

_BOUNDARIES = ("CropBox", "BleedBox", "TrimBox", "ArtBox")
_LIMIT = 4096
_RGB = 3
_COLOR_SPACE_SIZE = 4
_IDENTITY = Matrix(1, 0, 0, 1, 0, 0)


def _bound(count: int) -> None:
    if count > _LIMIT:
        msg = "prepress relationships exceed 4096 entries"
        code = "object_count"
        raise LimitError(code, msg)


@contextmanager
def _edit(document: Document, name: str) -> Generator[_core._OperationBudget, None, None]:
    """Share one budget and savepoint across the reads and writes of a prepress edit."""
    core = document._core()  # noqa: SLF001
    operation = _core.begin_operation(core, name)
    try:
        yield operation
    except BaseException:
        _core.abort_canvas(core, operation)
        raise
    _core.end_transaction(core, True)  # noqa: FBT003 - commit the operation.


def _finite(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        msg = "expected a finite print coordinate or component"
        raise ValueError(msg)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class BoxStyle:
    """Stored DeviceRGB guideline colour, width, solid/dashed style and dash pattern."""

    color: tuple[float, float, float] = (0, 0, 0)
    width: float = 1
    dashed: bool = False
    dashes: tuple[float, ...] = (3,)

    def __post_init__(self) -> None:
        """Validate the RGB components and PDF dash lengths."""
        if (
            not isinstance(self.color, tuple)
            or len(self.color) != _RGB
            or not all(0 <= _finite(c) <= 1 for c in self.color)
        ):
            msg = "box color needs three DeviceRGB components between zero and one"
            raise ValueError(msg)
        if _finite(self.width) < 0 or not isinstance(self.dashed, bool) or not isinstance(self.dashes, tuple):
            msg = "box style needs a nonnegative width, a dashed flag, and a tuple of dash lengths"
            raise ValueError(msg)
        _bound(len(self.dashes))
        if any(_finite(d) < 0 for d in self.dashes) or (self.dashes and not any(self.dashes)):
            msg = "dash lengths must be nonnegative and cannot all be zero"
            raise ValueError(msg)


def _dictionary(value: object, key: str) -> Dictionary:
    if not isinstance(value, Dictionary) or isinstance(value, Stream):
        msg = f"/{key} must be a dictionary"
        raise PdfError(msg)
    return value


def _local_dictionary(owner: Dictionary, key: str) -> Dictionary:
    """Return a writable owner-local dictionary without expanding its entries in Python."""
    if (entry := owner.get(key)) is None:
        owner[key] = {}
    else:
        obj = _dictionary(entry, key)
        if not obj.is_indirect:
            return obj
        owner._copy_direct(key, obj)  # noqa: SLF001
    return owner[key]


def box_styles(page: Page) -> dict[str, BoxStyle]:
    """Resolve the four local box guideline dictionaries, applying their defaults."""
    obj = page.obj.get("BoxColorInfo")
    result: dict[str, BoxStyle] = {}
    if obj is None:
        return result
    info = _dictionary(obj, "BoxColorInfo")
    for key in _BOUNDARIES:
        if info.get(key) is None:
            continue
        style = _dictionary(info[key], key)
        color = style.get("C", (0, 0, 0))
        dashes = style.get("D", (3,))
        if not isinstance(color, (Array, tuple)) or len(color) != _RGB or not isinstance(dashes, (Array, tuple)):
            msg = "invalid box color or dash array"
            raise PdfError(msg)
        _bound(len(dashes))
        style_name = typed.BoxStyle(style).S
        if style_name not in ("S", "D"):
            msg = "unknown page box guideline style"
            raise PdfError(msg)
        result[key] = BoxStyle(
            color=(color[0], color[1], color[2]),
            width=typed.BoxStyle(style).W or 0,
            dashed=style_name == "D",
            dashes=tuple(dashes),
        )
    return result


def write_box_styles(page: Page, styles: Mapping[str, BoxStyle]) -> None:
    message = "box styles map CropBox, BleedBox, TrimBox or ArtBox to BoxStyle values"
    if not isinstance(styles, Mapping):
        raise TypeError(message)
    prepared = {}
    for count, (key, style) in enumerate(styles.items(), 1):
        if count > len(_BOUNDARIES) or key not in _BOUNDARIES or not isinstance(style, BoxStyle):
            raise ValueError(message)
        prepared[key] = style
    with _edit(page.document, "box styles"):
        obj = page.obj
        if not prepared and obj.get("BoxColorInfo") is None:
            return
        if prepared:
            _require_media_version(page.document, (1, 4))
        info = _local_dictionary(obj, "BoxColorInfo")
        for key in _BOUNDARIES:
            if key in prepared:
                style = prepared[key]
                entry = _local_dictionary(info, key)
                entry.update(C=style.color, W=style.width, S=Name("D" if style.dashed else "S"), D=style.dashes)
            elif key in info:
                del info[key]
        if not info:
            del obj["BoxColorInfo"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SeparationPlate:
    """The colorant of one pre-separated page and its optional Separation or DeviceN space."""

    colorant: Name | String
    color_space: Array | None = None

    def __post_init__(self) -> None:
        """Check the device colorant against the optional colour space."""
        if not isinstance(self.colorant, (Name, String)):
            msg = "a plate colorant must be a PDF Name or String"
            raise TypeError(msg)
        if self.color_space is not None:
            _validate_space(self.colorant, self.color_space)


def _validate_space(colorant: Name | String, space: Array) -> None:
    if not isinstance(space, Array) or len(space) < _COLOR_SPACE_SIZE:
        msg = "a plate color space must be a Separation or DeviceN array"
        raise ValueError(msg)
    name = colorant if isinstance(colorant, Name) else bytes(colorant).decode("utf-8", "surrogateescape")
    if space[0] == "Separation" and len(space) == _COLOR_SPACE_SIZE and space[1] == name:
        view = typed.SeparationColorSpace(space)
        _ = view[2], view[3]
        return
    if (
        space[0] == "DeviceN"
        and len(space) in (_COLOR_SPACE_SIZE, _COLOR_SPACE_SIZE + 1)
        and isinstance(names := space[1], Array)
    ):
        _bound(len(names))
        if name in names:
            device = typed.DeviceNColorSpace(space)
            _ = device[2], device[3]
            if len(space) > _COLOR_SPACE_SIZE:
                _ = device[4]
            return
    msg = "the colorant must belong to the plate's Separation or DeviceN space"
    raise ValueError(msg)


def _member_list(info: Dictionary) -> Array:
    members = info.get("Pages")
    if not isinstance(members, Array) or not members:
        msg = "a separation group requires a nonempty /Pages array"
        raise PdfError(msg)
    _bound(len(members))
    return members


def separations(page: Page) -> dict[Page, SeparationPlate]:
    """Resolve each plate and check the ordered membership on every participant."""
    from pyropdf._page import Page  # noqa: PLC0415

    own = page.obj.get("SeparationInfo")
    if own is None:
        return {}
    members = _member_list(_dictionary(own, "SeparationInfo"))
    objects = tuple(members)
    verified = {members}
    visited = len(objects)
    result: dict[Page, SeparationPlate] = {}
    for obj in objects:
        if not isinstance(obj, Dictionary) or obj.xref is None or obj.generation is None:
            msg = "a separation must refer to an indirect page"
            raise PdfError(msg)
        plate = Page(page.document, obj.xref, obj.generation)
        if plate not in page.document.pages or plate in result:
            msg = "duplicate or missing page in a separation group"
            raise PdfError(msg)
        info = _dictionary(obj.get("SeparationInfo"), "SeparationInfo")
        other = _member_list(info)
        if other not in verified:
            visited += len(other)
            _bound(visited)
            if tuple(other) != objects:
                msg = "all plates must have the same ordered separation group"
                raise PdfError(msg)
            verified.add(other)
        result[plate] = SeparationPlate(colorant=info["DeviceColorant"], color_space=info.get("ColorSpace"))
    if page not in result:
        msg = "a separation group must include its own page"
        raise PdfError(msg)
    return result


def _prepare_plates(
    doc: Document,
    plates: Mapping[Page, SeparationPlate],
    old: Mapping[Page, SeparationPlate],
    operation: _core._OperationBudget,
) -> dict[Page, tuple[SeparationPlate, Array | None]]:
    _bound(len(plates))
    # Snapshot caller mappings and direct colour spaces before any owner
    # is replaced. A space may itself be stored inside a departing plate.
    prepared: dict[Page, tuple[SeparationPlate, Array | None]] = {}
    for count, (member, plate) in enumerate(plates.items(), 1):
        _bound(count)
        operation.charge("memory_size", 256)
        if member not in doc.pages or not isinstance(plate, SeparationPlate):
            msg = "separations require current pages of this document and SeparationPlate values"
            raise ValueError(msg)
        if member not in old and member.obj.get("SeparationInfo") is not None:
            msg = "detach a page from its existing separation group before joining another"
            raise ValueError(msg)
        space = plate.color_space
        if space is not None:
            _validate_space(plate.colorant, space)
            if not space.is_indirect:
                space = doc.objects[doc.objects.add(space)]
        prepared[member] = plate, space
    return prepared


def write_separations(page: Page, plates: Mapping[Page, SeparationPlate]) -> None:
    """Replace or dissolve the complete group; reject accidental joins of existing groups."""
    if not isinstance(plates, Mapping):
        msg = "separations must map pages to SeparationPlate values"
        raise TypeError(msg)
    doc = page.document
    with _edit(doc, "page separations") as operation:
        old = separations(page)
        prepared = _prepare_plates(doc, plates, old, operation)
        if prepared and page not in prepared:
            msg = "the separation group must include its own page"
            raise ValueError(msg)
        for member in old.keys() - prepared.keys():
            del member.obj["SeparationInfo"]
        if not prepared:
            return
        refs = doc.objects[doc.objects.add([member.obj for member in prepared])]
        _require_media_version(doc, (1, 3))
        for member, (spec, space) in prepared.items():
            obj = member.obj
            info = _local_dictionary(obj, "SeparationInfo")
            info.update(Pages=refs, DeviceColorant=spec.colorant)
            if space is None:
                info.pop("ColorSpace", None)
            else:
                info["ColorSpace"] = space


@dataclass(frozen=True, slots=True, kw_only=True)
class FixedPrint:
    """Watermark matrix and translations as fractions of the target media width and height."""

    matrix: Matrix = _IDENTITY
    horizontal: float = 0
    vertical: float = 0

    def __post_init__(self) -> None:
        """Validate the six matrix components and both translations."""
        if not isinstance(self.matrix, Matrix):
            msg = "fixed print placement requires a Matrix"
            raise TypeError(msg)
        for value in (*self.matrix, self.horizontal, self.vertical):
            _finite(value)

    def _entries(self) -> dict[str, object]:
        return {"Type": Name("FixedPrint"), "Matrix": tuple(self.matrix), "H": self.horizontal, "V": self.vertical}


def write_fixed_print(obj: Dictionary, placement: FixedPrint | None) -> None:
    if obj.get("Subtype") != "Watermark":
        msg = "fixed print placement belongs to a watermark"
        raise ValueError(msg)
    if placement is None:
        if "FixedPrint" in obj:
            del obj["FixedPrint"]
        return
    if not isinstance(placement, FixedPrint):
        msg = "fixed print placement must be a FixedPrint value"
        raise TypeError(msg)
    _require_media_version(obj._document, (1, 6))  # noqa: SLF001
    _local_dictionary(obj, "FixedPrint").update(placement._entries())  # noqa: SLF001


def fixed_print(obj: Dictionary) -> FixedPrint | None:
    if obj.get("Subtype") != "Watermark" or obj.get("FixedPrint") is None:
        return None
    view = typed.FixedPrint(_dictionary(obj["FixedPrint"], "FixedPrint"))
    if view.Type != "FixedPrint":
        msg = "invalid /FixedPrint/Type"
        raise PdfError(msg)
    return FixedPrint(matrix=view.Matrix or _IDENTITY, horizontal=view.H or 0, vertical=view.V or 0)


def _form(appearance: Stream) -> None:
    if not isinstance(appearance, Stream) or not appearance.is_indirect or appearance.get("Subtype") != "Form":
        msg = "print appearance must be an indirect Form XObject"
        raise ValueError(msg)
    _ = typed.XObjectFormType1(appearance).BBox


def _mark_form(appearance: Stream) -> None:
    _form(appearance)
    if (entry := appearance.get("Colorants")) is None:
        return
    colorants = _dictionary(entry, "Colorants")
    _bound(len(colorants))
    for key, space in colorants.items():
        if (
            not isinstance(space, Array)
            or len(space) != _COLOR_SPACE_SIZE
            or space[0] != "Separation"
            or space[1] != key
        ):
            msg = "a printer's mark colorant must match its Separation color space"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class PrinterMark:
    """A printable, read-only printer's mark with a supplied Form XObject and optional mark name."""

    rect: Rect
    appearance: Stream
    name: Name | None = None

    def __post_init__(self) -> None:
        """Check the rectangle, normal Form and optional /MN."""
        from pyropdf._threads import _display_rect  # noqa: PLC0415

        object.__setattr__(self, "rect", _display_rect(self.rect))
        _mark_form(self.appearance)
        if self.name is not None and not isinstance(self.name, Name):
            msg = "a printer's mark name must be a PDF Name"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class Watermark:
    """A printable watermark with a supplied Form and optional fixed placement on the target media."""

    rect: Rect
    appearance: Stream
    fixed_print: FixedPrint | None = None

    def __post_init__(self) -> None:
        """Check the rectangle, normal Form, and optional fixed print placement."""
        from pyropdf._threads import _display_rect  # noqa: PLC0415

        object.__setattr__(self, "rect", _display_rect(self.rect))
        _form(self.appearance)
        if self.fixed_print is not None and not isinstance(self.fixed_print, FixedPrint):
            msg = "watermark placement must be a FixedPrint value"
            raise TypeError(msg)


def add_print(page: Page, spec: PrinterMark | Watermark) -> tuple[int, int]:
    from pyropdf._threads import _display_to_pdf_rect  # noqa: PLC0415

    doc = page.document
    with _edit(doc, "add print annotation"):
        mark = isinstance(spec, PrinterMark)
        if mark:
            _mark_form(spec.appearance)
        else:
            _form(spec.appearance)
        _require_media_version(doc, (1, 4) if mark else (1, 6))
        obj = page.obj
        entries: dict[str, object] = {
            "Type": Name("Annot"),
            "Subtype": Name("PrinterMark" if mark else "Watermark"),
            "Rect": _display_to_pdf_rect(doc, page, spec.rect),
            "P": obj,
            "F": 68 if mark else 4,
            "AP": {"N": spec.appearance},
        }
        if isinstance(spec, PrinterMark) and spec.name is not None:
            entries["MN"] = spec.name
        if isinstance(spec, Watermark) and spec.fixed_print is not None:
            entries["FixedPrint"] = spec.fixed_print._entries()  # noqa: SLF001
        annot = doc.objects[doc.objects.add(entries)]
        append_annotation(page, annot)
        return annot.xref, annot.generation


def append_annotation(page: Page, annot: Dictionary) -> None:
    """Attach the new annotation under the page's own array, preserving trap order."""
    if annot.xref is None or annot.generation is None:
        msg = "a newly allocated annotation must be indirect"
        raise ValueError(msg)
    if not _core.annotation_attach(
        page.document._core(),  # noqa: SLF001
        page.xref,
        page._generation,  # noqa: SLF001
        (annot.xref, annot.generation),
    ):
        raise page._stale()  # noqa: SLF001


def print_appearance(
    obj: Dictionary,
) -> typed.XObjectFormPrinterMark | typed.XObjectFormTrapNet | typed.XObjectFormType1 | None:
    """Resolve the active normal appearance, including a named state dictionary."""
    kind = {
        "PrinterMark": typed.XObjectFormPrinterMark,
        "TrapNet": typed.XObjectFormTrapNet,
        "Watermark": typed.XObjectFormType1,
    }.get(obj.get("Subtype"))
    if kind is None or obj.get("AP") is None:
        return None
    entry = _dictionary(obj["AP"], "AP").get("N")
    if isinstance(entry, Dictionary) and not isinstance(entry, Stream):
        state = obj.get("AS")
        entry = entry.get(state) if isinstance(state, Name) else None
    if not isinstance(entry, Stream):
        msg = "the print annotation has no active normal appearance"
        raise PdfError(msg)
    _form(entry)
    return kind(entry)


for _public in (BoxStyle, SeparationPlate, FixedPrint, PrinterMark, Watermark):
    _public.__module__ = "pyropdf"
