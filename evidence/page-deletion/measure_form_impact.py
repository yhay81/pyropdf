# ruff: noqa: INP001 - a script, not a package.
"""Measure the reference-driven form impact helper with a warm index.

Run: uv run python evidence/page-deletion/measure_form_impact.py
The Rust benchmark builds a form with one affected widget and a varying
number of unrelated fields. It reports the full reference-index build and
the median time per helper call over seven batches of 200 calls. This is a
helper benchmark, not an end-to-end page-deletion measurement.
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
        "pyropdf-store",
        "refs::tests::measure_form_impact",
        "--locked",
        "--",
        "--ignored",
        "--nocapture",
    ]
    run = subprocess.run(command, check=True, capture_output=True, text=True)  # noqa: S603
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print("- Rust test profile (debug); median of seven batches of 200 helper calls")  # noqa: T201
    print("\n| Unrelated fields | Initial index | Helper call |\n| ---: | ---: | ---: |")  # noqa: T201
    rows = 0
    for line in run.stdout.splitlines():
        if line.startswith("FORM_IMPACT|"):
            _, count, index_ms, helper_ms = line.split("|")
            print(f"| {int(count):,} | {index_ms} ms | {helper_ms} ms |")  # noqa: T201
            rows += 1
    if rows != EXPECTED_ROWS:
        msg = f"expected {EXPECTED_ROWS} benchmark rows, received {rows}"
        raise RuntimeError(msg)


if __name__ == "__main__":
    main()
