# ruff: noqa: INP001, T201, S603 - a script that runs its sides and prints a table.
"""Rotate and render every page with pyropdf and with pylopdf, at 110, 440, and 1,760 pages (issue #38).

Usage, from the repository root:

    uv run python evidence/edit-render/measure.py PYLOPDF_PYTHON BILL

PYLOPDF_PYTHON is a Python with pylopdf installed, such as a pylopdf
checkout's ``.venv/bin/python``, and BILL is bill-hr815.pdf. pylopdf builds
the documents as evidence/pylopdf-baseline/edit_read_loop.py builds them;
both libraries then read the same files, each in a process of its own, and
the bridge's split comes from evidence/edit-render/split. It prints Markdown.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIZES = (110, 440, 1_760)
#: A low resolution, so that the loop measures the edit and the re-read more than rasterization.
DPI = 18.0


def side(python: str, *args: str) -> str:
    """Run side.py with ``python`` and return its output."""
    return subprocess.run([python, str(HERE / "side.py"), *args], capture_output=True, text=True, check=True).stdout


def version(python: str, library: str) -> str:
    """Return the version of ``library`` under ``python``."""
    code = f"import {library}, sys; print({library}.__version__, sys.version.split()[0])"
    return subprocess.run([python, "-c", code], capture_output=True, text=True, check=True).stdout.strip()


def main() -> None:
    """Build the documents, measure both libraries and the split, and print the tables."""
    pylopdf_python, bill = sys.argv[1], sys.argv[2]
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory)
        side(pylopdf_python, "build", bill, str(out))
        rows = []
        for pages in SIZES:
            path = str(out / f"pages-{pages}.pdf")
            ours = side(sys.executable, "measure", "pyropdf", path, str(DPI)).split()
            theirs = side(pylopdf_python, "measure", "pylopdf", path, str(DPI)).split()
            rows.append((pages, float(ours[1]), float(ours[2]), float(theirs[1]), float(theirs[2])))
        split = subprocess.run(
            ["cargo", "run", "--release", "--quiet", "--manifest-path", str(HERE / "split" / "Cargo.toml"), "--"]
            + [str(out / f"pages-{pages}.pdf") for pages in SIZES],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    print(f"- pyropdf {version(sys.executable, 'pyropdf')}, pylopdf {version(pylopdf_python, 'pylopdf')}")
    print(f"- {platform.system()} {platform.machine()}; pages rendered at {DPI:g} dpi")
    print()
    print("## Rotate and render every page")
    print()
    print(
        "| Pages | pyropdf: render all | pyropdf: rotate and render all | Ratio "
        "| pylopdf: render all | pylopdf: rotate and render all | Ratio |"
    )
    print("| ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for pages, render, edit, their_render, their_edit in rows:
        print(
            f"| {pages:,} | {render:.2f} s | {edit:.2f} s | {edit / render:.1f}x "
            f"| {their_render:.2f} s | {their_edit:.2f} s | {their_edit / their_render:.1f}x |"
        )
    print()
    print(split.strip())


if __name__ == "__main__":
    main()
