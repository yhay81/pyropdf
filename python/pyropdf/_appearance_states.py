"""Annotation and widget appearances selected by the viewer's interaction state."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal, cast

from pyropdf import _core
from pyropdf._errors import MalformedPdfError
from pyropdf._objects import Array, Dictionary, Name, Stream

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pyropdf._document import Document
    from pyropdf._page import Page

Mode = Literal["normal", "rollover", "down"]
_MAX_NAME = 127
_KEYS = {"normal": "N", "rollover": "R", "down": "D"}


def _mode(mode: Mode) -> str:
    if mode not in _KEYS:
        msg = "appearance mode must be normal, rollover or down"
        raise ValueError(msg)
    return _KEYS[mode]


def _box(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (Array, list, tuple)) or len(value) != 4:  # noqa: PLR2004
        msg = "an appearance box must have four coordinates"
        raise ValueError(msg)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        msg = "appearance coordinates must be numbers"
        raise TypeError(msg)
    x0, y0, x1, y1 = map(float, value)
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or x0 >= x1 or y0 >= y1:
        msg = "an appearance box must be finite and have positive area"
        raise ValueError(msg)
    return x0, y0, x1, y1


def _entries(doc: Document, value: object, label: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Dictionary) or isinstance(value, Stream):
        msg = f"{label} must be a dictionary"
        raise MalformedPdfError(msg)
    _core.read_operation(doc._core(), lambda budget: budget.charge("memory_size", 160 * (len(value) + 1)))  # noqa: SLF001
    return dict(value)


def _form(doc: Document, value: Stream) -> Stream:
    if not isinstance(value, Stream) or not value.is_indirect or value._document is not doc:  # noqa: SLF001
        msg = "an appearance must be an indirect Form stream in this document"
        raise ValueError(msg)
    if value.get("Subtype") != Name("Form"):
        msg = "an appearance stream needs /Subtype /Form"
        raise ValueError(msg)
    _box(value.get("BBox"))
    return value


def _key(name: str) -> Name:
    if not isinstance(name, str) or not name or len(name) > _MAX_NAME or len(name.encode("utf-8")) > _MAX_NAME:
        msg = "an appearance state needs a nonempty PDF name of at most 127 bytes"
        raise ValueError(msg)
    return Name(name)


class AppearanceOwner:
    """Shared selection and writing of the /N, /R and /D appearance entries."""

    __slots__ = ()
    _page: Page

    @property
    def obj(self) -> Dictionary:
        """Return the live annotation dictionary."""
        raise NotImplementedError

    def appearance_stream(self, mode: Mode = "normal", *, state: str | None = None) -> Stream | None:
        """Return the displayed Form for a normal, rollover or down appearance.

        A missing interaction appearance falls back to normal. An appearance
        dictionary selects the named state or the annotation's current /AS.
        """
        key = _mode(mode)
        selected = _key(state) if state is not None else None
        obj = self.obj
        ap = obj.get("AP")
        if ap is None:
            return None
        if not isinstance(ap, Dictionary) or isinstance(ap, Stream):
            msg = "/AP must be a dictionary"
            raise MalformedPdfError(msg)
        chosen = ap.get(key)
        if chosen is None and key != "N":
            chosen = ap.get("N")
        if isinstance(chosen, Dictionary) and not isinstance(chosen, Stream):
            current = selected if selected is not None else obj.get("AS")
            chosen = chosen.get(current) if isinstance(current, Name) else None
        if chosen is None:
            return None
        try:
            return _form(self._page.document, chosen)
        except (TypeError, ValueError) as exc:
            raise MalformedPdfError(str(exc)) from exc

    def set_appearance(
        self,
        mode: Mode,
        source: Stream | bytes,
        *,
        state: str | None = None,
        bbox: Sequence[float] | None = None,
        resources: Mapping[str, object] | None = None,
    ) -> Stream:
        """Attach a Form or build it from PDF drawing commands in the given local bounding box.

        Byte sources use the owner's rectangle size when bbox is omitted. A named
        state updates one member of a state dictionary. Other interaction modes,
        state names and shared appearance objects retain their values.
        """
        key = _mode(mode)
        selected = _key(state) if state is not None else None
        doc = self._page._document  # noqa: SLF001
        with doc.transaction("annotation appearance"):
            obj = self.obj
            entries = _entries(doc, obj.get("AP"), "/AP")
            previous = entries.get(key)
            if selected is not None and isinstance(previous, Stream):
                msg = "a named appearance cannot replace an existing single-state Form"
                raise ValueError(msg)
            if isinstance(source, Stream):
                if bbox is not None or resources is not None:
                    msg = "bbox and resources are properties of an existing Form"
                    raise ValueError(msg)
                form = _form(doc, source)
            elif isinstance(source, bytes):
                if bbox is None:
                    x0, y0, x1, y1 = _box(obj.get("Rect"))
                    box = (0.0, 0.0, x1 - x0, y1 - y0)
                else:
                    box = _box(bbox)
                fields = {
                    "Type": Name("XObject"),
                    "Subtype": Name("Form"),
                    "FormType": 1,
                    "BBox": box,
                    "Resources": {} if resources is None else resources,
                }
                form = cast("Stream", doc.objects[doc.objects.add_stream(source, fields)])
            else:
                msg = "appearance source must be a Form stream or bytes"
                raise TypeError(msg)
            if selected is None:
                entries[key] = form
            else:
                entries[key] = {**_entries(doc, previous, "appearance states"), str(selected): form}
            obj["AP"] = entries
            return form
