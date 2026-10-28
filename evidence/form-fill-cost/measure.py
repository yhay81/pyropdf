# ruff: noqa: INP001 - a script, not a package.
"""Measure one text-field fill as pages without fields join the document.

Usage: uv run python evidence/form-fill-cost/measure.py FORM FILLER

FORM is pylopdf's tests/assets/real_world/f1040.pdf and FILLER its
tests/assets/real_world/bill-hr815.pdf, as pylopdf's
evidence/pylopdf-baseline/form_fill_cost.py takes them. FILLER's pages
join FORM 0, 4, and 16 times, and the document is saved and opened again.
Each row reports:

- first fill: the first fill of the first text field, which also draws
  the other text and choice widgets that have no appearance yet.
- one fill: the best of seven later fills of that field, as the pylopdf
  script times them.
- check box: the best of seven fills of the first check box, alternately
  checked and cleared.
"""

from __future__ import annotations

import platform
import sys
import time
from pathlib import Path

import pyropdf

ROUNDS = 7


def main() -> None:
    """Print the results as a Markdown table."""
    form, filler = Path(sys.argv[1]), Path(sys.argv[2])
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print()  # noqa: T201
    print("| Pages | Objects | First fill | One fill | Check box |")  # noqa: T201
    print("| ---: | ---: | ---: | ---: | ---: |")  # noqa: T201
    for copies in (0, 4, 16):
        doc = pyropdf.open(form)
        for _ in range(copies):
            doc.pages.extend(pyropdf.open(filler).pages)
        doc = pyropdf.open(doc.to_bytes())
        field = next(field for field in doc.form.values() if field.type == pyropdf.FieldType.TEXT)
        start = time.perf_counter()
        field.value = "first"
        first = time.perf_counter() - start
        times = []
        for round_ in range(ROUNDS):
            start = time.perf_counter()
            field.value = f"value {round_}"
            times.append(time.perf_counter() - start)
        box = next(field for field in doc.form.values() if field.type == pyropdf.FieldType.CHECKBOX)
        checks = []
        for round_ in range(ROUNDS):
            start = time.perf_counter()
            box.value = round_ % 2 == 0
            checks.append(time.perf_counter() - start)
        print(  # noqa: T201
            f"| {len(doc.pages):,} | {len(doc.objects):,} | {first * 1e3:.2f} ms | {min(times) * 1e3:.2f} ms"
            f" | {min(checks) * 1e3:.2f} ms |"
        )


if __name__ == "__main__":
    main()
