# ruff: noqa: INP001, T201, S603, S607, D103, PLR2004, PLR0915, N802 - measurement scripts.
"""Size the options for other instances of a variable face (issue #598).

Usage:
    uv run --with fonttools --with pypdfium2 --with pymupdf python measure_instances.py PROBE OUT.json

PROBE is the binary that ``cargo build --release`` makes from ``instance/``.
The faces are the ones Windows 11 installs, and are not committed; the Arabic
one is pyropdf's own test data.

- ``static``: the prototype writes a static program, ``Font.from_bytes`` loads
  it, and the canvas draws and subsets it as any static face.
- ``paths``: the same glyphs as path operators in ``/ActualText``, with each
  glyph repeated inline, or once as a Form XObject.

Every file goes to four readers.
"""

import json
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

import pymupdf
import pypdfium2
from fontTools.pens.areaPen import AreaPen
from fontTools.pens.basePen import BasePen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.DefaultTable import DefaultTable
from fontTools.varLib import instancer

import pyropdf

WINDOWS = Path("C:/Windows/Fonts")
ARABIC = (
    Path(__file__).resolve().parents[2] / "crates/pyropdf-engine/src/fonts/testdata/NotoSansArabic-Variable-subset.ttf"
)
LATIN = "Hello, office AVA 123"
JAPANESE = "日本語のテキスト。Hello"
PAGES = 100
CASES = [
    ("bahnschrift", WINDOWS / "bahnschrift.ttf", {"wght": 700}, LATIN),
    ("bahnschrift", WINDOWS / "bahnschrift.ttf", {"wght": 300, "wdth": 75}, LATIN),
    ("segoe-ui-variable", WINDOWS / "SegUIVar.ttf", {"wght": 700}, LATIN),
    ("sitka", WINDOWS / "SitkaVF.ttf", {"wght": 700}, LATIN),
    ("noto-sans-jp", WINDOWS / "NotoSansJP-VF.ttf", {"wght": 400}, JAPANESE),
    ("noto-sans-jp", WINDOWS / "NotoSansJP-VF.ttf", {"wght": 700}, JAPANESE),
    ("noto-sans-arabic-subset", ARABIC, {"wght": 700}, "مرحبا"),
]


def readers(data: bytes, text: str) -> dict[str, object]:
    """Return what each reader extracts from ``data``, with whether it holds the text."""
    wanted = "".join(text.split())
    found: dict[str, str] = {}
    document = pyropdf.open(data)
    found["pyropdf"] = document.pages[0].text()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "probe.pdf"
        path.write_bytes(data)
        out = Path(folder) / "out.txt"
        subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", "-enc", "UTF-8", str(path), str(out)], check=False, capture_output=True
        )
        found["xpdf"] = out.read_text("utf-8", "replace") if out.exists() else ""
    found["mupdf"] = pymupdf.open(stream=data, filetype="pdf")[0].get_text()
    pdfium = pypdfium2.PdfDocument(data)
    found["pdfium"] = pdfium[0].get_textpage().get_text_bounded()
    result: dict[str, object] = {}
    for reader, value in found.items():
        squeezed = "".join(value.replace("\x00", "").split())
        result[reader] = "ok" if squeezed == wanted else repr(squeezed[:40])
    return result


def differential(ours: Path, theirs: Path) -> dict[str, object]:
    """Compare every glyph of two static programs: advance, bounds, and area."""
    a, b = TTFont(ours), TTFont(theirs)
    set_a, set_b = a.getGlyphSet(), b.getGlyphSet()
    order_a, order_b = a.getGlyphOrder(), b.getGlyphOrder()
    advance = bounds = area = 0
    worst_bounds = worst_area = 0.0
    for id_a, name_b in zip(range(len(order_a)), order_b, strict=True):
        name_a = order_a[id_a]
        if abs(a["hmtx"][name_a][0] - b["hmtx"][name_b][0]) > 1:
            advance += 1
        box_a, box_b = BoundsPen(set_a), BoundsPen(set_b)
        set_a[name_a].draw(box_a)
        set_b[name_b].draw(box_b)
        if (box_a.bounds is None) != (box_b.bounds is None):
            bounds += 1
        elif box_a.bounds is not None:
            gap = max(abs(x - y) for x, y in zip(box_a.bounds, box_b.bounds, strict=True))
            worst_bounds = max(worst_bounds, gap)
            bounds += gap > 1
        area_a, area_b = AreaPen(set_a), AreaPen(set_b)
        set_a[name_a].draw(area_a)
        set_b[name_b].draw(area_b)
        ratio = abs(abs(area_a.value) - abs(area_b.value)) / max(abs(area_b.value), 1.0)
        worst_area = max(worst_area, ratio)
        area += ratio > 0.01
    return {
        "advances_off_by_more_than_1": advance,
        "bounds_off_by_more_than_1": bounds,
        "worst_bounds_units": round(worst_bounds, 1),
        "areas_off_by_more_than_1pct": area,
        "worst_area_ratio": round(worst_area, 4),
    }


def oracle_static(path: Path, axes: dict[str, float]) -> TTFont:
    """Return fontTools' instance, for comparing outlines and metrics.

    The shaping tables go around the instancer: it raises KeyError on a
    variation index in some Windows faces' GDEF, and the static program of the
    prototype copies them as they are too, so both shape alike.
    """
    source = TTFont(path)
    shaping = {tag: source.getTableData(tag) for tag in ("GDEF", "GPOS", "GSUB") if tag in source}
    for tag in shaping:
        del source[tag]
    reference = instancer.instantiateVariableFont(source, dict(axes))
    for tag, data in shaping.items():
        table = DefaultTable(tag)
        table.data = data
        reference[tag] = table
    return reference


def stamp(font: pyropdf.Font, text: str) -> tuple[pyropdf.Document, float]:
    """Draw ``text`` once on each of 100 pages."""
    document = pyropdf.Document()
    started = time.perf_counter()
    for _ in range(PAGES):
        page = document.pages.new((400, 100))
        with page.draw() as canvas:
            canvas.text(text, (10, 50), font=font, size=14)
    return document, time.perf_counter() - started


def saved_program(document: pyropdf.Document) -> int:
    """Return the bytes of the font programs of ``document``."""
    size = 0
    for xref in document.objects:
        value = document.objects[xref]
        if isinstance(value, pyropdf.Dictionary):
            for key in ("FontFile2", "FontFile3"):
                program = value.get(key)
                if isinstance(program, pyropdf.Stream):
                    size += len(program.read())
    return size


class _Pdf(BasePen):
    """Writes a glyph as PDF path operators; quadratics become cubics."""

    def __init__(self, glyph_set: object) -> None:
        super().__init__(glyph_set)
        self.parts: list[str] = []

    def _moveTo(self, pt: tuple[float, float]) -> None:
        self.parts.append(f"{pt[0]:.0f} {pt[1]:.0f} m")

    def _lineTo(self, pt: tuple[float, float]) -> None:
        self.parts.append(f"{pt[0]:.0f} {pt[1]:.0f} l")

    def _curveToOne(self, a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> None:
        self.parts.append(f"{a[0]:.0f} {a[1]:.0f} {b[0]:.0f} {b[1]:.0f} {c[0]:.0f} {c[1]:.0f} c")

    def _closePath(self) -> None:
        self.parts.append("h")


def write_pdf(objects: list[bytes]) -> bytes:
    """Write objects 1.. into a file whose root is object 1."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def stream(data: bytes, extra: str = "") -> bytes:
    """Return a Flate stream object body."""
    packed = zlib.compress(data, 9)
    return f"<</Length {len(packed)}/Filter/FlateDecode{extra}>>\nstream\n".encode() + packed + b"\nendstream"


def actual_text(text: str) -> str:
    return "FEFF" + text.encode("utf-16-be").hex().upper()


def paths_pdf(static: Path, text: str, *, reuse: bool) -> bytes:
    """Return 100 pages that draw ``text`` as paths in ``/ActualText``."""
    font = TTFont(static)
    glyph_set = font.getGlyphSet()
    cmap = font.getBestCmap()
    scale = 14 / font["head"].unitsPerEm
    names = [cmap[ord(char)] for char in text if ord(char) in cmap]
    outlines: dict[str, str] = {}
    for name in names:
        pen = _Pdf(glyph_set)
        glyph_set[name].draw(pen)
        outlines[name] = " ".join(pen.parts)
    placed = []
    x = 0.0
    for name in names:
        placed.append((name, x))
        x += font["hmtx"][name][0]
    # The pages hold 1 catalog, 2 pages; then forms; then page and content pairs.
    forms = sorted(outlines)
    objects: list[bytes] = [b"<</Type/Catalog/Pages 2 0 R>>", b""]
    numbers = {}
    for name in forms if reuse else ():
        numbers[name] = len(objects) + 1
        box = "[-5000 -5000 5000 5000]"
        objects.append(stream((outlines[name] + " f").encode(), f"/Type/XObject/Subtype/Form/BBox{box}"))
    resources = (
        "<<"
        + ("/XObject<<" + "".join(f"/G{i} {numbers[n]} 0 R" for i, n in enumerate(forms)) + ">>" if reuse else "")
        + ">>"
    )
    kids = []
    for _ in range(PAGES):
        parts = [f"/Span<</ActualText<{actual_text(text)}>>>BDC q {scale:.5f} 0 0 {scale:.5f} 10 50 cm"]
        for name, offset in placed:
            body = f"/G{forms.index(name)} Do" if reuse else outlines[name] + " f"
            parts.append(f"q 1 0 0 1 {offset:.0f} 0 cm {body} Q")
        parts.append("Q EMC")
        content = len(objects) + 1
        objects.append(stream("\n".join(parts).encode()))
        kids.append(len(objects) + 1)
        objects.append(
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 100]/Resources {resources}/Contents {content} 0 R>>".encode()
        )
    objects[1] = f"<</Type/Pages/Count {PAGES}/Kids[{' '.join(f'{k} 0 R' for k in kids)}]>>".encode()
    return write_pdf(objects)


def main() -> None:
    probe, out = sys.argv[1], Path(sys.argv[2])
    rows = []
    with tempfile.TemporaryDirectory() as folder:
        scratch = Path(folder)
        for label, path, axes, text in CASES:
            setting = ",".join(f"{tag}={value}" for tag, value in axes.items())
            print(label, setting, flush=True)
            ours = scratch / f"{label}-{setting}.ttf"
            report = json.loads(
                subprocess.run(
                    [probe, str(path), str(ours), *[f"{t}={v}" for t, v in axes.items()]],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )

            started = time.perf_counter()
            reference = oracle_static(path, axes)
            fonttools_s = time.perf_counter() - started
            theirs = scratch / f"{label}-{setting}-fonttools.ttf"
            reference.save(theirs)

            default = pyropdf.Font.from_file(path)
            try:
                pyropdf.Font.from_file(path, weight=int(axes["wght"]))
                refused = None
            except pyropdf.PdfError as error:
                refused = str(error)

            started = time.perf_counter()
            font = pyropdf.Font.from_bytes(ours.read_bytes())
            load = time.perf_counter() - started
            document, draw = stamp(font, text)
            page_pixels = bytes(document.pages[0].pixmap(dpi=144))
            started = time.perf_counter()
            saved = document.to_bytes()
            save = time.perf_counter() - started
            compact = document.to_bytes(prune=True, dedupe=True, compress=True, object_streams=True)
            reopened = pyropdf.open(saved)

            oracle, _ = stamp(pyropdf.Font.from_bytes(theirs.read_bytes()), text)
            oracle_pixels = bytes(oracle.pages[0].pixmap(dpi=144))
            base, _ = stamp(default, text)
            base_pixels = bytes(base.pages[0].pixmap(dpi=144))

            def differing(x: bytes, y: bytes) -> object:
                return sum(p != q for p, q in zip(x, y, strict=True)) if len(x) == len(y) else "sizes differ"

            paths_inline = paths_pdf(ours, text, reuse=False)
            paths_forms = paths_pdf(ours, text, reuse=True)
            rows.append(
                {
                    "face": label,
                    "axes": axes,
                    "default_weight": default.weight,
                    "weight_axis": [
                        [a.axisTag, a.minValue, a.defaultValue, a.maxValue]
                        for a in TTFont(path, lazy=True)["fvar"].axes
                    ],
                    "today_refused": refused,
                    "instance_probe": report,
                    "fonttools_instancer_s": round(fonttools_s, 2),
                    "against_fonttools": differential(ours, theirs),
                    "load_s": round(load, 4),
                    "draw_s": round(draw, 3),
                    "save_s": round(save, 3),
                    "saved_kib": round(len(saved) / 1024, 1),
                    "compact_kib": round(len(compact) / 1024, 1),
                    "saved_program_kib": round(saved_program(reopened) / 1024, 2),
                    "bytes_differing_from_fonttools_static": differing(page_pixels, oracle_pixels),
                    "bytes_differing_from_default_instance": differing(page_pixels, base_pixels),
                    "static_readers": readers(saved, text),
                    "paths_inline_kib": round(len(paths_inline) / 1024, 1),
                    "paths_forms_kib": round(len(paths_forms) / 1024, 1),
                    "paths_readers": readers(paths_inline, text),
                    "paths_content_items": sorted(
                        {type(item).__name__ for item in pyropdf.open(paths_inline).pages[0].content()}
                    ),
                    "static_content_items": sorted({type(item).__name__ for item in reopened.pages[0].content()}),
                },
            )
        # Two weights of one face in one document.
        light = scratch / "jp-a.ttf"
        heavy = scratch / "jp-b.ttf"
        jp = WINDOWS / "NotoSansJP-VF.ttf"
        subprocess.run([probe, str(jp), str(light), "wght=400"], check=True, capture_output=True)
        subprocess.run([probe, str(jp), str(heavy), "wght=700"], check=True, capture_output=True)
        document = pyropdf.Document()
        page = document.pages.new((400, 100))
        with page.draw() as canvas:
            canvas.text("日本語", (10, 60), font=pyropdf.Font.from_bytes(light.read_bytes()), size=14)
            canvas.text("日本語", (10, 30), font=pyropdf.Font.from_bytes(heavy.read_bytes()), size=14)
        reopened = pyropdf.open(document.to_bytes())
        programs = sum(
            1
            for xref in reopened.objects
            if isinstance(reopened.objects[xref], pyropdf.Dictionary) and "FontFile2" in reopened.objects[xref]
        )
    out.write_text(
        json.dumps(
            {"rows": rows, "two_weights": {"programs": programs, "text": reopened.pages[0].text()}},
            indent=1,
            ensure_ascii=False,
        )
        + "\n",
        "utf-8",
    )
    print(out)


if __name__ == "__main__":
    main()
