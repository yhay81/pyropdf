"""PDF viewer targets and their data-only interpretation (ISO 32000-2, clause 12)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from pyropdf._attachments import _version
from pyropdf._objects import Array, Dictionary, Name, String, _catalog

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._structure import StructureElement
    from pyropdf._targets import Destination

_FITS = {"Fit": 0, "FitB": 0, "FitH": 1, "FitV": 1, "FitBH": 1, "FitBV": 1, "FitR": 4, "XYZ": 3}


def require_navigation_version(document: Document, catalog: Dictionary, minimum: tuple[int, int]) -> None:
    """Check existing objects before promoting a document to PDF 2.0 for navigation."""
    from pyropdf._metadata import _require_pdf20, _require_version  # noqa: PLC0415

    if minimum == (2, 0):
        _require_pdf20(document, catalog, "PDF 2.0 navigation")
    else:
        _require_version(document, catalog, minimum)


@dataclass(frozen=True, slots=True, kw_only=True)
class UriAction:
    """A URI reference and the optional image-map click flag of a PDF URI action."""

    uri: str
    is_map: bool = False

    def __post_init__(self) -> None:
        from pyropdf._actions import _check_text_budget, _non_empty_str  # noqa: PLC0415

        _check_text_budget((_non_empty_str(self.uri, "uri"),), "URI")
        if type(self.is_map) is not bool:
            msg = "is_map must be a boolean"
            raise TypeError(msg)

    def resolve(
        self,
        document: Document,
        *,
        document_uri: str | None = None,
        position: tuple[float, float] | None = None,
        rectangle: tuple[float, float, float, float] | None = None,
    ) -> str:
        """Resolve the URI without opening it.

        The catalog base takes precedence over the supplied document location.
        For an image map, supply a click in PDF user space and the annotation
        rectangle, or coordinates already relative to its upper-left corner.
        """
        uri = self.uri
        if self.is_map and position is not None:
            x, y = position
            if rectangle is not None:
                left, _bottom, _right, top = rectangle
                x, y = x - left, top - y
            if not math.isfinite(x) or not math.isfinite(y):
                msg = "an image-map click needs finite coordinates"
                raise ValueError(msg)
            uri += f"?{round(x)},{round(y)}"
        base = urljoin(document_uri or "", document.base_uri or "")
        return urljoin(base, uri)


@dataclass(frozen=True, slots=True, kw_only=True)
class WindowsLaunch:
    """Windows-specific fields in a launch action, retained as data (PDF 1.1 through 1.7)."""

    file: bytes
    directory: bytes | None = None
    operation: str | None = None
    parameters: bytes | None = None
    fallback_file: str | None = None
    new_window: bool | None = None

    def __post_init__(self) -> None:
        from pyropdf._actions import _check_text_budget, _non_empty_str  # noqa: PLC0415

        fields = (self.file, self.directory, self.parameters)
        if not self.file or any(v is not None and not isinstance(v, bytes) for v in fields):
            msg = "Windows filenames, directories and parameters are PDF byte strings"
            raise ValueError(msg)
        if self.operation not in (None, "open", "print"):
            msg = "the Windows launch operation must be open or print"
            raise ValueError(msg)
        if self.fallback_file is not None:
            _non_empty_str(self.fallback_file, "fallback_file")
        _check_text_budget((value for value in (self.operation, self.fallback_file) if value), "Windows launch")
        if sum(len(v) for v in fields if v is not None) > 1_048_576:  # noqa: PLR2004 - same one-MiB limit as link and outline texts.
            from pyropdf._errors import LimitError  # noqa: PLC0415

            msg = "Windows launch byte strings exceed the 1-MiB action input limit"
            code = "annotation_input_size"
            raise LimitError(code, msg)
        if self.new_window is not None and type(self.new_window) is not bool:
            msg = "new_window must be a boolean or None"
            raise TypeError(msg)

    @property
    def effective_operation(self) -> str:
        """Return the declared verb or the Windows default, open."""
        return self.operation or "open"


@dataclass(frozen=True, slots=True, kw_only=True)
class StructureDestination:
    """A PDF 2.0 structure destination and its page-based fallback.

    ``element`` is an indirect structure-element object number, a destination
    name object or a byte-string destination key. Fit operands are preserved in
    the PDF coordinate system, including explicit nulls.
    """

    element: int | Name | bytes
    fallback: Destination
    fit: str = "Fit"
    operands: tuple[float | None, ...] = ()

    def __post_init__(self) -> None:
        from pyropdf._targets import Destination  # noqa: PLC0415

        if not ((type(self.element) is int and self.element > 0) or isinstance(self.element, (Name, bytes))):
            msg = "element must be a positive structure object number or destination name"
            raise ValueError(msg)
        if not isinstance(self.fallback, Destination):
            msg = "a structure destination needs a page destination fallback"
            raise TypeError(msg)
        if self.fit not in _FITS or len(self.operands) != _FITS[self.fit]:
            msg = "invalid structure destination fit or operand count"
            raise ValueError(msg)
        if any(
            v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v))
            for v in self.operands
        ):
            msg = "structure destination operands must be finite numbers or None"
            raise ValueError(msg)
        if self.fit == "FitR" and None in self.operands:
            msg = "a FitR structure destination requires four rectangle coordinates"
            raise ValueError(msg)
        object.__setattr__(self, "operands", tuple(self.operands))

    def resolve(self, document: Document) -> StructureElement | None:
        """Return the referenced structure element, or None for an unresolved or cyclic destination."""
        from pyropdf import _core  # noqa: PLC0415
        from pyropdf._objects import _unlocked  # noqa: PLC0415
        from pyropdf._structure import StructureElement, _is_struct_elem_dict  # noqa: PLC0415

        def read(budget: _core._OperationBudget) -> StructureElement | None:
            element: object = self.element
            seen: set[tuple[type, object]] = set()
            while isinstance(element, (bytes, Name)):
                budget.poll()
                budget.charge("named_destination_count", 1)
                budget.check("named_destination_depth", len(seen) + 1)
                identity = (type(element), element)
                if identity in seen:
                    return None
                seen.add(identity)
                dest = lookup_destination(document, element)
                sd = dest.get("SD") if isinstance(dest, Dictionary) else None
                if not isinstance(sd, Array) or not len(sd):
                    return None
                element = sd[0]
            if isinstance(element, int):
                try:
                    element = document.objects[element]
                except KeyError:
                    return None
            if (
                isinstance(element, Dictionary)
                and element.xref is not None
                and _is_struct_elem_dict(element)
                and isinstance(_catalog(document).get("StructTreeRoot"), Dictionary)
            ):
                return StructureElement(document, element.xref, element.generation or 0)
            return None

        return _core.read_operation(_unlocked(document), read)


def lookup_destination(document: Document, name: bytes | Name) -> object:
    """Find an optional PDF 2.0 structure destination outside the normal page-destination index."""
    from pyropdf import _core  # noqa: PLC0415
    from pyropdf._objects import _unlocked  # noqa: PLC0415

    if _version(document) < (2, 0):
        return None
    if isinstance(name, Name):
        legacy = _catalog(document).get("Dests")
        return legacy.get(str(name)) if isinstance(legacy, Dictionary) else None

    key = bytes(name)

    def read(budget: _core._OperationBudget) -> object:
        names = _catalog(document).get("Names")
        node = names.get("Dests") if isinstance(names, Dictionary) else None
        seen: set[int] = set()
        depth = 0
        while isinstance(node, Dictionary):
            depth += 1
            budget.check("named_destination_depth", depth)
            budget.poll()
            if node.xref in seen:
                break
            if node.xref is not None:
                seen.add(node.xref)
            pairs = node.get("Names")
            if isinstance(pairs, Array):
                return _find_pair(pairs, key)
            children = node.get("Kids")
            if not isinstance(children, Array):
                return None
            budget.charge("named_destination_edge_count", len(children))
            node = next((child for child in children if _contains_key(child, key)), None)
        return None

    return _core.read_operation(_unlocked(document), read)


def _find_pair(pairs: Array, name: bytes) -> object:
    lo, hi = 0, len(pairs) // 2
    while lo < hi:
        mid = (lo + hi) // 2
        key = pairs[mid * 2]
        if not isinstance(key, String):
            return None
        if key == name:
            return pairs[mid * 2 + 1]
        if key < name:
            lo = mid + 1
        else:
            hi = mid
    return None


def _contains_key(obj: object, name: bytes) -> bool:
    limits = obj.get("Limits") if isinstance(obj, Dictionary) else None
    if not isinstance(limits, Array) or len(limits) != len(("lower", "upper")):
        return False
    lower, upper = limits
    return isinstance(lower, String) and isinstance(upper, String) and lower <= name <= upper


def _windows(action: Dictionary, win: Dictionary) -> WindowsLaunch | None:
    from pyropdf._presentations import _read_file_spec  # noqa: PLC0415

    file, directory, operation, parameters = (win.get(k) for k in ("F", "D", "O", "P"))
    window = action.get("NewWindow")
    if (
        not isinstance(file, String)
        or not file
        or any(v is not None and not isinstance(v, String) for v in (directory, operation, parameters))
    ):
        return None
    if (operation is not None and operation not in (b"open", b"print")) or (
        window is not None and not isinstance(window, bool)
    ):
        return None
    return WindowsLaunch(
        file=bytes(file),
        directory=directory,
        operation=operation.decode("ascii") if operation is not None else None,
        parameters=parameters,
        fallback_file=_read_file_spec(action.get("F")),
        new_window=window,
    )


def read_extra(document: Document, action: Dictionary) -> UriAction | WindowsLaunch | StructureDestination | None:

    kind = action.get("S")
    if kind == Name("URI") and "IsMap" in action:
        uri, is_map = action.get("URI"), action.get("IsMap")
        if isinstance(uri, String) and uri and isinstance(is_map, bool):
            return UriAction(uri=bytes(uri).decode("utf-8", "replace"), is_map=is_map)
    if kind == Name("Launch") and isinstance(win := action.get("Win"), Dictionary):
        return _windows(action, win)
    if kind in (None, Name("GoTo")):
        return read_structure(document, action)
    return None


def read_structure(document: Document, action: Dictionary) -> StructureDestination | None:
    from pyropdf._presentations import _ActionReadContext  # noqa: PLC0415

    dest = action.get("D")
    holder = action
    if "SD" not in holder and isinstance(dest, (String, Name)):
        target = lookup_destination(document, dest)
        if not isinstance(target, Dictionary):
            return None
        holder, dest = target, target.get("D")
    sd = holder.get("SD")
    if (
        isinstance(sd, Array)
        and len(("element", "fit")) <= len(sd) <= 2 + max(_FITS.values())
        and isinstance(sd[1], Name)
        and isinstance(dest, Array)
    ):
        element = sd[0]
        if isinstance(element, Dictionary):
            element = element.xref
        if not isinstance(element, (int, Name, bytes)) or isinstance(element, bool):
            return None
        fallback = _ActionReadContext(document).explicit_destination(dest)
        if fallback is not None:
            try:
                return StructureDestination(element=element, fallback=fallback, fit=str(sd[1]), operands=tuple(sd[2:]))
            except ValueError:
                return None
    return None


def action_dict(
    document: Document, target: UriAction | WindowsLaunch | StructureDestination
) -> tuple[dict[str, object], tuple[int, int]]:
    from pyropdf._presentations import _goto_action_dict  # noqa: PLC0415

    if isinstance(target, UriAction):
        return {"Type": Name("Action"), "S": Name("URI"), "URI": target.uri.encode(), "IsMap": target.is_map}, (1, 1)
    if isinstance(target, WindowsLaunch):
        if _version(document) >= (2, 0):
            msg = "Windows-specific launch parameters are deprecated in PDF 2.0"
            raise ValueError(msg)
        win = {
            k: v
            for k, v in (("F", target.file), ("D", target.directory), ("O", target.operation), ("P", target.parameters))
            if v is not None
        }
        result: dict[str, object] = {"Type": Name("Action"), "S": Name("Launch"), "Win": win}
        if target.fallback_file is not None:
            result["F"] = target.fallback_file
        if target.new_window is not None:
            result["NewWindow"] = target.new_window
        return result, (1, 2) if target.new_window is not None else (1, 1)
    base = target.fallback
    result = _goto_action_dict(document, base.page, base.point, base.zoom)
    element = target.element
    if isinstance(element, int):
        resolved = target.resolve(document)
        if resolved is None:
            msg = "the structure destination must reference a structure element"
            raise ValueError(msg)
        element = resolved.obj
    result["SD"] = [element, Name(target.fit), *target.operands]
    return result, (2, 0)


for _public in (UriAction, WindowsLaunch, StructureDestination):
    _public.__module__ = "pyropdf"
