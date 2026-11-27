# ruff: noqa: INP001, T201 - standalone profiling script that prints tables.
"""Benchmark and profile text, word, layout, and search extraction across the corpus.

Usage:
    python evidence/text-extraction-perf/profile_text.py measure \
        --engine pyropdf --out after.json tests/assets/real_world/*.pdf

    python evidence/text-extraction-perf/profile_text.py compare \
        --before before.json --after after.json --pymupdf pymupdf.json \
        --out comparison.json

Each document is reopened for every timed sample so pyropdf's per-version
page memo does not mask cold extraction cost. The script also records a
second warm pass on the same open document to separate one-time font/cache
setup from per-page extraction and Python object construction, and hashes
extracted text, words, and search hits to verify bit-for-bit output parity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_RUNS = 5
NEEDLE = "the"


def _time_operation(
    opener: Callable[[Path], Any],
    operation: Callable[[Any], object],
    path: Path,
    runs: int,
) -> float:
    """Return the best elapsed time in milliseconds over ``runs`` fresh opens."""
    best = float("inf")
    for _ in range(runs):
        doc = opener(path)
        start = time.perf_counter()
        operation(doc)
        best = min(best, time.perf_counter() - start)
        doc.close()
    return round(best * 1000.0, 3)


def _measure_pyropdf(files: list[Path], runs: int) -> dict[str, Any]:
    import pyropdf  # noqa: PLC0415 - loaded only when measuring pyropdf

    warnings.filterwarnings("ignore", category=pyropdf.PyropdfWarning)

    ops: dict[str, Callable[[Any], object]] = {
        "text": lambda doc: [page.text() for page in doc.pages],
        "doc_text": lambda doc: doc.text(),
        "words": lambda doc: [page.words() for page in doc.pages],
        "layout": lambda doc: [page.layout() for page in doc.pages],
        "search": lambda doc: [page.search(NEEDLE) for page in doc.pages],
    }

    documents: dict[str, dict[str, Any]] = {}
    for path in files:
        timings = {name: _time_operation(pyropdf.open, fn, path, runs) for name, fn in ops.items()}
        # Single-document parity digests and counts.
        doc = pyropdf.open(path)
        pages = list(doc.pages)
        page_texts = [page.text() for page in pages]
        page_words = [page.words() for page in pages]
        page_hits = [page.search(NEEDLE) for page in pages]
        # Warm pass on an already-open document (measures memo/cache reuse).
        warm_start = time.perf_counter()
        _ = [page.words() for page in pages]
        warm_words_ms = round((time.perf_counter() - warm_start) * 1000.0, 3)
        doc.close()

        text_digest = hashlib.sha256("\x00".join(page_texts).encode("utf-8")).hexdigest()[:16]
        words_digest = hashlib.sha256(
            json.dumps(
                [[(w.text, tuple(w.bbox), w.block, w.line, w.index) for w in words] for words in page_words],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        search_digest = hashlib.sha256(
            json.dumps([[tuple(r) for r in hits] for hits in page_hits], separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]

        documents[path.name] = {
            "pages": len(pages),
            "chars": sum(len(text) for text in page_texts),
            "words_count": sum(len(words) for words in page_words),
            "timings_ms": timings,
            "warm_words_ms": warm_words_ms,
            "digests": {"text": text_digest, "words": words_digest, "search": search_digest},
        }
        print(
            f"pyropdf {path.stem:30s} pages={len(pages):3d} "
            + " ".join(f"{key}={value:.2f}ms" for key, value in timings.items())
        )

    totals = {key: round(sum(entry["timings_ms"][key] for entry in documents.values()), 3) for key in ops}
    return {
        "engine": f"pyropdf {pyropdf.__version__}",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "runs": runs,
        "totals_ms": totals,
        "documents": documents,
    }


def _measure_pymupdf(files: list[Path], runs: int) -> dict[str, Any]:
    import pymupdf  # type: ignore[import-not-found]  # noqa: PLC0415 - optional oracle venv

    ops: dict[str, Callable[[Any], object]] = {
        "text": lambda doc: [page.get_text() for page in doc],
        "words": lambda doc: [page.get_text("words") for page in doc],
        "search": lambda doc: [page.search_for(NEEDLE) for page in doc],
    }

    documents: dict[str, dict[str, Any]] = {}
    for path in files:
        timings = {name: _time_operation(pymupdf.open, fn, path, runs) for name, fn in ops.items()}
        doc = pymupdf.open(path)
        page_count = doc.page_count
        doc.close()
        documents[path.name] = {"pages": page_count, "timings_ms": timings}
        print(
            f"pymupdf {path.stem:30s} pages={page_count:3d} "
            + " ".join(f"{key}={value:.2f}ms" for key, value in timings.items())
        )

    totals = {key: round(sum(entry["timings_ms"][key] for entry in documents.values()), 3) for key in ops}
    return {
        "engine": f"PyMuPDF {pymupdf.__version__} (MuPDF {pymupdf.mupdf_version})",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "runs": runs,
        "totals_ms": totals,
        "documents": documents,
    }


def _compare(before_path: Path, after_path: Path, pymupdf_path: Path | None, out_path: Path | None) -> int:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    pymupdf_data = json.loads(pymupdf_path.read_text(encoding="utf-8")) if pymupdf_path else None

    mismatches: list[str] = []
    for name, before_doc in before["documents"].items():
        after_doc = after["documents"].get(name)
        if after_doc is None or before_doc["digests"] != after_doc["digests"]:
            mismatches.append(name)

    print(f"Parity mismatches: {len(mismatches)} {mismatches}")
    summary: dict[str, Any] = {
        "before_platform": before["platform"],
        "after_platform": after["platform"],
        "parity_mismatches": mismatches,
        "totals_ms": {},
        "documents": {},
    }

    for op in ("text", "doc_text", "words", "layout", "search"):
        b_total = before["totals_ms"][op]
        a_total = after["totals_ms"][op]
        speedup = round(b_total / a_total, 3) if a_total > 0 else 0.0
        entry: dict[str, Any] = {"before": b_total, "after": a_total, "speedup": speedup}
        ratio_note = ""
        if pymupdf_data and op in pymupdf_data["totals_ms"]:
            m_total = pymupdf_data["totals_ms"][op]
            entry["pymupdf"] = m_total
            entry["ratio_before"] = round(b_total / m_total, 3)
            entry["ratio_after"] = round(a_total / m_total, 3)
            ratio_note = f"  vs PyMuPDF ({m_total:.2f}ms): {b_total / m_total:.2f}x -> {a_total / m_total:.2f}x"
        summary["totals_ms"][op] = entry
        print(f"TOTAL {op:8s}: {b_total:7.2f}ms -> {a_total:7.2f}ms ({speedup:.2f}x){ratio_note}")

    for name in sorted(before["documents"]):
        b_t = before["documents"][name]["timings_ms"]
        a_t = after["documents"][name]["timings_ms"]
        doc_entry: dict[str, Any] = {
            "pages": after["documents"][name]["pages"],
            "chars": after["documents"][name]["chars"],
            "words_count": after["documents"][name]["words_count"],
            "text_before_ms": b_t["text"],
            "text_after_ms": a_t["text"],
            "text_speedup": round(b_t["text"] / a_t["text"], 3) if a_t["text"] > 0 else 0.0,
            "words_before_ms": b_t["words"],
            "words_after_ms": a_t["words"],
            "words_speedup": round(b_t["words"] / a_t["words"], 3) if a_t["words"] > 0 else 0.0,
            "search_before_ms": b_t["search"],
            "search_after_ms": a_t["search"],
            "search_speedup": round(b_t["search"] / a_t["search"], 3) if a_t["search"] > 0 else 0.0,
        }
        if pymupdf_data and name in pymupdf_data["documents"]:
            m_t = pymupdf_data["documents"][name]["timings_ms"]
            doc_entry["pymupdf_text_ms"] = m_t["text"]
            doc_entry["pymupdf_words_ms"] = m_t["words"]
            doc_entry["pymupdf_search_ms"] = m_t["search"]
        summary["documents"][name] = doc_entry

    if out_path is not None:
        out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 1 if mismatches else 0


def main() -> int:
    """Parse arguments and run ``measure`` or ``compare``."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    measure = subparsers.add_parser("measure", help="Benchmark one engine across PDF files")
    measure.add_argument("--engine", choices=("pyropdf", "pymupdf"), required=True)
    measure.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    measure.add_argument("--out", type=Path, required=True)
    measure.add_argument("files", nargs="+", type=Path)

    compare = subparsers.add_parser("compare", help="Compare before/after JSON files and check parity")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    compare.add_argument("--pymupdf", type=Path)
    compare.add_argument("--out", type=Path)

    args = parser.parse_args()
    if args.command == "measure":
        files = sorted(args.files)
        data = _measure_pyropdf(files, args.runs) if args.engine == "pyropdf" else _measure_pymupdf(files, args.runs)
        args.out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return 0
    return _compare(args.before, args.after, args.pymupdf, args.out)


if __name__ == "__main__":
    sys.exit(main())
