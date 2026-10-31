# ruff: noqa: INP001, T201, S603, S607 - a script that runs the probe and prints its table.
"""Measure route C of issue #466: one font resource per document.

Usage: uv run python evidence/embedded-text/measure.py HEBREW LATIN JAPANESE

HEBREW is pylopdf-fonts-he's NotoSansHebrew-Variable.ttf, the face of
evidence/pylopdf-baseline/font_subsets.py; LATIN is a static TrueType face
(NotoSans-Regular.ttf); JAPANESE is a CFF-based OpenType face
(NotoSansJP-Regular.otf). Fonts are not committed.

The probe in probe/ shapes and writes the pages and embeds the whole program
once, as a provisional font object would hold it. This script opens the
result with pyropdf, checks every page's text and the first page's pixels,
subsets with doc.subset_fonts(), which keeps glyph IDs, and saves.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pyropdf

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
TARGET = ROOT / "target" / "embedded-text"
PROBE = TARGET / "release" / "embedded-text-probe"

FACES = {
    "hebrew": ("שלום עולם", "אבגדהוזחטיכלמנסעפצקרשת"),
    "latin": ("Hello, office ffi", "abcdefghijklmnopqrstuvwxyz"),
    "japanese": ("日本語のテキスト。", "あいうえおかきくけこ日本語文字列漢字"),
}


def texts(face: str, pages: int, *, varying: bool) -> list[str]:
    """Return the texts that the pages draw, in turn."""
    same, letters = FACES[face]
    if not varying:
        return [same]
    return [letters[i % len(letters)] + letters[(i * 7) % len(letters)] for i in range(pages)]


def font_programs(doc: pyropdf.Document) -> int:
    """Count the embedded font programs."""
    count = 0
    for xref in doc.objects:
        obj = doc.objects[xref]
        if isinstance(obj, pyropdf.Dictionary) and any(key in obj for key in ("FontFile", "FontFile2", "FontFile3")):
            count += 1
    return count


def run(face: str, path: str, pages: int, *, varying: bool, prune: bool) -> dict[str, object]:
    """Write, check, subset, and save one configuration."""
    out = TARGET / f"{face}-{pages}-{int(varying)}-{int(prune)}.pdf"
    page_texts = texts(face, pages, varying=varying)
    env = {key: value for key, value in os.environ.items() if key != "PRUNE"}
    if prune:
        env["PRUNE"] = "1"
    probe = subprocess.run(
        [PROBE, path, out, str(pages), *page_texts],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    written = json.loads(probe.stdout)
    data = out.read_bytes()
    doc = pyropdf.open(data)
    # Characters, not order: right-to-left marks reorder in extraction.
    text_ok = all(
        set("".join(doc.pages[i].text().split())) == set("".join(page_texts[i % len(page_texts)].split()))
        for i in range(pages)
    )
    pixmap = doc.pages[0].pixmap()
    inked = any(pixmap.pixel(x, y)[:3] != (255, 255, 255) for x in range(300) for y in range(100))
    started = time.perf_counter()
    subset = doc.subset_fonts()
    subset_seconds = time.perf_counter() - started
    saved = doc.to_bytes()
    after = pyropdf.open(saved)
    return {
        "face": face,
        "pages": pages,
        "varying": varying,
        "prune": prune,
        "glyphs": written["glyphs"],
        "write_seconds": written["seconds"],
        "written_bytes": len(data),
        "programs": font_programs(after),
        "subset_seconds": round(subset_seconds, 4),
        "saved_bytes": len(saved),
        "kept": [(font.kept, font.glyphs) for font in subset],
        "text_ok": text_ok,
        "inked": inked,
    }


def main() -> None:
    """Build the probe, measure every configuration, and print the table."""
    subprocess.run(
        ["cargo", "build", "--release", "--quiet", "--manifest-path", HERE / "probe" / "Cargo.toml"],
        check=True,
        env={**os.environ, "CARGO_TARGET_DIR": str(TARGET)},
    )
    paths = dict(zip(FACES, sys.argv[1:4], strict=True))
    rows = [
        run(face, path, pages, varying=varying, prune=prune)
        for face, path in paths.items()
        for pages, varying in ((1, False), (100, False), (100, True))
        for prune in (False, True)
    ]
    print(
        "| Face | Pages | Text | Pruned | Glyphs | Programs | Saved KiB | Kept/glyphs "
        "| Write s | Subset s | Text | Ink |"
    )
    print("| --- | ---: | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | --- | --- |")
    for row in rows:
        print(
            f"| {row['face']} | {row['pages']} | {'varying' if row['varying'] else 'same'} | "
            f"{'yes' if row['prune'] else 'no'} | {row['glyphs']} | {row['programs']} | "
            f"{row['saved_bytes'] / 1024:.1f} | {', '.join(f'{k}/{g}' for k, g in row['kept'])} | "
            f"{row['write_seconds']:.3f} | {row['subset_seconds']:.3f} | "
            f"{'ok' if row['text_ok'] else 'FAIL'} | {'ok' if row['inked'] else 'FAIL'} |"
        )
    (HERE / "results.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
