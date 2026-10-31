# ruff: noqa: INP001, T201 - a script that prints its table.
"""The same stamps through krilla's public API, as pylopdf draws them.

Usage: uv run --no-project --with pylopdf==0.16.0 python -I krilla_baseline.py HEBREW LATIN JAPANESE

The faces and texts are those of measure.py. Every page is its own
page.draw() commit, so each embeds its own subset (DESIGN.md section 2.1).
"""

import sys

import pylopdf

FACES = {
    "hebrew": ("שלום עולם", "אבגדהוזחטיכלמנסעפצקרשת"),
    "latin": ("Hello, office ffi", "abcdefghijklmnopqrstuvwxyz"),
    "japanese": ("日本語のテキスト。", "あいうえおかきくけこ日本語文字列漢字"),
}


def stamp(font: pylopdf.Font, face: str, pages: int, *, varying: bool) -> pylopdf.Document:
    """Stamp `face` on `pages` pages, one page.draw() commit per page."""
    same, letters = FACES[face]
    doc = pylopdf.Document()
    for i in range(pages):
        text = letters[i % len(letters)] + letters[(i * 7) % len(letters)] if varying else same
        with doc.pages.new().draw() as canvas:
            canvas.text(text, (72, 72), font=font, size=24)
    return doc


def font_programs(data: bytes) -> int:
    """Count the embedded font programs."""
    doc = pylopdf.open(data)
    return sum(
        1
        for xref in doc.objects
        if isinstance(obj := doc.objects[xref], pylopdf.Dictionary)
        and any(key in obj for key in ("FontFile", "FontFile2", "FontFile3"))
    )


print("| Face | Pages | Text | Programs | Saved KiB | dedupe=True KiB |")
print("| --- | ---: | --- | ---: | ---: | ---: |")
for face, path in zip(FACES, sys.argv[1:4], strict=True):
    font = pylopdf.Font.from_file(path)
    for pages, varying in ((1, False), (100, False), (100, True)):
        doc = stamp(font, face, pages, varying=varying)
        data = doc.to_bytes()
        deduped = doc.to_bytes(dedupe=True)
        print(
            f"| {face} | {pages} | {'varying' if varying else 'same'} | {font_programs(data)} | "
            f"{len(data) / 1024:.1f} | {len(deduped) / 1024:.1f} |"
        )
