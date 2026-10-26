# ruff: noqa: INP001 - a script, not a package.
"""Measure reading through object handles after an unrelated edit (issue #32).

For 100, 400, and 1,600 indirect dictionaries, the script times two loops,
each the median of 21 rounds:

- single reads: ``doc.objects[n]["T"]`` for every object, a fresh lookup;
- handles: the same property through handles taken before an unrelated edit
  of the catalog, which every round repeats.

The object model is the same in pylopdf, so the script runs on either
package. From the repository root:
``uv run python evidence/object-handles/measure.py`` measures pyropdf, and
``path/to/pylopdf/.venv/bin/python evidence/object-handles/measure.py pylopdf``
measures pylopdf.
"""

from __future__ import annotations

import importlib
import platform
import statistics
import sys
import time

SIZES = (100, 400, 1_600)
ROUNDS = 21


def measure(package: str, count: int) -> tuple[float, float]:
    """Return the median milliseconds of single reads and of handle reads after an edit."""
    module = importlib.import_module(package)
    doc = module.Document()
    numbers = [doc.objects.add({"T": f"field {i}", "V": i}) for i in range(count)]
    doc.catalog["Fields"] = [doc.objects[n] for n in numbers]
    doc = module.open(doc.to_bytes())
    handles = [doc.objects[n] for n in numbers]

    single: list[float] = []
    after_edit: list[float] = []
    for at in range(ROUNDS):
        start = time.perf_counter()
        for n in numbers:
            doc.objects[n]["T"]
        single.append(time.perf_counter() - start)

        doc.catalog["Unrelated"] = at
        start = time.perf_counter()
        for handle in handles:
            handle["T"]
        after_edit.append(time.perf_counter() - start)
    return statistics.median(single) * 1e3, statistics.median(after_edit) * 1e3


def main() -> None:
    """Print a Markdown table for the package named on the command line, pyropdf by default."""
    package = sys.argv[1] if len(sys.argv) > 1 else "pyropdf"
    version = importlib.import_module(package).__version__
    lines = [
        f"- {package} {version}, Python {platform.python_version()} on {platform.system()} {platform.machine()}",
        "",
        "| Objects | Single reads | Handles after an edit | Ratio |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for count in SIZES:
        single, after_edit = measure(package, count)
        lines.append(f"| {count:,} | {single:.2f} ms | {after_edit:.2f} ms | {after_edit / single:.2f} |")
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
