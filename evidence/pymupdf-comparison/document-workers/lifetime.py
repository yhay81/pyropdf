# ruff: noqa: INP001, T201, S603 - standalone Windows process probe
"""Exercise two open documents, cross-thread close, permit reuse, and drop."""

from __future__ import annotations

import gc
import json
import os
import runpy
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyropdf

ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    """Count native threads in the caller process at each lifecycle boundary."""
    shell = shutil.which("pwsh")
    if shell is None:
        message = "This Windows probe needs PowerShell 7"
        raise RuntimeError(message)
    path = ROOT / "tests/assets/real_world/bill-hr815.pdf"
    memory = runpy.run_path(str(ROOT / "evidence/path-columns/measure.py"))["memory"]
    readings = {}

    def observe(stage: str) -> int:
        command = f"(Get-Process -Id {os.getpid()}).Threads.Count"
        threads = int(subprocess.check_output([shell, "-NoProfile", "-NonInteractive", "-Command", command], text=True))
        current, peak = memory()
        readings[stage] = {"threads": threads, "rss_bytes": current, "peak_rss_bytes": peak}
        return threads

    def render(doc: pyropdf.Document, index: int) -> None:
        bytes(doc.pages[index].pixmap(dpi=144))

    idle = observe("idle")
    first = pyropdf.open(path)
    second = pyropdf.open(path)
    render(first, 0)
    first_count = observe("first_page")
    render(first, 1)
    warmed = observe("first_pool")
    render(second, 0)
    render(second, 1)
    concurrent = observe("second_document")
    with ThreadPoolExecutor(max_workers=1) as caller:
        caller.submit(first.close).result()
    closed = observe("after_cross_thread_close")
    render(second, 2)
    reused = observe("second_pool")
    del second
    gc.collect()
    dropped = observe("after_drop")
    print(json.dumps(readings))
    if not (first_count == closed == dropped == idle and idle < warmed <= idle + 4 and warmed == concurrent == reused):
        message = "Unexpected worker lifetime on this multi-core test host"
        raise SystemExit(message)


if __name__ == "__main__":
    main()
