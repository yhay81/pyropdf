# ruff: noqa: INP001, T201, S603, S607 - a script that builds the harness and prints its table.
"""Render the font programs that pyropdf subsets, hinted and unhinted (issues #18 and #611).

Usage, from the repository root:

    uv run python evidence/hinted-subsets/run.py

hayro 0.9 hints text by default (LaurenzV/hayro#1705). The script writes
mhlw-doc.pdf before and after ``Document.subset_fonts``, as
tests/test_font_subsetting.py and tests/test_font_subset.py compare them,
and the harness (``src/main.rs``, on the hayro commit of the workspace)
renders both at 72 dpi with and without hinting and counts the pixels that
differ. The canvas case of tests/test_canvas_embedded.py, text in a
CID-keyed CFF face that a save compacts, has no file before the save, so the
script writes the saved file again with the document's whole program in
place of the compacted one, and the harness compares those two. It also
prints the bytes that compaction writes for the CFF face, which
tests/test_canvas_embedded.py bounds. It prints Markdown.
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
STAMPS = {"latin": "Hello, office ffi", "japanese": "日本語のテキスト。"}


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


def font_program(document: pyropdf.Document) -> pyropdf.Stream:
    """Return the one CFF program that ``document`` embeds."""
    for number in document.objects:
        value = document.objects[number]
        if isinstance(value, pyropdf.Dictionary) and value.get("Type") == "FontDescriptor":
            program = value.get("FontFile3")
            if isinstance(program, pyropdf.Stream):
                return program
    message = "the document embeds no CFF program"
    raise ValueError(message)


def canvas_pair() -> tuple[Path, Path]:
    """Write the canvas text with the whole face and as its save compacts it, and return both paths."""
    document = pyropdf.Document()
    page = document.pages.new((300, 120))
    with page.draw() as canvas:
        canvas.text("日本語、かな。", (20, 40), font=pyropdf.Font.from_file(JAPANESE), size=24)
    after = OUT / "canvas-compacted.pdf"
    after.write_bytes(document.to_bytes())

    whole = font_program(document).read()
    saved = pyropdf.open(after)
    program = font_program(saved)
    compacted = len(program.read())
    program.write(whole)
    program["Subtype"] = pyropdf.Name("OpenType")
    before = OUT / "canvas-whole.pdf"
    before.write_bytes(saved.to_bytes())
    print(f"Canvas text: the program goes from {len(whole):,} to {compacted:,} bytes")
    return before, after


def stamp_sizes() -> None:
    """Print what 100 stamps in the CFF face save to, as test_100_stamps_save_no_larger_than_krilla draws them."""
    print()
    print("| stamps | saved bytes | program bytes |")
    print("| --- | --- | --- |")
    font = pyropdf.Font.from_file(JAPANESE)
    for face, text in STAMPS.items():
        letters = text.replace(" ", "")
        for varying in (False, True):
            document = pyropdf.Document()
            for index in range(100):
                line = letters[index % len(letters)] + letters[index * 7 % len(letters)] if varying else text
                with document.pages.new((612, 792)).draw() as canvas:
                    canvas.text(line, (72, 72), font=font, size=24)
            saved = document.to_bytes(dedupe=True, prune=True)
            program = len(font_program(pyropdf.open(saved)).read())
            print(f"| {face}, {'varying' if varying else 'same'} | {len(saved):,} | {program:,} |")


def main() -> None:
    """Write the documents, build and run the harness, and print the tables."""
    OUT.mkdir(parents=True, exist_ok=True)
    mhlw_before, mhlw_after = mhlw_pair()
    canvas_before, canvas_after = canvas_pair()
    subprocess.run(["cargo", "build", "--release", "--quiet"], cwd=HERE, check=True)
    executable = "hinted-subsets.exe" if platform.system() == "Windows" else "hinted-subsets"
    harness = HERE / "target" / "release" / executable
    table = subprocess.run(
        [
            str(harness),
            "mhlw-doc.pdf",
            str(mhlw_before),
            str(mhlw_after),
            "canvas text",
            str(canvas_before),
            str(canvas_after),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    print()
    print("| " + " | ".join(table[0].split("\t")) + " |")
    print("|" + " --- |" * len(table[0].split("\t")))
    for line in table[1:]:
        print("| " + " | ".join(line.split("\t")) + " |")
    stamp_sizes()


if __name__ == "__main__":
    main()
