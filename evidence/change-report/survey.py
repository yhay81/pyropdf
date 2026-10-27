# ruff: noqa: INP001, T201 - a script that prints a Markdown table.
"""Count the corpus documents that a change report would speak about.

For each document of tests/assets: its revisions (`startxref` keywords),
signature dictionaries (`/Type /Sig`, or a `/ByteRange`), a `/Perms`
entry (DocMDP or UR3), and the PDF/A and PDF/UA claims of its XMP
metadata. Run with `uv run python evidence/change-report/survey.py`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pyropdf

ASSETS = Path(__file__).resolve().parents[2] / "tests" / "assets"
CLAIM = re.compile(rb"(pdfa|pdfua)id:part\s*(?:=\s*['\"]\s*(\d+)|>\s*(\d+))")


def survey(path: Path) -> tuple[int, int, bool, list[str]]:
    """Return the revisions, signatures, `/Perms` presence, and claims of `path`."""
    data = path.read_bytes()
    revisions = len(re.findall(rb"startxref", data))
    doc = pyropdf.open(data)
    signatures = 0
    for number in doc.objects:
        try:
            value = doc.objects[number]
        except pyropdf.PdfError:
            continue
        if isinstance(value, pyropdf.Dictionary) and (value.get("Type") == pyropdf.Name("Sig") or "ByteRange" in value):
            signatures += 1
    catalog = doc.catalog
    claims: list[str] = []
    metadata = catalog.get("Metadata")
    if isinstance(metadata, pyropdf.Stream):
        for kind, attribute, element in CLAIM.findall(metadata.read()):
            claims.append(f"{kind.decode()}-{(attribute or element).decode()}")
    return revisions, signatures, "Perms" in catalog, claims


def main() -> int:
    """Print the table of every document of the corpus."""
    print("| Document | Revisions | Signatures | /Perms | Claims |")
    print("| --- | ---: | ---: | --- | --- |")
    for path in sorted(ASSETS.rglob("*.pdf")):
        name = path.relative_to(ASSETS)
        try:
            revisions, signatures, perms, claims = survey(path)
        except pyropdf.PdfError as error:
            print(f"| {name} | | | | not read: {type(error).__name__} |")
            continue
        print(f"| {name} | {revisions} | {signatures} | {'yes' if perms else ''} | {', '.join(claims)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
