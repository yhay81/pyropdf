# ruff: noqa: INP001, T201 - a script, not a package; it prints its results.
"""Record PyMuPDF's page fonts of the corpus, the oracle of ``tests/test_page_fonts.py``.

    python fonts.py

Runs PyMuPDF from the environment that ``run.py`` installs, as a separate
program, and writes ``results/page-fonts.json``: for each document of
``tests/assets/real_world``, every page's ``Page.get_fonts(full=True)``
as sorted lists of ``[xref, ext, type, basefont, name, encoding,
referencer]``.
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
out = {"engine": f"PyMuPDF {pymupdf.__version__}", "fonts": {}}
for name in sys.argv[1:]:
    with pymupdf.open(name) as doc:
        pages = [sorted(list(font) for font in page.get_fonts(full=True)) for page in doc]
        out["fonts"][name.rsplit("/", 1)[-1]] = pages
print(json.dumps(out, ensure_ascii=False, indent=1))
"""


def main() -> None:
    """Write PyMuPDF's fonts of every corpus page."""
    if not PYTHON.exists():
        msg = f"run run.py first, which installs PyMuPDF at {PYTHON}"
        raise SystemExit(msg)
    result = subprocess.run(  # noqa: S603 - the pinned environment
        [PYTHON, "-I", "-c", PROGRAM, *map(str, CORPUS)], capture_output=True, check=True, text=True
    )
    out = HERE / "results" / "page-fonts.json"
    out.write_text(result.stdout, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
