# ruff: noqa: INP001, T201, S603, S607 - a script that builds the harness and prints its table.
"""Time hayro-syntax's ``Pdf::new`` before and after page attributes are read on first use (issue #587).

Usage, from the repository root:

    uv run python evidence/hayro-page-attributes/run.py
    uv run python evidence/hayro-page-attributes/run.py --corpus DIRECTORY...

The harness depends on hayro-syntax twice, by git revision: at the base of
the upstream pull request and at its head (``Cargo.toml``). Without
arguments, pyropdf joins copies of bill-hr815.pdf with ``pages.extend`` into
documents of 110, 440, and 1,760 pages, as in evidence/edit-render, and the
harness opens each with both versions in turn. With ``--corpus``, it opens
every PDF below the directories instead, such as hayro's test files. It
prints Markdown.
"""

from __future__ import annotations

import platform
import re
import subprocess
import sys
from pathlib import Path

import pyropdf

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "target" / "evidence" / "hayro-page-attributes"
BILL = ROOT / "tests" / "assets" / "real_world" / "bill-hr815.pdf"
COPIES = (1, 4, 16)
#: A git dependency in ``Cargo.toml``: its name, repository, and revision.
GIT_DEPENDENCY = re.compile(r'^(\w+) = \{[^}]*git = "([^"]+)", rev = "([0-9a-f]+)"', re.MULTILINE)


def build_documents() -> list[Path]:
    """Write the documents of 110, 440, and 1,760 pages and return their paths."""
    paths = []
    for copies in COPIES:
        doc = pyropdf.open(BILL)
        for _ in range(copies - 1):
            doc.pages.extend(pyropdf.open(BILL).pages)
        path = OUT / f"pages-{len(doc.pages)}.pdf"
        path.write_bytes(doc.to_bytes())
        paths.append(path)
    return paths


def revisions() -> list[str]:
    """Return a link to each hayro-syntax commit that ``Cargo.toml`` names."""
    manifest = (HERE / "Cargo.toml").read_text(encoding="utf-8")
    return [f"{name}: {url}/commit/{rev}" for name, url, rev in GIT_DEPENDENCY.findall(manifest)]


def main() -> None:
    """Build the documents and the harness, run it, and print the table."""
    if sys.argv[1:2] == ["--corpus"]:
        arguments = sys.argv[1:]
        runs = 5
    else:
        OUT.mkdir(parents=True, exist_ok=True)
        arguments = [str(path) for path in build_documents()]
        runs = 101
    rustc = subprocess.run(["rustc", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    harness = ["cargo", "run", "--release", "--quiet", "--locked", "--manifest-path", str(HERE / "Cargo.toml"), "--"]
    table = subprocess.run([*harness, *arguments], capture_output=True, text=True, check=True).stdout
    for revision in revisions():
        print(f"- {revision}")
    print(f"- pyropdf {pyropdf.__version__}, {rustc}")
    print(f"- {platform.system()} {platform.machine()}; median of {runs} runs per document, version, and workload")
    print()
    print(table.strip())


if __name__ == "__main__":
    main()
