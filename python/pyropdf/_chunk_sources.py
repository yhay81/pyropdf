"""Constituent display locations and the immutable reader of a retrieval export."""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from pyropdf import _core
from pyropdf._operations import InlineImage, Operation, Operations, _from_core

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyropdf._geometry import Rect
    from pyropdf._page import Page
    from pyropdf._text import Line


class RetrievalVersion:
    """The immutable store version used by an export, including the caller's unsaved edits.

    Obtain it from :attr:`RetrievalSource.version`. Keeping the version also keeps its
    original input available. At most the last page's parsed operations are cached.
    """

    __slots__ = ("_charge", "_latest", "_snapshot")

    def __init__(self, snapshot: _core._PageSnapshot, charge: Callable[[str], None]) -> None:
        self._snapshot = snapshot
        self._charge = charge
        self._latest: tuple[int, Operations, dict[int, str]] | None = None

    def _reserve(self, text: str) -> None:
        self._charge(text)
        self._snapshot.charge_evidence(1)

    def same_as(self, other: RetrievalVersion) -> bool:
        """Return whether two exports captured the same version of the same store."""
        return self._snapshot.same_version(other._snapshot)

    def page_operations(self, page: int) -> Operations:
        """Read a page's stored operation sequence from this version, even after its source changes."""
        return self._page_data(page)[1]

    def _page_data(self, page: int) -> tuple[int, Operations, dict[int, str]]:
        cached = self._latest
        if cached is not None and cached[0] == page:
            return cached
        number, generation = self._snapshot.page_object(page)
        raw = _core.page_operations(self._snapshot, number, generation)
        if raw is None:  # The object and index come from the same immutable view.
            msg = "the captured version does not contain this page"
            raise KeyError(msg)
        result = (page, _from_core(raw), {})
        self._latest = result
        return result

    def _digest(self, page: int, operation: int) -> str:
        _, values, digests = self._page_data(page)
        if operation not in digests:
            self._reserve("")
            raw = values[operation]._source  # noqa: SLF001 - captured operations keep the exact input bytes.
            if raw is None:
                msg = "the captured content operation has no stored bytes"
                raise ValueError(msg)
            digests[operation] = hashlib.sha256(raw).hexdigest()
        return digests[operation]


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievalSource:
    """A contributor to an element. Display boxes describe the original glyph or image placement.

    A ``pdf`` glyph names its content stream and zero-based index in the captured page's operations.
    An ``unresolved`` glyph or image has a location but lacks an established content operator.
    An ``inferred`` contributor comes from layout or formatting, including synthetic spaces.
    The optional half-open ``text_range`` counts Python characters of the containing element.
    Table constituents preserve their original text and boxes independently of Markdown offsets.
    """

    kind: Literal["pdf", "unresolved", "inferred"]
    page: int
    page_xref: int
    text: str
    bbox: Rect | None
    text_range: tuple[int, int] | None
    stream: int | None = None
    operation: int | None = None
    operation_sha256: str | None = None
    """SHA-256 of the captured operation's original bytes for checking this location after serialization."""
    image_xref: int | None = None
    version: RetrievalVersion = field(repr=False, compare=False)

    def resolve(self) -> Operation | InlineImage | None:
        """Return the exact contributing operation, or None when no operator was established."""
        if self.operation is None:
            return None
        return self.version.page_operations(self.page)[self.operation]

    def to_dict(self) -> dict[str, Any]:
        """Return the portable locations. The in-process version reader stays on the Python result."""
        record: dict[str, Any] = {
            "kind": self.kind,
            "page": self.page,
            "page_xref": self.page_xref,
            "text": self.text,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "text_range": list(self.text_range) if self.text_range is not None else None,
            "stream": self.stream,
            "operation": self.operation,
            "operation_sha256": self.operation_sha256,
        }
        if self.image_xref is not None:
            record["image_xref"] = self.image_xref
        return record


def relocated(source: RetrievalSource, text: str, location: tuple[int, int] | None) -> RetrievalSource:
    """Keep the original glyph or image placement when its output text shifts or splits."""
    if source.text_range == location and source.text == text:
        return source
    source.version._reserve(text)  # noqa: SLF001
    return replace(source, text=text, text_range=location)


def clipped(sources: tuple[RetrievalSource, ...], start: int, end: int) -> tuple[RetrievalSource, ...]:
    """Keep only intersecting glyphs, without rescanning the full paragraph for every chunk."""
    result = []
    first = bisect_right(sources, start, key=lambda src: src.text_range[1] if src.text_range is not None else 0)
    for position in range(first, len(sources)):
        src = sources[position]
        if src.text_range is None:
            continue
        left, right = src.text_range
        if left >= end:
            break
        a, b = max(left, start), min(right, end)
        if a < b:
            result.append(relocated(src, src.text[a - left : b - left], (a - start, b - start)))
    return tuple(result)


def shifted(sources: tuple[RetrievalSource, ...], offset: int) -> tuple[RetrievalSource, ...]:
    """Move a line's ranges to the positions at which it contributes to its paragraph."""
    if offset == 0:
        return sources
    return tuple(
        relocated(src, src.text, (src.text_range[0] + offset, src.text_range[1] + offset))
        if src.text_range is not None
        else src
        for src in sources
    )


class PageEvidence:
    """Map one captured page's typed glyph rows into its retrieval layout."""

    def __init__(self, page: Page, version: RetrievalVersion) -> None:
        self.version = version
        self.page = page.index
        self.xref = page.xref
        self.lines = iter(line for block in page.layout(chars=True, unmapped="skip").blocks for line in block.lines)

    def source(  # noqa: PLR0913 - explicit coordinates for a source record.
        self,
        kind: Literal["pdf", "unresolved", "inferred"],
        text: str,
        bbox: Rect | None,
        location: tuple[int, int] | None,
        *,
        stream: int | None = None,
        operation: int | None = None,
        image_xref: int | None = None,
    ) -> RetrievalSource:
        self.version._reserve(text)  # noqa: SLF001
        return RetrievalSource(
            kind=kind,
            page=self.page,
            page_xref=self.xref,
            text=text,
            bbox=bbox,
            text_range=location,
            stream=stream,
            operation=operation,
            operation_sha256=self.version._digest(self.page, operation) if operation is not None else None,  # noqa: SLF001
            image_xref=image_xref,
            version=self.version,
        )

    def line_sources(self, text: str, bbox: Rect) -> tuple[RetrievalSource, ...]:
        """Align the original glyphs to the joined line without matching text in a different placement."""
        line = next(self.lines, None)
        if not text:
            return ()
        if line is None or line.bbox != bbox:
            return (self.source("unresolved", text, bbox, (0, len(text))),)
        return self._glyphs(line, text, bbox)

    def _glyphs(self, line: Line, text: str, bbox: Rect) -> tuple[RetrievalSource, ...]:
        out = []
        at = 0
        for span in line.spans:
            for char in span.chars:
                word = char.text
                if not word or (word.isspace() and (at == len(text) or (at == 0 and not text.startswith(word)))):
                    continue
                if not text.startswith(word, at) and text[at : at + 1] == " ":
                    out.append(self.source("inferred", " ", None, (at, at + 1)))
                    at += 1
                if not text.startswith(word, at):
                    if at < len(text):
                        out.append(self.source("unresolved", text[at:], bbox, (at, len(text))))
                    return tuple(out)
                end = at + len(word)
                kind = "inferred" if char.is_synthetic else ("pdf" if char.operation is not None else "unresolved")
                out.append(self.source(kind, word, char.bbox, (at, end), stream=char.stream, operation=char.operation))
                at = end
        if at != len(text):
            out.append(self.source("unresolved", text[at:], bbox, (at, len(text))))
        return tuple(out)


for _public in (RetrievalSource, RetrievalVersion):
    _public.__module__ = "pyropdf"
