# ruff: noqa: INP001, T201 - standalone release-wheel verification
"""Compare consecutive-page rendering against the stored per-page pixel hashes.

Each pass uses a fresh document. The first twelve passes render all pages
in one layout and resolution. Additional passes alternate layouts and DPI
and revisit pages out of order to exercise context reset and eviction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pyropdf
from pyropdf import _core

ROOT = Path(__file__).resolve().parents[2]
LAYOUTS = {
    "rgb": {},
    "rgb-orange": {"background": (200, 120, 40)},
    "gray": {"colorspace": "gray"},
    "rgba": {"alpha": True},
    "rgba-green": {"alpha": True, "background": (10, 200, 90, 128)},
    "gray-alpha": {"alpha": True, "colorspace": "gray"},
}
CASES = [(dpi, layout) for layout in LAYOUTS for dpi in (72, 144)]


def cases(pages: int, mode: str, case: int) -> list[tuple[int, int, str]]:
    """Return page positions and options for one fixed corpus pass."""
    order = list(range(pages))
    if mode == "revisit":
        order = [*order[::2], *order[1::2], *reversed(order)]
    return [(p, *CASES[case if mode == "sequential" else n % len(CASES)]) for n, p in enumerate(order)]


def main() -> None:
    """Check pixel layout and bytes, retaining any counterexamples."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    reference = json.loads(args.reference.read_bytes())
    checked = set()
    failures = []
    counts = {"sequential": 0, "mixed": 0, "revisit": 0}
    passes = [("sequential", n) for n in range(len(CASES))] + [("mixed", 0), ("revisit", 0)]
    for path in sorted((ROOT / "tests/assets/real_world").glob("*.pdf")):
        for mode, case in passes:
            with pyropdf.open(path) as doc:
                for index, dpi, layout in cases(len(doc.pages), mode, case):
                    key = f"{path.stem}/{index}/{dpi}/{layout}"
                    pixmap = doc.pages[index].pixmap(dpi=dpi, **LAYOUTS[layout])
                    row = [pixmap.width, pixmap.height, pixmap.channels, hashlib.sha256(bytes(pixmap)).hexdigest()]
                    if row != reference[key]:
                        failures.append({"mode": mode, "key": key, "actual": row})
                    counts[mode] += 1
                    checked.add(key)
    complete = checked == reference.keys()
    result = {
        "extension_sha256": hashlib.sha256(Path(_core.__file__).read_bytes()).hexdigest(),
        "probe_threads": os.getenv("PYROPDF_PROBE_THREADS"),
        "counts": counts,
        "covered_all_reference_keys": complete,
        "mismatches": failures,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(counts, "complete:", complete, "mismatches:", len(failures))
    if not complete or failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
