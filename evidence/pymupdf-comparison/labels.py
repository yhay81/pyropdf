# ruff: noqa: INP001, T201 - a script, not a package; it prints its results.
"""Record PyMuPDF's page labels of the corpus, the oracle of ``tests/test_page_labels.py``.

    python labels.py

Runs PyMuPDF from the environment that ``run.py`` installs, as a separate
program, and writes ``results/page-labels.json``: for each document of
``tests/assets/real_world``, every page's ``Page.get_label()``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PYTHON = ROOT / "target" / "pymupdf-comparison" / "pymupdf-env" / "bin" / "python"
CORPUS = sorted((ROOT / "tests" / "assets" / "real_world").glob("*.pdf"))
PROGRAM = """
import json, sys
import pymupdf
out = {"engine": f"PyMuPDF {pymupdf.__version__}", "labels": {}}
for name in sys.argv[1:]:
    with pymupdf.open(name) as doc:
        out["labels"][name.rsplit("/", 1)[-1]] = [page.get_label() for page in doc]
print(json.dumps(out, ensure_ascii=False, indent=2))
"""


def main() -> None:
    """Write PyMuPDF's labels of every corpus document."""
    if not PYTHON.exists():
        msg = f"run run.py first, which installs PyMuPDF at {PYTHON}"
        raise SystemExit(msg)
    result = subprocess.run(  # noqa: S603 - the pinned environment
        [PYTHON, "-I", "-c", PROGRAM, *map(str, CORPUS)], capture_output=True, check=True, text=True
    )
    out = HERE / "results" / "page-labels.json"
    out.write_text(result.stdout, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
