# ruff: noqa: INP001, T201, S603 - an explicit local benchmark of repository code.
"""Compile the old and current widths_glyphs against the actual store and time them."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCE = Path("crates/pyropdf-engine/src/fonts/settle.rs")


def output(*command: str) -> str:
    """Run a command in the checkout and return its standard output."""
    result = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    if result.returncode:
        sys.stderr.write(result.stderr)
        result.check_returncode()
    return result.stdout


def function(source: str) -> str:
    """Extract the complete, top-level private function, without copying its algorithm."""
    start = source.index("fn widths_glyphs(")
    end = source.index("\n}\n", start) + 2
    return "pub " + source[start:end]


def main() -> None:
    """Build an optimized harness with real Array/Value types and print its measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="89cbba51f8cd")
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    base = output("git", "rev-parse", args.base).strip()
    before = function(output("git", "show", f"{base}:{SOURCE.as_posix()}"))
    after = function((ROOT / SOURCE).read_text(encoding="utf-8"))
    build = output("cargo", "build", "--locked", "--release", "-p", "pyropdf-store", "--message-format=json")
    artifacts = [json.loads(line) for line in build.splitlines()]
    library = next(
        Path(filename)
        for artifact in artifacts
        if artifact.get("reason") == "compiler-artifact" and artifact["target"]["name"] == "pyropdf_store"
        for filename in artifact["filenames"]
        if filename.endswith(".rlib")
    )
    dependencies = library.parent if library.parent.name == "deps" else library.parent / "deps"
    scratch = ROOT / "target" / "widths-glyphs-684"
    scratch.mkdir(parents=True, exist_ok=True)
    source = scratch / "measure.rs"
    source.write_text(
        "mod before { use std::collections::BTreeSet; use pyropdf_store::value::Value;\n"
        + before
        + "\n}\nmod after { use pyropdf_store::value::Value;\n"
        + after
        + "\n}\n"
        + (HERE / "measure.rs").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    binary = scratch / ("measure.exe" if platform.system() == "Windows" else "measure")
    output(
        "rustc",
        "--edition=2024",
        "-O",
        "-Ccodegen-units=1",
        "-Clto=thin",
        str(source),
        "--extern",
        f"pyropdf_store={library}",
        "-L",
        f"dependency={dependencies}",
        "-o",
        str(binary),
    )
    print(f"Base: `{base}`. Current function SHA-256: `{hashlib.sha256(after.encode()).hexdigest()}`.")
    print(f"\n{platform.platform()}, {platform.machine()}, {platform.processor()}.")
    print(f"\n{output('rustc', '--version').strip()}; optimized, thin LTO, {args.rounds} alternating rounds.\n")
    print(output(str(binary), str(args.rounds)), end="")


if __name__ == "__main__":
    main()
