# ruff: noqa: INP001, T201 - isolated benchmark worker; stdout is its JSON protocol
"""Render in one wheel environment, opening a fresh document per request."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import time
from pathlib import Path

import pyropdf
from pyropdf import _core

CORPUS = Path(__file__).resolve().parents[2] / "tests/assets/real_world"
LAYOUTS = {
    "rgb": {},
    "rgb-orange": {"background": (200, 120, 40)},
    "gray": {"colorspace": "gray"},
    "rgba": {"alpha": True},
    "rgba-green": {"alpha": True, "background": (10, 200, 90, 128)},
    "gray-alpha": {"alpha": True, "colorspace": "gray"},
}


def hashes() -> dict:
    """Hash every page/layout at both measured resolutions."""
    result = {}
    for path in sorted(CORPUS.glob("*.pdf")):
        with pyropdf.open(path) as doc:
            for index, page in enumerate(doc.pages):
                for dpi in (72, 144):
                    for name, options in LAYOUTS.items():
                        pixmap = page.pixmap(dpi=dpi, **options)
                        result[f"{path.stem}/{index}/{dpi}/{name}"] = [
                            pixmap.width,
                            pixmap.height,
                            pixmap.channels,
                            hashlib.sha256(bytes(pixmap)).hexdigest(),
                        ]
    return result


def render(name: str, dpi: int) -> dict:
    """Include page iteration, rendering, raw sample copying and buffer release."""
    with pyropdf.open(CORPUS / f"{name}.pdf") as doc:
        start = time.perf_counter()
        pixels = [bytes(page.pixmap(dpi=dpi)) for page in doc.pages]
        del pixels
        elapsed = (time.perf_counter() - start) * 1000
    return {"milliseconds": elapsed}


def main() -> None:
    """Serve the parent's bounded set of corpus requests."""
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "engine": pyropdf.__version__,
                "extension_sha256": hashlib.sha256(Path(_core.__file__).read_bytes()).hexdigest(),
            }
        ),
        flush=True,
    )
    for line in sys.stdin:
        request = json.loads(line)
        result = hashes() if request["op"] == "hash" else render(request["document"], request["dpi"])
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
