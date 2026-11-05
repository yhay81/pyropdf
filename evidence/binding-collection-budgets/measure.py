# ruff: noqa: INP001, T201, SLF001
"""Compare binding allocations on the baseline and patched extension (#677).

Run with the same release-built extension and interpreter on each revision:
``.venv/Scripts/python evidence/binding-collection-budgets/measure.py``.
The layout is warmed so the column conversion, not interpretation, is timed.
"""

from __future__ import annotations

import json
import platform
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pyropdf
from pyropdf import _core

if TYPE_CHECKING:
    from collections.abc import Callable


def median_us(call: Callable[[], object], repetitions: int = 100) -> float:
    """Report the median of nine batches, in microseconds per call."""
    samples = []
    for _ in range(9):
        start = time.perf_counter()
        for _ in range(repetitions):
            call()
        samples.append((time.perf_counter() - start) * 1e6 / repetitions)
    return round(statistics.median(samples), 3)


def main() -> None:
    """Measure the two binding paths whose allocation strategy changes."""
    source = Path(__file__).resolve().parents[2] / "tests/assets/real_world/bill-hr815.pdf"
    document = pyropdf.open(source)
    page = document.pages[0]
    chars = page.chars()
    empty = pyropdf.Document()
    core = empty._core()
    names = ["missing"] * 1000
    results = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "page_chars": len(chars),
        "column_bytes": sum(chars.column(name).nbytes for name in chars.names),
        "warm_chars_us": median_us(page.chars),
        "type3_codes_1000_us": median_us(lambda: _core.type3_codes(core, [], 1000, names)),
    }
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
