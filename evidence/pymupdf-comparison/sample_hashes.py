# ruff: noqa: INP001, T201 - a script, not a package; it prints its count.
"""Hash the samples of every corpus page in each raster layout, to compare two builds.

    uv run python evidence/pymupdf-comparison/sample_hashes.py OUT

Renders every page of ``tests/assets/real_world`` at 72 dpi in RGB, gray,
and with alpha, on the default and on another background, and writes the
SHA-256 of each page's samples, with its channels, to ``OUT`` as JSON. Run it
on two builds and compare the files: equal files mean the change kept every
byte. #393 used it to show that narrowing through vello_cpu changed none.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pyropdf

CORPUS = Path(__file__).resolve().parents[2] / "tests" / "assets" / "real_world"
LAYOUTS = {
    "rgb": {},
    "rgb on orange": {"background": (200, 120, 40)},
    "gray": {"colorspace": "gray"},
    "rgba": {"alpha": True},
    "rgba on translucent green": {"alpha": True, "background": (10, 200, 90, 128)},
    "gray with alpha": {"alpha": True, "colorspace": "gray"},
}


def main() -> None:
    """Write the hash of every page in every layout."""
    hashes = {}
    for path in sorted(CORPUS.glob("*.pdf")):
        with pyropdf.open(path) as doc:
            for index, page in enumerate(doc.pages):
                for name, options in LAYOUTS.items():
                    pixmap = page.pixmap(dpi=72, **options)
                    digest = hashlib.sha256(bytes(pixmap)).hexdigest()
                    hashes[f"{path.stem}/{index}/{name}"] = f"{digest} {pixmap.channels}"
    Path(sys.argv[1]).write_text(json.dumps(hashes, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    print(len(hashes))


if __name__ == "__main__":
    main()
