# ruff: noqa: INP001 - a script, not a package.
"""Count the text runs of each corpus document that pass the filter of ``stretch.rs``, and name its font programs.

    uv run --no-sync python evidence/stretch-reload/count_runs.py

The script writes ``runs.txt`` beside itself. It reads the matrices of
``page.content()``, which hold the page's transform too, where the filter reads
only the text's, so it counts a run under an anisotropic transform of the page
as well: an upper bound.
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path

import pyropdf

HERE = Path(__file__).resolve().parent
CORPUS = sorted((HERE.parents[1] / "tests" / "assets" / "real_world").glob("*.pdf"))
#: What ``SAME_LENGTH`` in ``stretch.rs`` allows.
SAME_LENGTH = 1e-9


def _passes(run: pyropdf.TextRun) -> bool:
    """Return whether a glyph of an outline font's run has an x basis of another length than its y basis."""
    for glyph in run.glyphs:
        x, y = math.hypot(glyph.matrix.a, glyph.matrix.b), math.hypot(glyph.matrix.c, glyph.matrix.d)
        if glyph.glyph_id is not None and abs(x - y) > y * SAME_LENGTH:
            return True
    return False


def main() -> None:
    """Count every document's runs and write the counts."""
    warnings.simplefilter("ignore")
    lines = []
    for path in CORPUS:
        runs = passing = pages_passing = 0
        programs: set[str] = set()
        with pyropdf.open(path.read_bytes()) as document:
            for page in document.pages:
                programs |= {font.program or "none" for font in page.fonts()}
                found = [
                    _passes(item)
                    for item in page.content(paths=False, images=False, max_elements=None)
                    if isinstance(item, pyropdf.TextRun)
                ]
                runs += len(found)
                passing += sum(found)
                pages_passing += any(found)
            lines.append(
                f"{path.stem[:36]:36} {len(document.pages):3} pages, {pages_passing:3} with a passing run;"
                f" {passing:5} of {runs:5} runs pass; programs: {', '.join(sorted(programs)) or '-'}"
            )
    (HERE / "runs.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
