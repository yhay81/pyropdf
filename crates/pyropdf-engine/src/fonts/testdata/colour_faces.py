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
table, and ``eblc-only`` has that table and no outlines, which a load refuses.
``bitmap-only`` has no outlines at all. ``sbix-jpeg`` and ``cbdt-raw`` hold
strikes that drawing does not read, and ``sbix-damaged`` PNGs that do not
decode.

The ``bitmaps-*`` faces are for drawing in colour, with solid images that a
render can tell apart. Their em is 1000, and a strike's image covers one em
from the baseline. They draw:

- ``R``: red, in strikes of 32 and 120 pixels per em, with no outline;
- ``G``: green, in the strike of 120 alone, with a fallback outline;
- ``T``: red at half opacity, in the strike of 32;
- ``D``: in ``sbix``, a ``dupe`` record that repeats ``R``;
- ``O``: an outline, 700 units wide, and no bitmap;
- ``N``: nothing, and a space that is 300 units wide;
- U+E000 onward: ``MANY`` glyphs of the strike of 32, each its own colour.

``-only`` faces have no outlines. ``bitmaps-sbix-2048`` has an em of 2048 and
none of the ``MANY``.
"""

from __future__ import annotations

import io
import struct
import zlib
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
#: The glyphs of a ``bitmaps-*`` face beyond one font's 256 codes.
MANY = 300


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


def _sbix(graphic: bytes = b"png ", image: bytes = PNG) -> object:
    """Return an ``sbix`` table with one strike of an image for each covered glyph."""
    record = struct.pack(">hh4s", 0, 0, graphic) + image
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
    elif kind == "sbix-jpeg":
        font["sbix"] = _sbix(b"jpg ", b"\xff\xd8\xff\xd9")
    elif kind == "sbix-damaged":
        # A header that asks for one pixel, whose checksum and data do not hold.
        header = struct.pack(">I4sIIBBBBB", 13, b"IHDR", 1, 1, 8, 2, 0, 0, 0)
        font["sbix"] = _sbix(b"png ", b"\x89PNG\r\n\x1a\n" + header + bytes(16))
    elif kind == "cbdt":
        image = struct.pack(">BBbbB", 1, 1, 0, 1, 1) + struct.pack(">I", len(PNG)) + PNG
        _bitmaps(font, ("CBLC", "CBDT"), 3, 17, image)
    elif kind == "cbdt-raw":
        # Image format 1: small metrics, then one pixel of 32 bits.
        image = struct.pack(">BBbbB", 1, 1, 0, 1, 1) + bytes(4)
        _bitmaps(font, ("CBLC", "CBDT"), 3, 1, image)
    elif kind in {"eblc", "eblc-only"}:
        # Image format 2: small metrics, then one byte of a 1x1 bitmap.
        image = struct.pack(">BBbbB", 1, 1, 0, 1, 1) + b"\x80"
        _bitmaps(font, ("EBLC", "EBDT"), 2, 2, image)
    elif kind in {"svg", "svg-cff"}:
        _svg(font)
    if kind in {"bitmap-only", "eblc-only"}:
        del font["glyf"]
        del font["loca"]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def _png(size: int, rgba: tuple[int, int, int, int]) -> bytes:
    """Return a PNG of ``size`` by ``size`` pixels of one colour."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    rows = (b"\x00" + bytes(rgba) * size) * size
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def _bitmap_strikes(many: int) -> tuple[tuple[int, dict[str, bytes]], ...]:
    """Return the strikes of the ``bitmaps-*`` faces: pixels per em, and each glyph's PNG."""
    small = {"R": _png(32, (255, 0, 0, 255)), "T": _png(32, (255, 0, 0, 128))}
    for index in range(many):
        small[f"many{index}"] = _png(32, (index % 256, index // 256, 128, 255))
    return (32, small), (120, {"R": _png(120, (255, 0, 0, 255)), "G": _png(120, (0, 255, 0, 255))})


def _sbix_strikes(order: list[str], strikes: tuple[tuple[int, dict[str, bytes]], ...]) -> object:
    """Return an ``sbix`` table of ``strikes``, in which ``D`` repeats ``R`` where a strike has it."""
    bodies = []
    for ppem, images in strikes:
        offsets, records = [], b""
        at = 4 + 4 * (len(order) + 1)
        for name in order:
            offsets.append(at)
            if name in images:
                record = struct.pack(">hh4s", 0, 0, b"png ") + images[name]
            elif name == "D" and "R" in images:
                record = struct.pack(">hh4sH", 0, 0, b"dupe", order.index("R"))
            else:
                record = b""
            records += record
            at += len(record)
        offsets.append(at)
        bodies.append(
            struct.pack(">HH", ppem, 72) + b"".join(struct.pack(">I", offset) for offset in offsets) + records
        )
    at = 8 + 4 * len(bodies)
    starts = []
    for body in bodies:
        starts.append(struct.pack(">I", at))
        at += len(body)
    return _opaque("sbix", struct.pack(">HHI", 1, 1, len(bodies)) + b"".join(starts) + b"".join(bodies))


def _cbdt_strikes(font: TTFont, order: list[str], strikes: tuple[tuple[int, dict[str, bytes]], ...]) -> None:
    """Add ``CBLC`` and ``CBDT`` tables of ``strikes``: small metrics in the first, big in the others."""
    data = struct.pack(">HH", 3, 0)
    sizes, arrays = [], []
    arrays_at = 8 + 48 * len(strikes)
    for at_strike, (ppem, images) in enumerate(strikes):
        ids = [order.index(name) for name in images]
        first, last = min(ids), max(ids)
        start = len(data)
        offsets, at, body = [], 0, b""
        for glyph in range(first, last + 1):
            offsets.append(at)
            if order[glyph] in images:
                image = images[order[glyph]]
                if at_strike == 0:
                    metrics = struct.pack(">BBbbB", ppem, ppem, 0, ppem, ppem)
                else:
                    metrics = struct.pack(">BBbbBbbB", ppem, ppem, 0, ppem, ppem, 0, 0, ppem)
                record = metrics + struct.pack(">I", len(image)) + image
                body += record
                at += len(record)
        offsets.append(at)
        data += body
        image_format = 17 if at_strike == 0 else 18
        subtable = struct.pack(">HHI", 1, image_format, start)
        subtable += b"".join(struct.pack(">I", offset) for offset in offsets)
        array = struct.pack(">HHI", first, last, 8) + subtable
        metrics = bytes(12)
        sizes.append(
            struct.pack(">IIII", arrays_at, len(array), 1, 0)
            + metrics
            + metrics
            + struct.pack(">HHBBBb", first, last, ppem, ppem, 32, 1)
        )
        arrays.append(array)
        arrays_at += len(array)
    font["CBLC"] = _opaque("CBLC", struct.pack(">HHI", 3, 0, len(strikes)) + b"".join(sizes) + b"".join(arrays))
    font["CBDT"] = _opaque("CBDT", data)


def bitmap_face(
    table: str,
    *,
    outlines: bool,
    em: int = 1000,
    many: int = MANY,
    strikes: tuple[tuple[int, dict[str, bytes]], ...] | None = None,
) -> bytes:
    """Return the ``bitmaps-*`` face of ``table``, of ``em`` units with ``many`` of its numbered glyphs.

    ``strikes`` replaces its strikes, each pixels per em with each glyph's PNG.
    """
    order = [".notdef", "R", "G", "T", "D", "O", "N", "space", *(f"many{index}" for index in range(many))]
    cmap = {ord(name): name for name in "RGTDON"} | {0x20: "space"}
    cmap |= {0xE000 + index: f"many{index}" for index in range(many)}
    builder = FontBuilder(em, isTTF=True)
    builder.setupHead(unitsPerEm=em, created=0, modified=0)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(cmap)
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        if name in {"G", "O"}:
            _draw(pen, SQUARE)
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    advances = dict.fromkeys(order, (em, 0)) | {"O": (em * 7 // 10, 0), "space": (em * 3 // 10, 0)}
    builder.setupHorizontalMetrics(advances)
    builder.setupHorizontalHeader(ascent=em, descent=-em // 5)
    builder.setupNameTable({"familyName": f"Bitmaps {table}", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    font = builder.font
    strikes = strikes or _bitmap_strikes(many)
    if table == "sbix":
        font["sbix"] = _sbix_strikes(order, strikes)
    else:
        _cbdt_strikes(font, order, strikes)
    if not outlines:
        del font["glyf"]
        del font["loca"]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def main() -> None:
    """Write every face next to this script."""
    kinds = ("colr0", "colr1", "sbix", "cbdt", "eblc", "svg", "svg-cff", "bitmap-only")
    for kind in (*kinds, "sbix-jpeg", "cbdt-raw", "sbix-damaged", "eblc-only"):
        suffix = "otf" if kind == "svg-cff" else "ttf"
        (HERE / f"colour-{kind}.{suffix}").write_bytes(face(kind))
    for table in ("sbix", "cbdt"):
        (HERE / f"colour-bitmaps-{table}.ttf").write_bytes(bitmap_face(table, outlines=True))
        (HERE / f"colour-bitmaps-{table}-only.ttf").write_bytes(bitmap_face(table, outlines=False))
    (HERE / "colour-bitmaps-sbix-2048.ttf").write_bytes(bitmap_face("sbix", outlines=True, em=2048, many=0))


if __name__ == "__main__":
    main()
