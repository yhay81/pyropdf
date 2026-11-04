# ruff: noqa: INP001, T201, S603, S607, D103, SLF001, ANN001, ANN201, C901 - a measurement script.
"""How readers take the ways of writing the extra glyphs of a cluster (issue #637).

Usage: uv run --with pypdfium2 --with pymupdf --with pdfminer.six --with pypdf
python evidence/cluster-text/measure.py PDFJS_DIR results.json

``PDFJS_DIR`` holds ``node_modules/pdfjs-dist``, from ``npm install pdfjs-dist@4.10.38``.
Each face is one that Windows 11 installs, or a test face of this repository;
a sample whose face is missing is left out. The ways of writing are the
writers below, which replace pyropdf's own for one drawing.
"""

import collections
import io
import json
import subprocess
import sys
import tempfile
import unicodedata
import warnings
from itertools import groupby
from pathlib import Path

import pdfminer.high_level
import pymupdf
import pypdf
import pypdfium2

import pyropdf
from pyropdf import _canvas, _fonts

TEST_FACES = Path(__file__).resolve().parents[2] / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
SYSTEM_FACES = Path("C:/Windows/Fonts")
SAMPLES = [
    ("arabic, Noto", TEST_FACES / "NotoSansArabic-Variable-subset.ttf", "مرحبا عالم"),
    ("arabic, Calibri", SYSTEM_FACES / "calibri.ttf", "مرحبا بكم في شمس"),
    ("hebrew, Noto", TEST_FACES / "NotoSansHebrew-subset.ttf", "שָׁלוֹם עולם"),
    ("thai, Tahoma", SYSTEM_FACES / "tahoma.ttf", "น้ำ คำ ทำ"),
    ("tamil, Nirmala", SYSTEM_FACES / "Nirmala.ttc", "கொடு கோடு"),
    ("bengali, Nirmala", SYSTEM_FACES / "Nirmala.ttc", "কোন বৌ"),
    ("devanagari, Nirmala", SYSTEM_FACES / "Nirmala.ttc", "श्री हिन्दी"),
    ("lao, Leelawadee", SYSTEM_FACES / "LeelawUI.ttf", "ນຳ ຄຳ"),
    ("khmer, Leelawadee", SYSTEM_FACES / "LeelawUI.ttf", "ខ្មែរ កៅ"),
    ("myanmar, Myanmar Text", SYSTEM_FACES / "mmrtext.ttf", "မြန်မာ ကို"),
]
READERS = ["pyropdf", "xpdf", "mupdf", "pdfium", "pdfjs", "pypdf", "pdfminer"]
DIRECTION = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\n\r\x00"))
INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))

this_change_show = _canvas._GlyphRun.show
real_reads_as = _canvas._GlyphRun._reads_as
real_add_glyphs = _canvas.add_glyphs
real_cmap = _fonts._cmap


def read(data: bytes, pdfjs: Path) -> dict[str, str]:
    """Return what each reader extracts from the first page."""
    found = {}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        found["pyropdf"] = pyropdf.open(data).pages[0].text().strip()
    found["pyropdf_warned"] = str(bool(caught))
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "probe.pdf"
        path.write_bytes(data)
        out = Path(folder) / "out.txt"
        subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), str(out)], check=False, capture_output=True)
        found["xpdf"] = out.read_text("utf-8", "replace").strip() if out.exists() else ""
        script = Path(__file__).with_name("read_pdfjs.mjs")
        run = subprocess.run(
            ["node", str(script), str(pdfjs), str(path)], check=True, capture_output=True, text=True, encoding="utf-8"
        )
        found["pdfjs"] = run.stdout.strip()
    found["mupdf"] = pymupdf.open(stream=data, filetype="pdf")[0].get_text().strip()
    found["pdfium"] = pypdfium2.PdfDocument(data)[0].get_textpage().get_text_bounded().strip()
    found["pdfminer"] = pdfminer.high_level.extract_text(io.BytesIO(data)).strip()
    found["pypdf"] = pypdf.PdfReader(io.BytesIO(data)).pages[0].extract_text().strip()
    return found


def score(value: str, sample: str) -> str:
    """Return how close a reading is: exact, invisible, space, order, or stray."""
    value = unicodedata.normalize("NFC", value.translate(DIRECTION))
    sample = unicodedata.normalize("NFC", sample)
    if value == sample:
        return "exact"
    visible = value.translate(INVISIBLE)
    if visible == sample:
        return "invisible"
    if visible.replace(" ", "") == sample.replace(" ", ""):
        return "space"
    if collections.Counter(visible.replace(" ", "")) == collections.Counter(sample.replace(" ", "")):
        return "order"
    return "stray"


def before_show(self: _canvas._GlyphRun, line: str, glyphs: list[_canvas._ShapedGlyph], spacing: float) -> bytes:
    """Write as main did before this change: only a glyph with other text inside ``/ActualText``."""
    self.ops = []
    pending = 0.0
    rise = 0
    for glyph, advance, _y_advance, x_offset, y_offset, start, end, *_ in glyphs:
        text = line[start:end]
        offset = x_offset * self.scale
        if not glyph:
            pending -= advance * self.scale
            continue
        if y_offset != rise:
            self.flush()
            rise = y_offset
            self.ops.append(_canvas._fmt(rise * self.size / self.units_per_em) + b" Ts")
        if pending - offset:
            self.items.append(_canvas._fmt(pending - offset))
        code = b"<%04X>" % glyph
        if not text or self.mapped.get(glyph) == text:
            self.items.append(code)
        else:
            self.flush()
            self.ops.append(b"/Span <</ActualText <FEFF" + _fonts._utf16(text) + b">>> BDC\n[" + code + b"] TJ\nEMC")
        pending = self.widths[glyph] - advance * self.scale + offset
        if spacing and text == " ":
            pending -= spacing * 1000 / self.size
    self.flush()
    if rise:
        self.ops.append(b"0 Ts")
    return b"\n".join(self.ops)


def spanned_show(*, whole_cluster: bool):
    """Return a writer in visual order: one span over each cluster, or an empty one over each extra glyph."""

    def show(self: _canvas._GlyphRun, line: str, glyphs: list[_canvas._ShapedGlyph], spacing: float) -> bytes:
        self.ops = []
        pending = 0.0
        rise = 0
        for (first, last), group in groupby(glyphs, key=lambda glyph: glyph[8:]):
            cluster = list(group)
            needs_span = whole_cluster and not self._reads_as(line[first:last], cluster)
            if needs_span:
                self.flush()
                self.ops.append(b"/Span <</ActualText <FEFF" + _fonts._utf16(line[first:last]) + b">>> BDC")
            for glyph, advance, _y_advance, x_offset, y_offset, start, end, *_ in cluster:
                offset = x_offset * self.scale
                if not glyph:
                    pending -= advance * self.scale
                    continue
                extra = not whole_cluster and start == end
                if extra:
                    self.flush()
                    self.ops.append(b"/Span <</ActualText <FEFF>>> BDC")
                if y_offset != rise:
                    self.flush()
                    rise = y_offset
                    self.ops.append(_canvas._fmt(rise * self.size / self.units_per_em) + b" Ts")
                if pending - offset:
                    self.items.append(_canvas._fmt(pending - offset))
                self.items.append(b"<%04X>" % glyph)
                pending = self.widths[glyph] - advance * self.scale + offset
                if spacing and line[start:end] == " ":
                    pending -= spacing * 1000 / self.size
                if extra:
                    self.flush()
                    self.ops.append(b"EMC")
            if needs_span:
                self.flush()
                self.ops.append(b"EMC")
        self.flush()
        if rise:
            self.ops.append(b"0 Ts")
        return b"\n".join(self.ops)

    return show


def add_without_entry(document, xref, face, glyphs):
    """Add the glyphs, leaving out the entry of a glyph without text of its own."""
    return real_add_glyphs(
        document, xref, face, [(glyph, "" if text == _fonts._NO_TEXT else text) for glyph, text in glyphs]
    )


def reads_as_without_entry(self: _canvas._GlyphRun, text: str, cluster: list[_canvas._ShapedGlyph]) -> bool:
    """Keep the span of a cluster with extra glyphs, which no longer read as other text without their entries."""
    return all(glyph[5] != glyph[6] for glyph in cluster) and real_reads_as(self, text, cluster)


def cmap_with_empty_entry(mapped: dict[int, str]) -> bytes:
    """Write the entry of a glyph without text of its own as ``<gid> <>``."""
    return real_cmap(mapped).replace(b"<" + _fonts._utf16(_fonts._NO_TEXT) + b">", b"<>")


WAYS = {
    "before this change": (before_show, add_without_entry, real_cmap),
    "this change": (this_change_show, real_add_glyphs, real_cmap),
    "this change, no CMap entry": (this_change_show, add_without_entry, real_cmap),
    "this change, empty CMap entry": (this_change_show, real_add_glyphs, cmap_with_empty_entry),
    "span in visual order": (spanned_show(whole_cluster=True), real_add_glyphs, real_cmap),
    "empty span over each extra glyph": (spanned_show(whole_cluster=False), real_add_glyphs, real_cmap),
}


def draw(face: pyropdf.Font, sample: str, way: str) -> bytes:
    show, add, cmap = WAYS[way]
    _canvas._GlyphRun.show = show
    _canvas._GlyphRun._reads_as = reads_as_without_entry if way == "this change, no CMap entry" else real_reads_as
    _canvas.add_glyphs = add
    _fonts._cmap = cmap
    try:
        document = pyropdf.Document()
        page = document.pages.new((500, 100))
        with page.draw() as canvas:
            canvas.text(sample, (10, 40), font=face, size=24)
        return document.to_bytes()
    finally:
        _canvas._GlyphRun.show = this_change_show
        _canvas._GlyphRun._reads_as = real_reads_as
        _canvas.add_glyphs = real_add_glyphs
        _fonts._cmap = real_cmap


def main() -> None:
    pdfjs = Path(sys.argv[1])
    results: dict[str, dict] = {"samples": {}, "scores": {}, "same_pixels": {}}
    for label, path, sample in SAMPLES:
        if not path.exists():
            continue
        face = pyropdf.Font.from_file(path)
        shaped = face._font.shape(sample)
        results["samples"][label] = {
            "text": sample,
            "glyphs_without_text": sum(glyph[5] == glyph[6] for glyph in shaped),
        }
        pixels = {}
        for way in WAYS:
            data = draw(face, sample, way)
            found = read(data, pdfjs)
            results["scores"].setdefault(way, {})[label] = {
                "read": found,
                "score": {reader: score(found[reader], sample) for reader in READERS},
            }
            pixels[way] = bytes(pyropdf.open(data).pages[0].pixmap(dpi=144))
        results["same_pixels"][label] = pixels["before this change"] == pixels["this change"]
    Path(sys.argv[2]).write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n", "utf-8")
    for way, row in results["scores"].items():
        print(f"\n{way}")
        print("  " + "".ljust(24) + "".join(reader.ljust(10) for reader in READERS))
        for label, entry in row.items():
            print("  " + label.ljust(24) + "".join(entry["score"][reader].ljust(10) for reader in READERS))
    print("\nthe same pixels in hayro as before this change:", results["same_pixels"])


if __name__ == "__main__":
    main()
