"""Read every save of every edit of the corpus with independent readers.

pypdfium2 and pymupdf read each document of the real-world corpus and of the
generated PDF/A and PDF/UA corpora, and each save of each edit of the
preservation harness's EDITS applied to it, in full and incrementally. Each
reader's reading of a save must match its reading of the original: the same
page count, plus one after ``new_page``, and on every page that the edit did
not touch, the same size and extracted text. The readers are development-only
oracles, outside CI and the package's dependencies:

    uv run --group oracles python tools/oracles.py

The script prints one line per document and reader and every difference, and
exits with status 1 on a difference that ``EXPLAINED`` does not list.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf
import pypdfium2

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parent.parent
# The preservation harness imports its sibling test modules.
sys.path.insert(0, str(ROOT / "tests"))
EDITS = runpy.run_path(str(ROOT / "tests" / "test_preservation.py"))["EDITS"]
ASSETS = ROOT / "tests" / "assets"
SOURCES = sorted(
    [*(ASSETS / "real_world").glob("*.pdf"), *(ASSETS / "pdfa").glob("*.pdf"), *(ASSETS / "pdfua").glob("*.pdf")]
)

#: A reader's reading of each page: its size in points, rounded, and its text.
Reading = list[tuple[tuple[float, float], str]]

#: Differences that are expected, by "reader file edit mode page", with the reason or issue.
EXPLAINED: dict[str, str] = {}


def pdfium(data: bytes) -> Reading:
    """Return pypdfium2's reading of ``data``."""
    document = pypdfium2.PdfDocument(data)
    try:
        out = []
        for page in document:
            width, height = page.get_size()
            out.append(((round(width, 2), round(height, 2)), page.get_textpage().get_text_range()))
        return out
    finally:
        document.close()


def mupdf(data: bytes) -> Reading:
    """Return pymupdf's reading of ``data``."""
    with pymupdf.open(stream=data, filetype="pdf") as document:
        return [((round(page.rect.width, 2), round(page.rect.height, 2)), page.get_text()) for page in document]


READERS: dict[str, Callable[[bytes], Reading]] = {"pypdfium2": pdfium, "pymupdf": mupdf}


def differences(original: Reading, saved: Reading, untouched: list[int], *, added: int) -> list[str]:
    """Return how ``saved`` differs from ``original`` on the pages that the edit did not touch."""
    if len(saved) != len(original) + added:
        return [f"page count {len(saved)}, expected {len(original) + added}"]
    out = []
    for index in untouched:
        if saved[index][0] != original[index][0]:
            out.append(f"page {index} size {saved[index][0]}, was {original[index][0]}")
        if saved[index][1] != original[index][1]:
            out.append(f"page {index} text changed")
    return out


def check(source: Path) -> list[str]:
    """Return the unexplained differences of every save of every edit of ``source``."""
    data = source.read_bytes()
    originals = {reader: read(data) for reader, read in READERS.items()}
    unexplained = []
    for edit, apply in sorted(EDITS.items()):
        for incremental in (False, True):
            doc = pyropdf.open(data)
            pages = [page.obj.xref for page in doc.pages]
            touched = apply(doc)
            saved = doc.to_bytes(incremental=incremental)
            untouched = [index for index, number in enumerate(pages) if number not in touched]
            mode = "incremental" if incremental else "full"
            for reader, read in READERS.items():
                found = differences(originals[reader], read(saved), untouched, added=edit == "new_page")
                for difference in found:
                    key = f"{reader} {source.name} {edit} {mode} {difference.split(' size')[0].split(' text')[0]}"
                    note = EXPLAINED.get(key)
                    sys.stdout.write(f"  {reader} {edit} {mode}: {difference}{f' ({note})' if note else ''}\n")
                    if note is None:
                        unexplained.append(key)
    for reader, reading in originals.items():
        sys.stdout.write(f"{source.relative_to(ASSETS)} {reader}: {len(reading)} pages\n")
    return unexplained


def main() -> int:
    """Check every source, and return the exit status."""
    pymupdf.TOOLS.mupdf_display_errors(False)  # noqa: FBT003 - pymupdf's own API.
    unexplained = [key for source in SOURCES for key in check(source)]
    saves = len(SOURCES) * len(EDITS) * 2
    sys.stdout.write(f"\n{len(SOURCES)} documents, {saves} saves, {len(READERS)} readers: ")
    sys.stdout.write(f"{len(unexplained)} unexplained differences\n")
    return 1 if unexplained else 0


if __name__ == "__main__":
    raise SystemExit(main())
