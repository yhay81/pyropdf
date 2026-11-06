# ruff: noqa: INP001, T201 - standalone benchmark worker
"""Measure peak process RSS for one or two concurrent complete document renders.

Run once per document/DPI/caller count with the Python of each wheel. Each
invocation starts fresh. The callers open independent documents and release
each page's raw RGB copy before rendering the next page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import runpy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pyropdf
from pyropdf import _core

ROOT = Path(__file__).resolve().parents[2]


def render(path: Path, dpi: int, barrier: Barrier) -> tuple[int, int]:
    """Synchronize callers at each page and account for every raw byte."""
    total = 0
    with pyropdf.open(path) as document:
        for page in document.pages:
            barrier.wait(timeout=120)
            total += len(bytes(page.pixmap(dpi=dpi)))
        return len(document.pages), total


def main() -> None:
    """Print a JSON record with the loaded artifact and process peak."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", choices=("bill-hr815", "usrguide"))
    parser.add_argument("dpi", type=int, choices=(72, 144))
    parser.add_argument("callers", type=int, choices=(1, 2))
    args = parser.parse_args()
    path = ROOT / "tests/assets/real_world" / f"{args.document}.pdf"
    memory = runpy.run_path(str(ROOT / "evidence/path-columns/measure.py"))["memory"]
    barrier = Barrier(args.callers)
    before, _ = memory()
    with ThreadPoolExecutor(max_workers=args.callers) as pool:
        futures = [pool.submit(render, path, args.dpi, barrier) for _ in range(args.callers)]
        results = [future.result() for future in futures]
    current, peak = memory()
    print(
        json.dumps(
            {
                "document": args.document,
                "dpi": args.dpi,
                "callers": args.callers,
                "python": platform.python_version(),
                "extension_sha256": hashlib.sha256(Path(_core.__file__).read_bytes()).hexdigest(),
                "pages_and_bytes_per_caller": results,
                "rss_before_bytes": before,
                "rss_after_bytes": current,
                "peak_rss_bytes": peak,
            }
        )
    )


if __name__ == "__main__":
    main()
