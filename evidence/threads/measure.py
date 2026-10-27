# ruff: noqa: INP001 - a script, not a package.
"""Render the pages of one document in 1 to 8 threads, and time each run.

Every thread renders its share of the pages of one shared ``Document``
through ``page.pixmap``, which releases the GIL while hayro renders, so the
speed-up shows on GIL-enabled CPython too; on a free-threaded build the
Python around each render runs in parallel as well. Each configuration runs
five times and the median is reported, after one warm-up run.

    uv run python evidence/threads/measure.py
"""

from __future__ import annotations

import os
import platform
import statistics
import sys
import sysconfig
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyropdf

ASSETS = Path(__file__).parents[2] / "tests" / "assets" / "real_world"
FILES = ("bill-hr815.pdf", "usrguide.pdf")
THREADS = (1, 2, 4, 8)
RUNS = 5
DPI = 72


def run(pages: list[pyropdf.Page], threads: int) -> float:
    """Render ``pages`` from a pool of ``threads`` threads; return the seconds taken."""
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=threads) as executor:
        for _ in executor.map(lambda page: page.pixmap(dpi=DPI), pages):
            pass
    return time.perf_counter() - start


def main() -> None:
    """Write a Markdown table of the timings to standard output."""
    free_threaded = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    build = " free-threaded" if free_threaded else ""
    lines = [
        f"Python {platform.python_version()}{build}, {platform.machine()}, {os.cpu_count()} CPUs",
        f"pages rendered at {DPI} dpi; median of {RUNS} runs",
        "",
        "| File | Pages | Threads | Seconds | Speed-up |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name in FILES:
        doc = pyropdf.open((ASSETS / name).read_bytes())
        pages = list(doc.pages)
        run(pages, max(THREADS))
        base = None
        for threads in THREADS:
            seconds = statistics.median(run(pages, threads) for _ in range(RUNS))
            base = base or seconds
            lines.append(f"| {name} | {len(pages)} | {threads} | {seconds:.3f} | {base / seconds:.2f}x |")
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
