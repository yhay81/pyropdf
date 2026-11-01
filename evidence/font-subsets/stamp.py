# ruff: noqa: INP001, T201 - a script that prints its table.
"""Stamp one embedded face on 100 pages with pyropdf's canvas (issue #481).

Usage: uv run python evidence/font-subsets/stamp.py HEBREW LATIN JAPANESE

The faces are those of evidence/embedded-text: pylopdf-fonts-he's
NotoSansHebrew-Variable.ttf, NotoSans-Regular.ttf, and NotoSansJP-Regular.otf.
Fonts are not committed. Each page is one ``page.draw()``. The script counts
the font programs and Type0 fonts of the saved file, checks every page's
text, and records the sizes before subsetting, which is issue #482.
"""

import json
import sys
import time
from pathlib import Path

import pyropdf

HERE = Path(__file__).resolve().parent
FACES = {
    "hebrew": ("שלום עולם", "אבגדהוזחטיכלמנסעפצקרשת"),
    "latin": ("Hello, office ffi", "abcdefghijklmnopqrstuvwxyz"),
    "japanese": ("日本語のテキスト。", "あいうえおかきくけこ日本語文字列漢字"),
}
PAGES = 100


def texts(face: str, *, varying: bool) -> list[str]:
    """Return the text of each page."""
    same, letters = FACES[face]
    if not varying:
        return [same] * PAGES
    return [letters[i % len(letters)] + letters[(i * 7) % len(letters)] for i in range(PAGES)]


def counts(document: pyropdf.Document) -> tuple[int, int]:
    """Count the font programs and the Type0 fonts."""
    programs = fonts = 0
    for xref in document.objects:
        value = document.objects[xref]
        if not isinstance(value, pyropdf.Dictionary):
            continue
        if any(key in value for key in ("FontFile", "FontFile2", "FontFile3")):
            programs += 1
        if value.get("Subtype") == "Type0":
            fonts += 1
    return programs, fonts


def run(face: str, path: str, *, varying: bool) -> dict[str, object]:
    """Stamp, save, reopen, and check one configuration."""
    font = pyropdf.Font.from_file(path)
    page_texts = texts(face, varying=varying)
    document = pyropdf.Document()
    started = time.perf_counter()
    for text in page_texts:
        page = document.pages.new((300, 100))
        with page.draw() as canvas:
            canvas.text(text, (10, 50), font=font, size=14)
    drawn = time.perf_counter() - started
    data = document.to_bytes()
    reopened = pyropdf.open(data)
    programs, fonts = counts(reopened)
    text_ok = all(page.text().strip() == text for page, text in zip(reopened.pages, page_texts, strict=True))
    return {
        "face": face,
        "pages": PAGES,
        "varying": varying,
        "draw_seconds": round(drawn, 3),
        "programs": programs,
        "type0_fonts": fonts,
        "saved_bytes": len(data),
        "program_bytes": len(font.program() or b""),
        "text_ok": text_ok,
    }


def main() -> None:
    """Run every face with the same and with varying text."""
    results = [
        run(face, path, varying=varying)
        for face, path in zip(FACES, sys.argv[1:], strict=True)
        for varying in (False, True)
    ]
    (HERE / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
    print("| Face | Text | Programs | Type0 fonts | Saved KiB | Program KiB | Draw s | Text |")
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for row in results:
        print(
            f"| {row['face']} | {'varying' if row['varying'] else 'same'} | {row['programs']} | "
            f"{row['type0_fonts']} | {row['saved_bytes'] / 1024:.1f} | {row['program_bytes'] / 1024:.1f} | "
            f"{row['draw_seconds']:.3f} | {'ok' if row['text_ok'] else 'WRONG'} |"
        )


if __name__ == "__main__":
    main()
