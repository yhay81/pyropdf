# ruff: noqa: INP001, T201, S603, S607, D102, D103, D107, D401, PLR2004, E501, PLC0415, N813, N802, RUF007, FBT003, C901, PLR0911 - measurement scripts.
"""Size the options for colour glyphs (issue #598).

Usage:
    uv run --with fonttools --with pypdfium2 --with pymupdf python measure_colour.py OUT.json FOLDER

Three parts:

- What the canvas does today with a colour face: Windows 11's Segoe UI Emoji
  (COLR v1 with monochrome fallback outlines), and small faces built here
  whose colour glyphs have no outline, or whose only glyphs are bitmaps.
- A Type 3 prototype: glyph procedures that paint a COLR v1 glyph's layers,
  with axial and radial shadings, and one that paints an image with a mask.
- A Form XObject prototype: the same painting in Forms inside ``/ActualText``.

Every file goes to four readers and three renderers. FOLDER receives the PDFs
and PNGs. The prototype paints what these glyphs use and counts what it skips:
``PaintComposite``, and stops with alpha, which need soft masks.
"""

import io
import json
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

import pymupdf
import pypdfium2
from fontTools.colorLib.builder import buildCOLR, buildCPAL
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.basePen import BasePen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot

import pyropdf

EMOJI_FACE = Path("C:/Windows/Fonts/seguiemj.ttf")
SAMPLE = "😀🎉👍🚀🌸🍣🐱🎌"
PAGES = 100


def readers(data: bytes, text: str) -> dict[str, str]:
    """Return what each reader makes of ``data``: ok, or the text it read."""
    wanted = "".join(text.split())
    found: dict[str, str] = {}
    found["pyropdf"] = pyropdf.open(data).pages[0].text()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "probe.pdf"
        path.write_bytes(data)
        out = Path(folder) / "out.txt"
        subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", "-enc", "UTF-8", str(path), str(out)], check=False, capture_output=True
        )
        found["xpdf"] = out.read_text("utf-8", "replace") if out.exists() else ""
    found["mupdf"] = pymupdf.open(stream=data, filetype="pdf")[0].get_text()
    found["pdfium"] = pypdfium2.PdfDocument(data)[0].get_textpage().get_text_bounded()
    result = {}
    for reader, value in found.items():
        squeezed = "".join(value.replace("\x00", "").split())
        result[reader] = "ok" if squeezed == wanted else repr(squeezed[:30])
    return result


def renders(data: bytes, stem: Path) -> dict[str, int]:
    """Render page 1 with three renderers, write PNGs, and count their colours."""
    counts = {}
    pixmap = pyropdf.open(data).pages[0].pixmap(dpi=144)
    pixmap.save(stem.with_name(stem.name + "-hayro.png"))
    counts["hayro"] = pixmap.color_count()
    mupdf = pymupdf.open(stream=data, filetype="pdf")[0].get_pixmap(dpi=144)
    mupdf.save(stem.with_name(stem.name + "-mupdf.png"))
    counts["mupdf"] = len(set(zip(*[iter(mupdf.samples)] * mupdf.n, strict=False)))
    image = pypdfium2.PdfDocument(data)[0].render(scale=2).to_pil()
    image.save(stem.with_name(stem.name + "-pdfium.png"))
    counts["pdfium"] = len(image.convert("RGB").getcolors(1 << 24) or [])
    return counts


# --- Small faces with no outline for their colour glyphs. ---------------------


def _square() -> object:
    pen = TTGlyphPen(None)
    pen.moveTo((100, 0))
    pen.lineTo((100, 800))
    pen.lineTo((900, 800))
    pen.lineTo((900, 0))
    pen.closePath()
    return pen.glyph()


def small_face(kind: str) -> bytes:
    """Build a face whose ``A`` is a colour glyph with an empty outline."""
    order = [".notdef", "A", "layer"]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({0x41: "A"})
    empty = TTGlyphPen(None).glyph()
    builder.setupGlyf({".notdef": empty, "A": empty, "layer": _square()})
    builder.setupHorizontalMetrics(dict.fromkeys(order, (1000, 0)))
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": f"Colour {kind}", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    font = builder.font
    if kind == "colr1":
        font["CPAL"] = buildCPAL([[(1.0, 0.0, 0.0, 1.0)]])
        paint = {
            "Format": ot.PaintFormat.PaintGlyph,
            "Paint": {"Format": ot.PaintFormat.PaintSolid, "PaletteIndex": 0, "Alpha": 1.0},
            "Glyph": "layer",
        }
        font["COLR"] = buildCOLR({"A": paint}, version=1)
    if kind in {"sbix", "bitmap-only"}:
        font["sbix"] = _sbix(len(order))
    if kind == "svg":
        table = newTable("SVG ")
        table.compressed = False
        table.docList = [ot_svg_doc()]
        font["SVG "] = table
    if kind == "bitmap-only":
        del font["glyf"]
        del font["loca"]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def ot_svg_doc() -> object:
    from fontTools.ttLib.tables.S_V_G_ import SVGDocument

    return SVGDocument(
        '<svg xmlns="http://www.w3.org/2000/svg"><rect id="glyph1" width="800" height="800" fill="red"/></svg>',
        1,
        1,
        False,
    )


def _sbix(glyphs: int) -> object:
    """A hand-packed ``sbix`` table with one 1x1 PNG for glyph 1."""
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    record = struct.pack(">hh4s", 0, 0, b"png ") + png
    offsets = [4 + 4 * (glyphs + 1)] * 2
    offsets.append(offsets[-1] + len(record))
    offsets.extend([offsets[-1]] * (glyphs + 1 - len(offsets)))
    strike = struct.pack(">HH", 72, 72) + b"".join(struct.pack(">I", at) for at in offsets) + record
    data = struct.pack(">HHI", 1, 1, 1) + struct.pack(">I", 12) + strike
    table = newTable("sbix")
    table.decompile = None
    table.compile = lambda _font: data
    return table


def today(folder: Path) -> dict[str, object]:
    """Report what the canvas does today with each colour face."""
    result: dict[str, object] = {}
    started = time.perf_counter()
    emoji = pyropdf.Font.from_file(EMOJI_FACE)
    load = time.perf_counter() - started
    document = pyropdf.Document()
    page = document.pages.new((400, 100))
    started = time.perf_counter()
    with page.draw() as canvas:
        canvas.text(SAMPLE, (10, 40), font=emoji, size=32)
    draw = time.perf_counter() - started
    data = document.to_bytes()
    (folder / "today-segoe.pdf").write_bytes(data)
    result["segoe_ui_emoji"] = {
        "load_s": round(load, 3),
        "first_draw_s": round(draw, 3),
        "saved_kib": round(len(data) / 1024, 1),
        "readers": readers(data, SAMPLE),
        "colours": renders(data, folder / "today-segoe"),
    }
    for kind in ("colr1", "sbix", "svg", "bitmap-only"):
        face = small_face(kind)
        row: dict[str, object] = {}
        try:
            font = pyropdf.Font.from_bytes(face)
        except pyropdf.PdfError as error:
            row["refused"] = str(error)
        else:
            document = pyropdf.Document()
            page = document.pages.new((100, 100))
            with page.draw() as canvas:
                canvas.text("A", (10, 20), font=font, size=48)
            data = document.to_bytes()
            row["pixels_drawn"] = pyropdf.open(data).pages[0].pixmap(dpi=72).color_count() > 1
            row["text"] = pyropdf.open(data).pages[0].text().strip()
        result[kind] = row
    return result


# --- COLR painted into PDF operators. -----------------------------------------


class _Outline(BasePen):
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


class Painter:
    """Turns COLR v1 paints into content operators and shading resources."""

    def __init__(self, font: TTFont) -> None:
        self.font = font
        self.table = font["COLR"].table
        self.palette = font["CPAL"].palettes[0]
        self.glyphs = font.getGlyphSet()
        self.base = {record.BaseGlyph: record for record in self.table.BaseGlyphList.BaseGlyphPaintRecord}
        self.shadings: list[str] = []
        self.states: dict[float, str] = {}
        self.skipped: dict[str, int] = {}
        self.outlines: dict[str, str] = {}
        self.box = "-500 -500 3000 3000 re f"

    def _colour(self, entry: object) -> tuple[float, float, float, float]:
        colour = self.palette[entry.PaletteIndex]
        return colour.red / 255, colour.green / 255, colour.blue / 255, colour.alpha / 255 * entry.Alpha

    def _outline(self, name: str) -> str:
        if name not in self.outlines:
            pen = _Outline(self.glyphs)
            self.glyphs[name].draw(pen)
            self.outlines[name] = " ".join(pen.parts)
        return self.outlines[name]

    def _function(self, line: object) -> str:
        stops = sorted(line.ColorStop, key=lambda stop: stop.StopOffset)
        if any(self._colour(stop)[3] < 1 for stop in stops):
            self.skipped["stop alpha"] = self.skipped.get("stop alpha", 0) + 1
        colours = [(stop.StopOffset, self._colour(stop)[:3]) for stop in stops]
        if colours[0][0] > 0:
            colours.insert(0, (0.0, colours[0][1]))
        if colours[-1][0] < 1:
            colours.append((1.0, colours[-1][1]))
        parts = [
            f"<</FunctionType 2/Domain[0 1]/N 1/C0[{a[1][0]:.3f} {a[1][1]:.3f} {a[1][2]:.3f}]/C1[{b[1][0]:.3f} {b[1][1]:.3f} {b[1][2]:.3f}]>>"
            for a, b in zip(colours, colours[1:], strict=False)
        ]
        if len(parts) == 1:
            return parts[0]
        bounds = " ".join(f"{offset:.4f}" for offset, _ in colours[1:-1])
        encode = " ".join("0 1" for _ in parts)
        return f"<</FunctionType 3/Domain[0 1]/Functions[{''.join(parts)}]/Bounds[{bounds}]/Encode[{encode}]>>"

    def paint(self, paint: object, depth: int = 0) -> str:
        """Return the operators of ``paint`` inside the current clip."""
        kind = paint.getFormatName()
        if depth > 64:
            self.skipped["depth"] = self.skipped.get("depth", 0) + 1
            return ""
        if kind == "PaintColrLayers":
            layers = self.table.LayerList.Paint[paint.FirstLayerIndex : paint.FirstLayerIndex + paint.NumLayers]
            return "\n".join(self.paint(layer, depth + 1) for layer in layers)
        if kind == "PaintColrGlyph":
            return self.paint(self.base[paint.Glyph].Paint, depth + 1)
        if kind == "PaintGlyph":
            return f"q {self._outline(paint.Glyph)} W n\n{self.paint(paint.Paint, depth + 1)}\nQ"
        if kind == "PaintSolid":
            red, green, blue, alpha = self._colour(paint)
            state = ""
            if alpha < 1:
                name = self.states.setdefault(round(alpha, 3), f"A{len(self.states)}")
                state = f"/{name} gs "
            return f"{state}{red:.3f} {green:.3f} {blue:.3f} rg {self.box}"
        if kind == "PaintLinearGradient":
            # The axis runs from p0 along p1 - p0 projected on the normal of p2 - p0.
            x0, y0, x1, y1, x2, y2 = paint.x0, paint.y0, paint.x1, paint.y1, paint.x2, paint.y2
            normal = (y2 - y0, x0 - x2)
            length = normal[0] ** 2 + normal[1] ** 2 or 1.0
            scale = ((x1 - x0) * normal[0] + (y1 - y0) * normal[1]) / length
            end = (x0 + normal[0] * scale, y0 + normal[1] * scale)
            self.shadings.append(
                f"<</ShadingType 2/ColorSpace/DeviceRGB/Coords[{x0} {y0} {end[0]:.1f} {end[1]:.1f}]/Extend[true true]/Function {self._function(paint.ColorLine)}>>",
            )
            return f"/S{len(self.shadings) - 1} sh"
        if kind == "PaintRadialGradient":
            self.shadings.append(
                f"<</ShadingType 3/ColorSpace/DeviceRGB/Coords[{paint.x0} {paint.y0} {paint.r0} {paint.x1} {paint.y1} {paint.r1}]/Extend[true true]/Function {self._function(paint.ColorLine)}>>",
            )
            return f"/S{len(self.shadings) - 1} sh"
        if kind == "PaintTransform":
            t = paint.Transform
            matrix = f"{t.xx:.5f} {t.yx:.5f} {t.xy:.5f} {t.yy:.5f} {t.dx:.2f} {t.dy:.2f}"
            return f"q {matrix} cm\n{self.paint(paint.Paint, depth + 1)}\nQ"
        if kind == "PaintTranslate":
            return f"q 1 0 0 1 {paint.dx} {paint.dy} cm\n{self.paint(paint.Paint, depth + 1)}\nQ"
        if kind == "PaintScale":
            return f"q {paint.scaleX} 0 0 {paint.scaleY} 0 0 cm\n{self.paint(paint.Paint, depth + 1)}\nQ"
        if kind == "PaintScaleAroundCenter":
            cx, cy = paint.centerX, paint.centerY
            return f"q 1 0 0 1 {cx} {cy} cm {paint.scaleX} 0 0 {paint.scaleY} 0 0 cm 1 0 0 1 {-cx} {-cy} cm\n{self.paint(paint.Paint, depth + 1)}\nQ"
        self.skipped[kind] = self.skipped.get(kind, 0) + 1
        return self.paint(paint.BackdropPaint, depth + 1) if kind == "PaintComposite" else ""

    def resources(self, extra: str = "") -> str:
        shadings = "".join(f"/S{index} {body}" for index, body in enumerate(self.shadings))
        states = "".join(f"/{name}<</ca {alpha}>>" for alpha, name in self.states.items())
        return f"<</Shading<<{shadings}>>/ExtGState<<{states}>>{extra}>>"


def write_pdf(objects: list[bytes]) -> bytes:
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
    packed = zlib.compress(data, 9)
    return f"<</Length {len(packed)}/Filter/FlateDecode{extra}>>\nstream\n".encode() + packed + b"\nendstream"


def to_unicode(chars: list[str]) -> bytes:
    lines = [
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
        "/CMapName /Prototype def /CMapType 2 def",
        "1 begincodespacerange <00> <FF> endcodespacerange",
        f"{len(chars)} beginbfchar",
    ]
    lines += [f"<{code:02X}> <{char.encode('utf-16-be').hex().upper()}>" for code, char in enumerate(chars)]
    lines += ["endbfchar", "endcmap", "CMapName currentdict /CMap defineresource pop end end"]
    return "\n".join(lines).encode()


def mask_image() -> tuple[bytes, bytes]:
    """Return a 64x64 RGB gradient and its circular mask."""
    rgb, alpha = bytearray(), bytearray()
    for y in range(64):
        for x in range(64):
            rgb += bytes((x * 4, y * 4, 128))
            alpha.append(255 if (x - 32) ** 2 + (y - 32) ** 2 < 900 else 0)
    return bytes(rgb), bytes(alpha)


def build(font: TTFont, text: str, *, kind: str) -> tuple[bytes, dict[str, object]]:
    """Return 100 pages that draw ``text`` as Type 3 text or as Forms."""
    cmap = font.getBestCmap()
    upem = font["head"].unitsPerEm
    chars = list(text)
    names = [cmap[ord(char)] for char in chars]
    painter = Painter(font)
    procedures = []
    for name in names:
        started = len(painter.shadings)
        body = painter.paint(painter.base[name].Paint)
        procedures.append((name, body, len(painter.shadings) - started))
    advances = [font["hmtx"][name][0] for name in names]
    objects: list[bytes] = [b"<</Type/Catalog/Pages 2 0 R>>", b""]
    resource_number = len(objects) + 1
    objects.append(painter.resources().encode())
    size = 32
    if kind == "type3":
        scale = 1 / upem
        procs = []
        for (_, body, _), advance in zip(procedures, advances, strict=True):
            procs.append(len(objects) + 1)
            objects.append(stream(f"{advance} 0 d0\n{body}".encode()))
        unicode_number = len(objects) + 1
        objects.append(stream(to_unicode(chars)))
        char_procs = "".join(f"/g{index} {number} 0 R" for index, number in enumerate(procs))
        differences = " ".join(f"/g{index}" for index in range(len(procs)))
        widths = " ".join(str(advance) for advance in advances)
        font_number = len(objects) + 1
        objects.append(
            (
                f"<</Type/Font/Subtype/Type3/FontBBox[-500 -500 3000 3000]/FontMatrix[{scale:.6f} 0 0 {scale:.6f} 0 0]"
                f"/CharProcs<<{char_procs}>>/Encoding<</Type/Encoding/Differences[0 {differences}]>>"
                f"/FirstChar 0/LastChar {len(procs) - 1}/Widths[{widths}]/Resources {resource_number} 0 R/ToUnicode {unicode_number} 0 R>>"
            ).encode(),
        )
        resources = f"<</Font<</T0 {font_number} 0 R>>>>"
        code = "".join(f"{index:02X}" for index in range(len(chars)))
        content = f"BT /T0 {size} Tf 10 40 Td <{code}> Tj ET".encode()
    else:
        forms = []
        for _, body, _ in procedures:
            forms.append(len(objects) + 1)
            objects.append(
                stream(
                    body.encode(),
                    f"/Type/XObject/Subtype/Form/BBox[-500 -500 3000 3000]/Resources {resource_number} 0 R",
                )
            )
        names_dict = "".join(f"/G{index} {number} 0 R" for index, number in enumerate(forms))
        resources = f"<</XObject<<{names_dict}>>>>"
        scale = size / upem
        parts, x = [], 0.0
        for index, (char, advance) in enumerate(zip(chars, advances, strict=True)):
            hexed = "FEFF" + char.encode("utf-16-be").hex().upper()
            parts.append(
                f"/Span<</ActualText<{hexed}>>>BDC q {scale:.5f} 0 0 {scale:.5f} {10 + x:.2f} 40 cm /G{index} Do Q EMC"
            )
            x += advance * scale
        content = "\n".join(parts).encode()
    kids = []
    for _ in range(PAGES):
        number = len(objects) + 1
        objects.append(stream(content))
        kids.append(len(objects) + 1)
        objects.append(
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 100]/Resources {resources}/Contents {number} 0 R>>".encode()
        )
    objects[1] = f"<</Type/Pages/Count {PAGES}/Kids[{' '.join(f'{kid} 0 R' for kid in kids)}]>>".encode()
    decoded = [len(body) for _, body, _ in procedures]
    return write_pdf(objects), {
        "glyph_operators_bytes": decoded,
        "shadings": [count for _, _, count in procedures],
        "skipped": painter.skipped,
    }


def image_glyph() -> bytes:
    """One Type 3 glyph that paints an image with a soft mask, as a bitmap face would."""
    rgb, alpha = mask_image()
    objects: list[bytes] = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Count 1/Kids[8 0 R]>>"]
    objects.append(
        stream(alpha, "/Type/XObject/Subtype/Image/Width 64/Height 64/ColorSpace/DeviceGray/BitsPerComponent 8")
    )
    objects.append(
        stream(
            rgb, "/Type/XObject/Subtype/Image/Width 64/Height 64/ColorSpace/DeviceRGB/BitsPerComponent 8/SMask 3 0 R"
        )
    )
    objects.append(stream(b"1000 0 d0 q 1000 0 0 1000 0 0 cm /Im0 Do Q"))
    objects.append(stream(to_unicode(["😀"])))
    objects.append(
        b"<</Type/Font/Subtype/Type3/FontBBox[0 0 1000 1000]/FontMatrix[0.001 0 0 0.001 0 0]/CharProcs<</g0 5 0 R>>"
        b"/Encoding<</Type/Encoding/Differences[0/g0]>>/FirstChar 0/LastChar 0/Widths[1000]"
        b"/Resources<</XObject<</Im0 4 0 R>>>>/ToUnicode 6 0 R>>",
    )
    objects.append(stream(b"BT /T0 48 Tf 10 20 Td <00> Tj ET"))
    objects.append(b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]/Resources<</Font<</T0 7 0 R>>>>/Contents 9 0 R>>")
    return write_pdf([*objects[:7], objects[8], objects[7]])


def main() -> None:
    out, folder = Path(sys.argv[1]), Path(sys.argv[2])
    folder.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"today": today(folder)}
    print("today done", flush=True)
    font = TTFont(EMOJI_FACE, lazy=True)
    for kind in ("type3", "form"):
        started = time.perf_counter()
        data, details = build(font, SAMPLE, kind=kind)
        elapsed = time.perf_counter() - started
        (folder / f"{kind}.pdf").write_bytes(data)
        document = pyropdf.open(data)
        items = {type(item).__name__ for item in document.pages[0].content()}
        result[kind] = {
            "build_s": round(elapsed, 2),
            "file_kib_100_pages": round(len(data) / 1024, 1),
            "readers": readers(data, SAMPLE),
            "colours": renders(data, folder / kind),
            "content_items": sorted(items),
            **details,
        }
        print(kind, "done", flush=True)
    data = image_glyph()
    (folder / "type3-image.pdf").write_bytes(data)
    result["type3_image"] = {"readers": readers(data, "😀"), "colours": renders(data, folder / "type3-image")}
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", "utf-8")
    print(out)


if __name__ == "__main__":
    main()
