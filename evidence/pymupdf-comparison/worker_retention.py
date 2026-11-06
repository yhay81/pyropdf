# ruff: noqa: INP001, T201, S603 - standalone Windows benchmark for the current process
"""Observe native threads on one long-lived caller after a partial document render."""

from __future__ import annotations

import gc
import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pyropdf

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "tests/assets/real_world"


def main() -> None:
    """Sample only this worker process at the documented lifecycle points."""
    powershell = shutil.which("pwsh")
    if powershell is None:
        msg = "This Windows probe needs PowerShell 7"
        raise RuntimeError(msg)
    memory = runpy.run_path(str(ROOT / "evidence/path-columns/measure.py"))["memory"]
    rows = {}

    def record(stage: str) -> None:
        count = subprocess.check_output(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", f"(Get-Process -Id {os.getpid()}).Threads.Count"],
            text=True,
        )
        current, peak = memory()
        rows[stage] = {"threads": int(count.strip()), "rss_bytes": current, "peak_rss_bytes": peak}

    record("start")
    with pyropdf.open(ASSETS / "bill-hr815.pdf") as doc:
        for index in range(2):
            bytes(doc.pages[index].pixmap(dpi=144))
        record("after_two_pages")
    del doc
    gc.collect()
    record("after_close")
    with pyropdf.open(ASSETS / "pdfium-type3.pdf") as doc:
        bytes(doc.pages[0].pixmap(dpi=72))
    record("after_another_document")
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
