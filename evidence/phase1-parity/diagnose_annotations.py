# ruff: noqa: INP001 - an evidence script, not a package.
"""Isolate the corpus highlight's rendering difference from the page content.

Run with pyropdf's Python after run.py: diagnose_annotations.py ORACLE_PYTHON.
The output records both hashes and the annotation-disabled comparison.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import compare

import pyropdf

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TARGET = ROOT / "target" / "parity"
STEM = "pdfium-links-highlights-annots"
RENDER = """
import importlib, sys
from pathlib import Path
engine = importlib.import_module(sys.argv[1])
page = engine.open(Path(sys.argv[2]).read_bytes()).pages[0]
raster = page.pixmap(dpi=72, annotations=False)
Path(sys.argv[3]).write_bytes(bytes(raster))
"""


def main() -> None:
    """Render each engine without annotations and isolate the differing bounds."""
    (oracle,) = sys.argv[1:]
    source = ROOT / "tests" / "assets" / "real_world" / f"{STEM}.pdf"
    for engine, python in (("pylopdf", oracle), ("pyropdf", sys.executable)):
        subprocess.run(  # noqa: S603 - fixed diagnostic code, user-selected oracle interpreter.
            [python, "-c", RENDER, engine, str(source), str(TARGET / f"{engine}-annotations-off.rgb")], check=True
        )
    samples = [(TARGET / engine / f"{STEM}-0.rgb").read_bytes() for engine in ("pylopdf", "pyropdf")]
    disabled = [(TARGET / f"{engine}-annotations-off.rgb").read_bytes() for engine in ("pylopdf", "pyropdf")]
    metadata = json.loads((TARGET / "pyropdf" / f"{STEM}.json").read_text())["pages"][0]["render"]
    coordinates = [
        (offset % metadata["stride"] // 3, offset // metadata["stride"])
        for offset, (a, b) in enumerate(zip(*samples, strict=True))
        if abs(a - b) > compare.CHANNEL_TOLERANCE
    ]
    with pyropdf.open(source) as document:
        highlight = next(
            annotation for annotation in document.pages[0].obj["Annots"] if annotation["Subtype"] == "Highlight"
        )
        rectangle = [float(value) for value in highlight["Rect"]]
        has_appearance = "AP" in highlight
    result = {
        "document": source.name,
        "page": 1,
        "highlight_pdf_rect": rectangle,
        "highlight_has_appearance": has_appearance,
        "differing_pixel_bounds_inclusive": (
            [
                min(x for x, _ in coordinates),
                min(y for _, y in coordinates),
                max(x for x, _ in coordinates),
                max(y for _, y in coordinates),
            ]
            if coordinates
            else None
        ),
        "default_sha256": [hashlib.sha256(data).hexdigest() for data in samples],
        "annotations_disabled_sha256": [hashlib.sha256(data).hexdigest() for data in disabled],
        "annotations_disabled_equal": disabled[0] == disabled[1],
    }
    (HERE / "annotation-diagnostic.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
