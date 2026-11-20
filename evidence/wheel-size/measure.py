# ruff: noqa: INP001 - standalone evidence script.
"""Measure a full native release matrix downloaded with gh run download."""

from __future__ import annotations

import argparse
import json
import runpy
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGETS = (
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
)


def main() -> None:
    """Emit all 15 identities, compressed sizes and extension contributions as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="parent of the extracted workflow artifact directories")
    parser.add_argument("--run", required=True, help="release workflow run ID identifying the built source")
    args = parser.parse_args()
    checker = runpy.run_path(str(ROOT / "tools/wheel_size.py"))
    wheels = []
    for abi in ("abi3", "cp314t", "cp315t"):
        prefix = "wheels" if abi == "abi3" else f"free-threaded-wheels-{abi}"
        for target in TARGETS:
            path = checker["only_wheel"](args.directory / f"{prefix}-{target}")
            wheel = checker["measure"](path)
            if wheel.tag.split("-")[1] != abi:
                msg = f"expected {abi}, found {wheel.tag} in {path}"
                raise ValueError(msg)
            wheels.append({"target": target, **asdict(wheel)})
    sys.stdout.write(json.dumps({"release_run": args.run, "wheels": wheels}, indent=2) + "\n")


if __name__ == "__main__":
    main()
