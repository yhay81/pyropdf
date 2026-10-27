# ruff: noqa: INP001, T201 - a script, not a package, that prints its tables.
"""Measure the sources of layout chars on the corpus (issue #98).

Run from the repository root: ``uv run python evidence/text-provenance/measure.py``.
``coverage`` counts, for every page of every corpus document, the chars
that name the operation that showed them. ``timing`` prints the median
time of ``Page.layout(chars=True)`` on the first pages of the largest
documents, newly opened each round, so that a build without sources can
run it too. results/<system>-<machine>.md keeps one run of each.
"""

from __future__ import annotations

import platform
import statistics
import sys
import time
import warnings
from pathlib import Path

import pyropdf

ASSETS = Path(__file__).resolve().parents[2] / "tests" / "assets" / "real_world"
SHOWS_TEXT = {"Tj", "TJ", "'", '"'}
TIMED = ("usrguide.pdf", "bill-hr815.pdf", "f1040.pdf", "nics-background-checks-2015-11.pdf", "mhlw-doc.pdf")
PAGES = 5
ROUNDS = 11


def _chars(page: pyropdf.Page) -> list[pyropdf.Char]:
    layout = page.layout(chars=True, unmapped="replacement")
    return [
        char
        for block in layout.blocks
        for line in block.lines
        for span in line.spans
        for char in span.chars
        if not char.is_synthetic
    ]


def coverage() -> None:
    """Print, per document, the chars with and without sources, and the pages without any."""
    print("| Document | Pages | Chars | With a source | Pages with own text but no source |")
    print("| --- | ---: | ---: | ---: | ---: |")
    totals = [0, 0, 0, 0]
    for path in sorted(ASSETS.glob("*.pdf")):
        document = pyropdf.open(path.read_bytes())
        if document.needs_password:
            continue
        pages = chars = sourced = unsourced_pages = 0
        for page in document.pages:
            pages += 1
            found = _chars(page)
            with_source = sum(char.operation is not None for char in found)
            chars += len(found)
            sourced += with_source
            shows = any(operation.operator in SHOWS_TEXT for operation in page.operations)
            if found and shows and not with_source:
                unsourced_pages += 1
        print(f"| {path.name} | {pages} | {chars} | {sourced} | {unsourced_pages} |")
        for at, value in enumerate((pages, chars, sourced, unsourced_pages)):
            totals[at] += value
    print(f"| All | {totals[0]} | {totals[1]} | {totals[2]} | {totals[3]} |")


def timing() -> None:
    """Print the median time of a first layout with chars of the first pages."""
    print("| Document | `layout(chars=True)`, first pages |")
    print("| --- | ---: |")
    for name in TIMED:
        data = (ASSETS / name).read_bytes()
        times = []
        for _ in range(ROUNDS):
            document = pyropdf.open(data)
            pages = list(document.pages)[:PAGES]
            started = time.perf_counter()
            for page in pages:
                page.layout(chars=True)
            times.append(time.perf_counter() - started)
        print(f"| {name} | {statistics.median(times) * 1e3:.2f} ms |")


def main() -> None:
    """Print the tables that the arguments name."""
    warnings.simplefilter("ignore", pyropdf.PyropdfWarning)
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")
    print()
    for table in sys.argv[1:] or ["coverage", "timing"]:
        {"coverage": coverage, "timing": timing}[table]()
        print()


if __name__ == "__main__":
    main()
