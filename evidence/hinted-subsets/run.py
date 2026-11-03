# ruff: noqa: INP001, T201, S603, S607 - a script that builds the harness and prints its table.
"""Render the font programs that pyropdf subsets, hinted and unhinted (issue #18).

Usage, from the repository root:

    uv run python evidence/hinted-subsets/run.py

hayro 0.9 hints text by default (LaurenzV/hayro#1705). The script writes
mhlw-doc.pdf before and after ``Document.subset_fonts``, as
tests/test_font_subsetting.py and tests/test_font_subset.py compare them,
and the harness (``src/main.rs``, on the hayro commit of the workspace)
renders both at 72 dpi with and without hinting and counts the pixels that
differ. The canvas case of tests/test_canvas_embedded.py, text in a
CID-keyed CFF face that a save compacts, has no document of its own before
the save, so the script compares pyropdf's own renders before and after the
save, which hint only when pyropdf renders with hinting. It prints Markdown.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import pyropdf

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "target" / "evidence" / "hinted-subsets"
MHLW = ROOT / "tests" / "assets" / "real_world" / "mhlw-doc.pdf"
JAPANESE = ROOT / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata" / "NotoSansJP-subset.otf"


def mhlw_pair() -> tuple[Path, Path]:
    """Write mhlw-doc.pdf as it is and after ``subset_fonts``, and return both paths."""
    before = OUT / "mhlw-doc.pdf"
    before.write_bytes(MHLW.read_bytes())
    doc = pyropdf.open(MHLW)
    subsets = doc.subset_fonts()
    after = OUT / "mhlw-doc-subset.pdf"
    after.write_bytes(doc.to_bytes())
    sizes = f"{before.stat().st_size:,} to {after.stat().st_size:,} bytes"
    print(f"mhlw-doc.pdf: programs subset: {len(subsets)}; the file goes from {sizes}")
    return before, after


def canvas_cff() -> tuple[int, int]:
    """Return the differing and all pixels of the canvas text before and after its save compacts the face."""
    document = pyropdf.Document()
    page = document.pages.new((300, 120))
    with page.draw() as canvas:
        canvas.text("日本語、かな。", (20, 40), font=pyropdf.Font.from_file(JAPANESE), size=24)
    before = bytes(page.pixmap(dpi=72))
    after = bytes(pyropdf.open(document.to_bytes()).pages[0].pixmap(dpi=72))
    pixels = page.pixmap(dpi=72)
    step = len(before) // (pixels.width * pixels.height)
    differing = sum(1 for at in range(0, len(before), step) if before[at : at + step] != after[at : at + step])
    return differing, pixels.width * pixels.height


def main() -> None:
    """Write the documents, build and run the harness, and print the tables."""
    OUT.mkdir(parents=True, exist_ok=True)
    before, after = mhlw_pair()
    subprocess.run(["cargo", "build", "--release", "--quiet"], cwd=HERE, check=True)
    executable = "hinted-subsets.exe" if platform.system() == "Windows" else "hinted-subsets"
    harness = HERE / "target" / "release" / executable
    table = subprocess.run(
        [str(harness), "mhlw-doc.pdf", str(before), str(after)], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    print()
    print("| " + " | ".join(table[0].split("\t")) + " |")
    print("|" + " --- |" * len(table[0].split("\t")))
    for line in table[1:]:
        print("| " + " | ".join(line.split("\t")) + " |")
    differing, pixels = canvas_cff()
    print()
    print(
        "Canvas text in a CID-keyed CFF face, rendered by pyropdf before and after the save: "
        f"{differing} of {pixels} pixels differ."
    )


if __name__ == "__main__":
    main()
