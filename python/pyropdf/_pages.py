"""``doc.pages``: the pages of a document as a sequence that adds, deletes, and moves pages."""

from __future__ import annotations

import io
import math
import os
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, overload

from pyropdf import _core
from pyropdf._collections import _has_named_pages, _prune_named_pages_for_removed_pages
from pyropdf._errors import LimitError
from pyropdf._geometry import Matrix
from pyropdf._images import (
    _J2K_SIGNATURE,
    _JP2_SIGNATURE,
    _PNG_SIGNATURE,
    _effective_limit,
    _encoded_image_size,
    _jp2_exif_bytes,
    _open_cbz_archive,
    _read_bounded_stream,
    _read_cbz_member,
    _select_cbz_entries,
    add_image,
)
from pyropdf._interchange import _has_dparts, _prune_dparts_for_removed_pages
from pyropdf._objects import _unlocked
from pyropdf._page import Page
from pyropdf._page_labels import _label, _read_page_labels
from pyropdf._threads import (
    _has_catalog_threads,
    _prune_threads_for_removed_pages,
    _strip_copied_page_beads,
)
from pyropdf._web_capture import _guard_capture_pages, _has_capture_indexes

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO, TypeAlias

    from PIL.Image import Image as PillowImage

    from pyropdf._canvas._canvas import Canvas
    from pyropdf._core import Pixmap
    from pyropdf._document import Document

    _ImageSource: TypeAlias = str | os.PathLike[str] | bytes | bytearray | memoryview | Pixmap
    _CbzSource: TypeAlias = str | os.PathLike[str] | bytes | bytearray | memoryview | BinaryIO

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
#: The most pixels in any single decoded Pillow frame (matching Rust decode_png::MAX_PIXELS).
_MAX_DECODED_PIXELS = 64_000_000
#: The most pixels, over all its frames, that one call reads from a multi-frame image.
_MAX_FRAME_PIXELS = 256_000_000
#: The most bytes of encoded image data that one call reads.
_MAX_IMAGE_INPUT = 268_435_456
#: The Exif tags of orientation, resolution, and its unit, and the units that the unit tag names.
_EXIF_ORIENTATION = 0x0112
_EXIF_X_RESOLUTION, _EXIF_Y_RESOLUTION, _EXIF_RESOLUTION_UNIT = 0x011A, 0x011B, 0x0128
_EXIF_UNITS = {2: 1.0, 3: _CM_PER_INCH}
_EXIF_MAX_ORIENTATION = 8
_TRANSPOSED_ORIENTATIONS = frozenset({5, 6, 7, 8})


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
        """Delete page ``index``, or the pages of a slice, in one transaction.

        Links, outline destinations, named destinations, named pages, and
        article thread beads that refer to a removed page are pruned in the
        same transaction.
        """
        doc = self._document._core()  # noqa: SLF001
        objects = _core.page_objects(doc)
        if isinstance(index, slice):
            removed = set(range(len(objects))[index])
            if not removed:
                return
        else:
            removed = {_resolve(index, len(objects))}
        kept = [obj for at, obj in enumerate(objects) if at not in removed]
        has_threads = _has_catalog_threads(self._document)
        has_named = _has_named_pages(self._document)
        removed_pairs = {objects[at] for at in removed}
        has_capture = _has_capture_indexes(self._document)
        has_dparts = _has_dparts(self._document)
        if has_threads or has_named or has_capture or has_dparts:
            with self._document.transaction("pages"):
                if has_capture:
                    _guard_capture_pages(self._document, removed_pairs)
                if has_threads:
                    _prune_threads_for_removed_pages(self._document, removed_pairs)
                if has_named:
                    _prune_named_pages_for_removed_pages(self._document, removed_pairs)
                if has_dparts:
                    _prune_dparts_for_removed_pages(self._document, objects, removed_pairs)
                _core.set_pages(doc, kept)
            return
        _core.set_pages(doc, kept)

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
        source: _ImageSource,
        *,
        dpi: float | tuple[float, float] | None = None,
        size: tuple[float, float] | None = None,
        index: int | None = None,
    ) -> list[Page]:
        """Add one page per frame of an image, sized by its resolution or fitted into ``size``, in one transaction.

        ``source`` is a path, encoded bytes, or a :class:`Pixmap`. JPEG and
        JPEG 2000 data is embedded as it is, without recompression, and PNG
        keeps its alpha as a soft mask; each is one frame. Exif orientation
        (1–8) is honored without recompressing JPEG streams. Other formats,
        such as multi-page TIFF, GIF, or WebP, are read through Pillow when
        it is installed, one frame per page, up to 4,096 frames and 256
        million pixels in all, from up to 256 MiB of data. A frame's
        resolution comes from its JFIF density or Exif resolution, PNG
        ``pHYs``, or Pillow's ``dpi``, else 96 dpi. Pass ``dpi``, one number
        or ``(x, y)``, to override it, or ``size``, a ``(width, height)``
        page size in points, to fit each frame centered inside that size at
        its aspect ratio (mutually exclusive with ``dpi``). The pages go where
        :meth:`new` puts one, in order, and are returned.
        """
        _unlocked(self._document)
        override, page_size, start = self._image_page_options(dpi=dpi, size=size, index=index)
        return self._add_image_sources((source,), "new_from_image", override=override, page_size=page_size, start=start)

    def new_from_images(
        self,
        sources: Iterable[_ImageSource],
        *,
        dpi: float | tuple[float, float] | None = None,
        size: tuple[float, float] | None = None,
        index: int | None = None,
    ) -> list[Page]:
        """Add pages from an ordered collection of images in one undoable transaction.

        Each item of ``sources`` is a path, encoded bytes, or a
        :class:`Pixmap`, converted with the same lossless JPEG/JPEG 2000
        passthrough, PNG alpha soft-mask handling, and Exif orientation as
        :meth:`new_from_image`. All frames across ``sources`` share the 4,096
        frame limit, the 256 million pixel cap, and the document's
        :class:`DocumentLimits`. An empty iterable returns ``[]`` without
        modifying the document.
        """
        from pyropdf._core import Pixmap  # noqa: PLC0415 - the stub names the extension's class.

        _unlocked(self._document)
        if isinstance(sources, (str, bytes, bytearray, memoryview, os.PathLike, Pixmap)) or not isinstance(
            sources, Iterable
        ):
            msg = f"sources must be an iterable of image sources, not {type(sources).__name__}"
            raise TypeError(msg)
        override, page_size, start = self._image_page_options(dpi=dpi, size=size, index=index)
        return self._add_image_sources(sources, "new_from_images", override=override, page_size=page_size, start=start)

    def new_from_cbz(
        self,
        source: _CbzSource,
        *,
        dpi: float | tuple[float, float] | None = None,
        size: tuple[float, float] | None = None,
        index: int | None = None,
    ) -> list[Page]:
        """Add pages from the image entries of a CBZ (ZIP) archive in one undoable transaction.

        ``source`` is a path, bytes, or a binary file-like object. Image
        entries are sorted in deterministic natural filename order, skipping
        ``__MACOSX`` and non-image metadata files. Path-traversal names,
        symbolic links, encrypted entries, and corrupt archive members are
        refused atomically with :class:`ValueError`. Each member is read in
        bounded chunks charged against the document's :class:`DocumentLimits`
        (file size, per-entry and total decompressed size, decompression
        ratio, memory size, page count, duration, and cancellation) as well
        as the 4,096 frame and 256 million pixel caps. JPEG and JPEG 2000
        members are embedded without lossy recompression, PNG keeps its alpha
        soft mask, and Exif orientation is honored.
        """
        _unlocked(self._document)
        override, page_size, start = self._image_page_options(dpi=dpi, size=size, index=index)
        document = self._document
        core = document._core()  # noqa: SLF001
        operation = _core.begin_operation(core, "new_from_cbz")
        pages: list[Page] = []
        try:
            with _open_cbz_archive(source, operation, max_input=_MAX_IMAGE_INPUT) as (archive, archive_size):
                entries = _select_cbz_entries(
                    archive,
                    operation,
                    archive_size=archive_size,
                    max_frames=_MAX_FRAMES,
                    max_input=_MAX_IMAGE_INPUT,
                )
                operation.check("page_count", len(self) + len(entries))
                state = _ImageBatch(operation, existing_pages=len(self), override=override, page_size=page_size)
                total_decompressed = 0
                for info, max_span in entries:
                    operation.poll()
                    raw = _read_cbz_member(
                        archive,
                        info,
                        operation,
                        archive_size=archive_size,
                        max_input=_MAX_IMAGE_INPUT,
                        total_so_far=total_decompressed,
                        max_span=max_span,
                    )
                    total_decompressed += len(raw)
                    try:
                        self._append_encoded_frames(raw, state, pages, start=start)
                    finally:
                        operation.release("memory_size", len(raw))
        except BaseException:
            _core.abort_canvas(core, operation)
            raise
        _core.end_transaction(core, True)  # noqa: FBT003 - commit all pages as one transaction.
        return pages

    def _image_page_options(
        self,
        *,
        dpi: float | tuple[float, float] | None,
        size: tuple[float, float] | None,
        index: int | None,
    ) -> tuple[tuple[float, float] | None, tuple[float, float] | None, int | None]:
        """Validate ``dpi``, ``size``, and ``index`` for ``new_from_image*``."""
        if dpi is not None and size is not None:
            msg = "pass either dpi or size, not both"
            raise ValueError(msg)
        override = _dpi_pair(dpi) if dpi is not None else None
        page_size = _validate_size(size) if size is not None else None
        if index is not None:
            if not _is_int(index):
                msg = f"page indexes must be integers, not {type(index).__name__}"
                raise TypeError(msg)
            # Resolve a negative index once, so later frames follow earlier ones.
            index = max(0, index + len(self)) if index < 0 else index
        return override, page_size, index

    def _add_image_sources(
        self,
        sources: Iterable[_ImageSource],
        name: str,
        *,
        override: tuple[float, float] | None,
        page_size: tuple[float, float] | None,
        start: int | None,
    ) -> list[Page]:
        """Convert an iterable of image sources into pages in one budgeted transaction."""
        document = self._document
        core = document._core()  # noqa: SLF001
        iterator = iter(sources)
        try:
            first = next(iterator)
        except StopIteration:
            return []
        operation = _core.begin_operation(core, name)
        pages: list[Page] = []
        try:
            state = _ImageBatch(operation, existing_pages=len(self), override=override, page_size=page_size)
            self._append_image_source(first, state, pages, start=start)
            for item in iterator:
                operation.poll()
                self._append_image_source(item, state, pages, start=start)
        except BaseException:
            _core.abort_canvas(core, operation)
            raise
        _core.end_transaction(core, True)  # noqa: FBT003 - commit all pages as one transaction.
        return pages

    def _append_image_source(
        self,
        source: _ImageSource,
        state: _ImageBatch,
        pages: list[Page],
        *,
        start: int | None,
    ) -> None:
        """Read one image source under ``state.budget`` and append one page per frame."""
        from pyropdf._core import Pixmap  # noqa: PLC0415 - the stub names the extension's class.

        budget = state.budget
        budget.poll()
        if isinstance(source, Pixmap):
            state.charge_frame(source.width, source.height)
            x_dpi, y_dpi = source.resolution
            data = _core.pixmap_png(self._document._core(), source)  # noqa: SLF001
            self._append_frame_page(data, (float(x_dpi), float(y_dpi)), 1, state, pages, start=start)
            return
        data, charged = _read_image_input(source, budget)
        try:
            self._append_encoded_frames(data, state, pages, start=start)
        finally:
            if charged:
                budget.release("memory_size", charged)

    def _append_encoded_frames(
        self,
        data: bytes,
        state: _ImageBatch,
        pages: list[Page],
        *,
        start: int | None,
    ) -> None:
        """Decode or pass through encoded image bytes and append pages under ``state``."""
        if data.startswith(b"\xff\xd8\xff"):
            width, height = _require_encoded_size(data)
            state.charge_frame(width, height)
            found, orientation = _jpeg_info(data)
            self._append_frame_page(data, found, orientation, state, pages, start=start)
            return
        if data.startswith(_PNG_SIGNATURE):
            width, height = _require_encoded_size(data)
            state.charge_frame(width, height)
            found, orientation = _png_info(data)
            self._append_frame_page(data, found, orientation, state, pages, start=start)
            return
        if data.startswith((_JP2_SIGNATURE, _J2K_SIGNATURE)):
            width, height = _require_encoded_size(data)
            state.charge_frame(width, height)
            exif = _jp2_exif_bytes(data)
            found, orientation = _exif_info(exif) if exif is not None else (None, 1)
            self._append_frame_page(data, found, orientation, state, pages, start=start)
            return
        for frame_data, found, orientation in _pillow_frames(data, state):
            self._append_frame_page(frame_data, found, orientation, state, pages, start=start)

    def _append_frame_page(  # noqa: PLR0913 - page creation state and frame metadata.
        self,
        data: bytes,
        found_dpi: tuple[float, float] | None,
        orientation: int,
        state: _ImageBatch,
        pages: list[Page],
        *,
        start: int | None,
    ) -> None:
        """Embed one frame and draw it on a new page, honoring Exif orientation and page fitting."""
        document = self._document
        xref = add_image(document, data)
        stream = document.objects[xref]
        raw_w, raw_h = float(stream["Width"]), float(stream["Height"])
        if orientation in _TRANSPOSED_ORIENTATIONS:
            disp_px_w, disp_px_h = raw_h, raw_w
            embedded_dpi = (found_dpi[1], found_dpi[0]) if found_dpi is not None else None
        else:
            disp_px_w, disp_px_h = raw_w, raw_h
            embedded_dpi = found_dpi
        x_dpi, y_dpi = state.override or embedded_dpi or (_DEFAULT_DPI, _DEFAULT_DPI)
        display_w = disp_px_w * _POINTS_PER_INCH / x_dpi
        display_h = disp_px_h * _POINTS_PER_INCH / y_dpi
        if state.page_size is not None:
            page_w, page_h = state.page_size
            scale = min(page_w / display_w, page_h / display_h)
            drawn_w, drawn_h = display_w * scale, display_h * scale
            x0, y0 = (page_w - drawn_w) / 2.0, (page_h - drawn_h) / 2.0
        else:
            page_w, page_h = display_w, display_h
            drawn_w, drawn_h = display_w, display_h
            x0, y0 = 0.0, 0.0
        page = self.new((page_w, page_h), index=None if start is None else start + len(pages))
        with page.draw() as canvas:
            _draw_oriented_image(canvas, xref, (x0, y0, drawn_w, drawn_h), orientation)
        pages.append(page)

    def insert(self, index: int, pages: Page | Iterable[Page]) -> None:
        """Insert copies of ``pages`` before ``index``, clamped as ``list.insert`` clamps it, in one transaction.

        ``pages`` is one :class:`Page` or an iterable of pages of one open
        document, this one or another. Pages of this document may repeat;
        each copy is a new page object that shares the content streams and
        resources of its original, with annotations, widgets, and structure
        elements of its own. Actions and 3D measurements that target an
        annotation on the source page target its copy. References to other
        pages' annotations and shared media assets keep their identities.
        Pages of another document are imported with what they reach, once
        each per call; their links to pages imported
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
        has_capture = _has_capture_indexes(source)
        if source is not self._document:
            from pyropdf._media import finish_media_import  # noqa: PLC0415

            if has_capture:
                _guard_capture_pages(source, set(chosen))
            operation = _core.begin_operation(doc, "pages")
            try:
                imported = _core.import_pages_with_source(doc, source._core(), chosen, at)  # noqa: SLF001
                if imported is not None:
                    media, core = imported
                    with source._import_source(core) as captured:  # noqa: SLF001
                        finish_media_import(self._document, captured, range(at, at + len(chosen)), media)
            except BaseException:
                _core.abort_canvas(doc, operation)
                raise
            _core.end_transaction(doc, True)  # noqa: FBT003 - commit the complete import.
            return
        order = [*objects[:at], *chosen, *objects[at:]]
        copies = [False] * at + [True] * len(chosen) + [False] * (count - at)
        has_threads = _has_catalog_threads(self._document)
        if has_threads or has_capture:
            with self._document.transaction("pages"):
                if has_capture:
                    _guard_capture_pages(source, set(chosen))
                _core.set_pages(doc, order, copies)
                if has_threads:
                    after = _core.page_objects(doc)
                    _strip_copied_page_beads(self._document, after[at : at + len(chosen)])
            return
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
        order = [objects[at] for at in chosen]
        removed_pairs = {obj for at, obj in enumerate(objects) if at not in seen}
        has_capture = bool(removed_pairs or any(copies)) and _has_capture_indexes(self._document)
        has_threads = _has_catalog_threads(self._document)
        has_named = bool(removed_pairs) and _has_named_pages(self._document)
        has_dparts = bool(removed_pairs) and _has_dparts(self._document)
        if (has_threads and (removed_pairs or any(copies))) or has_named or has_capture or has_dparts:
            with self._document.transaction("pages"):
                if has_capture:
                    affected = removed_pairs.union(obj for obj, copied in zip(order, copies, strict=True) if copied)
                    _guard_capture_pages(self._document, affected)
                if removed_pairs and has_threads:
                    _prune_threads_for_removed_pages(self._document, removed_pairs)
                if has_named:
                    _prune_named_pages_for_removed_pages(self._document, removed_pairs)
                if has_dparts:
                    _prune_dparts_for_removed_pages(self._document, objects, removed_pairs)
                _core.set_pages(doc, order, copies)
                if has_threads and any(copies):
                    after = _core.page_objects(doc)
                    _strip_copied_page_beads(
                        self._document,
                        [after[pos] for pos, is_copy in enumerate(copies) if is_copy],
                    )
            return
        _core.set_pages(doc, order, copies)

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


@dataclass(slots=True)
class _ImageBatch:
    """Shared frame, pixel, and document-limit accounting across one ``new_from_*`` call."""

    budget: _core._OperationBudget
    existing_pages: int
    override: tuple[float, float] | None
    page_size: tuple[float, float] | None
    frames: int = 0
    pixels: int = 0

    def charge_frame(self, width: int, height: int) -> None:
        """Enforce frame count, pixel cap, page count, and cancellation for one frame."""
        self.budget.poll()
        self.frames += 1
        if self.frames > _MAX_FRAMES:
            code = "content_element_count"
            msg = f"the image has more than {_MAX_FRAMES} frames"
            raise LimitError(code, msg)
        self.budget.check("page_count", self.existing_pages + self.frames)
        self.pixels += width * height
        if self.pixels > _MAX_FRAME_PIXELS:
            code = "content_element_count"
            msg = f"the frames of the image have more than {_MAX_FRAME_PIXELS} pixels in all"
            raise LimitError(code, msg)


def _draw_oriented_image(
    canvas: Canvas,
    xref: int,
    placement: tuple[float, float, float, float],
    orientation: int,
) -> None:
    """Draw image ``xref`` into ``(x0, y0, dw, dh)`` honoring Exif ``orientation`` (1–8)."""
    x0, y0, dw, dh = placement
    if orientation == 1:
        canvas.image((x0, y0, x0 + dw, y0 + dh), xref, keep_aspect=False)
        return
    matrices = {
        2: Matrix(-1, 0, 0, 1, x0 + dw, y0),
        3: Matrix(-1, 0, 0, -1, x0 + dw, y0 + dh),
        4: Matrix(1, 0, 0, -1, x0, y0 + dh),
        5: Matrix(0, 1, 1, 0, x0, y0),
        6: Matrix(0, 1, -1, 0, x0 + dw, y0),
        7: Matrix(0, -1, -1, 0, x0 + dw, y0 + dh),
        8: Matrix(0, -1, 1, 0, x0, y0 + dh),
    }
    raw_w, raw_h = (dh, dw) if orientation in _TRANSPOSED_ORIENTATIONS else (dw, dh)
    with canvas.transform(matrices[orientation]):
        canvas.image((0, 0, raw_w, raw_h), xref, keep_aspect=False)


def _require_encoded_size(data: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` for JPEG, JPEG 2000, or PNG bytes."""
    size = _encoded_image_size(data)
    if size is None:
        msg = "the image data has no readable dimensions"
        raise ValueError(msg)
    return size


def _read_image_input(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview,
    budget: _core._OperationBudget,
) -> tuple[bytes, int]:
    """Read one path or buffer image source under ``image_input_size`` and ``memory_size``."""
    limit = _effective_limit(budget, _MAX_IMAGE_INPUT, "image_input_size")
    if isinstance(source, (bytes, bytearray, memoryview)):
        size = source.nbytes if isinstance(source, memoryview) else len(source)
        if size > limit:
            raise _image_input_error(limit)
        budget.check("image_input_size", size)
        budget.charge("memory_size", size)
        try:
            data = source if isinstance(source, bytes) else bytes(source)
        except BaseException:
            budget.release("memory_size", size)
            raise
        return data, size
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as file:  # noqa: PTH123 - os.PathLike and str alike.
            file_size = os.fstat(file.fileno()).st_size
            if file_size > limit:
                raise _image_input_error(limit)
            if file_size > 0:
                budget.check("image_input_size", file_size)
                budget.check("memory_size", file_size)
            return _read_bounded_stream(file, budget, hard_max=limit, limit_code="image_input_size")
    msg = f"source must be a path, bytes, or a Pixmap, not {type(source).__name__}"
    raise TypeError(msg)


def _image_input_error(limit: int | None = None) -> LimitError:
    """Return the error for encoded image data larger than one call reads."""
    effective = _MAX_IMAGE_INPUT if limit is None else limit
    msg = f"the image data is larger than {effective} bytes"
    return LimitError("image_input_size", msg)


def _jpeg_info(data: bytes) -> tuple[tuple[float, float] | None, int]:
    """Return a JPEG's resolution (JFIF density, else Exif) and its Exif orientation (1–8)."""
    jfif_dpi: tuple[float, float] | None = None
    exif_dpi: tuple[float, float] | None = None
    orientation = 1
    found_exif = False
    at = 2
    # Each segment before the scan is fill bytes, a marker, a length, and a
    # body; the markers of RST and TEM have neither (ISO/IEC 10918-1 B.1.1).
    while at < len(data) and data[at] == 0xFF:  # noqa: PLR2004 - a marker's prefix
        while at < len(data) and data[at] == 0xFF:  # noqa: PLR2004 - fill bytes
            at += 1
        if at >= len(data) or data[at] in (0xDA, 0xD9):
            break
        marker = data[at]
        at += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # noqa: PLR2004 - TEM, RST
            continue
        length = int.from_bytes(data[at : at + 2])
        if length < 2:  # noqa: PLR2004 - length includes its own 2 bytes
            break
        body = data[at + 2 : at + length]
        if marker == 0xE0 and body[:5] == b"JFIF\x00" and len(body) >= 12 and jfif_dpi is None:  # noqa: PLR2004
            unit, x, y = body[7], int.from_bytes(body[8:10]), int.from_bytes(body[10:12])
            if x and y and unit in (1, 2):
                scale = 1.0 if unit == 1 else _CM_PER_INCH
                jfif_dpi = (x * scale, y * scale)
        elif marker == 0xE1 and body[:6] == b"Exif\x00\x00" and not found_exif:  # noqa: PLR2004 - APP1
            exif_dpi, orientation = _exif_info(body[6:])
            found_exif = True
        at += length
    return jfif_dpi or exif_dpi, orientation


def _exif_info(tiff: bytes) -> tuple[tuple[float, float] | None, int]:
    """Return the resolution and orientation (1–8) from the first image directory of Exif TIFF data."""
    orders: dict[bytes, Literal["little", "big"]] = {b"II": "little", b"MM": "big"}
    order = orders.get(tiff[:2])
    if order is None:
        return None, 1

    def number(at: int, size: int) -> int:
        field = tiff[at : at + size]
        return int.from_bytes(field, order) if len(field) == size else 0

    directory = number(4, 4)
    found: dict[int, float] = {}
    orientation = 1
    entry_count = min(number(directory, 2), (len(tiff) - directory - 2) // 12) if directory + 2 <= len(tiff) else 0
    entries = range(directory + 2, directory + 2 + 12 * entry_count, 12)
    for entry in entries:
        tag, kind = number(entry, 2), number(entry + 2, 2)
        if tag in (_EXIF_X_RESOLUTION, _EXIF_Y_RESOLUTION) and kind == 5:  # noqa: PLR2004 - RATIONAL
            at = number(entry + 8, 4)
            numerator, denominator = number(at, 4), number(at + 4, 4)
            found[tag] = numerator / denominator if denominator else 0.0
        elif tag == _EXIF_RESOLUTION_UNIT and kind == 3:  # noqa: PLR2004 - SHORT
            found[tag] = number(entry + 8, 2)
        elif tag == _EXIF_ORIENTATION and kind == 3:  # noqa: PLR2004 - SHORT
            value = number(entry + 8, 2)
            if 1 <= value <= _EXIF_MAX_ORIENTATION:
                orientation = value
    x, y = found.get(_EXIF_X_RESOLUTION), found.get(_EXIF_Y_RESOLUTION)
    scale = _EXIF_UNITS.get(int(found.get(_EXIF_RESOLUTION_UNIT, 2)))
    dpi = (x * scale, y * scale) if x and y and scale else None
    return dpi, orientation


def _png_info(data: bytes) -> tuple[tuple[float, float] | None, int]:
    """Return a PNG's resolution (``pHYs``, else ``eXIf``) and its Exif orientation (1–8)."""
    phys_dpi: tuple[float, float] | None = None
    exif_dpi: tuple[float, float] | None = None
    orientation = 1
    found_exif = False
    at = len(_PNG_SIGNATURE)
    while at + 12 <= len(data):
        length = int.from_bytes(data[at : at + 4])
        if at + 12 + length > len(data):
            break
        kind = data[at + 4 : at + 8]
        body = data[at + 8 : at + 8 + length]
        if kind == b"pHYs" and len(body) == 9 and body[8] == 1 and phys_dpi is None:  # noqa: PLR2004
            x, y = int.from_bytes(body[:4]), int.from_bytes(body[4:8])
            if x and y:
                phys_dpi = (_png_resolution(x), _png_resolution(y))
        elif kind == b"eXIf" and not found_exif:
            exif_dpi, orientation = _exif_info(body)
            found_exif = True
        elif kind == b"IEND":
            break
        at += 12 + length
    return phys_dpi or exif_dpi, orientation


def _png_resolution(pixels_per_metre: int) -> float:
    """Return the resolution in inches of ``pixels_per_metre``, rounded to one decimal when that stores the same.

    Whole pixels per metre miss most resolutions in inches by a little: 72
    dpi is stored as 2835, 72.009 dpi. A low resolution keeps its exact
    value, since its nearest tenth stores another number.
    """
    exact = pixels_per_metre * _METRES_PER_INCH
    rounded = round(exact, 1)
    return rounded if round(rounded / _METRES_PER_INCH) == pixels_per_metre else exact


def _valid_dpi(value: object) -> tuple[float, float] | None:
    """Return a positive finite ``(x, y)`` DPI pair from Pillow metadata, or ``None`` if invalid."""
    if not isinstance(value, (tuple, list)) or len(value) != 2:  # noqa: PLR2004
        return None
    x_raw, y_raw = value
    if isinstance(x_raw, (bool, str, bytes)) or isinstance(y_raw, (bool, str, bytes)):
        return None
    try:
        x_dpi, y_dpi = float(x_raw), float(y_raw)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    if not (math.isfinite(x_dpi) and math.isfinite(y_dpi) and x_dpi > 0 and y_dpi > 0):
        return None
    return (x_dpi, y_dpi)


def _pillow_frames(data: bytes, state: _ImageBatch) -> Iterator[tuple[bytes, tuple[float, float] | None, int]]:
    """Return each frame of an image that Pillow reads, as PNG data with its resolution and orientation."""
    try:
        from PIL import Image, ImageOps, ImageSequence  # noqa: PLC0415 - Pillow is optional.
    except ImportError:
        msg = "the image is not JPEG, JPEG 2000, or PNG; other formats need Pillow: pip install pillow"
        raise ValueError(msg) from None
    try:
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        code = "content_element_count"
        msg = f"the image exceeds the pixel limit: {exc}"
        raise LimitError(code, msg) from exc
    except Exception as exc:  # Pillow raises its own errors for each format.
        msg = f"the image data is not in a format that Pillow reads: {exc}"
        raise ValueError(msg) from exc
    return _converted_frames(
        image, ImageSequence.Iterator(image), state, ImageOps.exif_transpose, Image.DecompressionBombError
    )


def _encode_pillow_frame(
    frame: PillowImage,
    exif_transpose: Callable[[PillowImage], PillowImage | None],
) -> tuple[bytes, tuple[float, float] | None]:
    """Apply Exif orientation to ``frame`` and return PNG bytes with display-space DPI."""
    raw_orientation = frame.getexif().get(_EXIF_ORIENTATION, 1)
    dpi = _valid_dpi(frame.info.get("dpi"))
    if raw_orientation in _TRANSPOSED_ORIENTATIONS and dpi is not None:
        dpi = (dpi[1], dpi[0])
    oriented = frame
    try:
        if raw_orientation != 1:
            transposed = exif_transpose(frame)
            if transposed is not None:
                oriented = transposed
        alpha = oriented.mode in ("RGBA", "LA", "PA") or "transparency" in oriented.info
        gray = oriented.mode in ("1", "L", "LA", "I", "I;16", "F")
        mode = ("LA" if alpha else "L") if gray else ("RGBA" if alpha else "RGB")
        out = io.BytesIO()
        oriented.convert(mode).save(out, "PNG")
        return out.getvalue(), dpi
    finally:
        if oriented is not frame:
            oriented.close()


def _converted_frames(
    image: PillowImage,
    sequence: Iterable[PillowImage],
    state: _ImageBatch,
    exif_transpose: Callable[[PillowImage], PillowImage | None],
    bomb_error: type[BaseException],
) -> Iterator[tuple[bytes, tuple[float, float] | None, int]]:
    """Yield each frame of ``sequence`` as PNG data with its resolution and orientation, within limits."""
    with image:
        for frame in sequence:
            width, height = frame.size
            if width <= 0 or height <= 0:
                msg = "the image frame has non-positive dimensions"
                raise ValueError(msg)
            pixels = width * height
            if pixels > _MAX_DECODED_PIXELS:
                code = "content_element_count"
                msg = f"the image frame has {pixels} pixels, over the {_MAX_DECODED_PIXELS}-pixel limit"
                raise LimitError(code, msg)
            state.charge_frame(width, height)
            raster_bytes = pixels * 4
            state.budget.check("decompressed_size", raster_bytes)
            state.budget.charge("total_decompressed_size", raster_bytes)
            state.budget.charge("memory_size", raster_bytes)
            try:
                encoded, dpi = _encode_pillow_frame(frame, exif_transpose)
            except bomb_error as exc:
                code = "content_element_count"
                msg = f"the image exceeds the pixel limit: {exc}"
                raise LimitError(code, msg) from exc
            except (LimitError, ValueError):
                raise
            except Exception as exc:
                msg = f"the image frame could not be decoded: {exc}"
                raise ValueError(msg) from exc
            finally:
                state.budget.release("memory_size", raster_bytes)
            yield encoded, dpi, 1
