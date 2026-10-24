"""Count embedded font programs after stamping one face on many pages.

Usage: python font_subsets.py FONT

FONT is the Noto Sans Hebrew variable font of pylopdf-fonts-he
(NotoSansHebrew-Variable.ttf). Every page gets its own page.draw() commit.
"""

import sys

import pylopdf

LETTERS = "אבגדהוזחטיכלמנסעפצקרשת"
FONT = pylopdf.Font.from_file(sys.argv[1])


def stamp(pages: int, *, varying: bool) -> pylopdf.Document:
    doc = pylopdf.Document()
    for i in range(pages):
        page = doc.pages.new()
        if varying:
            text = LETTERS[i % len(LETTERS)] + LETTERS[(i * 7) % len(LETTERS)]
        else:
            text = "שלום עולם"
        with page.draw() as canvas:
            canvas.text(text, (72, 72), font=FONT, size=24)
    return doc


def font_programs(data: bytes) -> int:
    doc = pylopdf.open(data)
    count = 0
    for xref in doc.objects:
        obj = doc.objects[xref]
        if isinstance(obj, pylopdf.Dictionary) and any(
            key in obj for key in ("FontFile", "FontFile2", "FontFile3")
        ):
            count += 1
    return count


for pages in (1, 10, 100):
    data = stamp(pages, varying=False).to_bytes()
    print(
        f"{pages:4d} pages | {len(data) / 1024:7.1f} KiB | "
        f"{font_programs(data):4d} embedded font programs"
    )

for varying in (False, True):
    doc = stamp(100, varying=varying)
    plain = len(doc.to_bytes())
    deduped = len(doc.to_bytes(dedupe=True))
    label = "varying text" if varying else "same text"
    print(
        f"100 pages, {label:12} | {plain / 1024:7.1f} KiB | "
        f"dedupe=True {deduped / 1024:7.1f} KiB"
    )
