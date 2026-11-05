# ruff: noqa: INP001, T201, S603 - a benchmark that runs explicitly supplied Python interpreters.
"""Compare uncached ruled-table extraction in two installed pyropdf builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIN_RUNS = 25
MIN_SECONDS = 0.5


def sample() -> dict[str, dict[str, int | str]]:
    """Time first extraction on fresh documents, excluding opening and hashing."""
    sys.path.insert(0, str(ROOT / "tests"))
    from test_tables import _table_pdf  # noqa: PLC0415 - use the existing fixture builder.

    import pyropdf  # noqa: PLC0415 - only the measured interpreter imports the library.

    results = {}
    for rows, columns in [(2, 2), (10, 4)]:
        left, bottom, dx, dy = 40, 40, 60, 20
        right, top = left + columns * dx, bottom + rows * dy
        operators = ["q 0 G 1 w"]
        operators.extend(f"{left} {y} m {right} {y} l" for y in range(bottom, top + 1, dy))
        operators.extend(f"{x} {bottom} m {x} {top} l" for x in range(left, right + 1, dx))
        operators.append("S Q")
        operators.extend(
            f"BT /F1 8 Tf {left + column * dx + 5} {top - row * dy - 14} Td (R{row}C{column}) Tj ET"
            for row in range(rows)
            for column in range(columns)
        )
        data = _table_pdf("\n".join(operators))
        expected = tuple(tuple(f"R{row}C{column}" for column in range(columns)) for row in range(rows))
        times = []
        started = time.perf_counter()
        digest = ""
        while len(times) < MIN_RUNS or time.perf_counter() - started < MIN_SECONDS:
            with pyropdf.open(data) as document:
                page = document.pages[0]
                before = time.perf_counter_ns()
                tables = page.tables()
                elapsed = time.perf_counter_ns() - before
                if len(tables) != 1 or tables[0].rows != expected:
                    msg = "The generated grid was not extracted completely"
                    raise RuntimeError(msg)
                encoded = json.dumps([(table.bbox, table.rows) for table in tables]).encode()
                current = hashlib.sha256(encoded).hexdigest()
                if digest and digest != current:
                    msg = "Extraction changed across repetitions"
                    raise RuntimeError(msg)
                digest = current
                times.append(elapsed)
        results[f"{rows} x {columns}"] = {"median_ns": int(statistics.median(times)), "sha256": digest}
    return results


def main() -> None:
    """Alternate the supplied builds and compare medians and output digests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pythons", nargs=2, type=Path, metavar=("BASE", "HEAD"))
    parser.add_argument("--base", default="unknown")
    parser.add_argument("--head", default="unknown")
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    if args.sample:
        print(json.dumps(sample()))
        return
    if args.pythons is None or args.rounds < 1:
        parser.error("supply --pythons BASE HEAD and positive --rounds")
    times: dict[str, list[list[int]]] = {}
    digests: dict[str, str] = {}
    for _ in range(args.rounds):
        for index, executable in enumerate(args.pythons):
            completed = subprocess.run(
                [str(executable.resolve()), str(Path(__file__).resolve()), "--sample"],
                check=True,
                capture_output=True,
                text=True,
            )
            for grid, result in json.loads(completed.stdout).items():
                times.setdefault(grid, [[], []])[index].append(result["median_ns"])
                if digests.setdefault(grid, result["sha256"]) != result["sha256"]:
                    msg = f"Different extracted cells or geometry for {grid}"
                    raise RuntimeError(msg)
    print(f"Base {args.base}, head {args.head}, {args.rounds} alternating rounds, {platform.platform()}.")
    print("\n| Grid | Base µs | Head µs | Change |")
    print("| --- | ---: | ---: | ---: |")
    for grid, values in times.items():
        before, after = (statistics.median(value) / 1000 for value in values)
        print(f"| {grid} | {before:.1f} | {after:.1f} | {(after / before - 1):+.1%} |")
    print("\nExtracted cell text and geometry have matching SHA-256 digests in every round.")


if __name__ == "__main__":
    main()
