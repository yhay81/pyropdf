"""Read every save of every edit of the corpus with independent readers.

pypdfium2, pymupdf, and pdf.js read each document of the real-world corpus
and of the generated PDF/A and PDF/UA corpora, and each save of each edit of
the preservation harness's EDITS applied to it, in full and incrementally.
Each reader's reading of a save must match its reading of the original: the
edited document's page count, and on every page that the edit did not touch,
found again by its object number, the same size and extracted text. The readers are
development-only oracles, outside the package's dependencies:

    uv run --group oracles python tools/oracles.py --readers pypdfium2 pymupdf
    npm ci --ignore-scripts --prefix tools/pdfjs
    uv run python tools/oracles.py --readers pdfjs

CI runs the pdf.js reader from tools/pdfjs's lockfile. The script prints one
line per document and reader and every difference, and exits with status 1
on a difference that ``EXPLAINED`` does not list.
"""

from __future__ import annotations

import argparse
import json
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

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


def pdfium(files: list[bytes]) -> list[Reading]:
    """Return pypdfium2's reading of each of ``files``."""
    import pypdfium2  # noqa: PLC0415 - the oracles group, which CI does not install.

    out = []
    for data in files:
        document = pypdfium2.PdfDocument(data)
        try:
            pages = []
            for page in document:
                width, height = page.get_size()
                pages.append(((round(width, 2), round(height, 2)), page.get_textpage().get_text_range()))
            out.append(pages)
        finally:
            document.close()
    return out


def mupdf(files: list[bytes]) -> list[Reading]:
    """Return pymupdf's reading of each of ``files``."""
    import pymupdf  # noqa: PLC0415 - the oracles group, which CI does not install.

    pymupdf.TOOLS.mupdf_display_errors(False)  # noqa: FBT003 - pymupdf's own API.
    out = []
    for data in files:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            out.append(
                [((round(page.rect.width, 2), round(page.rect.height, 2)), page.get_text()) for page in document]
            )
    return out


def pdfjs(files: list[bytes]) -> list[Reading]:
    """Return pdf.js's reading of each of ``files``, through tools/pdfjs/read.mjs."""
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        for index, data in enumerate(files):
            path = Path(directory) / f"{index}.pdf"
            path.write_bytes(data)
            paths.append(str(path))
        script = str(ROOT / "tools" / "pdfjs" / "read.mjs")
        result = subprocess.run(["node", script, *paths], capture_output=True, text=True, check=False)  # noqa: S603, S607 - a fixed script on generated files.
    if result.returncode:
        sys.stderr.write(result.stderr)
        msg = f"pdf.js exited with status {result.returncode}"
        raise SystemExit(msg)
    return [
        [((round(width, 2), round(height, 2)), text) for width, height, text in json.loads(line)]
        for line in result.stdout.splitlines()
    ]


READERS: dict[str, Callable[[list[bytes]], list[Reading]]] = {"pypdfium2": pdfium, "pymupdf": mupdf, "pdfjs": pdfjs}


def differences(original: Reading, saved: Reading, untouched: list[tuple[int, int]], *, count: int) -> list[str]:
    """Return how ``saved`` differs from ``original`` on the pages that the edit did not touch.

    ``untouched`` pairs each such page's index in the original with its index
    in the save, since edits add and move pages.
    """
    if len(saved) != count:
        return [f"page count {len(saved)}, expected {count}"]
    out = []
    for index, at in untouched:
        if saved[at][0] != original[index][0]:
            out.append(f"page {index} size {saved[at][0]}, was {original[index][0]}")
        if saved[at][1] != original[index][1]:
            out.append(f"page {index} text changed")
    return out


def check(source: Path, readers: list[str]) -> list[str]:
    """Return the unexplained differences of every save of every edit of ``source``."""
    data = source.read_bytes()
    files = [data]
    cases = []
    for edit, apply in sorted(EDITS.items()):
        for incremental in (False, True):
            doc = pyropdf.open(data)
            pages = [page.obj.xref for page in doc.pages]
            touched = apply(doc)
            after = [page.obj.xref for page in doc.pages]
            files.append(doc.to_bytes(incremental=incremental))
            untouched = [
                (index, after.index(number))
                for index, number in enumerate(pages)
                if number not in touched and number in after
            ]
            cases.append((edit, "incremental" if incremental else "full", untouched, len(after)))
    unexplained = []
    for reader in readers:
        original, *saved = READERS[reader](files)
        sys.stdout.write(f"{source.relative_to(ASSETS)} {reader}: {len(original)} pages\n")
        for (edit, mode, untouched, count), reading in zip(cases, saved, strict=True):
            for difference in differences(original, reading, untouched, count=count):
                key = f"{reader} {source.name} {edit} {mode} {difference.split(' size')[0].split(' text')[0]}"
                note = EXPLAINED.get(key)
                sys.stdout.write(f"  {reader} {edit} {mode}: {difference}{f' ({note})' if note else ''}\n")
                if note is None:
                    unexplained.append(key)
    return unexplained


def main() -> int:
    """Check every source, and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--readers", nargs="+", choices=sorted(READERS), default=sorted(READERS))
    readers = parser.parse_args().readers
    unexplained = [key for source in SOURCES for key in check(source, readers)]
    saves = len(SOURCES) * len(EDITS) * 2
    sys.stdout.write(f"\n{len(SOURCES)} documents, {saves} saves, readers {', '.join(readers)}: ")
    sys.stdout.write(f"{len(unexplained)} unexplained differences\n")
    return 1 if unexplained else 0


if __name__ == "__main__":
    raise SystemExit(main())
