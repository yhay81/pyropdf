# ruff: noqa: INP001 - standalone comparison script under evidence/.
r"""Compare pyropdf ``doc.markdown()``/``doc.to_chunks()`` against ``pymupdf4llm`` in isolated environments.

``pymupdf4llm`` and ``pymupdf-layout`` are AGPL and never belong in pyropdf's
dependencies. Run each collector in its own Python environment and score the
captured JSON files together::

    uv run python evidence/pymupdf-comparison/compare_pymupdf4llm.py collect pyropdf /tmp/pyropdf-md.json
    target/pymupdf4llm-venv/bin/python evidence/pymupdf-comparison/compare_pymupdf4llm.py \
        collect pymupdf4llm /tmp/mupdf4llm.json
    target/pymupdf4llm-venv/bin/python evidence/pymupdf-comparison/compare_pymupdf4llm.py \
        collect pymupdf4llm-layout /tmp/mupdf4llm-layout.json
    uv run python evidence/pymupdf-comparison/compare_pymupdf4llm.py score \
        evidence/pymupdf-comparison/results/pymupdf4llm COMMIT \
        /tmp/pyropdf-md.json /tmp/mupdf4llm.json /tmp/mupdf4llm-layout.json
"""

from __future__ import annotations

import itertools
import json
import platform
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CORPUS = ROOT / "tests" / "assets" / "real_world"
QUALITY_DIR = HERE / "quality"

_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$")


#: Pages scored for Markdown & chunk fidelity: every native-text ground-truth label.
def native_labels() -> list[Path]:
    """Return every native-text ground-truth label JSON sorted by stem."""
    found: list[Path] = []
    for path in sorted(QUALITY_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("mode", "native") == "native":
            found.append(path)
    return found


def normalize(text: str) -> str:
    """Normalize text with NFKC and strip whitespace for cross-engine comparison."""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _markdown_table_rows(markdown: str) -> list[list[str]]:
    """Extract data rows from GitHub-Flavored Markdown pipe tables."""
    rows: list[list[str]] = []
    lines = markdown.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|") or "|" not in stripped[1:]:
            continue
        if _TABLE_SEP_RE.match(stripped):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        rows.append(cells)
    return rows


def _score_anchors(anchors: list[str], text: str) -> dict[str, Any]:
    flat = normalize(text)
    positions = [flat.find(normalize(anchor)) for anchor in anchors]
    found = [(anchor, pos) for anchor, pos in zip(anchors, positions, strict=True) if pos >= 0]
    pairs = list(itertools.pairwise(found))
    inversions = [(first, second) for (first, a), (second, b) in pairs if a > b]
    missing = [anchor for anchor, pos in zip(anchors, positions, strict=True) if pos < 0]
    return {
        "anchors": len(anchors),
        "found": len(found),
        "pairs": len(pairs),
        "in_order": len(pairs) - len(inversions),
        "missing": missing,
        "inversions": inversions,
    }


def _score_markdown_table(expected_table: dict[str, Any], markdown: str) -> dict[str, Any]:
    rows = _markdown_table_rows(markdown)
    by_first = {normalize(row[0]): row for row in rows if row}
    expected_rows: dict[str, list[str]] = expected_table["rows"]
    rows_found = 0
    cells_right = 0
    total_cells = sum(len(cells) for cells in expected_rows.values())
    for key, expected_cells in expected_rows.items():
        got = by_first.get(normalize(key))
        if got is None:
            continue
        rows_found += 1
        cells_right += sum(
            1 for want, actual in zip(expected_cells, got, strict=False) if normalize(want) == normalize(actual)
        )
    return {
        "rows": len(expected_rows),
        "rows_found": rows_found,
        "cells": total_cells,
        "cells_right": cells_right,
        "pipe_rows": len(rows),
    }


def score_page(label: dict[str, Any], obs: dict[str, Any]) -> dict[str, Any]:
    """Score one engine's Markdown and chunk output against one ground-truth label."""
    md = obs["markdown"]
    chunk_texts: list[str] = obs["chunks"]
    joined_chunks = "\n".join(chunk_texts)
    entry: dict[str, Any] = {
        "anchors": _score_anchors(label["anchors"], md),
        "chunk_anchors": _score_anchors(label["anchors"], joined_chunks),
        "headings": len(_HEADING_RE.findall(md)),
        "lists": len(_LIST_RE.findall(md)),
        "pipe_rows": len(_markdown_table_rows(md)),
        "chunks": len(chunk_texts),
        "max_chunk_chars": max((len(c) for c in chunk_texts), default=0),
        "markdown_ms": obs["markdown_ms"],
        "chunks_ms": obs["chunks_ms"],
    }
    if "table" in label:
        entry["table"] = _score_markdown_table(label["table"], md)
    return entry


def _time_best(fn: Callable[[], _T], *, runs: int = 3) -> tuple[_T, float]:
    best_ms = float("inf")
    value: _T | None = None
    for _ in range(runs):
        start = time.perf_counter()
        value = fn()
        elapsed = (time.perf_counter() - start) * 1000.0
        best_ms = min(best_ms, elapsed)
    return value, round(best_ms, 2)  # type: ignore[return-value]


def _collect_pyropdf(paths: list[Path]) -> dict[str, Any]:
    import pyropdf  # noqa: PLC0415

    pages: dict[str, Any] = {}
    for path in paths:
        label = json.loads(path.read_text(encoding="utf-8"))
        source = CORPUS / label["document"]
        page_idx = label["page"] - 1

        def run_md(src: Path = source, idx: int = page_idx) -> str:
            with pyropdf.open(src) as doc:
                return doc.markdown(pages=[idx])

        def run_chunks(src: Path = source, idx: int = page_idx) -> list[str]:
            with pyropdf.open(src) as doc:
                return [chunk.text for chunk in doc.to_chunks(pages=[idx], max_chars=2000)]

        md, md_ms = _time_best(run_md)
        chunks, chunks_ms = _time_best(run_chunks)
        pages[path.stem] = {"markdown": md, "chunks": chunks, "markdown_ms": md_ms, "chunks_ms": chunks_ms}
    return {
        "engine": "pyropdf",
        "version": f"pyropdf {pyropdf.__version__}",
        "python": platform.python_version(),
        "pages": pages,
    }


def _collect_pymupdf4llm(paths: list[Path], *, with_layout: bool) -> dict[str, Any]:
    import pymupdf  # noqa: PLC0415
    import pymupdf4llm  # noqa: PLC0415

    pymupdf4llm.use_layout(with_layout)
    mode_tag = "with pymupdf-layout" if with_layout else "without pymupdf-layout"
    pages: dict[str, Any] = {}
    for path in paths:
        label = json.loads(path.read_text(encoding="utf-8"))
        source = str(CORPUS / label["document"])
        page_idx = label["page"] - 1

        def run_md(src: str = source, idx: int = page_idx) -> str:
            return str(pymupdf4llm.to_markdown(src, pages=[idx], use_ocr=False))

        def run_chunks(src: str = source, idx: int = page_idx) -> list[str]:
            raw = pymupdf4llm.to_markdown(src, pages=[idx], page_chunks=True, use_ocr=False)
            return [str(item["text"]) for item in raw]

        md, md_ms = _time_best(run_md)
        chunks, chunks_ms = _time_best(run_chunks)
        pages[path.stem] = {"markdown": md, "chunks": chunks, "markdown_ms": md_ms, "chunks_ms": chunks_ms}
    return {
        "engine": "pymupdf4llm-layout" if with_layout else "pymupdf4llm",
        "version": f"pymupdf4llm {pymupdf4llm.__version__} (PyMuPDF {pymupdf.__version__}, {mode_tag})",
        "python": platform.python_version(),
        "pages": pages,
    }


def collect(engine: str, out: str) -> None:
    """Collect Markdown and chunk outputs for all native labeled pages."""
    paths = native_labels()
    if engine == "pyropdf":
        payload = _collect_pyropdf(paths)
    elif engine == "pymupdf4llm":
        payload = _collect_pymupdf4llm(paths, with_layout=False)
    elif engine == "pymupdf4llm-layout":
        payload = _collect_pymupdf4llm(paths, with_layout=True)
    else:
        msg = f"unknown engine {engine!r}; expected pyropdf, pymupdf4llm, or pymupdf4llm-layout"
        raise SystemExit(msg)
    Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _render_markdown(
    commit: str,
    versions: list[str],
    totals: dict[str, dict[str, float]],
    pages: dict[str, dict[str, Any]],
) -> str:
    summary_header = (
        "| Engine | MD anchors | Ordered pairs | Chunk anchors | MD table cells "
        "| Headings | Lists | Pipe rows | MD ms | Chunks ms |"
    )
    per_page_header = (
        "| Page | Engine | MD anchors | Ordered pairs | Chunk anchors "
        "| Table cells | Headings | Pipe rows | Chunks | MD ms |"
    )
    lines = [
        f"# Markdown and retrieval chunk comparison: pyropdf vs pymupdf4llm ({commit})",
        "",
        "Measured on native-text labeled pages (`evidence/pymupdf-comparison/quality/*.json`), including",
        "Japanese business forms (`mhlw-deposit-report-p1`, `mhlw-doc-p1`, `mhlw-doc-p2`), tax forms,",
        "wide and rotated tables, two-column scans with OCR layers, and prose. `pymupdf4llm` runs strictly",
        "in an isolated temporary venv outside pyropdf's dependencies.",
        "",
        "## Summary across all scored pages",
        "",
        summary_header,
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for version in versions:
        t = totals[version]
        lines.append(
            f"| {version} | {int(t['anchors_found'])}/{int(t['anchors'])} "
            f"| {int(t['in_order'])}/{int(t['pairs'])} "
            f"| {int(t['chunk_found'])}/{int(t['anchors'])} "
            f"| {int(t['table_cells_right'])}/{int(t['table_cells'])} "
            f"| {int(t['headings'])} | {int(t['lists'])} | {int(t['pipe_rows'])} "
            f"| {t['markdown_ms']:.1f} | {t['chunks_ms']:.1f} |"
        )

    lines += [
        "",
        "## Per-page results",
        "",
        per_page_header,
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for stem, page_data in pages.items():
        for version in versions:
            s = page_data[version]
            table_cell = f"{s['table']['cells_right']}/{s['table']['cells']}" if "table" in s else "—"
            lines.append(
                f"| {stem} | {version} | {s['anchors']['found']}/{s['anchors']['anchors']} "
                f"| {s['anchors']['in_order']}/{s['anchors']['pairs']} "
                f"| {s['chunk_anchors']['found']}/{s['chunk_anchors']['anchors']} "
                f"| {table_cell} | {s['headings']} | {s['pipe_rows']} | {s['chunks']} | {s['markdown_ms']:.1f} |"
            )

    lines += ["", "## Missing anchors and order inversions", ""]
    for stem, page_data in pages.items():
        for version in versions:
            a = page_data[version]["anchors"]
            lines.extend(f"- {stem}, {version}: missing anchor `{missing}`" for missing in a["missing"])
            lines.extend(f"- {stem}, {version}: `{second}` precedes `{first}`" for first, second in a["inversions"])

    return "\n".join(lines) + "\n"


def build_report(
    commit: str,
    results: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Score collected observations and return ``(json_payload, markdown_report)``."""
    versions = [item["version"] for item in results]
    pages: dict[str, dict[str, Any]] = {}
    totals: dict[str, dict[str, float]] = {
        v: {
            "anchors_found": 0,
            "anchors": 0,
            "in_order": 0,
            "pairs": 0,
            "chunk_found": 0,
            "table_cells_right": 0,
            "table_cells": 0,
            "headings": 0,
            "lists": 0,
            "pipe_rows": 0,
            "markdown_ms": 0.0,
            "chunks_ms": 0.0,
        }
        for v in versions
    }

    for stem, label in sorted(labels.items()):
        pages[stem] = {"document": label["document"], "page": label["page"], "checked": label.get("checked", False)}
        for item in results:
            version = item["version"]
            scored = score_page(label, item["pages"][stem])
            pages[stem][version] = scored
            t = totals[version]
            t["anchors_found"] += scored["anchors"]["found"]
            t["anchors"] += scored["anchors"]["anchors"]
            t["in_order"] += scored["anchors"]["in_order"]
            t["pairs"] += scored["anchors"]["pairs"]
            t["chunk_found"] += scored["chunk_anchors"]["found"]
            t["headings"] += scored["headings"]
            t["lists"] += scored["lists"]
            t["pipe_rows"] += scored["pipe_rows"]
            t["markdown_ms"] += scored["markdown_ms"]
            t["chunks_ms"] += scored["chunks_ms"]
            if "table" in scored:
                t["table_cells_right"] += scored["table"]["cells_right"]
                t["table_cells"] += scored["table"]["cells"]

    payload = {"commit": commit, "engines": versions, "totals": totals, "pages": pages}
    return payload, _render_markdown(commit, versions, totals, pages)


def score(stem: str, commit: str, *inputs: str) -> None:
    """Score collected engine JSON files and write ``<stem>.json`` and ``<stem>.md``."""
    results = [json.loads(Path(name).read_text(encoding="utf-8")) for name in inputs]
    labels = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in native_labels()}
    payload, markdown = build_report(commit, results, labels)
    Path(f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    Path(f"{stem}.md").write_text(markdown, encoding="utf-8")


def main() -> None:
    """CLI entry point: ``collect <engine> <out>`` or ``score <stem> <commit> <inputs...>``."""
    if len(sys.argv) < 3:  # noqa: PLR2004
        sys.exit("Usage: compare_pymupdf4llm.py collect <engine> <out> | score <stem> <commit> <files...>")
    command, *rest = sys.argv[1:]
    if command == "collect":
        collect(rest[0], rest[1])
    elif command == "score":
        score(rest[0], rest[1], *rest[2:])
    else:
        sys.exit(f"Unknown command: {command!r}")


if __name__ == "__main__":
    main()
