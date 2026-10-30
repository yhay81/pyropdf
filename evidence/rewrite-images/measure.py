# ruff: noqa: INP001, T201 - a script that prints its table.
"""Measure what doc.rewrite_images() saves on every corpus document that places images.

Run from the repository root: ``uv run python evidence/rewrite-images/measure.py``.
It prints the Markdown table of ``README.md``: each document's pruned full save
before and after ``rewrite_images(dpi_threshold=150, dpi_target=100,
quality=65)``, and how far its pages rendered at the target 100 dpi move:
the mean and the largest absolute difference of one 8-bit sample.
"""

from __future__ import annotations

import time
from pathlib import Path

import pyropdf

ASSETS = Path(__file__).resolve().parents[2] / "tests" / "assets" / "real_world"
THRESHOLD, TARGET, QUALITY = 150, 100, 65


def renders(doc: pyropdf.Document) -> list[bytes]:
    """Return the samples of every page rendered at the target resolution."""
    return [bytes(page.pixmap(dpi=TARGET)) for page in doc.pages]


def main() -> None:
    """Print one row per corpus document that places an image object."""
    print("| Document | Images | Rewritten | Saved before | Saved after | Change | Mean diff | Max diff | Seconds |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for path in sorted(ASSETS.glob("*.pdf")):
        doc = pyropdf.open(path.read_bytes())
        if not any(image.item.xref for page in doc.pages for image in page.images(annotations=True)):
            continue
        before = len(doc.to_bytes(prune=True))
        pixels = renders(doc)
        start = time.perf_counter()
        report = doc.rewrite_images(dpi_threshold=THRESHOLD, dpi_target=TARGET, quality=QUALITY)
        seconds = time.perf_counter() - start
        after = len(doc.to_bytes(prune=True))
        total = largest = count = 0
        for old, new in zip(pixels, renders(doc), strict=True):
            differences = [abs(a - b) for a, b in zip(old, new, strict=True)]
            total += sum(differences)
            largest = max(largest, *differences)
            count += len(differences)
        change = f"{(after - before) / before:+.1%}"
        print(
            f"| {path.name} | {report.considered} | {report.rewritten} | {before:,} | {after:,} | {change} "
            f"| {total / count:.2f} | {largest} | {seconds:.2f} |"
        )


if __name__ == "__main__":
    main()
