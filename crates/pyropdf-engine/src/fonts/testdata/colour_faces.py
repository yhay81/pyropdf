# ruff: noqa: INP001, PLR2004, S101 - a script that writes test data, not a package.
"""Write the small colour faces of the engine's and the canvas's tests.

Run from the repository root:
``uv run --with fonttools python crates/pyropdf-engine/src/fonts/testdata/colour_faces.py``.
The faces are committed, so the tests do not need fontTools. Each draws:

- ``A``: a colour glyph with an empty outline;
- ``B``: a colour glyph with a fallback outline;
- ``C``: an ordinary glyph;
- ``D``: a colour glyph whose outline is two points, which paint nothing;
- ``E``: an empty glyph that no table covers;
- a space that no table covers, and a no-break space that the table covers.

In ``eblc``, the table is a monochrome bitmap table, which is not a colour
table. ``bitmap-only`` has no outlines at all, and a load refuses it.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from fontTools.colorLib.builder import buildCOLR, buildCPAL
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import newTable
from fontTools.ttLib.tables import otTables
from fontTools.ttLib.tables.S_V_G_ import SVGDocument

if TYPE_CHECKING:
    from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
ORDER = (".notdef", "A", "B", "C", "D", "E", "space", "nbspace", "layer")
CMAP = {0x41: "A", 0x42: "B", 0x43: "C", 0x44: "D", 0x45: "E", 0x20: "space", 0xA0: "nbspace"}
#: The glyphs that the colour table covers.
COVERED = ("A", "B", "D", "nbspace")
SQUARE = ((100, 0), (100, 800), (900, 800), (900, 0))
LINE = ((100, 0), (900, 800))
#: A 1x1 PNG.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)
SHAPES = {"B": SQUARE, "C": SQUARE, "D": LINE, "layer": SQUARE}


def _draw(pen: object, points: tuple[tuple[int, int], ...]) -> None:
    pen.moveTo(points[0])
    for point in points[1:]:
        pen.lineTo(point)
    pen.closePath()


def _truetype(builder: FontBuilder) -> None:
    glyphs = {}
    for name in ORDER:
        pen = TTGlyphPen(None)
        if name in SHAPES:
            _draw(pen, SHAPES[name])
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)


def _cff(builder: FontBuilder) -> None:
    glyphs = {}
    for name in ORDER:
        pen = T2CharStringPen(1000, None)
        if name in SHAPES:
            _draw(pen, SHAPES[name])
        glyphs[name] = pen.getCharString()
    builder.setupCFF("Colour-Regular", {"FullName": "Colour Regular"}, glyphs, {})


def _opaque(tag: str, data: bytes) -> object:
    """Return a table that compiles to ``data``, which fontTools then does not read."""
    table = newTable(tag)
    table.compile = lambda _font: data
    return table


def _sbix() -> object:
    """Return an ``sbix`` table with one strike of a PNG for each covered glyph."""
    record = struct.pack(">hh4s", 0, 0, b"png ") + PNG
    offsets, at = [], 4 + 4 * (len(ORDER) + 1)
    for name in ORDER:
        offsets.append(at)
        at += len(record) if name in COVERED else 0
    offsets.append(at)
    strike = struct.pack(">HH", 72, 72) + b"".join(struct.pack(">I", offset) for offset in offsets)
    strike += record * len(COVERED)
    return _opaque("sbix", struct.pack(">HHI", 1, 1, 1) + struct.pack(">I", 12) + strike)


def _bitmaps(font: TTFont, tags: tuple[str, str], major: int, image_format: int, image: bytes) -> None:
    """Add a bitmap location and data table: one strike whose covered glyphs each have ``image``."""
    location, data = tags
    ids = [ORDER.index(name) for name in COVERED]
    first, last = min(ids), max(ids)
    offsets, at = [], 0
    for glyph in range(first, last + 1):
        offsets.append(at)
        at += len(image) if ORDER[glyph] in COVERED else 0
    offsets.append(at)
    subtable = struct.pack(">HHI", 1, image_format, 4) + b"".join(struct.pack(">I", offset) for offset in offsets)
    records = struct.pack(">HHI", first, last, 8) + subtable
    metrics = bytes(12)
    size = struct.pack(">IIII", 8 + 48, len(records), 1, 0) + metrics + metrics
    size += struct.pack(">HHBBBb", first, last, 72, 72, 32 if major == 3 else 1, 1)
    assert len(size) == 48
    font[location] = _opaque(location, struct.pack(">HHI", major, 0, 1) + size + records)
    body = b"".join(image for name in ORDER[first : last + 1] if name in COVERED)
    font[data] = _opaque(data, struct.pack(">HH", major, 0) + body)


def _svg(font: TTFont) -> None:
    documents = []
    for name in COVERED:
        glyph = ORDER.index(name)
        text = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            f'<rect id="glyph{glyph}" width="800" height="800" fill="red"/></svg>'
        )
        documents.append(SVGDocument(text, glyph, glyph, compressed=False))
    table = newTable("SVG ")
    table.docList = documents
    font["SVG "] = table


def _colr(font: TTFont, version: int) -> None:
    font["CPAL"] = buildCPAL([[(1.0, 0.0, 0.0, 1.0)]])
    if version == 0:
        glyphs = {name: [("layer", 0)] for name in COVERED}
    else:
        paint = {
            "Format": otTables.PaintFormat.PaintGlyph,
            "Paint": {"Format": otTables.PaintFormat.PaintSolid, "PaletteIndex": 0, "Alpha": 1.0},
            "Glyph": "layer",
        }
        glyphs = dict.fromkeys(COVERED, paint)
    font["COLR"] = buildCOLR(glyphs, version=version, glyphMap=font.getReverseGlyphMap())


def face(kind: str) -> bytes:
    """Return the face of ``kind``."""
    cff = kind == "svg-cff"
    builder = FontBuilder(1000, isTTF=not cff)
    builder.setupHead(unitsPerEm=1000, created=0, modified=0)
    builder.setupGlyphOrder(list(ORDER))
    builder.setupCharacterMap(CMAP)
    (_cff if cff else _truetype)(builder)
    builder.setupHorizontalMetrics(dict.fromkeys(ORDER, (1000, 0)))
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": f"Colour {kind}", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    font = builder.font
    if kind in {"colr0", "colr1"}:
        _colr(font, int(kind[-1]))
    elif kind in {"sbix", "bitmap-only"}:
        font["sbix"] = _sbix()
    elif kind == "cbdt":
        image = struct.pack(">BBbbB", 1, 1, 0, 1, 1) + struct.pack(">I", len(PNG)) + PNG
        _bitmaps(font, ("CBLC", "CBDT"), 3, 17, image)
    elif kind == "eblc":
        # Image format 2: small metrics, then one byte of a 1x1 bitmap.
        image = struct.pack(">BBbbB", 1, 1, 0, 1, 1) + b"\x80"
        _bitmaps(font, ("EBLC", "EBDT"), 2, 2, image)
    elif kind in {"svg", "svg-cff"}:
        _svg(font)
    if kind == "bitmap-only":
        del font["glyf"]
        del font["loca"]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def main() -> None:
    """Write every face next to this script."""
    for kind in ("colr0", "colr1", "sbix", "cbdt", "eblc", "svg", "svg-cff", "bitmap-only"):
        suffix = "otf" if kind == "svg-cff" else "ttf"
        (HERE / f"colour-{kind}.{suffix}").write_bytes(face(kind))


if __name__ == "__main__":
    main()
