# ruff: noqa: INP001, T201 - a script that prints its table.
"""The stamps of krilla_baseline.py, drawn with pyropdf's page.draw().

Usage: uv run python evidence/embedded-text/pyropdf_stamps.py HEBREW LATIN JAPANESE

The faces, texts, and pages are those of krilla_baseline.py: every page is
its own page.draw() commit. The save subsets each face to its drawn glyphs.
"""

import sys

import pyropdf

FACES = {
    "hebrew": ("שלום עולם", "אבגדהוזחטיכלמנסעפצקרשת"),
    "latin": ("Hello, office ffi", "abcdefghijklmnopqrstuvwxyz"),
    "japanese": ("日本語のテキスト。", "あいうえおかきくけこ日本語文字列漢字"),
}


def stamp(font: pyropdf.Font, face: str, pages: int, *, varying: bool) -> pyropdf.Document:
    """Stamp `face` on `pages` pages, one page.draw() commit per page."""
    same, letters = FACES[face]
    doc = pyropdf.Document()
    for i in range(pages):
        text = letters[i % len(letters)] + letters[(i * 7) % len(letters)] if varying else same
        with doc.pages.new((612, 792)).draw() as canvas:
            canvas.text(text, (72, 72), font=font, size=24)
    return doc


def kib(doc: pyropdf.Document, **options: object) -> str:
    """Return the saved size in KiB."""
    return f"{len(doc.to_bytes(**options)) / 1024:.1f}"  # ty: ignore[invalid-argument-type]


print("| Face | Pages | Text | Saved KiB | dedupe=True KiB | + object streams KiB |")
print("| --- | ---: | --- | ---: | ---: | ---: |")
for face, path in zip(FACES, sys.argv[1:4], strict=True):
    font = pyropdf.Font.from_file(path)
    for pages, varying in ((1, False), (100, False), (100, True)):
        doc = stamp(font, face, pages, varying=varying)
        dedupe = kib(doc, dedupe=True, prune=True)
        streams = kib(doc, dedupe=True, prune=True, object_streams=True, cross_reference="stream")
        text = "varying" if varying else "same"
        print(f"| {face} | {pages} | {text} | {kib(doc)} | {dedupe} | {streams} |")
