# ruff: noqa: INP001 - a script, not a package.
"""Measure only the prototype's form-index construction and warm update.

Run: python3 evidence/form-fill-cost/measure_index.py
The Rust test constructs its own document, then reports the median of seven
batches of 100 updates. This is not a measurement of complete field filling.
"""

from __future__ import annotations

import platform
import subprocess

EXPECTED_ROWS = 5


def main() -> None:
    """Run the ignored Rust benchmark and print its result table."""
    command = [
        "cargo",
        "test",
        "-p",
        "pyropdf-python",
        "forms::appearance_tests::measure_fill_index_updates",
        "--locked",
        "--",
        "--ignored",
        "--nocapture",
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)  # noqa: S603
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print("- Rust test profile (debug); median of seven batches of 100 updates")  # noqa: T201
    print("\n| Fields | Initial index | Warm update |\n| ---: | ---: | ---: | ")  # noqa: T201
    rows = 0
    for line in result.stdout.splitlines():
        if line.startswith("PYRO_FILL_INDEX "):
            _, fields, cold, warm = line.split()
            print(f"| {int(fields):,} | {cold} ms | {warm} ms | ")  # noqa: T201
            rows += 1
    if rows != EXPECTED_ROWS:
        msg = f"expected {EXPECTED_ROWS} benchmark rows, received {rows}"
        raise RuntimeError(msg)


if __name__ == "__main__":
    main()
