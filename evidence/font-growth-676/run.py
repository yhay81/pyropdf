# ruff: noqa: INP001, T201, S603, S607 - a script that builds the harness and prints its table.
"""Time the font subsetter and compactor before and after issue #676.

Usage, from the repository root:

    uv run python evidence/font-growth-676/run.py FACE...

The harness (``src/main.rs``) times ``fonts::subset`` and
``fonts::compact::compact`` of the pyropdf-engine that it is built against.
The script builds it twice in release: in a temporary worktree of the base
commit (``--base``, by default the merge base with ``origin/main``) and in
this checkout. It runs the two builds in turn for ``--rounds`` rounds, takes
each one's median over the rounds, checks that both write results of the
same size, and prints Markdown.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import statistics
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HARNESS = HERE.relative_to(ROOT)


def git(*args: str) -> str:
    """Run git in the repository and return its output."""
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def build(checkout: Path, target: Path) -> Path:
    """Build the harness of `checkout` in release and return its binary."""
    manifest = checkout / HARNESS / "Cargo.toml"
    subprocess.run(
        ["cargo", "build", "--release", "--locked", "--quiet", "--manifest-path", str(manifest)],
        check=True,
        env={**os.environ, "CARGO_TARGET_DIR": str(target)},
    )
    return target / "release" / ("font-growth-676.exe" if platform.system() == "Windows" else "font-growth-676")


def run(binary: Path, faces: list[str]) -> dict[tuple[str, str], tuple[list[str], int]]:
    """Run `binary` once on `faces`: its fields and median by face and operation."""
    out = subprocess.run([str(binary), *faces], check=True, capture_output=True, text=True).stdout
    rows = {}
    for line in out.splitlines():
        fields = line.split("\t")
        rows[fields[0], fields[1]] = (fields[2:6], int(fields[6]))
    return rows


def main() -> None:
    """Build both harnesses, time them in turn, and print the table."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("faces", nargs="+")
    parser.add_argument("--base", default=None, help="the commit before the change")
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    faces = [str(Path(face).resolve()) for face in args.faces]
    base = args.base or git("merge-base", "HEAD", "origin/main")
    head = git("rev-parse", "HEAD")
    scratch = Path(tempfile.mkdtemp(prefix="font-growth-676-"))
    checkout = scratch / "base"
    git("worktree", "add", "--detach", str(checkout), base)
    try:
        shutil.copytree(HERE, checkout / HARNESS, dirs_exist_ok=True, ignore=shutil.ignore_patterns("target"))
        binaries = {
            "base": build(checkout, scratch / "target-base"),
            "head": build(ROOT, scratch / "target-head"),
        }
        times: dict[str, dict[tuple[str, str], list[int]]] = {"base": {}, "head": {}}
        fields: dict[str, dict[tuple[str, str], list[str]]] = {"base": {}, "head": {}}
        for _ in range(args.rounds):
            for name, binary in binaries.items():
                for key, (row, median) in run(binary, faces).items():
                    times[name].setdefault(key, []).append(median)
                    fields[name][key] = row
    finally:
        git("worktree", "remove", "--force", str(checkout))
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"Base {base[:7]}, head {head[:7]}, {args.rounds} rounds, {platform.platform()}, {platform.processor()}.")
    print()
    print("| Face | Operation | Glyphs | Kept | Face bytes | Result bytes | Base µs | Head µs | Change |")
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for key in fields["base"]:
        glyphs, kept, size, out = fields["base"][key]
        changed = fields["head"][key][3]
        out = f"{int(out):,}" if changed == out else f"{int(out):,} → {int(changed):,}"
        before = statistics.median(times["base"][key])
        after = statistics.median(times["head"][key])
        change = f"{(after - before) / before:+.1%}" if before else "-"
        print(
            f"| {key[0]} | {key[1]} | {int(glyphs):,} | {int(kept):,} | {int(size):,} | {out} "
            f"| {before:,.0f} | {after:,.0f} | {change} |"
        )


if __name__ == "__main__":
    main()
