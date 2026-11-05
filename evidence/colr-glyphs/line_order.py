# ruff: noqa: INP001, T201, PLR2004, S101, S603 - a measured experiment on its own generated PDFs.
"""Compare Type 3 metrics at 1.25 em line spacing for issue #670.

Run with fonttools, pymupdf, pypdfium2, and xpdf's pdftotext installed::

    uv run --no-sync --with fonttools --with pymupdf --with pypdfium2
        python evidence/colr-glyphs/line_order.py --xpdf PATH --output PATH

The uniform baseline removes only the writer's vertical coordinate wrapper.
This reproduces the former writer without requiring its extension to be built.
The final ``writer`` case measures the unmodified current canvas output.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf
import pypdfium2
from fontTools.ttLib import TTFont

import pyropdf

ROOT = Path(__file__).resolve().parents[2]
FONTS = ROOT / "crates/pyropdf-engine/src/fonts/testdata"
CASES = (
    "uniform",
    "zero_box",
    "em_box",
    "d1",
    "descriptor",
    "descriptor_cancel",
    "uniform_1000",
    "anisotropic",
    "writer",
)


def fonts(document: pyropdf.Document) -> list[pyropdf.Dictionary]:
    """Return the generated document's Type 3 fonts."""
    return [
        value
        for number in document.objects
        if isinstance(value := document.objects[number], pyropdf.Dictionary) and value.get("Subtype") == "Type3"
    ]


def emoji(face: pyropdf.Font, path: Path) -> str:
    """Choose the same first 100 painted characters as compare.py's page()."""
    found = []
    with TTFont(path, lazy=True) as source:
        for code in sorted(source.getBestCmap()):
            if code < 0x1F300:
                continue
            document = pyropdf.Document()
            page = document.pages.new((100, 100))
            try:
                with page.draw() as canvas:
                    canvas.text(chr(code), (10, 60), font=face, size=24)
            except pyropdf.PdfError:
                continue
            if fonts(document):
                found.append(chr(code))
            if len(found) == 100:
                break
    assert len(found) == 100
    return "".join(found)


def page(face: pyropdf.Font, text: str, width: int) -> bytes:
    """Draw ten lines at 24 points, with their baselines 30 points apart."""
    document = pyropdf.Document()
    sheet = document.pages.new((width, 340))
    with sheet.draw() as canvas:
        for index in range(10):
            canvas.text(text[index * 10 : (index + 1) * 10], (10, 30 + 30 * index), font=face, size=24)
    return document.to_bytes()


def uniform(data: bytes) -> bytes:
    """Restore uniform glyph units by removing exactly the current writer's wrapper."""
    document = pyropdf.open(data)
    with document.transaction():
        for font in fonts(document):
            matrix = [float(value) for value in font["FontMatrix"]]
            ratio = matrix[0] / matrix[3]
            if ratio == 1:
                continue
            for procedure in font["CharProcs"].values():
                operators = procedure.read()
                if b"\n" not in operators:
                    assert operators.endswith(b" 0 d0")
                    continue
                match = re.fullmatch(rb"([^\n]* d0)\nq 1 0 0 ([0-9.]+) 0 0 cm\n(.*)\nQ", operators, re.DOTALL)
                assert match is not None
                assert abs(float(match[2]) - ratio) < 1e-12
                procedure.write(match[1] + match[3])
            box = [float(value) for value in font["FontBBox"]]
            font["FontBBox"] = [box[0], box[1] / ratio, box[2], box[3] / ratio]
            matrix[3] = matrix[0]
            font["FontMatrix"] = matrix
    return document.to_bytes()


def descriptor(document: pyropdf.Document, font: pyropdf.Dictionary, *, cancel: bool) -> None:
    """Try ink metrics in glyph units, or deliberately compensate xpdf's estimate."""
    box = [float(value) for value in font["FontBBox"]]
    factor = next(float(width) for width in font["Widths"] if width > 0) * float(font["FontMatrix"][0]) / 0.5
    ascent, descent = (750 / factor, -250 / factor) if cancel else (box[3], box[1])
    values = {
        "Type": pyropdf.Name("FontDescriptor"),
        "FontName": pyropdf.Name("Colour"),
        "Flags": 4,
        "FontBBox": box,
        "ItalicAngle": 0,
        "Ascent": ascent,
        "Descent": descent,
        "CapHeight": ascent,
        "StemV": 0,
    }
    font["FontDescriptor"] = document.objects[document.objects.add(values)]


def candidate(data: bytes, name: str) -> bytes:
    """Change only Type 3 dictionaries and procedures, retaining the page's layout."""
    document = pyropdf.open(data)
    with document.transaction():
        for font in fonts(document):
            matrix = [float(value) for value in font["FontMatrix"]]
            box = [float(value) for value in font["FontBBox"]]
            widths = [float(value) for value in font["Widths"]]
            if name == "zero_box":
                font["FontBBox"] = [0, 0, 0, 0]
            elif name == "em_box":
                font["FontBBox"] = [0, -0.25 / matrix[3], 1 / matrix[0], 0.75 / matrix[3]]
            elif name.startswith("descriptor"):
                descriptor(document, font, cancel=name == "descriptor_cancel")
            if name not in {"d1", "uniform_1000", "anisotropic"}:
                continue
            sx = 1000 * matrix[0] if name == "uniform_1000" else 1.0
            sy = next(width for width in widths if width > 0) * matrix[0] / 0.5 if name == "anisotropic" else sx
            if name != "d1":
                font["FontMatrix"] = [matrix[0] / sx, 0, 0, matrix[3] / sy, 0, 0]
                font["FontBBox"] = [box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy]
                font["Widths"] = [width * sx for width in widths]
            for procedure in font["CharProcs"].values():
                header, _separator, rest = procedure.read().partition(b"\n")
                width = float(header.split()[0])
                if name == "d1":
                    changed = f"{width:g} 0 {' '.join(str(value) for value in box)} d1\n".encode() + rest
                else:
                    changed = f"{width * sx:g} 0 d0\nq {sx:.16g} 0 0 {sy:.16g} 0 0 cm\n".encode() + rest + b"\nQ"
                procedure.write(changed)
    return document.to_bytes()


def read(data: bytes, tool: str) -> tuple[dict[str, str], dict[str, bytes], dict[str, list[float]]]:
    """Extract text, render RGB at 72 dpi, and retain text geometry in three readers."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "page.pdf"
        path.write_bytes(data)
        xpdf = subprocess.run([tool, "-enc", "UTF-8", str(path), "-"], check=True, capture_output=True).stdout.decode(
            "utf-8"
        )
    with pyropdf.open(data) as document, pymupdf.open(stream=data, filetype="pdf") as mupdf:
        pdfium = pypdfium2.PdfDocument(data)
        pdfium_page = pdfium[0]
        textpage = pdfium_page.get_textpage()
        sheet = document.pages[0]
        text = {
            "xpdf": xpdf,
            "pyropdf": sheet.text(),
            "mupdf": mupdf[0].get_text(),
            "pdfium": textpage.get_text_bounded(),
        }
        pixels = {
            "pyropdf": bytes(sheet.pixmap(dpi=72).to_rgb()),
            "mupdf": mupdf[0].get_pixmap(dpi=72, alpha=False).samples,
            "pdfium": pdfium_page.render(scale=1).to_pil().convert("RGB").tobytes(),
        }
        geometry = {
            "pyropdf": [
                value
                for run in sheet.content(paths=False, images=False).text_runs
                for glyph in run.glyphs
                for value in (*glyph.matrix, *(glyph.bbox or ()))
            ],
            "mupdf": [
                value
                for block in mupdf[0].get_text("rawdict")["blocks"]
                for line in block.get("lines", [])
                for span in line["spans"]
                for char in span["chars"]
                for value in (span["size"], *char["origin"], *char["bbox"])
            ],
            "pdfium": [value for index in range(textpage.count_chars()) for value in textpage.get_charbox(index)],
        }
        textpage.close()
        pdfium_page.close()
        pdfium.close()
    return {reader: "".join(value.replace("\x00", "").split()) for reader, value in text.items()}, pixels, geometry


def compare(source: bytes, text: str, tool: str) -> dict[str, object]:
    """Measure all candidates against the former uniform-coordinate output."""
    baseline = uniform(source)
    _text, baseline_pixels, baseline_geometry = read(baseline, tool)
    results = {}
    for name in CASES:
        data = source if name == "writer" else candidate(baseline, name)
        extracted, pixels, geometry = read(data, tool)
        results[name] = {
            "reads_in_order": {reader: value == text for reader, value in extracted.items()},
            "xpdf_text": extracted["xpdf"],
            "pixels": {
                reader: {
                    "max_channel_difference": max(
                        abs(a - b) for a, b in zip(value, baseline_pixels[reader], strict=True)
                    ),
                    "changed_channels": sum(a != b for a, b in zip(value, baseline_pixels[reader], strict=True)),
                }
                for reader, value in pixels.items()
            },
            "geometry_max_difference_points": {
                reader: max(abs(a - b) for a, b in zip(value, baseline_geometry[reader], strict=True))
                if len(value) == len(baseline_geometry[reader])
                else None
                for reader, value in geometry.items()
            },
        }
    return results


def main() -> None:
    """Write the reproducible comparison and the exact tool/font versions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xpdf", default=shutil.which("pdftotext"))
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/seguiemj.ttf"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.xpdf:
        parser.error("--xpdf must point to xpdf's pdftotext")
    version = subprocess.run([args.xpdf, "-v"], capture_output=True, check=False)
    result = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "versions": {
            name: importlib.metadata.version(name) for name in ("pyropdf", "pymupdf", "pypdfium2", "fonttools")
        }
        | {"pdfium": str(pypdfium2.version.PDFIUM_INFO)},
        "xpdf": (version.stdout + version.stderr).decode("utf-8").strip(),
        "size_points": 24,
        "line_spacing_points": 30,
        "cases": {},
    }
    for name in ("segoe", "cbdt", "sbix", "wide-cbdt", "wide-sbix"):
        path = args.font if name == "segoe" else FONTS / f"colour-bitmaps-{name.removeprefix('wide-')}.ttf"
        data = path.read_bytes()
        if name.startswith("wide-"):
            with TTFont(io.BytesIO(data), recalcTimestamp=False) as font:
                font["hmtx"].metrics = {
                    glyph: (1500, bearing) for glyph, (_width, bearing) in font["hmtx"].metrics.items()
                }
                buffer = io.BytesIO()
                font.save(buffer)
                data = buffer.getvalue()
        face = pyropdf.Font.from_bytes(data)
        text = emoji(face, path) if name == "segoe" else "".join(char * 10 for char in "RGRGRGRGRG")
        result["cases"][name] = {
            "font": path.name,
            "font_sha256": hashlib.sha256(data).hexdigest(),
            "text": text,
            "results": compare(page(face, text, 400 if name.startswith("wide-") else 320), text, args.xpdf),
        }
        print(f"Measured {name}", flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
