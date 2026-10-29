# ruff: noqa: INP001, T201 - a script that prints its table.
"""Measure what doc.subset_fonts() saves on every corpus document.

Run from the repository root: ``uv run python evidence/font-subset/measure.py``.
It prints the Markdown table of ``README.md``: each document's full save
before and after subsetting, and the programs that shrank.
"""

from __future__ import annotations

import time
from pathlib import Path

import pyropdf

ASSETS = Path(__file__).resolve().parents[2] / "tests" / "assets" / "real_world"


def main() -> None:
    """Print one row per corpus document."""
    print("| Document | Saved before | Saved after | Change | Programs subset | Glyphs kept | Seconds |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for path in sorted(ASSETS.glob("*.pdf")):
        doc = pyropdf.open(path.read_bytes())
        before = len(doc.to_bytes())
        start = time.perf_counter()
        fonts = doc.subset_fonts()
        seconds = time.perf_counter() - start
        after = len(doc.to_bytes())
        kept = ", ".join(f"{font.kept}/{font.glyphs}" for font in fonts) or "-"
        change = f"{(after - before) / before:+.1%}"
        print(f"| {path.name} | {before:,} | {after:,} | {change} | {len(fonts)} | {kept} | {seconds:.2f} |")


if __name__ == "__main__":
    main()
