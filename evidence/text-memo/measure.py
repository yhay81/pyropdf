# ruff: noqa: INP001 - a script, not a package.
"""Measure a page's memoized results against computing them (issue #132).

Run from the repository root: ``uv run python evidence/text-memo/measure.py``.
It prints a Markdown table; results/<system>-<machine>.md keeps one run.
"""

from __future__ import annotations

import platform
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable

ASSETS = Path(__file__).resolve().parents[2] / "tests" / "assets" / "real_world"
NAMES = ("usrguide.pdf", "bill-hr815.pdf", "f1040.pdf", "nics-background-checks-2015-11.pdf")
VIEWS: dict[str, Callable[[pyropdf.Page], object]] = {
    "`text`": lambda page: page.text(),
    "`words`": lambda page: page.words(),
    "`layout(chars=True)`": lambda page: page.layout(chars=True),
    "`content`": lambda page: page.content(),
}
ROUNDS = 21


def median(times: list[float]) -> float:
    """Return the median of ``times``."""
    return sorted(times)[len(times) // 2]


def measure(data: bytes, view: Callable[[pyropdf.Page], object]) -> tuple[float, float]:
    """Return the median seconds of computing the first page's result and of reusing it.

    A computation is the first call on a newly opened document. A reuse is a
    call after an edit that misses the page: a new object that nothing refers to.
    """
    computed, reused = [], []
    for _ in range(ROUNDS):
        doc = pyropdf.open(data)
        page = doc.pages[0]
        started = time.perf_counter()
        view(page)
        computed.append(time.perf_counter() - started)
        doc.objects.add({"Unrelated": len(reused)})
        started = time.perf_counter()
        view(page)
        reused.append(time.perf_counter() - started)
    return median(computed), median(reused)


def main() -> None:
    """Print the table."""
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print()  # noqa: T201
    print("| Document | Result | Computed | Reused after an edit elsewhere | Ratio |")  # noqa: T201
    print("| --- | --- | ---: | ---: | ---: |")  # noqa: T201
    for name in NAMES:
        data = (ASSETS / name).read_bytes()
        for label, view in VIEWS.items():
            computed, reused = measure(data, view)
            print(f"| {name} | {label} | {computed * 1e3:.2f} ms | {reused * 1e6:.1f} µs | {computed / reused:.0f}× |")  # noqa: T201


if __name__ == "__main__":
    main()
