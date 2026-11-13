# ruff: noqa: INP001, S101, T201 - standalone evidence script with correctness assertions.
"""Measure cold validation and reused outline counts for issue #795.

Run with the release extension built by ``uv sync``:
``uv run evidence/outline-count-validation/measure.py > evidence/outline-count-validation/results.json``.
"""

from __future__ import annotations

import json
import platform
import statistics
import time

import pyropdf

CHAPTERS = 64
SAMPLES = 25


def fixture(total: int) -> bytes:
    """Make a two-level outline with the same number of chapters at each size."""
    doc = pyropdf.Document()
    doc.pages.new()
    entries: list[pyropdf.OutlineEntry] = []
    for chapter in range(CHAPTERS):
        entries.append(pyropdf.OutlineEntry(1, f"Chapter {chapter}", is_open=True))
        entries.extend(pyropdf.OutlineEntry(2, f"Child {child}") for child in range(total // CHAPTERS - 1))
    doc.outline = entries
    return doc.to_bytes()


def measure(total: int) -> dict[str, object]:
    """Exclude fixture creation, opening, and handle lookup from the timings."""
    data = fixture(total)
    cold: list[float] = []
    for _ in range(SAMPLES):
        with pyropdf.open(data) as doc:
            item = doc.outline[0]
            start = time.perf_counter_ns()
            item.is_open = False
            cold.append((time.perf_counter_ns() - start) / 1_000)
            assert item.obj["Count"] == -(total // CHAPTERS - 1)

    rows: dict[str, object] = {"items": total, "cold_toggle_median_us": round(statistics.median(cold), 1)}
    # No history keeps the old draft alive: commit must carry verification.
    with pyropdf.open(data) as doc:
        doc.history.max_size = 0
        chapters = list(doc.outline)
        chapters[0].is_open = True
        for batch in (False, True):
            durations: list[float] = []
            for sample in range(SAMPLES):
                flag = sample % 2 != 0
                start = time.perf_counter_ns()
                if batch:
                    with doc.transaction("Toggle all chapters"):
                        for item in chapters:
                            item.is_open = flag
                else:
                    chapters[0].is_open = flag
                durations.append((time.perf_counter_ns() - start) / 1_000)
            name = "warm_batch_64_median_us" if batch else "warm_toggle_median_us"
            rows[name] = round(statistics.median(durations), 1)
        assert doc.catalog["Outlines"]["Count"] == CHAPTERS
    return rows


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "samples": SAMPLES,
                "history_max_size": 0,
                "measurements": [measure(total) for total in (512, 2048)],
            },
            indent=2,
        )
    )
