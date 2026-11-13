# ruff: noqa: INP001, T201 - standalone benchmark script under evidence/.
"""Measure end-to-end Python batch workflows for issue #765 on ``main``.

Exercises the three public workflows named in issue #765 inside explicit
``with doc.transaction():`` blocks:
1. Batch ``OutlineItem.is_open`` toggles (pure writes vs interleaved
   ``if item.children:`` reads) and full ``doc.outline = entries`` replacement;
2. Single-page Tagged PDF canvas commits (varying element counts, showing one
   ``/ParentTree`` write per canvas) vs multi-page Tagged PDF canvas commits;
3. Multi-attachment ``doc.attachments.add(...)`` insertions and deleting one
   attachment from a multi-leaf ``/EmbeddedFiles`` name tree (demonstrating both
   tree flattening and orphaned ``/Kids`` indirect objects).
"""

from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable

CHAPTERS = 64
WARMUP = 2


class PythonRow(TypedDict):
    """One measured end-to-end Python workload."""

    structure: str
    workload: str
    detail: str
    median_us: float
    p90_us: float
    extra: str


def _saved_defects(doc: pyropdf.Document) -> int:
    """Serialize ``doc`` (populating trailer ``/Size``) and count Arlington defects."""
    with pyropdf.open(doc.to_bytes()) as saved:
        return len(saved.check().defects)


def _time_undo(doc: pyropdf.Document, samples: int, fn: Callable[[], None]) -> tuple[float, float]:
    """Run ``fn`` ``samples`` times, undoing outside the timed span."""
    for _ in range(WARMUP):
        fn()
        doc.history.undo()
    durations_us: list[float] = []
    for _ in range(samples):
        t0 = time.perf_counter()
        fn()
        durations_us.append((time.perf_counter() - t0) * 1e6)
        doc.history.undo()
    durations_us.sort()
    mid = durations_us[len(durations_us) // 2]
    p90 = durations_us[((len(durations_us) - 1) * 9) // 10]
    return round(mid, 1), round(p90, 1)


def _build_outline_entries(total: int) -> list[pyropdf.OutlineEntry]:
    """Build a 3-level outline of ``total`` items with ``CHAPTERS`` chapters."""
    per_chapter = total // CHAPTERS
    dest = pyropdf.Destination(0)
    entries: list[pyropdf.OutlineEntry] = []
    for ch in range(CHAPTERS):
        entries.append(pyropdf.OutlineEntry(1, f"Chapter {ch + 1}", dest, is_open=True))
        rest = per_chapter - 1
        sections = max(1, rest // 2)
        remaining = rest
        for sec in range(sections):
            if remaining == 0:
                break
            entries.append(pyropdf.OutlineEntry(2, f"Section {ch + 1}.{sec + 1}", dest, is_open=True))
            remaining -= 1
            if remaining > 0 and (sec + 1 < sections or remaining == 1):
                entries.append(pyropdf.OutlineEntry(3, f"Subsection {ch + 1}.{sec + 1}.1", dest))
                remaining -= 1
        while remaining > 0:
            entries.append(pyropdf.OutlineEntry(3, f"Detail {ch + 1}.{remaining}", dest))
            remaining -= 1
    return entries


def _measure_outline() -> list[PythonRow]:
    """Measure Python outline batch toggles and bulk ``doc.outline = ...``."""
    rows: list[PythonRow] = []
    for total in (512, 2048):
        entries = _build_outline_entries(total)
        doc = pyropdf.Document()
        doc.pages.new()
        doc.outline = entries
        chapters = list(doc.outline)
        samples = 15 if total >= 2048 else 25  # noqa: PLR2004

        for ops in (1, 16, 64):
            subset = chapters[:ops]

            def pure_batch(items: list[pyropdf.OutlineItem] = subset, d: pyropdf.Document = doc) -> None:
                with d.transaction("outline pure"):
                    for item in items:
                        item.is_open = False

            med, p90 = _time_undo(doc, samples, pure_batch)
            rows.append(
                PythonRow(
                    structure="OutlineCount",
                    workload=f"is_open x{ops} (pure)",
                    detail=f"N={total}",
                    median_us=med,
                    p90_us=p90,
                    extra="0 reads in loop",
                )
            )

            def interleaved_batch(items: list[pyropdf.OutlineItem] = subset, d: pyropdf.Document = doc) -> None:
                with d.transaction("outline interleaved"):
                    for item in items:
                        if item.children:
                            item.is_open = False

            med_i, p90_i = _time_undo(doc, samples, interleaved_batch)
            rows.append(
                PythonRow(
                    structure="OutlineCount",
                    workload=f"if item.children: is_open x{ops}",
                    detail=f"N={total}",
                    median_us=med_i,
                    p90_us=p90_i,
                    extra=f"{ops} Open::view() settles",
                )
            )

        closed_entries = [
            pyropdf.OutlineEntry(e.level, e.title, e.destination, is_open=(False if e.level == 1 else e.is_open))
            for e in entries
        ]

        def replace_all(
            d: pyropdf.Document = doc,
            replacement: list[pyropdf.OutlineEntry] = closed_entries,
        ) -> None:
            d.outline = replacement

        med_r, p90_r = _time_undo(doc, samples, replace_all)
        rows.append(
            PythonRow(
                structure="OutlineCount",
                workload="doc.outline = entries",
                detail=f"N={total}",
                median_us=med_r,
                p90_us=p90_r,
                extra="single-pass O(N)",
            )
        )
        if _saved_defects(doc):
            msg = "unexpected structural defects on outline doc"
            raise RuntimeError(msg)
        doc.close()
    return rows


def _measure_tagged_canvas() -> list[PythonRow]:
    """Measure single-page vs multi-page Tagged PDF canvas commits."""
    rows: list[PythonRow] = []
    black = pyropdf.Color(0.1, 0.2, 0.3)

    # 1. Single canvas with M tagged elements: commits once -> 1 ParentTree update.
    for elems in (10, 50, 200):
        doc = pyropdf.Document()
        page = doc.pages.new()

        def draw_single(p: pyropdf.Page = page, count: int = elems) -> None:
            with p.draw() as canvas:
                for idx in range(count):
                    y = 20.0 + (idx % 35) * 20.0
                    with canvas.tag("P"):
                        canvas.rect((20.0, y, 100.0, y + 12.0), fill=black)

        med, p90 = _time_undo(doc, 21, draw_single)
        draw_single()
        struct_root = doc.catalog["StructTreeRoot"]
        if not isinstance(struct_root, pyropdf.Dictionary):
            msg = "expected StructTreeRoot dictionary"
            raise TypeError(msg)
        next_key = struct_root["ParentTreeNextKey"]
        defects = _saved_defects(doc)
        rows.append(
            PythonRow(
                structure="ParentTree",
                workload=f"1 canvas, {elems} tag('P') elements",
                detail="1 page",
                median_us=med,
                p90_us=p90,
                extra=f"ParentTreeNextKey={next_key}, defects={defects}",
            )
        )
        doc.close()

    # 2. Multi-page transaction: P canvases inside `with doc.transaction():`.
    for pages_n in (10, 50):
        doc = pyropdf.Document()
        for _ in range(pages_n):
            doc.pages.new()
        page_list = list(doc.pages)

        def draw_multi(
            d: pyropdf.Document = doc,
            plist: list[pyropdf.Page] = page_list,
        ) -> None:
            with d.transaction("tagged pages"):
                for p in plist:
                    with p.draw() as canvas, canvas.tag("P"):
                        canvas.rect((20.0, 40.0, 120.0, 54.0), fill=black)

        med, p90 = _time_undo(doc, 15, draw_multi)
        draw_multi()
        struct_root = doc.catalog["StructTreeRoot"]
        if not isinstance(struct_root, pyropdf.Dictionary):
            msg = "expected StructTreeRoot dictionary"
            raise TypeError(msg)
        next_key = struct_root["ParentTreeNextKey"]
        defects = _saved_defects(doc)
        rows.append(
            PythonRow(
                structure="ParentTree",
                workload=f"{pages_n} canvases x 4-op tag('P') in tx",
                detail=f"{pages_n} pages",
                median_us=med,
                p90_us=p90,
                extra=f"ParentTreeNextKey={next_key}, defects={defects}",
            )
        )
        doc.close()
    return rows


def _build_multi_leaf_attachments_pdf() -> tuple[bytes, list[int]]:
    """Construct a valid PDF whose ``/EmbeddedFiles`` name tree has 4 ``/Kids`` leaves."""
    doc = pyropdf.Document()
    doc.pages.new()
    specs: list[tuple[str, pyropdf.Dictionary]] = []
    for i in range(16):
        name = f"seed_{i:02d}.txt"
        doc.attachments.add(name, f"seed-{i}".encode())
    # Inspect current flat /EmbeddedFiles entries and Partition into 4 indirect /Kids leaves.
    names_dict = doc.catalog["Names"]
    if not isinstance(names_dict, pyropdf.Dictionary):
        msg = "expected /Names dictionary"
        raise TypeError(msg)
    ef_tree = names_dict["EmbeddedFiles"]
    if not isinstance(ef_tree, pyropdf.Dictionary):
        msg = "expected /EmbeddedFiles dictionary"
        raise TypeError(msg)
    raw_pairs = list(ef_tree["Names"])  # type: ignore[arg-type]
    for k, v in zip(raw_pairs[::2], raw_pairs[1::2], strict=True):
        if not isinstance(k, pyropdf.String) or not isinstance(v, pyropdf.Dictionary):
            msg = "unexpected pair in /Names"
            raise TypeError(msg)
        specs.append((k.text, v))

    kid_xrefs: list[int] = []
    kids_array: list[pyropdf.Dictionary] = []
    for chunk_start in range(0, 16, 4):
        chunk = specs[chunk_start : chunk_start + 4]
        flat: list[object] = []
        for k_text, fs in chunk:
            flat.extend((pyropdf.String(k_text.encode()), fs))
        leaf_xref = doc.objects.add(
            {
                "Limits": [pyropdf.String(chunk[0][0].encode()), pyropdf.String(chunk[-1][0].encode())],
                "Names": flat,
            }
        )
        kid_xrefs.append(leaf_xref)
        leaf_obj = doc.objects[leaf_xref]
        if not isinstance(leaf_obj, pyropdf.Dictionary):
            msg = "expected indirect leaf dictionary"
            raise TypeError(msg)
        kids_array.append(leaf_obj)

    del ef_tree["Names"]
    ef_tree["Kids"] = kids_array
    data = doc.to_bytes()
    doc.close()
    with pyropdf.open(data) as check_doc:
        if check_doc.check().defects:
            msg = f"seeded multi-leaf tree has defects: {check_doc.check().defects}"
            raise RuntimeError(msg)
    return data, kid_xrefs


def _measure_attachments() -> list[PythonRow]:
    """Measure batch ``doc.attachments.add`` and multi-leaf flatten behavior."""
    rows: list[PythonRow] = []
    for count in (10, 50):
        doc = pyropdf.Document()
        doc.pages.new()
        items = [(f"file_{i:03d}.bin", b"0123456789abcdef") for i in range(count)]

        def add_batch(
            d: pyropdf.Document = doc,
            batch: list[tuple[str, bytes]] = items,
        ) -> None:
            with d.transaction("attachments add"):
                for name, payload in batch:
                    d.attachments.add(name, payload)

        med, p90 = _time_undo(doc, 15, add_batch)
        rows.append(
            PythonRow(
                structure="TreeLimits",
                workload=f"doc.attachments.add x{count} in tx",
                detail="initial=0",
                median_us=med,
                p90_us=p90,
                extra=f"{count} _writable() Open::view() settles",
            )
        )
        doc.close()

    # Verify what happens to 4 indirect `/Kids` leaves when 1 attachment is deleted.
    pdf_bytes, kid_xrefs = _build_multi_leaf_attachments_pdf()
    doc = pyropdf.open(pdf_bytes)
    del doc.attachments["seed_05.txt"]
    surviving_orphans = sum(1 for xref in kid_xrefs if doc.objects.get(xref) is not None)
    ef_after = doc.catalog["Names"]["EmbeddedFiles"]  # type: ignore[index]
    has_kids_after = "Kids" in ef_after  # type: ignore[operator]
    defects_after = _saved_defects(doc)
    doc.close()
    rows.append(
        PythonRow(
            structure="TreeLimits",
            workload="del doc.attachments['seed_05.txt'] on 4-leaf /Kids tree",
            detail="initial=16 (4 leaves)",
            median_us=0.0,
            p90_us=0.0,
            extra=f"has_kids={has_kids_after}, orphaned_kids={surviving_orphans}/4, defects={defects_after}",
        )
    )
    return rows


def main() -> None:
    """Run all Python measurements, optionally writing JSON to ``sys.argv[1]``."""
    rows = [
        *_measure_outline(),
        *_measure_tagged_canvas(),
        *_measure_attachments(),
    ]
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")

    print("## End-to-End Python Measurements (`measure_python.py` on `main`)\n")
    print(f"- Python: {platform.python_version()} ({platform.system()} {platform.machine()})\n")
    print("| Structure | Workload | Detail | Median (µs) | p90 (µs) | Observation |")
    print("| :-------- | :------- | :----- | ----------: | -------: | :---------- |")
    for r in rows:
        print(
            f"| `{r['structure']}` | `{r['workload']}` | {r['detail']} "
            f"| {r['median_us']:.1f} | {r['p90_us']:.1f} | {r['extra']} |"
        )


if __name__ == "__main__":
    main()
