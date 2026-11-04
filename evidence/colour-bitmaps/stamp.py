# ruff: noqa: INP001, T201, PLR2004 - a script that prints its table.
"""Stamp a colour bitmap face on 100 pages with pyropdf's canvas (issue #631).

Usage: uv run --with fonttools python evidence/colour-bitmaps/stamp.py [FACE ...]

The method is that of ``evidence/font-subsets/stamp.py``: each page is one
``page.draw()``, "same" draws one text on every page and "varying" two
characters per page, and the sizes come from the same three saves. The script
counts the Type 3 fonts, procedures, images, and font programs of the saved
file and checks every page's text.

It builds two faces with the code of ``colour_faces.py``, with no outlines: a
CBDT and an ``sbix`` table of one strike of 109 pixels per em, as Noto Color
Emoji has, whose images are flat shapes in a round mask, of about 1 KB each.
FACE names other faces to stamp, which are not committed: each draws its first
26 mapped characters above U+2000.
"""

import json
import math
import struct
import sys
import time
import zlib
from pathlib import Path

import pyropdf

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"))

import colour_faces  # noqa: E402 - found through the path above.

PAGES = 100
SIZE = 109
GLYPHS = 26


def png(seed: int) -> bytes:
    """Return a drawing of ``SIZE`` pixels, flat colours in a round mask, chosen by ``seed``."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    base = (seed * 37 % 256, seed * 91 % 256, seed * 151 % 256)
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)
        for x in range(SIZE):
            u, v = x / SIZE - 0.5, y / SIZE - 0.5
            radius = math.hypot(u, v) * 2
            if radius >= 1:
                rows += bytes(4)
                continue
            # Bands of shade, an edge, and two marks that differ by glyph.
            shade = 1 - int(radius * 6) / 12
            mark = math.hypot(u - 0.2, v + 0.15) < 0.08 or math.hypot(u + 0.2, v + 0.15 + seed % 5 / 40) < 0.08
            colour = (24, 24, 24) if mark or radius > 0.96 else tuple(int(channel * shade) for channel in base)
            rows += bytes((*colour, 255))
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    idat = chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + idat + chunk(b"IEND", b"")


def built_faces() -> dict[str, tuple[pyropdf.Font, str]]:
    """Return the two faces that this builds, with the characters that they draw."""
    strikes = ((SIZE, {f"many{index}": png(index) for index in range(GLYPHS)}),)
    letters = "".join(chr(0xE000 + index) for index in range(GLYPHS))
    return {
        table: (
            pyropdf.Font.from_bytes(colour_faces.bitmap_face(table, outlines=False, many=GLYPHS, strikes=strikes)),
            letters,
        )
        for table in ("cbdt", "sbix")
    }


def named_face(path: str) -> tuple[pyropdf.Font, str]:
    """Return a face from a file, with characters that it maps."""
    font = pyropdf.Font.from_file(path)
    letters = "".join(char for char in font.chars() if ord(char) > 0x2000 and not char.isspace())[:GLYPHS]
    return font, letters


def texts(letters: str, *, varying: bool) -> list[str]:
    """Return the text of each page."""
    if not varying:
        return [letters[:5]] * PAGES
    return [letters[i % len(letters)] + letters[(i * 7) % len(letters)] for i in range(PAGES)]


def counts(document: pyropdf.Document) -> dict[str, int]:
    """Count the fonts, procedures, images, and programs of the saved file."""
    fonts = procedures = programs = images = 0
    for xref in document.objects:
        value = document.objects[xref]
        if not isinstance(value, pyropdf.Dictionary):
            continue
        if value.get("Subtype") == "Type3":
            fonts += 1
            procedures += len(value["CharProcs"])
            images += len(value["Resources"].get("XObject", {}))
        for key in ("FontFile", "FontFile2", "FontFile3"):
            programs += isinstance(value.get(key), pyropdf.Stream)
    return {"type3_fonts": fonts, "procedures": procedures, "images": images, "programs": programs}


def run(name: str, font: pyropdf.Font, letters: str, *, varying: bool) -> dict[str, object]:
    """Stamp, save, reopen, and check one configuration."""
    page_texts = texts(letters, varying=varying)
    document = pyropdf.Document()
    started = time.perf_counter()
    for text in page_texts:
        page = document.pages.new((300, 100))
        with page.draw() as canvas:
            canvas.text(text, (10, 50), font=font, size=14)
    drawn = time.perf_counter() - started
    data = document.to_bytes()
    reopened = pyropdf.open(data)
    return {
        "face": name,
        "pages": PAGES,
        "varying": varying,
        "draw_seconds": round(drawn, 3),
        **counts(reopened),
        "saved_bytes": len(data),
        "saved_dedupe_bytes": len(document.to_bytes(prune=True, dedupe=True)),
        "saved_compressed_bytes": len(document.to_bytes(prune=True, dedupe=True, compress=True, object_streams=True)),
        "face_bytes": len(font.program() or b""),
        "text_ok": all(page.text().strip() == text for page, text in zip(reopened.pages, page_texts, strict=True)),
    }


def main() -> None:
    """Run every face with the same and with varying text, and write the results."""
    faces = built_faces()
    for path in sys.argv[1:]:
        faces[Path(path).stem] = named_face(path)
    results = [
        run(name, font, letters, varying=varying)
        for name, (font, letters) in faces.items()
        for varying in (False, True)
    ]
    (HERE / "results.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    for row in results:
        print(row)


if __name__ == "__main__":
    main()
