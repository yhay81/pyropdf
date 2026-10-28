# ruff: noqa: INP001 - a script, not a package.
"""Measure the chars of a page as columns against one record per glyph (issue #99).

Run from the repository root: ``uv run --with numpy python evidence/char-buffers/measure.py``.
NumPy is optional; without it the NumPy column reads ``n/a``. It prints a
Markdown table; results/<system>-<machine>.md keeps one run.
"""

from __future__ import annotations

import gc
import platform
import time
import tracemalloc
from pathlib import Path
from typing import TYPE_CHECKING

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    import numpy as np
except ImportError:  # pragma: no cover - NumPy is optional.
    np = None

ASSETS = Path(__file__).resolve().parents[2] / "tests" / "assets" / "real_world"
NAMES = ("bill-hr815.pdf", "usrguide.pdf", "f1040.pdf")
ROUNDS = 11


def median(times: list[float]) -> float:
    """Return the median of ``times``."""
    return sorted(times)[len(times) // 2]


def timed(call: Callable[[], object]) -> float:
    """Return the median seconds of ``call`` over :data:`ROUNDS` calls."""
    times = []
    for _ in range(ROUNDS):
        started = time.perf_counter()
        call()
        times.append(time.perf_counter() - started)
    return median(times)


def held(call: Callable[[], object]) -> tuple[int, object]:
    """Return the bytes of the Python heap that the result of ``call`` holds, and the result."""
    gc.collect()
    tracemalloc.start()
    result = call()
    gc.collect()
    size = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    return size, result


def largest_page(doc: pyropdf.Document) -> pyropdf.Page:
    """Return the page of ``doc`` with the most chars."""
    return max(doc.pages, key=lambda page: len(page.chars()))


def records_x0(layout: pyropdf.Layout) -> list[float]:
    """Return the left edge of every char of ``layout`` through its records."""
    lines = [line for block in layout.blocks for line in block.lines]
    return [char.bbox[0] for line in lines for span in line.spans for char in span.chars]


def main() -> None:
    """Print the table."""
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print(f"- NumPy {np.__version__ if np else 'not installed'}")  # noqa: T201
    print()  # noqa: T201
    print(  # noqa: T201
        "| Page | Chars | Build: `layout()` | Build: records | Build: columns "
        "| Heap: records | Heap: columns | Column bytes | `x0`: records | `x0`: `tolist` | `x0`: NumPy |"
    )
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")  # noqa: T201
    for name in NAMES:
        doc = pyropdf.open(ASSETS / name)
        page = largest_page(doc)
        # Each reads a memoized layout; what differs is what each builds.
        spans_time = timed(lambda page=page: page.layout())
        layout_time = timed(lambda page=page: page.layout(chars=True))
        chars_time = timed(lambda page=page: page.chars())
        layout_heap, layout = held(lambda page=page: page.layout(chars=True))
        chars_heap, chars = held(lambda page=page: page.chars())
        column_bytes = sum(chars.column(column).nbytes for column in chars.names)
        records_time = timed(lambda layout=layout: records_x0(layout))
        tolist_time = timed(lambda chars=chars: chars.column("x0").tolist())
        if np is None:
            numpy_time = "n/a"
        else:
            read = timed(lambda chars=chars: np.frombuffer(chars.column("x0"), np.float64).copy())
            numpy_time = f"{read * 1e6:.1f} µs"
        print(  # noqa: T201
            f"| {name} p{page.index + 1} | {len(chars):,} | {spans_time * 1e3:.2f} ms "
            f"| {layout_time * 1e3:.2f} ms | {chars_time * 1e3:.3f} ms "
            f"| {layout_heap / 2**20:.2f} MiB | {chars_heap / 2**10:.1f} KiB | {column_bytes / 2**20:.2f} MiB "
            f"| {records_time * 1e3:.2f} ms | {tolist_time * 1e3:.3f} ms | {numpy_time} |"
        )


if __name__ == "__main__":
    main()
