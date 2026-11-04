# ruff: noqa: INP001, T201, PLR2004, S603 - a script that prints its results and runs fixed tools.
"""Compare canvas COLR glyphs with another renderer, and measure a page of 100 emoji (issue #633).

Usage, on one line::

    uv run --with "blackrenderer[skia]" --with fonttools --with pymupdf --with pypdfium2
        python evidence/colr-glyphs/compare.py [FACE] [OUTPUT_FOLDER]

FACE defaults to Segoe UI Emoji. For each emoji of ``SAMPLE``, the canvas
draws it alone at 64 points; hayro, MuPDF, and PDFium render the page at 144
dpi, and blackrenderer, a COLR v1 renderer on Skia that shares no code with
pyropdf, renders the same glyph at the same scale and place. A pixel's
difference is the largest of its three channels, over white. Renders go to
OUTPUT_FOLDER when it is given.

Then one page draws ``PAGE_GLYPHS`` distinct emoji that paint, ten lines at 24
points, and the script times the drawing, saves it three ways, renders it in
each reader, and reads its text with pyropdf, MuPDF, PDFium, and pdftotext.
"""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pymupdf
import pypdfium2
import skia
from blackrenderer.backends import getSurfaceClass
from blackrenderer.font import BlackRendererFont
from fontTools.ttLib import TTFont

import pyropdf

FACE = Path("C:/Windows/Fonts/seguiemj.ttf")
SAMPLE = "😀🎉👍🚀🌸🍣🐱🎌"
#: The size and place of a sample glyph, in points, on a page of ``BOX`` by ``BOX``.
SIZE, ORIGIN, BASELINE, BOX = 64, 8, 64, 80
DPI = 144
PAGE_GLYPHS = 100
#: Thresholds of a pixel's difference, in 8-bit levels, whose shares the comparison reports.
LEVELS = (8, 32)


def _pixels(rgba: bytes, channels: int) -> list[tuple[int, int, int]]:
    """Return the RGB pixels of ``rgba`` over white."""
    out = []
    for at in range(0, len(rgba), channels):
        red, green, blue = rgba[at], rgba[at + 1], rgba[at + 2]
        alpha = rgba[at + 3] if channels == 4 else 255
        out.append(tuple(round(value * alpha / 255 + 255 - alpha) for value in (red, green, blue)))
    return out


def _reference(font: BlackRendererFont, glyph: str, scale: float) -> list[tuple[int, int, int]]:
    """Return blackrenderer's render of ``glyph`` where the sample page draws it."""
    size = BOX * scale
    surface = getSurfaceClass("skia")()
    with surface.canvas((0, 0, round(size), round(size))) as canvas:
        canvas.translate(ORIGIN * scale, (BOX - BASELINE) * scale)
        units = SIZE * scale / font.unitsPerEm
        canvas.scale(units, units)
        font.drawGlyph(glyph, canvas)
    image = surface._image  # noqa: SLF001 - the backend keeps its last image.
    data = image.convert(colorType=skia.kRGBA_8888_ColorType, alphaType=skia.kUnpremul_AlphaType).tobytes()
    return _pixels(data, 4)


def _differences(render: list[tuple[int, int, int]], reference: list[tuple[int, int, int]]) -> dict[str, float]:
    pairs = zip(render, reference, strict=True)
    worst = [max(abs(a - b) for a, b in zip(one, other, strict=True)) for one, other in pairs]
    inked = [value for value, pixel in zip(worst, reference, strict=True) if pixel != (255, 255, 255)]
    result = {"mean": round(statistics.fmean(inked), 2)}
    for level in LEVELS:
        result[f"within_{level}"] = round(sum(value <= level for value in inked) / len(inked), 4)
    return result


def _renders(data: bytes) -> dict[str, list[tuple[int, int, int]]]:
    pixmap = pyropdf.open(data).pages[0].pixmap(dpi=DPI).to_rgb()
    mupdf = pymupdf.open(stream=data, filetype="pdf")[0].get_pixmap(dpi=DPI, alpha=False)
    pdfium = pypdfium2.PdfDocument(data)[0].render(scale=DPI / 72).to_pil().convert("RGB")
    return {
        "hayro": _pixels(bytes(pixmap), 3),
        "mupdf": _pixels(mupdf.samples, mupdf.n),
        "pdfium": _pixels(pdfium.tobytes(), 3),
    }


def sample(face: pyropdf.Font, path: Path, output: Path | None) -> dict[str, object]:
    """Compare each emoji of ``SAMPLE`` in three readers with blackrenderer's."""
    reference_font = BlackRendererFont(path)
    order = TTFont(path, lazy=True).getGlyphOrder()
    results = {}
    for emoji in SAMPLE:
        document = pyropdf.Document()
        page = document.pages.new((BOX, BOX))
        with page.draw() as canvas:
            canvas.text(emoji, (ORIGIN, BASELINE), font=face, size=SIZE)
        (font,) = [value for value in map(document.objects.__getitem__, document.objects) if _is_type3(value)]
        (name,) = [str(name) for name in font["CharProcs"]]
        glyph = order[int(name[1:-1])]
        data = document.to_bytes()
        reference = _reference(reference_font, glyph, DPI / 72)
        renders = _renders(data)
        results[emoji] = {reader: _differences(pixels, reference) for reader, pixels in renders.items()}
        if output:
            _save(output / f"U+{ord(emoji):04X}-blackrenderer.png", reference)
            for reader, pixels in renders.items():
                _save(output / f"U+{ord(emoji):04X}-{reader}.png", pixels)
    return results


def _is_type3(value: object) -> bool:
    return isinstance(value, pyropdf.Dictionary) and value.get("Subtype") == "Type3"


def _save(path: Path, pixels: list[tuple[int, int, int]]) -> None:
    side = round(len(pixels) ** 0.5)
    pyropdf.Pixmap.from_bytes(_ppm(pixels, side)).save(path)


def _ppm(pixels: list[tuple[int, int, int]], side: int) -> bytes:
    return f"P6 {side} {side} 255\n".encode() + bytes(value for pixel in pixels for value in pixel)


def _painted_emoji(face: pyropdf.Font, path: Path) -> str:
    """Return ``PAGE_GLYPHS`` characters from U+1F300 up whose glyphs the canvas paints in colour."""
    found = []
    for code in sorted(TTFont(path, lazy=True).getBestCmap()):
        if code < 0x1F300 or len(found) == PAGE_GLYPHS:
            continue
        document = pyropdf.Document()
        page = document.pages.new((100, 100))
        try:
            with page.draw() as canvas:
                canvas.text(chr(code), (10, 60), font=face, size=24)
        except pyropdf.PdfError:
            continue
        if any(_is_type3(document.objects[number]) for number in document.objects):
            found.append(chr(code))
    return "".join(found)


def _text(data: bytes) -> dict[str, str]:
    found = {
        "pyropdf": pyropdf.open(data).pages[0].text(),
        "mupdf": pymupdf.open(stream=data, filetype="pdf")[0].get_text(),
        "pdfium": pypdfium2.PdfDocument(data)[0].get_textpage().get_text_bounded(),
    }
    tool = shutil.which("pdftotext")
    if tool:
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "page.pdf"
            pdf.write_bytes(data)
            subprocess.run([tool, "-enc", "UTF-8", str(pdf), str(pdf.with_suffix(".txt"))], check=False)
            found["pdftotext"] = pdf.with_suffix(".txt").read_text("utf-8", "replace")
    return {reader: "".join(value.replace("\x00", "").split()) for reader, value in found.items()}


def page(face: pyropdf.Font, path: Path) -> dict[str, object]:
    """Measure one page that draws 100 distinct emoji."""
    text = _painted_emoji(face, path)
    lines = [text[at : at + 10] for at in range(0, len(text), 10)]
    document = pyropdf.Document()
    sheet = document.pages.new((320, 340))
    started = time.perf_counter()
    with sheet.draw() as canvas:
        for index, line in enumerate(lines):
            canvas.text(line, (10, 30 + 30 * index), font=face, size=24)
    drawn = time.perf_counter() - started
    data = document.to_bytes()
    renders = {}
    for reader, render in (
        ("hayro", lambda: pyropdf.open(data).pages[0].pixmap(dpi=DPI)),
        ("mupdf", lambda: pymupdf.open(stream=data, filetype="pdf")[0].get_pixmap(dpi=DPI)),
        ("pdfium", lambda: pypdfium2.PdfDocument(data)[0].render(scale=DPI / 72)),
    ):
        started = time.perf_counter()
        render()
        renders[reader] = round(time.perf_counter() - started, 3)
    read = _text(data)
    return {
        "glyphs": len(text),
        "text": text,
        "draw_seconds": round(drawn, 3),
        "objects": len(document.objects),
        "type3_fonts": sum(_is_type3(document.objects[number]) for number in document.objects),
        "saved_bytes": len(data),
        "saved_dedupe_bytes": len(document.to_bytes(prune=True, dedupe=True)),
        "saved_compressed_bytes": len(document.to_bytes(prune=True, dedupe=True, compress=True, object_streams=True)),
        "render_seconds": renders,
        "text_reads": {reader: value == text for reader, value in read.items()},
    }


def main() -> None:
    """Print the comparison and the page's measurements as JSON."""
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else FACE
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    face = pyropdf.Font.from_file(path)
    result = {"face": face.name, "sample": sample(face, path, output), "page": page(face, path)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
