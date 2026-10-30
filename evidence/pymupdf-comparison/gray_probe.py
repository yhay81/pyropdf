# ruff: noqa: INP001, T201 - standalone oracle probe.
"""Show how the engines' similarly named ``set_to_gray`` options differ.

Run the first two commands with PyMuPDF 2.0, the third with pyropdf main::

    python gray_probe.py make /tmp/gray.pdf
    python gray_probe.py pymupdf /tmp/gray.pdf
    python gray_probe.py pyropdf /tmp/gray.pdf

The fixture has a red vector rectangle and no images. It is generated using
PyMuPDF only in the oracle's separate environment, never imported by pyropdf.
Each run independently opens it, calls ``rewrite_images(set_to_gray=True)``,
and reads the same interior pixel before and after.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


def main() -> None:
    """Create the fixture or run one engine against it."""
    engine, filename = sys.argv[1:]
    path = Path(filename)
    if engine == "make":
        pymupdf = importlib.import_module("pymupdf")

        doc = pymupdf.open()
        page = doc.new_page(width=200, height=200)
        page.draw_rect(pymupdf.Rect(20, 20, 100, 80), color=(1, 0, 0), fill=(1, 0, 0))
        page.insert_text((20, 120), "Page text", fontsize=12, color=(0, 0, 1))
        page.add_freetext_annot(pymupdf.Rect(15, 145, 190, 180), "Annotation text", fontsize=12)
        doc.save(path)
        return
    if engine == "pymupdf":
        pymupdf = importlib.import_module("pymupdf")

        with pymupdf.open(path) as doc:
            page = doc[0]
            before = page.get_pixmap()
            offset = 30 * before.stride + 30 * before.n
            old = list(before.samples[offset : offset + 3])
            doc.rewrite_images(set_to_gray=True)
            after = page.get_pixmap()
            offset = 30 * after.stride + 30 * after.n
            new = list(after.samples[offset : offset + 3])
            print(json.dumps({"engine": "PyMuPDF", "before_rgb": old, "after_rgb": new}))
        return
    if engine == "pyropdf":
        import pyropdf  # noqa: PLC0415 - independent environment.

        with pyropdf.open(path) as doc:
            page = doc.pages[0]
            before = page.pixmap()
            offset = 30 * before.stride + 30 * before.channels
            old = list(bytes(before)[offset : offset + 3])
            report = doc.rewrite_images(set_to_gray=True)
            after = page.pixmap()
            offset = 30 * after.stride + 30 * after.channels
            new = list(bytes(after)[offset : offset + 3])
            print(json.dumps({"engine": "pyropdf", "before_rgb": old, "after_rgb": new, "rewritten": report.rewritten}))
        return
    raise ValueError(engine)


if __name__ == "__main__":
    main()
