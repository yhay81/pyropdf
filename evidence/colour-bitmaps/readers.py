# ruff: noqa: INP001, T201, S603, S607 - a script that prints its table.
"""Read what the canvas writes for a colour bitmap face in other readers (issue #631).

Usage: uv run --with pymupdf --with pypdfium2 python evidence/colour-bitmaps/readers.py

Each committed ``bitmaps-*`` face draws a line that mixes colour and outline
glyphs. The script saves it and prints what each reader reads and the colour
that each renderer paints where the red and the green glyph lie. ``pdftotext``
and ``pdftoppm`` are used when they are on the path.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf
import pypdfium2

import pyropdf

FACES = Path(__file__).resolve().parents[2] / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
TEXT = "RG O R"
RED_AT = (32, 38)
GREEN_AT = (56, 38)


def draw(face: str) -> bytes:
    """Return a saved page that draws ``TEXT`` in ``face``, whose first glyphs lie at ``RED_AT`` and ``GREEN_AT``."""
    document = pyropdf.Document()
    with document.pages.new((200, 60)).draw() as canvas:
        canvas.text(TEXT, (20, 50), font=pyropdf.Font.from_file(FACES / face), size=24)
    return document.to_bytes()


def poppler(data: bytes) -> tuple[str | None, list[tuple[int, ...]] | None]:
    """Return what ``pdftotext`` reads, and the colours that ``pdftoppm`` paints."""
    text = colours = None
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "page.pdf"
        path.write_bytes(data)
        if shutil.which("pdftotext"):
            out = Path(folder) / "out.txt"
            subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), str(out)], check=True, capture_output=True)
            text = out.read_text("utf-8").strip()
        if shutil.which("pdftoppm"):
            ppm = subprocess.run(
                ["pdftoppm", "-r", "72", "-singlefile", str(path)], check=True, capture_output=True
            ).stdout
            header = ppm.split(maxsplit=4)
            width, height = int(header[1]), int(header[2])
            samples = ppm[len(ppm) - 3 * width * height :]
            colours = [tuple(samples[3 * (y * width + x) : 3 * (y * width + x) + 3]) for x, y in (RED_AT, GREEN_AT)]
    return text, colours


def main() -> None:
    """Print one line per face and reader."""
    for face in ("colour-bitmaps-cbdt.ttf", "colour-bitmaps-sbix.ttf"):
        data = draw(face)
        page = pyropdf.open(data).pages[0]
        pixmap = page.pixmap(dpi=72)
        print(face, "pyropdf", repr(page.text().strip()), [pixmap.pixel(*at)[:3] for at in (RED_AT, GREEN_AT)])
        mupdf = pymupdf.open(stream=data, filetype="pdf")[0]
        picture = mupdf.get_pixmap(dpi=72)
        print(face, "mupdf", repr(mupdf.get_text().strip()), [picture.pixel(*at) for at in (RED_AT, GREEN_AT)])
        pdfium = pypdfium2.PdfDocument(data)[0]
        image = pdfium.render(scale=1).to_pil().convert("RGB")
        text = pdfium.get_textpage().get_text_bounded().strip()
        print(face, "pdfium", repr(text), [image.getpixel(at) for at in (RED_AT, GREEN_AT)])
        print(face, "poppler", *poppler(data))


if __name__ == "__main__":
    main()
