# ruff: noqa: INP001, T201 - standalone measurement script
"""Measure opt-in glyph caching on fresh documents; do not change the benchmark defaults.

Run with the release-wheel environment prepared by run.py. Alternating the
option order limits systematic first/second-run bias. Both options read raw
RGB samples, keep hinting disabled, and open a new document for every sample.
This is a timing experiment, not a call-stack profile or a quality assertion.
"""

from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path

import pyropdf

ROOT = Path(__file__).resolve().parents[2]
RUNS = 5


def main() -> None:
    """Write every timing sample and the best-of-five corpus totals."""
    results = {}
    for path in sorted((ROOT / "tests/assets/real_world").glob("*.pdf")):
        cases = {}
        for dpi in (72, 144):
            samples: dict[str, list[float]] = {"default": [], "glyph_cache": []}
            for run in range(RUNS):
                for enabled in (False, True) if run % 2 == 0 else (True, False):
                    with pyropdf.open(path) as doc:
                        start = time.perf_counter()
                        pixels = [bytes(page.pixmap(dpi=dpi, hinting=False, glyph_cache=enabled)) for page in doc.pages]
                        del pixels
                        elapsed = (time.perf_counter() - start) * 1000
                    samples["glyph_cache" if enabled else "default"].append(elapsed)
            cases[str(dpi)] = samples
            print(path.stem, dpi, {key: round(min(values), 2) for key, values in samples.items()}, flush=True)
        results[path.stem] = cases
    output = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "engine": pyropdf.__version__,
        "runs": RUNS,
        "hinting": False,
        "units": "milliseconds",
        "results": results,
        "totals": {
            str(dpi): {
                key: sum(min(cases[str(dpi)][key]) for cases in results.values()) for key in ("default", "glyph_cache")
            }
            for dpi in (72, 144)
        },
    }
    Path(sys.argv[1]).write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
