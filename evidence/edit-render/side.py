# ruff: noqa: INP001, T201 - a script that prints its results.
"""Run one library's side of measure.py: build the documents, or time the loops.

``python side.py build BILL OUT_DIR`` writes the 110-, 440-, and 1,760-page
documents with pylopdf, as evidence/pylopdf-baseline/edit_read_loop.py builds
them. ``python side.py measure LIBRARY FILE DPI`` prints the seconds of
rendering every page and of rotating then rendering every page.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path


def build(source: Path, out: Path) -> None:
    """Write pages-110.pdf, pages-440.pdf, and pages-1760.pdf from ``source``."""
    pylopdf = importlib.import_module("pylopdf")
    for copies in (1, 4, 16):
        doc = pylopdf.open(source)
        for _ in range(copies - 1):
            doc.pages.extend(pylopdf.open(source).pages)
        data = doc.to_bytes()
        (out / f"pages-{len(doc.pages)}.pdf").write_bytes(data)


def measure(library: str, path: Path, dpi: float) -> None:
    """Print the render-every-page and rotate-and-render-every-page times."""
    module = importlib.import_module(library)
    doc = module.open(path)
    start = time.perf_counter()
    for page in doc.pages:
        page.pixmap(dpi=dpi)
    render_all = time.perf_counter() - start

    doc = module.open(path)
    start = time.perf_counter()
    for index in range(len(doc.pages)):
        page = doc.pages[index]
        page.rotation = 90
        page.pixmap(dpi=dpi)
    edit_render_all = time.perf_counter() - start
    print(f"{len(doc.pages)} {render_all:.4f} {edit_render_all:.4f}")


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        measure(sys.argv[2], Path(sys.argv[3]), float(sys.argv[4]))
