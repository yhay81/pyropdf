# ruff: noqa: INP001 - a script, not a package.
"""Measure reading every field's value through handles, by field count.

Usage: uv run python evidence/form-read-cost/measure.py

The forms match pylopdf's evidence/pylopdf-baseline/field_read_cost.py:
100, 400, and 1,600 text fields without values, 50 to a page. They are
built from raw objects, since adding widgets waits for issue #193. Each
row is the median of 7 rounds, each on a newly opened document:

- one walk: the first ``form_fields`` call, which walks the field tree.
- list and read: ``list(doc.form.values())`` and then every field's
  ``value``, the walk included.
- read after listing: only the values, as the pylopdf script times them.
"""

from __future__ import annotations

import platform
import statistics
import time

import pyropdf
from pyropdf import _core

PER_PAGE = 50
ROUNDS = 7


def build(count: int) -> bytes:
    """Return a PDF of `count` text fields, 50 to a page."""
    pages = range(0, count, PER_PAGE)
    page_numbers = [4 + index for index in range(len(pages))]
    field_base = 4 + len(pages)
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 3 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{n} 0 R' for n in page_numbers)}] /Count {len(pages)} >>",
        3: f"<< /Fields [{' '.join(f'{field_base + i} 0 R' for i in range(count))}] >>",
    }
    for page_number, first in zip(page_numbers, pages, strict=True):
        numbers = range(field_base + first, field_base + min(first + PER_PAGE, count))
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Annots [{' '.join(f'{n} 0 R' for n in numbers)}] >>"
        )
        for row, number in enumerate(numbers):
            x = 36 + (row % 5) * 100
            y = 36 + (row // 5) * 70
            objects[number] = (
                f"<< /FT /Tx /T (f{number - field_base}) /Type /Annot /Subtype /Widget"
                f" /Rect [{x} {y} {x + 90} {y + 20}] /P {page_number} 0 R /F 4 >>"
            )
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def main() -> None:
    """Print the results as a Markdown table."""
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print()  # noqa: T201
    print("| Fields | One walk | List and read | Read after listing | Walks |")  # noqa: T201
    print("| ---: | ---: | ---: | ---: | ---: |")  # noqa: T201
    for count in (100, 400, 1600):
        data = build(count)
        walks, totals, reads = [], [], []
        for _ in range(ROUNDS):
            doc = pyropdf.open(data)
            start = time.perf_counter()
            _core.form_fields(doc._core())  # noqa: SLF001 - the walk is internal.
            walks.append(time.perf_counter() - start)

            doc = pyropdf.open(data)
            start = time.perf_counter()
            fields = list(doc.form.values())
            listed = time.perf_counter()
            values = [field.value for field in fields]
            end = time.perf_counter()
            totals.append(end - start)
            reads.append(end - listed)
            computations = _core.form_walk_counts(doc._core())[1]  # noqa: SLF001
        print(  # noqa: T201
            f"| {len(values):,} | {statistics.median(walks) * 1e3:.2f} ms | {statistics.median(totals) * 1e3:.2f} ms"
            f" | {statistics.median(reads) * 1e3:.2f} ms | {computations} |"
        )


if __name__ == "__main__":
    main()
