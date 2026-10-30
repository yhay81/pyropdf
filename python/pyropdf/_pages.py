"""``doc.pages``: the pages of a document as a sequence that adds, deletes, and moves pages."""

from __future__ import annotations

import io
import math
import os
from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Literal, overload

from pyropdf import _core
from pyropdf._errors import LimitError
from pyropdf._images import _J2K_SIGNATURE, _JP2_SIGNATURE, _PNG_SIGNATURE, add_image
from pyropdf._page import Page
from pyropdf._page_labels import _label, _read_page_labels

if TYPE_CHECKING:
    from PIL.Image import Image as PillowImage

    from pyropdf._core import Pixmap
    from pyropdf._document import Document

#: The largest finite PDF real that readers keep (a 32-bit float).
_FLOAT32_MAX = float.fromhex("0x1.fffffep+127")
_A4 = (595.0, 842.0)
_MIN_PAGE_ENTRY_LIMIT = 4096
#: The resolution of an image that records none, as ``Pixmap.from_bytes`` reads it.
_DEFAULT_DPI = 96.0
_POINTS_PER_INCH = 72.0
_CM_PER_INCH = 2.54
_METRES_PER_INCH = 0.0254
#: The most frames that one call reads from a multi-frame image.
_MAX_FRAMES = 4096
#: The most pixels, over all its frames, that one call reads from a multi-frame image.
_MAX_FRAME_PIXELS = 256_000_000
#: The Exif tags of a resolution and its unit, and the units that the unit tag names.
_EXIF_X_RESOLUTION, _EXIF_Y_RESOLUTION, _EXIF_RESOLUTION_UNIT = 0x011A, 0x011B, 0x0128
_EXIF_UNITS = {2: 1.0, 3: _CM_PER_INCH}


def _is_int(value: object) -> bool:
    """Return whether ``value`` is an integer other than a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _page_entries(pages: Iterable[int], count: int) -> list[int]:
    """Check page entries against ``count`` pages, reading at most one entry past their limit.

    The limit is the larger of ``count`` and 4,096, so that repeating pages
    never costs more than one pass over a large document.
    """
    limit = max(count, _MIN_PAGE_ENTRY_LIMIT)
    entries: list[int] = []
    for index in pages:
        if len(entries) >= limit:
            raise _entry_limit_error(limit)
        _resolve(index, count)
        entries.append(index)
    return entries


def _entry_limit_error(limit: int) -> LimitError:
    """Return the error for more than ``limit`` page entries in one call."""
    msg = f"pages cannot hold more than {limit} entries (the larger of the page count and {_MIN_PAGE_ENTRY_LIMIT})"
    return LimitError("page_entry_count", msg)


def _resolve(index: object, count: int) -> int:
    """Resolve a page index, negative ones from the end, against ``count`` pages."""
    if not _is_int(index):
        msg = f"page indexes must be integers, not {type(index).__name__}"
        raise TypeError(msg)
    assert isinstance(index, int)  # noqa: S101 - narrows the type for ty.
    resolved = index + count if index < 0 else index
    if not 0 <= resolved < count:
        msg = f"page index {index} is out of range for {count} pages"
        raise IndexError(msg)
    return resolved


class Pages(Sequence[Page]):
    """The pages of a document, in order.

    Indexing, slicing, and iteration give :class:`Page` handles, which keep
    naming their pages through later edits. :meth:`new`, :meth:`insert`,
    ``del``, :meth:`select`, and :meth:`move` each change the page tree in
    one transaction.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap the pages of ``document``; use ``doc.pages`` instead."""
        self._document = document

    def __len__(self) -> int:
        """Return the number of pages."""
        return _core.page_count(self._document._core())  # noqa: SLF001

    def labeled(self, label: str) -> list[int]:
        """Return the indexes of the pages whose page label is ``label``.

        An empty label, or a document without page labels, matches no page.
        """
        if not isinstance(label, str):
            msg = f"label must be a string: {label!r}"
            raise TypeError(msg)
        if not label:
            return []
        ranges = _read_page_labels(self._document)
        if not ranges:
            return []
        return [index for index in range(len(self)) if _label(ranges, index) == label]

    @overload
    def __getitem__(self, index: int) -> Page: ...
    @overload
    def __getitem__(self, index: slice) -> list[Page]: ...
    def __getitem__(self, index: int | slice) -> Page | list[Page]:
        """Return page ``index``, or a list of the pages of a slice."""
        doc = self._document._core()  # noqa: SLF001
        if isinstance(index, slice):
            objects = _core.page_objects(doc)
            return [Page(self._document, number, generation) for number, generation in objects[index]]
        number, generation = _core.page_object(doc, _resolve(index, _core.page_count(doc)))
        return Page(self._document, number, generation)

    def __iter__(self) -> Iterator[Page]:
        """Iterate over the pages; a structural edit during the iteration raises ``RuntimeError``."""
        doc = self._document._core()  # noqa: SLF001
        objects = _core.page_objects(doc)
        for at, (number, generation) in enumerate(objects):
            if _core.page_count(doc) != len(objects) or _core.page_object(doc, at) != (number, generation):
                msg = "the pages changed during iteration"
                raise RuntimeError(msg)
            yield Page(self._document, number, generation)

    def __contains__(self, page: object) -> bool:
        """Return whether ``page`` is a current page of this document."""
        if not isinstance(page, Page) or page.document is not self._document:
            return False
        return _core.page_index(self._document._core(), page.xref, page._generation) is not None  # noqa: SLF001

    def index(self, page: object, start: int = 0, stop: int | None = None) -> int:
        """Return the index of ``page``, in time that does not grow with the page count."""
        if page not in self:
            msg = f"{page!r} is not a page of the document"
            raise ValueError(msg)
        assert isinstance(page, Page)  # noqa: S101 - narrows the type for ty.
        found = page.index
        if found < start or (stop is not None and found >= stop):
            msg = f"{page!r} is not in the range"
            raise ValueError(msg)
        return found

    def __delitem__(self, index: int | slice) -> None:
        """Delete page ``index``, or the pages of a slice, in one transaction."""
        doc = self._document._core()  # noqa: SLF001
        objects = _core.page_objects(doc)
        if isinstance(index, slice):
            removed = set(range(len(objects))[index])
            if not removed:
                return
        else:
            removed = {_resolve(index, len(objects))}
        _core.set_pages(doc, [obj for at, obj in enumerate(objects) if at not in removed])

    def new(self, size: tuple[float, float] = _A4, *, index: int | None = None) -> Page:
        """Add a blank page of ``size`` points, A4 by default, where ``list.insert`` would put it.

        Without ``index`` the page goes last. The page has its own MediaBox,
        and no rotation or crop of the page tree's root carries over to it.
        """
        width, height = _validate_size(size)
        doc = self._document._core()  # noqa: SLF001
        count = _core.page_count(doc)
        if index is None:
            at = count
        elif not _is_int(index):
            msg = f"page indexes must be integers, not {type(index).__name__}"
            raise TypeError(msg)
        else:
            at = max(0, index + count) if index < 0 else min(index, count)
        number, generation = _core.new_page(doc, at, width, height)
        return Page(self._document, number, generation)

    def new_from_image(
        self,
        source: str | os.PathLike[str] | bytes | bytearray | memoryview | Pixmap,
        *,
        dpi: float | tuple[float, float] | None = None,
        index: int | None = None,
    ) -> list[Page]:
        """Add one page per frame of an image, each the size of its frame at its resolution, in one transaction.

        ``source`` is a path, encoded bytes, or a :class:`Pixmap`. JPEG and
        JPEG 2000 data is embedded as it is, without recompression, and PNG
        keeps its alpha as a soft mask; each is one frame. Other formats,
        such as multi-page TIFF, GIF, or WebP, are read through Pillow when
        it is installed, one frame per page, up to 4,096 frames and 256
        million pixels in all. A frame's resolution comes from its JFIF
        density or Exif resolution, PNG ``pHYs``, or Pillow's ``dpi``, else
        96 dpi, and ``dpi``, one number or ``(x, y)``,
        overrides it. The pages go where :meth:`new` puts one, in order, and
        are returned.
        """
        frames = _image_frames(source)
        override = _dpi_pair(dpi) if dpi is not None else None
        document = self._document
        if index is not None:
            if not _is_int(index):
                msg = f"page indexes must be integers, not {type(index).__name__}"
                raise TypeError(msg)
            # Resolve a negative index once, so later frames follow earlier ones.
            index = max(0, index + len(self)) if index < 0 else index
        pages: list[Page] = []
        with document.transaction("new_from_image"):
            for offset, (data, found) in enumerate(frames):
                xref = add_image(document, data)
                stream = document.objects[xref]
                x_dpi, y_dpi = override or found or (_DEFAULT_DPI, _DEFAULT_DPI)
                size = (
                    stream["Width"] * _POINTS_PER_INCH / x_dpi,
                    stream["Height"] * _POINTS_PER_INCH / y_dpi,
                )
                page = self.new(size, index=None if index is None else index + offset)
                with page.draw() as canvas:
                    canvas.image((0, 0, *size), xref, keep_aspect=False)
                pages.append(page)
        return pages

    def insert(self, index: int, pages: Page | Iterable[Page]) -> None:
        """Insert copies of ``pages`` before ``index``, clamped as ``list.insert`` clamps it, in one transaction.

        ``pages`` is one :class:`Page` or an iterable of pages of one open
        document, this one or another. Pages of this document may repeat;
        each copy is a new page object that shares the content streams and
        resources of its original, with annotations, widgets, and structure
        elements of its own. Pages of another document are imported with
        what they reach, once each per call; their links to pages imported
        with them keep leading there, and their other links within that
        document go. Their structure elements join this document's
        structure tree, under its single ``Document`` element when it has
        one. The iterable may hold up to the larger of the source's page
        count and 4,096 pages.
        """
        if not _is_int(index):
            msg = f"the insertion index must be an integer, not {type(index).__name__}"
            raise TypeError(msg)
        doc = self._document._core()  # noqa: SLF001
        source, chosen = _source_pages(pages)
        objects = _core.page_objects(doc)
        count = len(objects)
        if source is None:
            return
        at = max(0, index + count) if index < 0 else min(index, count)
        if source is not self._document:
            _core.import_pages(doc, source._core(), chosen, at)  # noqa: SLF001
            return
        order = [*objects[:at], *chosen, *objects[at:]]
        copies = [False] * at + [True] * len(chosen) + [False] * (count - at)
        _core.set_pages(doc, order, copies)

    def extend(self, pages: Page | Iterable[Page]) -> None:
        """Append copies of ``pages``, as :meth:`insert` at the end inserts them."""
        self.insert(len(self), pages)

    def select(self, indexes: Iterable[int], /) -> None:
        """Keep the pages at ``indexes``, in that order, and remove the others, in one transaction.

        Negative indexes count from the end. A repeated index adds a copy, as
        :meth:`insert` copies a page. The navigation that leads to a removed
        page goes, as for ``del``.
        """
        doc = self._document._core()  # noqa: SLF001
        objects = _core.page_objects(doc)
        chosen = [_resolve(index, len(objects)) for index in _page_entries(indexes, len(objects))]
        seen: set[int] = set()
        copies = []
        for at in chosen:
            copies.append(at in seen)
            seen.add(at)
        _core.set_pages(doc, [objects[at] for at in chosen], copies)

    def move(self, index: int, to: int) -> None:
        """Move page ``index`` so that it ends at index ``to``, in one transaction."""
        doc = self._document._core()  # noqa: SLF001
        objects = _core.page_objects(doc)
        source = _resolve(index, len(objects))
        target = _resolve(to, len(objects))
        if source == target:
            return
        moved = objects.pop(source)
        objects.insert(target, moved)
        _core.set_pages(doc, objects)

    def __repr__(self) -> str:
        """Return a representation naming the document."""
        return f"<Pages of {self._document!r}>"


def _source_pages(pages: Page | Iterable[Page]) -> tuple[Document | None, list[tuple[int, int]]]:
    """Return the source document and each page's stable object identity."""
    if isinstance(pages, Page):
        items: Iterable[object] = (pages,)
    elif isinstance(pages, Iterable) and not isinstance(pages, (str, bytes)):
        items = pages
    else:
        msg = f"pages must be a Page or an iterable of pages: {pages!r}"
        raise TypeError(msg)
    source: Document | None = None
    limit = 0
    indexes: list[tuple[int, int]] = []
    for page in items:
        if not isinstance(page, Page):
            msg = f"pages must be Page views: {page!r}"
            raise TypeError(msg)
        if source is None:
            source = page.document
            limit = max(len(source.pages), _MIN_PAGE_ENTRY_LIMIT)
        elif page.document is not source:
            msg = "the pages of one call must come from one document"
            raise ValueError(msg)
        if len(indexes) >= limit:
            raise _entry_limit_error(limit)
        _ = page.index  # Refuse a stale page before accepting its identity.
        indexes.append((page.xref, page._generation))  # noqa: SLF001
    return source, indexes


def _validate_size(size: object) -> tuple[float, float]:
    """Return a page size of two positive finite numbers within PDF's real-number range."""
    if not isinstance(size, (tuple, list)) or len(size) != 2:  # noqa: PLR2004 - a width and a height.
        msg = f"size must be a (width, height) pair: {size!r}"
        raise TypeError(msg)
    values = []
    for value in size:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            msg = f"size must hold numbers: {size!r}"
            raise TypeError(msg)
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            msg = f"size must be positive finite numbers: {size!r}"
            raise ValueError(msg)
        if number > _FLOAT32_MAX:
            msg = f"size must lie within PDF's real-number range: {size!r}"
            raise ValueError(msg)
        values.append(number)
    return values[0], values[1]


__all__ = ["Pages"]


def _dpi_pair(dpi: float | tuple[float, float]) -> tuple[float, float]:
    """Return a positive, finite ``(x, y)`` resolution."""
    values = [dpi, dpi] if isinstance(dpi, (int, float)) else list(dpi)
    numbers = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    ]
    if len(values) != 2 or len(numbers) != 2:  # noqa: PLR2004 - x and y
        msg = f"dpi must be a positive number or a pair of them: {dpi!r}"
        raise ValueError(msg)
    return (numbers[0], numbers[1])


def _image_frames(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview | Pixmap,
) -> Iterable[tuple[bytes, tuple[float, float] | None]]:
    """Return each frame of ``source`` as JPEG, JPEG 2000, or PNG data with the resolution it records."""
    from pyropdf._core import Pixmap  # noqa: PLC0415 - the stub names the extension's class.

    if isinstance(source, Pixmap):
        x_dpi, y_dpi = source.resolution
        return [(source.to_bytes("png", max_size=None), (float(x_dpi), float(y_dpi)))]
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    elif isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as file:  # noqa: PTH123 - os.PathLike and str alike.
            data = file.read()
    else:
        msg = f"source must be a path, bytes, or a Pixmap, not {type(source).__name__}"
        raise TypeError(msg)
    if data.startswith(b"\xff\xd8\xff"):
        return [(data, _jpeg_dpi(data))]
    if data.startswith(_PNG_SIGNATURE):
        return [(data, _png_dpi(data))]
    if data.startswith((_JP2_SIGNATURE, _J2K_SIGNATURE)):
        return [(data, None)]
    return _pillow_frames(data)


def _jpeg_dpi(data: bytes) -> tuple[float, float] | None:
    """Return the resolution of a JPEG's JFIF density, else of its Exif segment, in inches or centimetres."""
    exif = None
    at = 2
    # Each segment before the scan is a marker, a length, and a body.
    while at + 4 <= len(data) and data[at] == 0xFF and data[at + 1] not in (0xDA, 0xD9):  # noqa: PLR2004 - SOS, EOI
        marker, length = data[at + 1], int.from_bytes(data[at + 2 : at + 4])
        body = data[at + 4 : at + 2 + length]
        if marker == 0xE0 and body[:5] == b"JFIF\x00" and len(body) >= 12:  # noqa: PLR2004 - APP0 to its densities
            unit, x, y = body[7], int.from_bytes(body[8:10]), int.from_bytes(body[10:12])
            if x and y and unit in (1, 2):
                scale = 1.0 if unit == 1 else _CM_PER_INCH
                return (x * scale, y * scale)
        elif marker == 0xE1 and body[:6] == b"Exif\x00\x00" and exif is None:  # noqa: PLR2004 - APP1
            exif = _exif_dpi(body[6:])
        at += 2 + length
    return exif


def _exif_dpi(tiff: bytes) -> tuple[float, float] | None:
    """Return the resolution in the first image directory of Exif data, when it records one in inches or centimetres."""
    orders: dict[bytes, Literal["little", "big"]] = {b"II": "little", b"MM": "big"}
    order = orders.get(tiff[:2])
    if order is None:
        return None

    def number(at: int, size: int) -> int:
        field = tiff[at : at + size]
        return int.from_bytes(field, order) if len(field) == size else 0

    directory = number(4, 4)
    found: dict[int, float] = {}
    entries = range(directory + 2, directory + 2 + 12 * number(directory, 2), 12)
    for entry in entries:
        tag, kind = number(entry, 2), number(entry + 2, 2)
        if tag in (_EXIF_X_RESOLUTION, _EXIF_Y_RESOLUTION) and kind == 5:  # noqa: PLR2004 - RATIONAL
            at = number(entry + 8, 4)
            numerator, denominator = number(at, 4), number(at + 4, 4)
            found[tag] = numerator / denominator if denominator else 0.0
        elif tag == _EXIF_RESOLUTION_UNIT and kind == 3:  # noqa: PLR2004 - SHORT
            found[tag] = number(entry + 8, 2)
    x, y = found.get(_EXIF_X_RESOLUTION), found.get(_EXIF_Y_RESOLUTION)
    scale = _EXIF_UNITS.get(int(found.get(_EXIF_RESOLUTION_UNIT, 2)))
    return (x * scale, y * scale) if x and y and scale else None


def _png_dpi(data: bytes) -> tuple[float, float] | None:
    """Return the resolution of a PNG's ``pHYs`` chunk, when it is in metres."""
    at = len(_PNG_SIGNATURE)
    while at + 8 <= len(data):
        length = int.from_bytes(data[at : at + 4])
        kind = data[at + 4 : at + 8]
        body = data[at + 8 : at + 8 + length]
        if kind == b"pHYs" and len(body) == 9 and body[8] == 1:  # noqa: PLR2004 - two sizes and a unit
            x, y = int.from_bytes(body[:4]), int.from_bytes(body[4:8])
            return (_png_resolution(x), _png_resolution(y)) if x and y else None
        if kind in (b"IDAT", b"IEND"):
            return None
        at += 12 + length
    return None


def _png_resolution(pixels_per_metre: int) -> float:
    """Return the resolution in inches of ``pixels_per_metre``, rounded to one decimal unless that leaves none.

    Whole pixels per metre miss most resolutions in inches by a little: 72
    dpi is stored as 2835, 72.009 dpi.
    """
    exact = pixels_per_metre * _METRES_PER_INCH
    return round(exact, 1) or exact


def _pillow_frames(data: bytes) -> Iterator[tuple[bytes, tuple[float, float] | None]]:
    """Return each frame of an image that Pillow reads, as PNG data with its resolution, one at a time."""
    try:
        from PIL import Image, ImageSequence  # noqa: PLC0415 - Pillow is optional.
    except ImportError:
        msg = "the image is not JPEG, JPEG 2000, or PNG; other formats need Pillow: pip install pillow"
        raise ValueError(msg) from None
    try:
        image = Image.open(io.BytesIO(data))
    except Exception as exc:  # Pillow raises its own errors for each format.
        msg = f"the image data is not in a format that Pillow reads: {exc}"
        raise ValueError(msg) from exc
    return _converted_frames(image, ImageSequence.Iterator(image))


def _converted_frames(
    image: PillowImage, sequence: Iterable[PillowImage]
) -> Iterator[tuple[bytes, tuple[float, float] | None]]:
    """Yield each frame of ``sequence`` as PNG data with its resolution, within the frame and pixel limits."""
    pixels = 0
    with image:
        for number, frame in enumerate(sequence):
            if number >= _MAX_FRAMES:
                code = "content_element_count"
                msg = f"the image has more than {_MAX_FRAMES} frames"
                raise LimitError(code, msg)
            width, height = frame.size
            pixels += width * height
            if pixels > _MAX_FRAME_PIXELS:
                code = "content_element_count"
                msg = f"the frames of the image have more than {_MAX_FRAME_PIXELS} pixels in all"
                raise LimitError(code, msg)
            alpha = frame.mode in ("RGBA", "LA", "PA") or "transparency" in frame.info
            gray = frame.mode in ("1", "L", "LA", "I", "I;16", "F")
            mode = ("LA" if alpha else "L") if gray else ("RGBA" if alpha else "RGB")
            out = io.BytesIO()
            frame.convert(mode).save(out, "PNG")
            found = frame.info.get("dpi")
            dpi = (float(found[0]), float(found[1])) if found and found[0] and found[1] else None
            yield out.getvalue(), dpi
