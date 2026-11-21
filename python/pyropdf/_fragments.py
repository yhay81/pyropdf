"""PDF URI fragments (ISO 32000-2 Annex O and RFC 8118 section 3)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote_from_bytes, unquote_to_bytes

from pyropdf import _core
from pyropdf._name_resources import _scan
from pyropdf._objects import Array, Dictionary, String, _unlocked

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyropdf._annotations import Annotation
    from pyropdf._attachments import Attachment
    from pyropdf._document import Document
    from pyropdf._page import Page
    from pyropdf._targets import Destination

_BOUNDARY = re.compile("[&#]")
_BAD_ESCAPE = re.compile("%(?![0-9a-fA-F]{2})")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9._~-]*")
_NUMERIC = re.compile(rb"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_MAX_BYTES = 65536
_MAX_PARAMETERS = 1024
_MAX_PAGE_DIGITS = 10
_TARGETS = {"page", "nameddest", "structelem", "comment", "ef"}
_VIEWS = {
    "Fit": {0},
    "FitB": {0},
    "FitH": {0, 1},
    "FitBH": {0, 1},
    "FitV": {0, 1},
    "FitBV": {0, 1},
    "FitR": {4},
    "XYZ": {0, 3},
}
_OPTIONS = {"zoom", "view", "highlight", "viewrect", "search", "fdf"}


@dataclass(frozen=True, slots=True, kw_only=True)
class FragmentParameter:
    """One parameter of a PDF fragment, resolved in its original order."""

    name: str
    """The parameter name in its original spelling."""
    value: int | str | bytes | tuple[str | float, ...]
    """The interpreted page number, target name, view operands, search terms or FDF URI. Unknown values remain bytes."""
    outcome: Literal["resolved", "unresolved", "option", "unknown"]
    """Whether a target was found, is absent, selects a viewer option, or is an unrecognized parameter."""
    target: Page | Destination | Dictionary | Annotation | Attachment | None = None
    """The page, destination, structure dictionary, annotation or attachment for a resolved object identifier."""
    remainder: str | None = None
    """The unchanged fragment for the embedded file after an ``ef`` selection, interpreted by that file's reader."""


def _parts(text: str) -> Iterator[tuple[str, int, int]]:
    """Yield each name and its encoded value span, stopping at the embedded-file boundary."""
    pos = count = 0
    while pos < len(text):
        count += 1
        if count > _MAX_PARAMETERS:
            msg = f"a PDF fragment has more than {_MAX_PARAMETERS} parameters"
            raise ValueError(msg)
        delimiter = _BOUNDARY.search(text, pos)
        end = delimiter.start() if delimiter else len(text)
        equal = text.find("=", pos, end)
        if equal < 0 or not _NAME.fullmatch(text, pos, equal):
            msg = "a PDF fragment parameter needs a name and '='"
            raise ValueError(msg)
        name = text[pos:equal]
        if _BAD_ESCAPE.search(text, equal + 1, end):
            msg = f"invalid percent escape in PDF fragment parameter {name!r}"
            raise ValueError(msg)
        yield name, equal + 1, end
        if name.lower() == "ef":
            return
        if delimiter and end + 1 == len(text):
            msg = "an empty PDF fragment parameter follows the separator"
            raise ValueError(msg)
        pos = end + 1


def _coordinates(parts: list[bytes]) -> tuple[float, ...]:
    if any(not _NUMERIC.fullmatch(item) for item in parts):
        msg = "a PDF fragment coordinate must be a finite decimal number"
        raise ValueError(msg)
    values = tuple(map(float, parts))
    if not all(map(math.isfinite, values)):
        msg = "a PDF fragment coordinate must be finite"
        raise ValueError(msg)
    return values


def _view(name: str, encoded: str) -> tuple[str | float, ...]:
    parts = [unquote_to_bytes(part) for part in encoded.split(",", 4)]
    if name == "view":
        mode = parts[0].decode("ascii")
        if len(parts) - 1 not in _VIEWS.get(mode, set()):
            msg = f"invalid PDF fragment view or operand count: {mode!r}"
            raise ValueError(msg)
        return (mode, *_coordinates(parts[1:]))
    counts = {1, 3} if name == "zoom" else {4}
    if len(parts) not in counts:
        msg = f"wrong number of coordinates for PDF fragment parameter {name}"
        raise ValueError(msg)
    values = _coordinates(parts)
    if (name == "zoom" and values[0] <= 0) or (name == "viewrect" and min(values[2:]) < 0):
        msg = f"invalid scale or rectangle dimensions for PDF fragment parameter {name}"
        raise ValueError(msg)
    return values


def _value(name: str, encoded: str) -> int | str | bytes | tuple[str | float, ...]:
    raw = unquote_to_bytes(encoded)
    if name == "page":
        if not raw.isdigit() or len(raw) > _MAX_PAGE_DIGITS or int(raw) < 1:
            msg = "a PDF fragment page is a positive one-based integer"
            raise ValueError(msg)
        return int(raw)
    if name == "structelem" or name not in _TARGETS | _OPTIONS:
        return raw
    if name in {"zoom", "view", "highlight", "viewrect"}:
        return _view(name, encoded)
    text = raw.decode("utf-8")
    if name == "search":
        if not re.fullmatch('"[^"\n]*"', text):
            msg = "a PDF fragment search word list must be enclosed in double quotes"
            raise ValueError(msg)
        return tuple(word for word in text[1:-1].split(" ") if word)
    return text


def _indexed_structure_id(root: Dictionary, identifier: bytes, budget: _core._OperationBudget) -> Dictionary | None:
    tree = root.get("IDTree")
    if isinstance(tree, Dictionary):
        pairs, _ = _scan(tree, budget)
        if identifier in pairs:
            leaf, at = pairs[identifier]
            found = leaf.obj["Names"][at + 1]
            if isinstance(found, Dictionary) and found.get("ID") == identifier and "S" in found:
                return found
    return None


def _structure_id(document: Document, identifier: bytes, budget: _core._OperationBudget) -> Dictionary | None:
    root = document.catalog.get("StructTreeRoot")
    if not isinstance(root, Dictionary):
        return None
    indexed = _indexed_structure_id(root, identifier, budget)
    if indexed is not None:
        return indexed
    # Some inputs lack an index or point to an element with a different
    # ID. Walk the actual kids, visiting shared or cyclic objects once.
    pending: list[Iterator[object]] = [iter((root.get("K"),))]
    seen = set()
    end = object()
    while pending:
        node = next(pending[-1], end)
        if node is end:
            pending.pop()
            continue
        budget.poll()
        budget.check("structure_tree_count", len(pending))
        if not isinstance(node, (Dictionary, Array)):
            continue
        if node.xref is not None:
            ref = (node.xref, node.generation)
            if ref in seen:
                continue
            budget.charge("memory_size", 128)
            seen.add(ref)
        if (
            isinstance(node, Dictionary)
            and isinstance(node.get("ID"), String)
            and node["ID"] == identifier
            and "S" in node
        ):
            return node
        budget.charge("memory_size", 128)
        pending.append(iter(node) if isinstance(node, Array) else iter((node.get("K"),)))
    return None


def _comment(page: Page, name: object, budget: _core._OperationBudget) -> Annotation | None:
    for annotation in page.annotations:
        budget.poll()
        if annotation.name == name:
            return annotation
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class PdfFragment:
    """Ordered content selectors and viewer parameters from a PDF URI's fragment."""

    text: str
    """The original fragment, including its separators and percent escapes, without the leading hash."""

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            msg = "PDF fragment text must be a string"
            raise TypeError(msg)
        if len(self.text) > _MAX_BYTES or len(self.text.encode("utf-8")) > _MAX_BYTES:
            msg = f"a PDF URI fragment exceeds {_MAX_BYTES} UTF-8 bytes"
            raise ValueError(msg)
        for name, start, end in _parts(self.text):
            _value(name.lower(), self.text[start:end])

    @classmethod
    def parse(cls, text: str, /) -> PdfFragment:
        """Read the fragment portion of a URI, optionally including its leading hash."""
        if not isinstance(text, str):
            msg = "PDF fragment text must be a string"
            raise TypeError(msg)
        return cls(text=text.removeprefix("#"))

    def with_parameter(self, name: str, value: str | bytes, /) -> PdfFragment:
        """Replace the last occurrence of a parameter, or insert it before an embedded file's fragment.

        Supply the decoded value. For example, pass ``"125,0,50"`` for a zoom or raw
        bytes for a structure ID. Other parameters keep their original spelling and escapes.
        """
        if not isinstance(name, str) or len(name) > _MAX_BYTES or not _NAME.fullmatch(name):
            msg = "a PDF fragment parameter needs a valid name"
            raise ValueError(msg)
        if not isinstance(value, (str, bytes)):
            msg = "a PDF fragment value must be a string or bytes"
            raise TypeError(msg)
        if len(value) > _MAX_BYTES:
            msg = "a PDF fragment parameter is too long"
            raise ValueError(msg)
        raw = value.encode("utf-8") if isinstance(value, str) else value
        key = name.lower()
        encoded = quote_from_bytes(raw, safe="," if key in {"zoom", "view", "viewrect", "highlight"} else "")
        match = None
        insert = len(self.text)
        for existing, start, end in _parts(self.text):
            if existing.lower() == key:
                match = (start, end)
            if existing.lower() == "ef":
                insert = start - len(existing) - 1
        if match is not None:
            start, end = match
            return PdfFragment(text=self.text[:start] + encoded + self.text[end:])
        item = f"{name}={encoded}"
        if insert < len(self.text):
            return PdfFragment(text=self.text[:insert] + item + "&" + self.text[insert:])
        return PdfFragment(text=self.text + ("&" if self.text else "") + item)

    def resolve(self, document: Document, /) -> tuple[FragmentParameter, ...]:
        """Resolve selectors left to right and return viewer requests as data.

        A comment uses the page selected earlier in the fragment. An embedded
        file receives the remaining fragment verbatim. FDF URIs are returned
        for the caller to handle.
        """

        def read(budget: _core._OperationBudget) -> tuple[FragmentParameter, ...]:
            page = None
            result = []
            budget.charge("memory_size", len(self.text) * 8)
            for name, start, end in _parts(self.text):
                budget.charge("memory_size", 256)
                key = name.lower()
                value = _value(key, self.text[start:end])
                target = remainder = None
                if key == "page" and isinstance(value, int):
                    page = document.pages[value - 1] if value <= len(document.pages) else None
                    target = page
                elif key == "nameddest" and isinstance(value, str):
                    target = document.destinations.get(value)
                    page = document.pages[target.page] if target is not None else None
                elif key == "structelem" and isinstance(value, bytes):
                    target = _structure_id(document, value, budget)
                elif key == "comment" and page is not None:
                    target = _comment(page, value, budget)
                elif key == "ef" and isinstance(value, str):
                    target = document.attachments.get(value)
                    remainder = self.text[end + 1 :] if end < len(self.text) else None
                outcome: Literal["resolved", "unresolved", "option", "unknown"]
                if key in _TARGETS:
                    outcome = "resolved" if target is not None else "unresolved"
                else:
                    outcome = "option" if key in _OPTIONS else "unknown"
                result.append(
                    FragmentParameter(name=name, value=value, outcome=outcome, target=target, remainder=remainder)
                )
            return tuple(result)

        return _core.read_operation(_unlocked(document), read)


for _public in (PdfFragment, FragmentParameter):
    _public.__module__ = "pyropdf"
