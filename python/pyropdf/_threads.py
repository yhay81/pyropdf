"""Article threads, beads, and thread actions (ISO 32000-2 §12.4.3, §12.6.4.7)."""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast, overload

from pyropdf import _core
from pyropdf._errors import LimitError, MalformedPdfError, PdfError, StaleObjectError, StalePageError
from pyropdf._geometry import Rect
from pyropdf._metadata import _require_version, pdf_date
from pyropdf._objects import Array, Dictionary, Name, Stream, String, _catalog, _Handle, _unlocked
from pyropdf._presentations import _FLOAT32_MAX, _MAX_NAVIGATION_STEPS, _read_file_spec, _to_display, _to_pdf

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._page import Page

#: Maximum number of beads in a single article thread ring.
_MAX_BEADS = 4096
#: Maximum number of article threads in ``Catalog["Threads"]``.
_MAX_THREADS = 4096
#: Maximum UTF-8 byte length of thread info strings written in one call.
_MAX_THREAD_TEXT_BYTES = 1024 * 1024
_RECT_COORDS = 4
_BEAD_TUPLE_LEN = 2

_THREAD_INFO_KEYS: dict[str, str] = {
    "title": "Title",
    "author": "Author",
    "subject": "Subject",
    "keywords": "Keywords",
    "created": "CreationDate",
    "modified": "ModDate",
}


def _page_cls() -> type[Page]:
    from pyropdf._page import Page  # noqa: PLC0415 - _page imports _links -> _targets -> _threads.

    return Page


def _is_non_bool_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_dict(value: object) -> bool:
    return isinstance(value, Dictionary) and not isinstance(value, Stream)


def _indirect_xref(value: object) -> int | None:
    if isinstance(value, _Handle) and value.is_indirect:
        return value.xref
    return None


def _dict_id(value: object) -> tuple[int, int] | None:
    if _is_dict(value):
        d = cast("Dictionary", value)
        if d.is_indirect and d.xref is not None and d.generation is not None:
            return (d.xref, d.generation)
    return None


def _resolve_seq_index(index: object, count: int, label: str) -> int:
    if not _is_non_bool_int(index):
        msg = f"{label} must be an int, not {type(index).__name__}"
        raise TypeError(msg)
    assert isinstance(index, int)  # noqa: S101 - narrows type for ty.
    resolved = index + count if index < 0 else index
    if not 0 <= resolved < count:
        msg = f"{label} {index} is out of range for {count} items"
        raise IndexError(msg)
    return resolved


def _clamp_insert_index(index: int | None, count: int) -> int:
    if index is None:
        return count
    if not _is_non_bool_int(index):
        msg = f"index must be an int or None: {index!r}"
        raise TypeError(msg)
    return max(0, index + count) if index < 0 else min(index, count)


def _display_rect(value: object) -> Rect:
    """Validate and normalize ``value`` into a finite, non-empty display :class:`Rect`."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray, memoryview)):
        msg = f"rect must be a Rect or a 4-number sequence: {value!r}"
        raise TypeError(msg)
    if len(value) != _RECT_COORDS:
        msg = f"rect must have 4 coordinates (x0, y0, x1, y1): {value!r}"
        raise ValueError(msg)
    coords: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            msg = f"rect coordinates must be numbers: {value!r}"
            raise TypeError(msg)
        num = float(item)
        if not math.isfinite(num) or abs(num) > _FLOAT32_MAX:
            msg = f"rect coordinates must be finite and within PDF real-number range: {value!r}"
            raise ValueError(msg)
        coords.append(num)
    x0, y0, x1, y1 = coords
    if not (x0 < x1 and y0 < y1):
        msg = f"rect must be non-empty with x0 < x1 and y0 < y1: {value!r}"
        raise ValueError(msg)
    return Rect(x0, y0, x1, y1)


def _validate_page_spec(page: object) -> Page | int:
    page_type = _page_cls()
    if isinstance(page, page_type):
        return page
    if _is_non_bool_int(page):
        assert isinstance(page, int)  # noqa: S101 - narrows type for ty.
        return page
    msg = f"page must be a Page or int: {page!r}"
    raise TypeError(msg)


def _resolve_page_arg(document: Document, page: Page | int) -> Page:
    page_type = _page_cls()
    if isinstance(page, page_type):
        if page.document is not document:
            msg = "page belongs to another document"
            raise ValueError(msg)
        _ = page.index
        return page
    if _is_non_bool_int(page):
        assert isinstance(page, int)  # noqa: S101 - narrows type for ty.
        return document.pages[page]
    msg = f"page must be a Page or int: {page!r}"
    raise TypeError(msg)


def _display_to_pdf_rect(document: Document, page: Page, rect: Rect) -> list[float]:
    space = _core.page_space(document._core(), page.xref, page._generation)  # noqa: SLF001
    if space is None:
        raise page._stale()  # noqa: SLF001
    rotation, crop, unit = space
    ax, ay = _to_pdf(crop, rotation, unit, rect.x0, rect.y0)
    bx, by = _to_pdf(crop, rotation, unit, rect.x1, rect.y1)
    px0, px1 = (ax, bx) if ax <= bx else (bx, ax)
    py0, py1 = (ay, by) if ay <= by else (by, ay)
    if not all(math.isfinite(c) and abs(c) <= _FLOAT32_MAX for c in (px0, py0, px1, py1)):
        msg = f"rect {rect!r} overflows PDF user-space coordinates"
        raise ValueError(msg)
    return [px0, py0, px1, py1]


def _pdf_to_display_rect(document: Document, page: Page, raw_rect: object) -> Rect:
    if not isinstance(raw_rect, Array) or len(raw_rect) != _RECT_COORDS:
        msg = "bead /R is not a 4-number rectangle"
        raise MalformedPdfError(msg)
    nums: list[float] = []
    for item in raw_rect:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            msg = "bead /R contains a non-finite or non-numeric coordinate"
            raise MalformedPdfError(msg)
        nums.append(float(item))
    space = _core.page_space(document._core(), page.xref, page._generation)  # noqa: SLF001
    if space is None:
        raise page._stale()  # noqa: SLF001
    rotation, crop, unit = space
    dx0, dy0 = _to_display(crop, rotation, unit, nums[0], nums[1])
    dx1, dy1 = _to_display(crop, rotation, unit, nums[2], nums[3])
    return Rect(min(dx0, dx1), min(dy0, dy1), max(dx0, dx1), max(dy0, dy1))


def _check_text_limit(utf8_bytes: int, label: str) -> None:
    if utf8_bytes > _MAX_THREAD_TEXT_BYTES:
        code = "text_input_size"
        msg = f"{label} exceeds the {_MAX_THREAD_TEXT_BYTES}-byte UTF-8 limit"
        raise LimitError(code, msg)


def _validate_info_value(attr_name: str, value: object) -> tuple[str, int]:
    if attr_name in {"created", "modified"} and isinstance(value, datetime):
        formatted = pdf_date(value)
        return formatted, len(formatted)
    if not isinstance(value, str):
        msg = f"{attr_name} must be a str: {value!r}"
        raise TypeError(msg)
    if not value:
        msg = f"{attr_name} must not be empty; use del to remove it"
        raise ValueError(msg)
    if len(value) > _MAX_THREAD_TEXT_BYTES:
        _check_text_limit(len(value) + 1, f"thread {attr_name}")
    utf8_len = len(value.encode("utf-8"))
    _check_text_limit(utf8_len, f"thread {attr_name}")
    return value, utf8_len


@dataclass(frozen=True, slots=True, init=False)
class BeadSpecification:
    """One bead to create inside an article thread: a page and a display-space rectangle.

    ``page`` is a :class:`Page` handle or a zero-based page index (negative
    indexes count from the end when added to a document), and ``rect`` is a
    non-empty :class:`Rect` or 4-number sequence in that page's display
    coordinates.
    """

    page: Page | int
    rect: Rect

    def __init__(self, page: Page | int, rect: Rect | Sequence[float]) -> None:
        """Validate ``page`` and ``rect``."""
        object.__setattr__(self, "page", _validate_page_spec(page))
        object.__setattr__(self, "rect", _display_rect(rect))


@dataclass(frozen=True, slots=True)
class ThreadAction:
    """An action that begins reading an article thread (``/S /Thread``, ISO 32000-2 Table 209).

    ``thread`` is a non-negative zero-based thread index or a non-empty thread
    title (``/D``), ``bead`` is an optional non-negative zero-based bead index
    within that thread (``/B``, defaulting to its first bead when ``None``),
    and ``file`` names an external PDF file when the thread lives in another
    document (``/F``).
    """

    thread: int | str
    bead: int | None = None
    _: KW_ONLY
    file: str | None = None

    def __post_init__(self) -> None:
        """Validate ``thread``, ``bead``, and ``file``."""
        if isinstance(self.thread, bool) or not isinstance(self.thread, (int, str)):
            msg = f"thread must be an int or str: {self.thread!r}"
            raise TypeError(msg)
        if isinstance(self.thread, int) and self.thread < 0:
            msg = f"thread index must be non-negative: {self.thread}"
            raise ValueError(msg)
        if isinstance(self.thread, str) and not self.thread:
            msg = "thread title must not be empty"
            raise ValueError(msg)
        if self.bead is not None:
            if not _is_non_bool_int(self.bead):
                msg = f"bead must be an int or None: {self.bead!r}"
                raise TypeError(msg)
            if self.bead < 0:
                msg = f"bead index must be non-negative: {self.bead}"
                raise ValueError(msg)
        if self.file is not None:
            if not isinstance(self.file, str):
                msg = f"file must be a str or None: {self.file!r}"
                raise TypeError(msg)
            if not self.file:
                msg = "file must not be empty"
                raise ValueError(msg)


def _catalog_thread_ids(document: Document) -> list[tuple[int, int]]:
    raw_threads = _catalog(document).get("Threads")
    if not isinstance(raw_threads, Array):
        return []
    if len(raw_threads) > _MAX_THREADS:
        code = "page_entry_count"
        msg = f"catalog /Threads exceeds the {_MAX_THREADS}-thread safety limit"
        raise LimitError(code, msg)
    ids: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for entry in raw_threads:
        pair = _dict_id(entry)
        if pair is not None and pair not in seen:
            seen.add(pair)
            ids.append(pair)
    return ids


def _write_catalog_threads(document: Document, thread_ids: Sequence[tuple[int, int]]) -> list[int]:
    """Write ``Catalog['Threads']`` as an indirect array (or remove it when empty), returning retired xrefs."""
    catalog = _catalog(document)
    existing = catalog.get("Threads")
    if not thread_ids:
        old_xref = _indirect_xref(existing)
        if "Threads" in catalog:
            del catalog["Threads"]
        return [] if old_xref is None else [old_xref]

    refs = [Dictionary(document, pair) for pair in thread_ids]
    if isinstance(existing, Array) and existing.is_indirect and existing.xref is not None:
        document.objects[existing.xref] = refs
        return []
    old_xref = _indirect_xref(existing)
    arr_xref = document.objects.add(refs)
    catalog["Threads"] = document.objects[arr_xref]
    return [] if old_xref is None else [old_xref]


def _thread_bead_ids(thread_dict: Dictionary) -> list[tuple[int, int]]:
    first = thread_dict.get("F")
    if not _is_dict(first):
        return []
    beads: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    current = cast("Dictionary", first)
    while True:
        pair = _dict_id(current)
        if pair is None or pair in seen:
            break
        if len(beads) >= _MAX_BEADS:
            code = "page_entry_count"
            msg = f"thread bead ring exceeds the {_MAX_BEADS}-bead safety limit"
            raise LimitError(code, msg)
        seen.add(pair)
        beads.append(pair)
        nxt = current.get("N")
        if not _is_dict(nxt):
            break
        current = cast("Dictionary", nxt)
    return beads


def _lookup_live_dict(document: Document, pair: tuple[int, int], kind_label: str) -> Dictionary:
    core = _unlocked(document)
    found = _core.object_lookup(core, pair[0])
    if found is None or found[0] != pair[1] or not isinstance(found[1], tuple) or found[1][0] != "d":
        msg = f"{kind_label} {pair[0]} {pair[1]} R does not exist in this version"
        raise StaleObjectError(msg)
    return Dictionary(document, pair)


def _walk_bead_for_thread(start: Dictionary, link_key: str) -> tuple[int, int] | None:
    seen: set[tuple[int, int]] = set()
    current = start
    steps = 0
    while steps < _MAX_BEADS:
        steps += 1
        t_pair = _dict_id(current.get("T"))
        if t_pair is not None:
            return t_pair
        c_pair = _dict_id(current)
        if c_pair is None or c_pair in seen:
            break
        seen.add(c_pair)
        neighbor = current.get(link_key)
        if not _is_dict(neighbor):
            break
        current = cast("Dictionary", neighbor)
    return None


def _resolve_bead_and_thread(document: Document, bead_id: tuple[int, int]) -> tuple[Dictionary, tuple[int, int]]:
    bead_dict = _lookup_live_dict(document, bead_id, "bead")
    live_threads = _catalog_thread_ids(document)
    if not live_threads:
        msg = f"bead {bead_id[0]} {bead_id[1]} R is not in any thread of doc.threads"
        raise StaleObjectError(msg)
    live_set = set(live_threads)

    candidate = _walk_bead_for_thread(bead_dict, "V") or _walk_bead_for_thread(bead_dict, "N")
    if candidate is not None and candidate in live_set:
        t_dict = Dictionary(document, candidate)
        if bead_id in _thread_bead_ids(t_dict):
            return bead_dict, candidate

    for t_pair in live_threads:
        t_dict = Dictionary(document, t_pair)
        if bead_id in _thread_bead_ids(t_dict):
            return bead_dict, t_pair

    msg = f"bead {bead_id[0]} {bead_id[1]} R is no longer reachable in doc.threads"
    raise StaleObjectError(msg)


def _relink_bead_ring(
    document: Document,
    thread_id: tuple[int, int],
    new_order: Sequence[tuple[int, int]],
) -> None:
    """Update ``/F``, ``/T``, ``/N``, and ``/V`` across ``new_order``, touching only changed entries."""
    thread_dict = Dictionary(document, thread_id)
    count = len(new_order)
    for idx, pair in enumerate(new_order):
        bead_dict = Dictionary(document, pair)
        prev_pair = new_order[(idx - 1) % count]
        next_pair = new_order[(idx + 1) % count]
        if _dict_id(bead_dict.get("V")) != prev_pair:
            bead_dict["V"] = Dictionary(document, prev_pair)
        if _dict_id(bead_dict.get("N")) != next_pair:
            bead_dict["N"] = Dictionary(document, next_pair)
        if idx == 0:
            if _dict_id(bead_dict.get("T")) != thread_id:
                bead_dict["T"] = thread_dict
        elif "T" in bead_dict:
            del bead_dict["T"]
    if _dict_id(thread_dict.get("F")) != new_order[0]:
        thread_dict["F"] = Dictionary(document, new_order[0])


def _append_page_beads(document: Document, page_dict: Dictionary, bead_pairs: Sequence[tuple[int, int]]) -> None:
    if not bead_pairs:
        return
    existing = page_dict.get("B")
    new_refs = [Dictionary(document, pair) for pair in bead_pairs]
    if isinstance(existing, Array):
        present = {_dict_id(item) for item in existing}
        to_add = [ref for pair, ref in zip(bead_pairs, new_refs, strict=True) if pair not in present]
        if not to_add:
            return
        updated = [*list(existing), *to_add]
        if existing.is_indirect and existing.xref is not None:
            document.objects[existing.xref] = updated
        else:
            page_dict["B"] = updated
    else:
        page_dict["B"] = new_refs


def _order_page_beads(
    document: Document,
    page_dict: Dictionary,
    ring: Sequence[tuple[int, int]],
    *,
    added: tuple[int, int] | None = None,
) -> None:
    """Fill this thread's slots in the page's /B array in ring order."""
    entries = page_dict.get("B")
    if not isinstance(entries, Array):
        if added is not None:
            page_dict["B"] = [Dictionary(document, added)]
        return

    members = set(ring)
    slots: dict[tuple[int, int], int] = {}
    for index, entry in enumerate(entries):
        pair = _dict_id(entry)
        if pair is not None and pair in members:
            slots[pair] = index
    if added is not None and added not in slots:
        slots[added] = len(entries)
        entries.append(Dictionary(document, added))

    page_order = (pair for pair in ring if pair in slots)
    for (previous, index), pair in zip(slots.items(), page_order, strict=True):
        if previous != pair:
            entries[index] = Dictionary(document, pair)


def _remove_beads_from_page_dict(
    document: Document,
    page_dict: Dictionary,
    removed_xrefs: set[int],
) -> int | None:
    existing = page_dict.get("B")
    if not isinstance(existing, Array):
        return None
    kept = [item for item in existing if _indirect_xref(item) not in removed_xrefs]
    if len(kept) == len(existing):
        return None
    if not kept:
        old_xref = _indirect_xref(existing)
        del page_dict["B"]
        return old_xref
    if existing.is_indirect and existing.xref is not None:
        document.objects[existing.xref] = kept
    else:
        page_dict["B"] = kept
    return None


def _remove_beads_from_pages(document: Document, removed_pairs: Sequence[tuple[int, int]]) -> list[int]:
    """Remove ``removed_pairs`` from their pages' ``/B`` arrays, returning retired ``/B`` array xrefs."""
    if not removed_pairs:
        return []
    removed_xrefs = {pair[0] for pair in removed_pairs}
    affected_pages: dict[tuple[int, int], None] = {}
    for pair in removed_pairs:
        found = _core.object_lookup(_unlocked(document), pair[0])
        if found is None or found[0] != pair[1]:
            continue
        p_pair = _dict_id(Dictionary(document, pair).get("P"))
        if p_pair is not None:
            affected_pages[p_pair] = None
    # Also check current pages in case a bead's /P differed from a page's /B list.
    for p_pair in _core.page_objects(_unlocked(document)):
        affected_pages[p_pair] = None
    retired: list[int] = []
    for p_pair in affected_pages:
        found_page = _core.object_lookup(_unlocked(document), p_pair[0])
        if found_page is None or found_page[0] != p_pair[1]:
            continue
        old_b = _remove_beads_from_page_dict(document, Dictionary(document, p_pair), removed_xrefs)
        if old_b is not None:
            retired.append(old_b)
    return retired


def _scrub_holder_action(
    holder: Dictionary,
    key: str,
    dead_threads: set[int],
    dead_beads: set[int],
) -> int | None:
    act = holder.get(key)
    if not _is_dict(act):
        return None
    act_dict = cast("Dictionary", act)
    if act_dict.get("S") != "Thread":
        return None
    d_xref = _indirect_xref(act_dict.get("D"))
    b_xref = _indirect_xref(act_dict.get("B"))
    if d_xref is not None and d_xref in dead_threads:
        old_act = _indirect_xref(act_dict)
        del holder[key]
        return old_act
    if b_xref is not None and b_xref in dead_beads and "B" in act_dict:
        del act_dict["B"]
    return None


def _scrub_page_annots(
    page_dict: Dictionary,
    dead_threads: set[int],
    dead_beads: set[int],
    extra: list[int],
) -> None:
    annots = page_dict.get("Annots")
    if not isinstance(annots, Array):
        return
    for annot in annots:
        if _is_dict(annot):
            retired = _scrub_holder_action(cast("Dictionary", annot), "A", dead_threads, dead_beads)
            if retired is not None:
                extra.append(retired)


def _scrub_page_actions(
    page_dict: Dictionary,
    dead_threads: set[int],
    dead_beads: set[int],
    extra: list[int],
) -> None:
    _scrub_page_annots(page_dict, dead_threads, dead_beads, extra)
    first = page_dict.get("PresSteps")
    if not _is_dict(first):
        return
    seen: set[int] = set()
    current = cast("Dictionary", first)
    steps = 0
    while steps < _MAX_NAVIGATION_STEPS:
        steps += 1
        xref = _indirect_xref(current)
        if xref is not None and xref in seen:
            break
        if xref is not None:
            seen.add(xref)
        for nav_key in ("NA", "PA"):
            retired = _scrub_holder_action(current, nav_key, dead_threads, dead_beads)
            if retired is not None:
                extra.append(retired)
        nxt = current.get("Next")
        if not _is_dict(nxt):
            break
        current = cast("Dictionary", nxt)


def _free_thread_objects(
    document: Document,
    removed_threads: set[int],
    removed_beads: set[int],
    extra_candidates: Sequence[int],
) -> None:
    candidates = sorted({*removed_threads, *removed_beads, *extra_candidates})
    if not candidates:
        return
    core = _unlocked(document)
    freed = set(_core.object_free_unreferenced(core, candidates))
    live_threads = removed_threads - freed
    live_beads = removed_beads - freed
    if not live_threads and not live_beads:
        return

    extra: list[int] = []
    catalog = _catalog(document)
    retired_open = _scrub_holder_action(catalog, "OpenAction", live_threads, live_beads)
    if retired_open is not None:
        extra.append(retired_open)
    for p_pair in _core.page_objects(core):
        _scrub_page_actions(Dictionary(document, p_pair), live_threads, live_beads, extra)
    for item in document.outline.walk():
        retired_item = _scrub_holder_action(item.obj, "A", live_threads, live_beads)
        if retired_item is not None:
            extra.append(retired_item)
    retry = sorted({*live_threads, *live_beads, *extra})
    if retry:
        _core.object_free_unreferenced(core, retry)


class Bead:
    """A handle on one bead dictionary of an article thread (ISO 32000-2 Table 163).

    Properties read the current version of the document. :attr:`rect` converts
    between ``/R`` in PDF user space and :attr:`page`'s display coordinates,
    and assigning :attr:`page` or :attr:`rect` updates the bead (and pages'
    ``/B`` arrays when moving across pages) in one transaction. A handle whose
    bead or thread was removed raises :class:`StaleObjectError`, and works
    again if undo restores it.
    """

    __slots__ = ("_document", "_id")

    def __init__(self, document: Document, xref: int, generation: int = 0) -> None:
        """Name bead ``xref`` of ``generation``; :class:`Beads` creates handles."""
        self._document = document
        self._id = (xref, generation)

    def _resolved(self) -> tuple[Dictionary, tuple[int, int]]:
        return _resolve_bead_and_thread(self._document, self._id)

    @property
    def document(self) -> Document:
        """Return the document that holds this bead."""
        return self._document

    @property
    def xref(self) -> int:
        """Return the bead's indirect object number."""
        return self._id[0]

    @property
    def obj(self) -> Dictionary:
        """Return the bead's dictionary in the low-level object model."""
        return self._resolved()[0]

    @property
    def thread(self) -> Thread:
        """Return the :class:`Thread` that owns this bead."""
        _, thread_id = self._resolved()
        return Thread(self._document, *thread_id)

    @property
    def page(self) -> Page:
        """Return or set the :class:`Page` on which this bead appears (``/P``)."""
        bead_dict, _ = self._resolved()
        p_pair = _dict_id(bead_dict.get("P"))
        if p_pair is None or _core.page_index(_unlocked(self._document), p_pair[0], p_pair[1]) is None:
            msg = f"bead {self._id[0]} /P does not reference a current page"
            raise MalformedPdfError(msg)
        return _page_cls()(self._document, p_pair[0], p_pair[1])

    @page.setter
    def page(self, target: Page | int) -> None:
        new_page = _resolve_page_arg(self._document, target)
        bead_dict, thread_id = self._resolved()
        old_page = self.page
        display_rect = _pdf_to_display_rect(self._document, old_page, bead_dict.get("R"))
        new_pdf_rect = _display_to_pdf_rect(self._document, new_page, display_rect)
        if new_page == old_page:
            return
        with self._document.transaction("bead page"):
            old_b_xref = _remove_beads_from_page_dict(self._document, old_page.obj, {self._id[0]})
            ring = _thread_bead_ids(Dictionary(self._document, thread_id))
            _order_page_beads(self._document, new_page.obj, ring, added=self._id)
            bead_dict["P"] = new_page.obj
            bead_dict["R"] = new_pdf_rect
            if old_b_xref is not None:
                _core.object_free_unreferenced(_unlocked(self._document), [old_b_xref])

    @property
    def rect(self) -> Rect:
        """Return or set the bead's rectangle in :attr:`page`'s display coordinates (``/R``)."""
        bead_dict, _ = self._resolved()
        return _pdf_to_display_rect(self._document, self.page, bead_dict.get("R"))

    @rect.setter
    def rect(self, rect: Rect | Sequence[float]) -> None:
        checked = _display_rect(rect)
        bead_dict, _ = self._resolved()
        pdf_rect = _display_to_pdf_rect(self._document, self.page, checked)
        with self._document.transaction("bead rect"):
            bead_dict["R"] = pdf_rect

    def __eq__(self, other: object) -> bool:
        """Compare document and object identity."""
        if not isinstance(other, Bead):
            return NotImplemented
        return (self._document, self._id) == (other._document, other._id)

    def __hash__(self) -> int:
        """Hash document and object identity."""
        return hash((id(self._document), self._id))

    def __repr__(self) -> str:
        """Return ``Bead(xref=..., page=..., rect=...)``, or a stale marker."""
        try:
            return f"Bead(xref={self._id[0]}, page={self.page.index}, rect={self.rect!r})"
        except (StaleObjectError, StalePageError, PdfError):
            return f"<stale Bead xref={self._id[0]}>"


class Beads(Sequence[Bead]):
    """The ordered sequence of :class:`Bead` handles in a :class:`Thread`, as ``thread.beads`` returns it.

    Walks the circular ``/F`` -> ``/N`` bead ring iteratively up to 4,096 beads
    with cycle detection. :meth:`add`, :meth:`move`, and ``del thread.beads[i]``
    each update the ring, ``BeadFirst["T"]``, and pages' ``/B`` arrays in one
    transaction. On a shared page, this thread fills its existing array slots in
    reading order. Adding a bead to the page appends one slot. The entries of
    other threads keep their relative order.

    Deleting every bead of a thread raises :class:`ValueError`;
    remove the thread from ``doc.threads`` instead.
    """

    __slots__ = ("_thread",)

    def __init__(self, thread: Thread) -> None:
        """Wrap ``thread``; use :attr:`Thread.beads`."""
        self._thread = thread

    def _ids(self) -> list[tuple[int, int]]:
        thread_dict = self._thread._dict()  # noqa: SLF001
        return _thread_bead_ids(thread_dict)

    def _handles(self) -> list[Bead]:
        document = self._thread.document
        return [Bead(document, *pair) for pair in self._ids()]

    @overload
    def __getitem__(self, index: int) -> Bead: ...
    @overload
    def __getitem__(self, index: slice) -> list[Bead]: ...
    def __getitem__(self, index: int | slice) -> Bead | list[Bead]:
        """Return one bead, or a list of beads for a slice."""
        return self._handles()[index]

    def __len__(self) -> int:
        """Return the number of beads in the thread."""
        return len(self._ids())

    def __iter__(self) -> Iterator[Bead]:
        """Iterate over the thread's beads in reading order."""
        return iter(self._handles())

    def add(self, page: Page | int, rect: Sequence[float], *, index: int | None = None) -> Bead:
        """Insert a new bead on ``page`` covering ``rect`` (in display coordinates) at ``index``.

        ``index`` is clamped like :meth:`list.insert`, appending when ``None``.
        Updates the circular ``/N``/``/V`` ring, ``Thread["F"]`` and
        ``BeadFirst["T"]`` when inserted first, and ``page["B"]`` in one
        transaction.
        """
        document = self._thread.document
        checked_rect = _display_rect(rect)
        if index is not None and not _is_non_bool_int(index):
            msg = f"index must be an int or None: {index!r}"
            raise TypeError(msg)
        target_page = _resolve_page_arg(document, page)
        pdf_rect = _display_to_pdf_rect(document, target_page, checked_rect)
        ring = self._ids()
        if len(ring) >= _MAX_BEADS:
            code = "page_entry_count"
            msg = f"a thread cannot hold more than {_MAX_BEADS} beads"
            raise LimitError(code, msg)
        pos = _clamp_insert_index(index, len(ring))

        with document.transaction("bead"):
            _require_version(document, _catalog(document), (1, 1))
            new_xref = document.objects.add({"Type": Name("Bead"), "P": target_page.obj, "R": pdf_rect})
            new_obj = document.objects[new_xref]
            new_pair = (new_xref, new_obj.generation or 0)
            new_order = [*ring[:pos], new_pair, *ring[pos:]]
            _relink_bead_ring(document, self._thread._id, new_order)  # noqa: SLF001
            _order_page_beads(document, target_page.obj, new_order, added=new_pair)
        return Bead(document, *new_pair)

    def move(self, index: int, to: int) -> None:
        """Move bead ``index`` so that it ends at ``to``, in one transaction."""
        ring = self._ids()
        src = _resolve_seq_index(index, len(ring), "bead index")
        dst = _resolve_seq_index(to, len(ring), "bead index")
        if src == dst:
            return
        new_order = list(ring)
        moved = new_order.pop(src)
        new_order.insert(dst, moved)
        document = self._thread.document
        with document.transaction("move bead"):
            _relink_bead_ring(document, self._thread._id, new_order)  # noqa: SLF001
            page_dict = Dictionary(document, moved).get("P")
            if isinstance(page_dict, Dictionary) and not isinstance(page_dict, Stream):
                _order_page_beads(document, page_dict, new_order)

    def __delitem__(self, index: int | slice) -> None:
        """Remove one bead or a slice of beads from the thread, in one transaction."""
        ring = self._ids()
        if isinstance(index, slice):
            removed_indices = set(range(len(ring))[index])
            if not removed_indices:
                return
        else:
            removed_indices = {_resolve_seq_index(index, len(ring), "bead index")}
        if len(removed_indices) >= len(ring):
            msg = "a thread requires at least one bead; delete the thread from doc.threads instead"
            raise ValueError(msg)
        new_order = [pair for idx, pair in enumerate(ring) if idx not in removed_indices]
        removed_pairs = [ring[idx] for idx in sorted(removed_indices)]
        document = self._thread.document
        with document.transaction("delete bead"):
            _relink_bead_ring(document, self._thread._id, new_order)  # noqa: SLF001
            retired_b = _remove_beads_from_pages(document, removed_pairs)
            _free_thread_objects(document, set(), {p[0] for p in removed_pairs}, retired_b)

    def __repr__(self) -> str:
        """Return ``Beads([...])``."""
        return f"Beads({self._handles()!r})"


class Thread:
    """A handle on an indirect article ``Thread`` dictionary in ``Document.threads`` (ISO 32000-2 Table 162).

    :attr:`beads` exposes the circular bead ring, and :attr:`title`,
    :attr:`author`, :attr:`subject`, :attr:`keywords`, :attr:`created`, and
    :attr:`modified` read, set, and delete entries of the thread information
    dictionary ``/I`` in one transaction each. A handle whose thread was
    removed raises :class:`StaleObjectError`, and works again if undo restores
    it.
    """

    __slots__ = ("_document", "_id")

    def __init__(self, document: Document, xref: int, generation: int = 0) -> None:
        """Name thread ``xref`` of ``generation``; :class:`Threads` creates handles."""
        self._document = document
        self._id = (xref, generation)

    def _dict(self) -> Dictionary:
        thread_dict = _lookup_live_dict(self._document, self._id, "thread")
        if self._id not in _catalog_thread_ids(self._document):
            msg = f"thread {self._id[0]} {self._id[1]} R is no longer in doc.threads"
            raise StaleObjectError(msg)
        return thread_dict

    def _read_info(self, pdf_key: str) -> str | None:
        info = self._dict().get("I")
        if not _is_dict(info):
            return None
        raw = cast("Dictionary", info).get(pdf_key)
        if isinstance(raw, String):
            return raw.text or None
        return None

    def _write_info(self, attr_name: str, value: object) -> None:
        pdf_key = _THREAD_INFO_KEYS[attr_name]
        checked, _ = _validate_info_value(attr_name, value)
        thread_dict = self._dict()
        with self._document.transaction(f"thread {attr_name}"):
            _require_version(self._document, _catalog(self._document), (1, 1))
            info = thread_dict.get("I")
            if _is_dict(info):
                cast("Dictionary", info)[pdf_key] = checked
            else:
                old_i = _indirect_xref(info)
                thread_dict["I"] = {pdf_key: checked}
                if old_i is not None:
                    _core.object_free_unreferenced(_unlocked(self._document), [old_i])

    def _delete_info(self, attr_name: str) -> None:
        pdf_key = _THREAD_INFO_KEYS[attr_name]
        thread_dict = self._dict()
        info = thread_dict.get("I")
        if not _is_dict(info) or pdf_key not in cast("Dictionary", info):
            return
        with self._document.transaction(f"thread {attr_name}"):
            info = thread_dict.get("I")
            if _is_dict(info):
                info_dict = cast("Dictionary", info)
                if pdf_key in info_dict:
                    del info_dict[pdf_key]
                if len(info_dict) == 0:
                    old_i = _indirect_xref(info_dict)
                    del thread_dict["I"]
                    if old_i is not None:
                        _core.object_free_unreferenced(_unlocked(self._document), [old_i])

    @property
    def document(self) -> Document:
        """Return the document that holds this thread."""
        return self._document

    @property
    def xref(self) -> int:
        """Return the thread's indirect object number."""
        return self._id[0]

    @property
    def obj(self) -> Dictionary:
        """Return the thread's dictionary in the low-level object model."""
        return self._dict()

    @property
    def beads(self) -> Beads:
        """Return the thread's ordered :class:`Beads` sequence."""
        self._dict()
        return Beads(self)

    @property
    def title(self) -> str | None:
        """Return or set the thread's title (``/I /Title``), or ``None``; ``del`` removes it."""
        return self._read_info("Title")

    @title.setter
    def title(self, value: str) -> None:
        self._write_info("title", value)

    @title.deleter
    def title(self) -> None:
        self._delete_info("title")

    @property
    def author(self) -> str | None:
        """Return or set the thread's author (``/I /Author``), or ``None``; ``del`` removes it."""
        return self._read_info("Author")

    @author.setter
    def author(self, value: str) -> None:
        self._write_info("author", value)

    @author.deleter
    def author(self) -> None:
        self._delete_info("author")

    @property
    def subject(self) -> str | None:
        """Return or set the thread's subject (``/I /Subject``), or ``None``; ``del`` removes it."""
        return self._read_info("Subject")

    @subject.setter
    def subject(self, value: str) -> None:
        self._write_info("subject", value)

    @subject.deleter
    def subject(self) -> None:
        self._delete_info("subject")

    @property
    def keywords(self) -> str | None:
        """Return or set the thread's keywords (``/I /Keywords``), or ``None``; ``del`` removes it."""
        return self._read_info("Keywords")

    @keywords.setter
    def keywords(self, value: str) -> None:
        self._write_info("keywords", value)

    @keywords.deleter
    def keywords(self) -> None:
        self._delete_info("keywords")

    @property
    def created(self) -> str | None:
        """Return or set the thread's creation date (``/I /CreationDate``), or ``None``; ``del`` removes it."""
        return self._read_info("CreationDate")

    @created.setter
    def created(self, value: str | datetime) -> None:
        self._write_info("created", value)

    @created.deleter
    def created(self) -> None:
        self._delete_info("created")

    @property
    def modified(self) -> str | None:
        """Return or set the thread's modification date (``/I /ModDate``), or ``None``; ``del`` removes it."""
        return self._read_info("ModDate")

    @modified.setter
    def modified(self, value: str | datetime) -> None:
        self._write_info("modified", value)

    @modified.deleter
    def modified(self) -> None:
        self._delete_info("modified")

    def __eq__(self, other: object) -> bool:
        """Compare document and object identity."""
        if not isinstance(other, Thread):
            return NotImplemented
        return (self._document, self._id) == (other._document, other._id)

    def __hash__(self) -> int:
        """Hash document and object identity."""
        return hash((id(self._document), self._id))

    def __repr__(self) -> str:
        """Return ``Thread(xref=..., title=...)``, or a stale marker."""
        try:
            return f"Thread(xref={self._id[0]}, title={self.title!r})"
        except StaleObjectError:
            return f"<stale Thread xref={self._id[0]}>"


def _normalize_bead_item(item: object) -> BeadSpecification:
    if isinstance(item, BeadSpecification):
        return item
    if isinstance(item, tuple):
        if len(item) != _BEAD_TUPLE_LEN:
            msg = f"bead tuple must be (page, rect), got {len(item)} elements: {item!r}"
            raise ValueError(msg)
        return BeadSpecification(item[0], item[1])
    msg = f"bead specification must be a BeadSpecification or (page, rect) tuple: {item!r}"
    raise TypeError(msg)


def _prepare_info_dict(fields: Mapping[str, object]) -> dict[str, str]:
    entries: dict[str, str] = {}
    total_utf8 = 0
    for attr_name, raw_val in fields.items():
        if raw_val is None:
            continue
        checked, utf8_len = _validate_info_value(attr_name, raw_val)
        total_utf8 += utf8_len
        _check_text_limit(total_utf8, "thread info")
        entries[_THREAD_INFO_KEYS[attr_name]] = checked
    return entries


def _prepare_thread_beads(
    document: Document,
    beads: Iterable[BeadSpecification | tuple[Page | int, Sequence[float]]],
) -> list[tuple[Page, list[float]]]:
    if isinstance(beads, (str, bytes, bytearray, memoryview)) or not isinstance(beads, Iterable):
        msg = f"beads must be an iterable of BeadSpecification or (page, rect) pairs: {type(beads).__name__}"
        raise TypeError(msg)
    raw_items = list(itertools.islice(beads, _MAX_BEADS + 1))
    if not raw_items:
        msg = "beads must contain at least one bead"
        raise ValueError(msg)
    if len(raw_items) > _MAX_BEADS:
        code = "page_entry_count"
        msg = f"a thread cannot hold more than {_MAX_BEADS} beads"
        raise LimitError(code, msg)
    prepared: list[tuple[Page, list[float]]] = []
    for raw in raw_items:
        spec = _normalize_bead_item(raw)
        target_page = _resolve_page_arg(document, spec.page)
        pdf_rect = _display_to_pdf_rect(document, target_page, spec.rect)
        prepared.append((target_page, pdf_rect))
    return prepared


def _collect_thread_removal(
    document: Document,
    removed_threads: Sequence[tuple[int, int]],
) -> tuple[set[int], list[tuple[int, int]], list[int]]:
    thread_xrefs: set[int] = set()
    bead_pairs: list[tuple[int, int]] = []
    extra_xrefs: list[int] = []
    for t_pair in removed_threads:
        thread_xrefs.add(t_pair[0])
        t_dict = Dictionary(document, t_pair)
        i_xref = _indirect_xref(t_dict.get("I"))
        if i_xref is not None:
            extra_xrefs.append(i_xref)
        bead_pairs.extend(_thread_bead_ids(t_dict))
    return thread_xrefs, bead_pairs, extra_xrefs


class Threads(Sequence[Thread]):
    """The document's article threads in ``Catalog["Threads"]`` order, as ``doc.threads`` returns it.

    Backed by an indirect ``Catalog["Threads"]`` array. :meth:`add`,
    :meth:`move`, ``del doc.threads[i]``, and ``del doc.threads`` each update
    the catalog, bead rings, and pages' ``/B`` arrays in one transaction and
    free unreferenced thread and bead objects.
    """

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        """Wrap ``document``; use :attr:`Document.threads`."""
        self._document = document

    def _ids(self) -> list[tuple[int, int]]:
        self._document._ensure_open()  # noqa: SLF001
        return _catalog_thread_ids(self._document)

    def _handles(self) -> list[Thread]:
        return [Thread(self._document, *pair) for pair in self._ids()]

    @overload
    def __getitem__(self, index: int) -> Thread: ...
    @overload
    def __getitem__(self, index: slice) -> list[Thread]: ...
    def __getitem__(self, index: int | slice) -> Thread | list[Thread]:
        """Return one thread, or a list of threads for a slice."""
        return self._handles()[index]

    def __len__(self) -> int:
        """Return the number of article threads in the document."""
        return len(self._ids())

    def __iter__(self) -> Iterator[Thread]:
        """Iterate over the document's article threads."""
        return iter(self._handles())

    def add(  # noqa: PLR0913 - mirrors Thread's six optional Info fields plus index.
        self,
        beads: Iterable[BeadSpecification | tuple[Page | int, Sequence[float]]],
        *,
        index: int | None = None,
        title: str | None = None,
        author: str | None = None,
        subject: str | None = None,
        keywords: str | None = None,
        created: str | datetime | None = None,
        modified: str | datetime | None = None,
    ) -> Thread:
        """Create an article thread from ``beads`` (at least one) and insert it at ``index``.

        Each item of ``beads`` is a :class:`BeadSpecification` or a
        ``(page, rect)`` pair in display coordinates. Optional metadata fields
        populate the thread's ``/I`` dictionary; ``created`` and ``modified``
        accept either a PDF date string or a :class:`datetime`.
        """
        document = self._document
        document._ensure_open()  # noqa: SLF001
        if index is not None and not _is_non_bool_int(index):
            msg = f"index must be an int or None: {index!r}"
            raise TypeError(msg)
        info_entries = _prepare_info_dict(
            {
                "title": title,
                "author": author,
                "subject": subject,
                "keywords": keywords,
                "created": created,
                "modified": modified,
            }
        )
        prepared_beads = _prepare_thread_beads(document, beads)
        existing_threads = _catalog_thread_ids(document)
        if len(existing_threads) >= _MAX_THREADS:
            code = "page_entry_count"
            msg = f"doc.threads cannot hold more than {_MAX_THREADS} threads"
            raise LimitError(code, msg)
        pos = _clamp_insert_index(index, len(existing_threads))

        with document.transaction("thread"):
            _require_version(document, _catalog(document), (1, 1))
            thread_xref = document.objects.add({"Type": Name("Thread")})
            thread_obj = document.objects[thread_xref]
            thread_pair = (thread_xref, thread_obj.generation or 0)

            bead_xrefs = [document.objects.add({"Type": Name("Bead")}) for _ in prepared_beads]
            bead_pairs: list[tuple[int, int]] = []
            count = len(bead_xrefs)
            page_groups: dict[tuple[int, int], list[tuple[int, int]]] = {}
            for idx, (target_page, pdf_rect) in enumerate(prepared_beads):
                b_xref = bead_xrefs[idx]
                b_obj = document.objects[b_xref]
                b_pair = (b_xref, b_obj.generation or 0)
                bead_pairs.append(b_pair)
                bead_entries: dict[str, object] = {
                    "Type": Name("Bead"),
                    "P": target_page.obj,
                    "R": pdf_rect,
                    "V": document.objects[bead_xrefs[(idx - 1) % count]],
                    "N": document.objects[bead_xrefs[(idx + 1) % count]],
                }
                if idx == 0:
                    bead_entries["T"] = thread_obj
                document.objects[b_xref] = bead_entries
                p_pair = (target_page.xref, target_page._generation)  # noqa: SLF001
                page_groups.setdefault(p_pair, []).append(b_pair)

            thread_entries: dict[str, object] = {
                "Type": Name("Thread"),
                "F": document.objects[bead_xrefs[0]],
            }
            if info_entries:
                thread_entries["I"] = info_entries
            document.objects[thread_xref] = thread_entries

            for p_pair, b_list in page_groups.items():
                _append_page_beads(document, Dictionary(document, p_pair), b_list)

            new_threads = [*existing_threads[:pos], thread_pair, *existing_threads[pos:]]
            retired = _write_catalog_threads(document, new_threads)
            if retired:
                _core.object_free_unreferenced(_unlocked(document), retired)
        return Thread(document, *thread_pair)

    def move(self, index: int, to: int) -> None:
        """Move thread ``index`` so that it ends at ``to``, in one transaction."""
        thread_ids = self._ids()
        src = _resolve_seq_index(index, len(thread_ids), "thread index")
        dst = _resolve_seq_index(to, len(thread_ids), "thread index")
        if src == dst:
            return
        new_ids = list(thread_ids)
        moved = new_ids.pop(src)
        new_ids.insert(dst, moved)
        with self._document.transaction("move thread"):
            retired = _write_catalog_threads(self._document, new_ids)
            if retired:
                _core.object_free_unreferenced(_unlocked(self._document), retired)

    def __delitem__(self, index: int | slice) -> None:
        """Remove one thread or a slice of threads, freeing unreferenced thread and bead objects."""
        thread_ids = self._ids()
        if isinstance(index, slice):
            removed_indices = set(range(len(thread_ids))[index])
            if not removed_indices:
                return
        else:
            removed_indices = {_resolve_seq_index(index, len(thread_ids), "thread index")}
        kept_ids = [pair for idx, pair in enumerate(thread_ids) if idx not in removed_indices]
        removed_ids = [thread_ids[idx] for idx in sorted(removed_indices)]
        thread_xrefs, bead_pairs, extra_xrefs = _collect_thread_removal(self._document, removed_ids)
        with self._document.transaction("delete thread"):
            retired_cat = _write_catalog_threads(self._document, kept_ids)
            retired_b = _remove_beads_from_pages(self._document, bead_pairs)
            _free_thread_objects(
                self._document,
                thread_xrefs,
                {p[0] for p in bead_pairs},
                [*extra_xrefs, *retired_cat, *retired_b],
            )

    def __repr__(self) -> str:
        """Return ``Threads([...])``."""
        return f"Threads({self._handles()!r})"


def _delete_all_threads(document: Document) -> None:
    catalog = _catalog(document)
    if "Threads" not in catalog:
        return
    thread_ids = _catalog_thread_ids(document)
    thread_xrefs, bead_pairs, extra_xrefs = _collect_thread_removal(document, thread_ids)
    with document.transaction("threads"):
        retired_cat = _write_catalog_threads(document, ())
        retired_b = _remove_beads_from_pages(document, bead_pairs)
        _free_thread_objects(
            document,
            thread_xrefs,
            {p[0] for p in bead_pairs},
            [*extra_xrefs, *retired_cat, *retired_b],
        )


def _has_catalog_threads(document: Document) -> bool:
    if document.needs_password:
        return False
    return "Threads" in _catalog(document)


def _partition_thread_beads(
    document: Document,
    ring: Sequence[tuple[int, int]],
    removed_pages: set[tuple[int, int]],
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    kept: list[tuple[int, int]] = []
    dropped: list[tuple[int, int]] = []
    for b_pair in ring:
        p_pair = _dict_id(Dictionary(document, b_pair).get("P"))
        if p_pair is None or p_pair in removed_pages:
            dropped.append(b_pair)
        else:
            kept.append(b_pair)
    return kept, dropped


def _clear_page_b_entries(document: Document, pages: Iterable[tuple[int, int]]) -> list[int]:
    retired: list[int] = []
    for p_pair in pages:
        p_dict = Dictionary(document, p_pair)
        if "B" in p_dict:
            b_xref = _indirect_xref(p_dict.get("B"))
            if b_xref is not None:
                retired.append(b_xref)
            del p_dict["B"]
    return retired


def _prune_threads_for_removed_pages(document: Document, removed_pages: set[tuple[int, int]]) -> None:
    """Splice beads on ``removed_pages`` out of ``Catalog['Threads']`` inside an open page transaction."""
    if not removed_pages or not _has_catalog_threads(document):
        return
    thread_ids = _catalog_thread_ids(document)
    kept_threads: list[tuple[int, int]] = []
    removed_thread_xrefs: set[int] = set()
    removed_bead_pairs: list[tuple[int, int]] = []
    extra_xrefs = _clear_page_b_entries(document, removed_pages)

    for t_pair in thread_ids:
        t_dict = Dictionary(document, t_pair)
        ring = _thread_bead_ids(t_dict)
        kept_beads, dropped_beads = _partition_thread_beads(document, ring, removed_pages)
        if not kept_beads:
            removed_thread_xrefs.add(t_pair[0])
            i_xref = _indirect_xref(t_dict.get("I"))
            if i_xref is not None:
                extra_xrefs.append(i_xref)
            removed_bead_pairs.extend(ring)
        else:
            kept_threads.append(t_pair)
            if dropped_beads:
                _relink_bead_ring(document, t_pair, kept_beads)
                removed_bead_pairs.extend(dropped_beads)

    if len(kept_threads) != len(thread_ids) or not kept_threads:
        extra_xrefs.extend(_write_catalog_threads(document, kept_threads))
    if removed_bead_pairs:
        extra_xrefs.extend(_remove_beads_from_pages(document, removed_bead_pairs))
    _free_thread_objects(
        document,
        removed_thread_xrefs,
        {p[0] for p in removed_bead_pairs},
        extra_xrefs,
    )


def _strip_copied_page_beads(document: Document, copied_pages: Iterable[tuple[int, int]]) -> None:
    """Remove inherited ``/B`` arrays from duplicated page objects whose beads belong to the source page."""
    if document.needs_password:
        return
    retired = _clear_page_b_entries(document, copied_pages)
    if retired:
        _core.object_free_unreferenced(_unlocked(document), retired)


def _thread_action_dict(document: Document, action: ThreadAction) -> tuple[dict[str, object], tuple[int, int]]:
    """Serialize ``action`` into an ``ActionThread`` dictionary and its minimum PDF version."""
    entries: dict[str, object] = {
        "Type": Name("Action"),
        "S": Name("Thread"),
    }
    if action.file is not None:
        entries["F"] = action.file
        entries["D"] = action.thread
        if action.bead is not None:
            entries["B"] = action.bead
        return entries, (1, 1)

    if isinstance(action.thread, int):
        threads = document.threads
        if not 0 <= action.thread < len(threads):
            msg = f"thread index {action.thread} is out of range for {len(threads)} threads"
            raise ValueError(msg)
        target_thread = threads[action.thread]
        entries["D"] = target_thread.obj
        if action.bead is not None:
            beads = target_thread.beads
            if not 0 <= action.bead < len(beads):
                msg = f"bead index {action.bead} is out of range for {len(beads)} beads"
                raise ValueError(msg)
            entries["B"] = beads[action.bead].obj
        return entries, (1, 1)

    entries["D"] = action.thread
    if action.bead is not None:
        entries["B"] = action.bead
    return entries, (1, 1)


def _resolve_action_bead_index(
    document: Document,
    raw_b: object,
    *,
    file: str | None,
    resolved_thread_pair: tuple[int, int] | None,
) -> int | None:
    if _is_non_bool_int(raw_b):
        assert isinstance(raw_b, int)  # noqa: S101 - narrows type for ty.
        return raw_b if raw_b >= 0 else None
    if file is not None:
        return None
    b_pair = _dict_id(raw_b)
    if b_pair is None:
        return None
    target_t_pair = resolved_thread_pair
    if target_t_pair is None:
        try:
            _, target_t_pair = _resolve_bead_and_thread(document, b_pair)
        except StaleObjectError:
            return None
    ring = _thread_bead_ids(Dictionary(document, target_t_pair))
    try:
        return ring.index(b_pair)
    except ValueError:
        return None


def _resolve_action_thread_target(
    document: Document,
    raw_d: object,
    *,
    file: str | None,
    thread_index_map: Mapping[tuple[int, int], int] | None,
) -> tuple[int | str, tuple[int, int] | None] | None:
    d_pair = _dict_id(raw_d) if file is None else None
    if d_pair is not None:
        if thread_index_map is None:
            thread_ids = _catalog_thread_ids(document)
            thread_index_map = {pair: idx for idx, pair in enumerate(thread_ids)}
        found_idx = thread_index_map.get(d_pair)
        return None if found_idx is None else (found_idx, d_pair)
    if _is_non_bool_int(raw_d):
        assert isinstance(raw_d, int)  # noqa: S101 - narrows type for ty.
        if raw_d < 0:
            return None
        t_pair: tuple[int, int] | None = None
        if file is None:
            thread_ids = _catalog_thread_ids(document)
            if raw_d < len(thread_ids):
                t_pair = thread_ids[raw_d]
        return raw_d, t_pair
    if isinstance(raw_d, String) and raw_d.text:
        return raw_d.text, None
    return None


def _read_thread_action_dict(
    document: Document,
    action_dict: Dictionary,
    *,
    thread_index_map: Mapping[tuple[int, int], int] | None = None,
) -> ThreadAction | None:
    """Read an ``/S /Thread`` action dictionary into a :class:`ThreadAction`, or ``None`` when unresolved."""
    if action_dict.get("S") != "Thread":
        return None
    file: str | None = None
    if "F" in action_dict:
        file = _read_file_spec(action_dict.get("F"))
        if file is None:
            return None

    resolved_d = _resolve_action_thread_target(
        document,
        action_dict.get("D"),
        file=file,
        thread_index_map=thread_index_map,
    )
    if resolved_d is None:
        return None
    thread_val, resolved_t_pair = resolved_d

    bead_val: int | None = None
    if "B" in action_dict and action_dict.get("B") is not None:
        bead_val = _resolve_action_bead_index(
            document,
            action_dict.get("B"),
            file=file,
            resolved_thread_pair=resolved_t_pair,
        )
        if bead_val is None:
            return None
    return ThreadAction(thread_val, bead_val, file=file)


def _read_thread_action(document: Document, holder: Dictionary) -> ThreadAction | None:
    """Return :class:`ThreadAction` when ``holder['A']`` is a ``/S /Thread`` action, else ``None``."""
    action = holder.get("A")
    if not _is_dict(action):
        return None
    return _read_thread_action_dict(document, cast("Dictionary", action))


for _public in (Bead, BeadSpecification, Beads, Thread, ThreadAction, Threads):
    _public.__module__ = "pyropdf"

__all__ = [
    "Bead",
    "BeadSpecification",
    "Beads",
    "Thread",
    "ThreadAction",
    "Threads",
]
