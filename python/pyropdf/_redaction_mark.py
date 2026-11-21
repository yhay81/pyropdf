"""Redaction marks and their replacement instructions (ISO 32000-2 Table 195)."""

from __future__ import annotations

import math
from dataclasses import KW_ONLY, dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._appearance_states import _form
from pyropdf._content import Color
from pyropdf._errors import MalformedPdfError
from pyropdf._geometry import Quad, Rect
from pyropdf._objects import Array, Name, Stream, String
from pyropdf._presentations import _pdf_real, _to_display, _to_pdf

if TYPE_CHECKING:
    from pyropdf._annotations import Annotation
    from pyropdf._page import Page

_MAX_QUADS = 4096
_QUAD_SIZE = 8
_RGB_COMPONENTS = 3


@dataclass(frozen=True, slots=True)
class RedactionMark:
    """Mark a rectangle or quadrilaterals for later redaction.

    Coordinates are in display space. Each optional quad lies inside the rectangle.
    The normal appearance outlines the proposed regions. Page content remains
    recoverable after either save mode. Applying a redaction is a separate operation.

    An overlay Form takes precedence over the replacement color and text. Text
    requires a PDF default appearance string whose font the document provides.
    """

    rect: Rect
    _: KW_ONLY
    quads: tuple[Quad, ...] = ()
    overlay_text: str | None = None
    default_appearance: bytes | None = None
    repeat: bool = False
    alignment: int = 0
    interior_color: Color | None = None
    overlay: Stream | None = None

    def __post_init__(self) -> None:
        """Validate region geometry and replacement values."""
        from pyropdf._annotations import _bound_input, _opaque_color, _rect, _text_value  # noqa: PLC0415

        rect = Rect(*_rect(self.rect))
        object.__setattr__(self, "rect", rect)
        if not isinstance(self.quads, (list, tuple)) or len(self.quads) > _MAX_QUADS:
            msg = "redaction quads must be a bounded sequence of at most 4096 quadrilaterals"
            raise ValueError(msg)
        object.__setattr__(self, "quads", tuple(self.quads))
        for quad in self.quads:
            if not isinstance(quad, Quad) or not quad.is_convex or quad.is_empty or quad.rect not in rect:
                msg = "each redaction quad must be a nonempty convex quadrilateral inside the rectangle"
                raise ValueError(msg)
            outline = (quad.ul, quad.ur, quad.lr, quad.ll, quad.ul)
            if not any(
                (b.x - a.x) * (c.y - a.y) != (b.y - a.y) * (c.x - a.x)
                for a, b, c in zip(outline, outline[1:], outline[2:], strict=False)
            ):
                msg = "a redaction quad must have positive area"
                raise ValueError(msg)
        if self.overlay_text is not None:
            _bound_input("Redact", _text_value(self.overlay_text, "overlay text"))
            if not self.default_appearance:
                msg = "overlay text requires default_appearance"
                raise ValueError(msg)
        if self.default_appearance is not None and (
            not isinstance(self.default_appearance, bytes) or len(self.default_appearance) > 1024 * 1024
        ):
            msg = "default_appearance must be a byte string of at most 1 MiB"
            raise ValueError(msg)
        if not isinstance(self.repeat, bool) or type(self.alignment) is not int or self.alignment not in (0, 1, 2):
            msg = "repeat must be boolean and alignment must be 0, 1 or 2"
            raise ValueError(msg)
        if self.interior_color is not None:
            _opaque_color(self.interior_color, "interior_color")


def _space(page: Page) -> tuple[int, tuple[float, float, float, float], float]:
    space = _core.page_space(page.document._core(), page.xref, page._generation)  # noqa: SLF001
    if space is None:
        raise page._stale()  # noqa: SLF001
    return space


def read_mark(annotation: Annotation) -> RedactionMark | None:
    """Read replacement instructions and the precise regions from a stored mark."""
    obj = annotation.obj
    if obj.get("Subtype") != Name("Redact"):
        return None
    coords = obj.get("QuadPoints")
    quads = []
    if coords is not None:
        if not isinstance(coords, Array) or not 0 < len(coords) <= _MAX_QUADS * _QUAD_SIZE or len(coords) % _QUAD_SIZE:
            msg = "redaction /QuadPoints must hold groups of eight coordinates"
            raise MalformedPdfError(msg)
        rotation, crop, unit = _space(annotation.page)
        _core.read_operation(
            annotation.page.document._core(),  # noqa: SLF001
            lambda budget: budget.charge("memory_size", len(coords) * 64),
        )
        for start in range(0, len(coords), _QUAD_SIZE):
            numbers = coords[start : start + _QUAD_SIZE]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in numbers):
                msg = "redaction /QuadPoints requires finite numbers"
                raise MalformedPdfError(msg)
            quads.append(
                Quad(
                    [_to_display(crop, rotation, unit, x, y) for x, y in zip(numbers[::2], numbers[1::2], strict=True)]
                )
            )
    color = obj.get("IC")
    if color is not None and (
        not isinstance(color, Array)
        or len(color) != _RGB_COMPONENTS
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in color)
    ):
        msg = "redaction /IC requires three RGB components"
        raise MalformedPdfError(msg)
    text, style, overlay = obj.get("OverlayText"), obj.get("DA"), obj.get("RO")
    if (text is not None and not isinstance(text, String)) or (style is not None and not isinstance(style, String)):
        msg = "redaction /OverlayText and /DA must be PDF strings"
        raise MalformedPdfError(msg)
    try:
        return RedactionMark(
            annotation.rect,
            quads=tuple(quads),
            overlay_text=text.text if text is not None else None,
            default_appearance=bytes(style) if style is not None else None,
            repeat=obj.get("Repeat", False),
            alignment=obj.get("Q", 0),
            interior_color=Color(*color) if color is not None else None,
            overlay=_form(annotation.page.document, overlay) if overlay is not None else None,
        )
    except (TypeError, ValueError) as exc:
        raise MalformedPdfError(str(exc)) from exc


def write_mark(annotation: Annotation, mark: RedactionMark) -> None:
    """Replace the mark instructions and review appearance atomically."""
    from pyropdf._threads import _display_to_pdf_rect  # noqa: PLC0415

    if not isinstance(mark, RedactionMark):
        msg = "redaction must be a RedactionMark"
        raise TypeError(msg)
    page = annotation.page
    doc = page.document
    with doc.transaction("edit redaction mark"):
        obj = annotation.obj
        if obj.get("Subtype") != Name("Redact"):
            msg = "redaction instructions require a Redact annotation"
            raise ValueError(msg)
        box = _display_to_pdf_rect(doc, page, mark.rect)
        obj["Rect"] = box
        rotation, crop, unit = _space(page)
        quads = [
            tuple(
                tuple(_pdf_real(v, "redaction coordinate") for v in _to_pdf(crop, rotation, unit, p.x, p.y))
                for p in quad
            )
            for quad in (mark.quads or (mark.rect.quad,))
        ]
        values = {
            "QuadPoints": [v for quad in quads for point in quad for v in point] if mark.quads else None,
            "OverlayText": mark.overlay_text,
            "DA": mark.default_appearance,
            "Repeat": mark.repeat or None,
            "Q": mark.alignment or None,
            "IC": mark.interior_color.rgb if mark.interior_color else None,
            "RO": _form(doc, mark.overlay) if mark.overlay is not None else None,
        }
        for key, value in values.items():
            if value is not None:
                obj[key] = value
            elif key in obj:
                del obj[key]
        commands = ["q 1 0 0 RG 0.8 w"]
        for quad in quads:
            tl, tr, bl, br = (f"{Decimal(str(x - box[0])):f} {Decimal(str(y - box[1])):f}" for x, y in quad)
            commands.append(f"{tl} m {tr} l {br} l {bl} l h {tl} m {br} l {bl} m {tr} l S")
        commands.append("Q")
        annotation.set_appearance(
            "normal", "\n".join(commands).encode("ascii"), bbox=(0, 0, box[2] - box[0], box[3] - box[1])
        )


def add_mark(page: Page, mark: RedactionMark) -> Annotation:
    """Allocate a mark and attach it to the selected page in one undo step."""
    from pyropdf._annotations import Annotation, _require  # noqa: PLC0415
    from pyropdf._threads import _display_to_pdf_rect  # noqa: PLC0415

    doc = page.document
    with doc.transaction("add redaction mark"):
        _require(doc, (1, 7))
        xref = doc.objects.add(
            {
                "Type": Name("Annot"),
                "Subtype": Name("Redact"),
                "P": page.obj,
                "Rect": _display_to_pdf_rect(doc, page, mark.rect),
                "F": 4,
            }
        )
        obj = doc.objects[xref]
        annots = page.obj.get("Annots")
        if annots is None:
            page.obj["Annots"] = [obj]
        elif isinstance(annots, Array):
            annots.append(obj)
        else:
            msg = "page /Annots must be an array"
            raise MalformedPdfError(msg)
        annotation = Annotation(page, (xref, 0), 0)
        write_mark(annotation, mark)
        return annotation


RedactionMark.__module__ = "pyropdf"
