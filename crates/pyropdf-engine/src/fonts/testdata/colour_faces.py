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
``sbix-jpeg`` and ``cbdt-raw`` hold strikes that drawing does not read, and
``sbix-damaged`` PNGs that do not decode. ``colr0`` and ``colr1`` paint the
square in red, which the canvas draws; ``colr-sweep`` paints it with a sweep
gradient, which it does not. ``colr-unclipped`` paints red with no outline or
clip box around it, so the paint covers the face's box, and its ``head``
gives that box with its corners swapped, as a damaged face may.

``colr-paints`` is for drawing COLR v1 in colour. Its em is 1000, and each
glyph's layers are a square from (100, 0) to (900, 800), or its left or right
half. Its palette is red, blue, green, and cyan. It draws:

- ``L``: a linear gradient from red at x = 100 to blue at x = 900;
- ``R``: a radial gradient from green at (500, 400) to blue 400 units out;
- ``T``: green, through a transform that halves it toward the origin;
- ``K``: blue, in a clip box of the left half;
- ``M``: cyan multiplied over red on the right half, which is black;
- ``I``: blue in the alpha of red's left half (``SrcIn``);
- ``O``: red, with blue's left half cut out (``DestOut``);
- ``P``: blue and red in ``Xor``, which the canvas does not draw;
- ``G``: ``PaintColrGlyph`` of ``L``;
- ``Y`` and ``J``: red, then blue at half alpha on the right half, layers that
  the two glyphs share;
- ``A``: red, from opaque at x = 100 to clear at x = 900, and ``F``: the same
  to clear blue, which in premultiplied alpha never shows;
- ``N``: the text's colour, and ``Q``: a gradient stop in it, which the canvas
  does not draw, and ``U``: the same gradient clipped to the space's empty
  outline, which shows nothing and still refuses;
- ``W``: a sweep gradient, with a fallback outline, and ``V``: the same paint
  with none;
- ``X``: ``PaintColrGlyph`` of itself, a cycle, with a fallback outline;
- ``Z``: green, under ``DEEP`` nested transforms, past skrifa's depth;
- ``H``: ``HEAVY`` layers, each its own gradient with alpha.

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


def _colr(font: TTFont, kind: str) -> None:
    font["CPAL"] = buildCPAL([[(1.0, 0.0, 0.0, 1.0)]])
    if kind == "colr0":
        glyphs = {name: [("layer", 0)] for name in COVERED}
    else:
        paint = {"Format": otTables.PaintFormat.PaintSolid, "PaletteIndex": 0, "Alpha": 1.0}
        if kind == "colr-sweep":
            paint = {
                "Format": otTables.PaintFormat.PaintSweepGradient,
                "ColorLine": _line((0, 0, 1.0), (1, 0, 0.5)),
                "centerX": 500,
                "centerY": 400,
                "startAngle": 0,
                "endAngle": 360,
            }
        root = paint if kind == "colr-unclipped" else _glyph("layer", paint)
        glyphs = dict.fromkeys(COVERED, root)
    version = 0 if kind == "colr0" else 1
    font["COLR"] = buildCOLR(glyphs, version=version, glyphMap=font.getReverseGlyphMap())
    if kind == "colr-unclipped":
        # Each axis's minimum above its maximum, which the save keeps as set.
        head = font["head"]
        head.xMin, head.yMin, head.xMax, head.yMax = 900, 800, 100, 0
        font.recalcBBoxes = False


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
    if kind in {"colr0", "colr1", "colr-sweep", "colr-unclipped"}:
        _colr(font, kind)
    elif kind == "sbix":
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
    if kind == "eblc-only":
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


#: The palette of ``colr-paints``: red, blue, green, and cyan.
PALETTE = ((1.0, 0.0, 0.0, 1.0), (0.0, 0.0, 1.0, 1.0), (0.0, 1.0, 0.0, 1.0), (0.0, 1.0, 1.0, 1.0))
RED, BLUE, GREEN, CYAN = range(4)
#: The palette index of the text's colour.
TEXT_COLOUR = 0xFFFF
#: The shapes of ``colr-paints``' layers, which no character maps.
LAYERS = {
    "square": SQUARE,
    "left": ((100, 0), (100, 800), (500, 800), (500, 0)),
    "right": ((500, 0), (500, 800), (900, 800), (900, 0)),
}
#: The layers of the glyph ``H``, each its own gradient.
HEAVY = 200
#: How deep ``Z`` nests its transforms, past skrifa's 64.
DEEP = 70


def _solid(index: int, alpha: float = 1.0) -> dict[str, object]:
    return {"Format": otTables.PaintFormat.PaintSolid, "PaletteIndex": index, "Alpha": alpha}


def _glyph(shape: str, paint: dict[str, object]) -> dict[str, object]:
    return {"Format": otTables.PaintFormat.PaintGlyph, "Paint": paint, "Glyph": shape}


def _line(*stops: tuple[float, int, float], extend: str = "pad") -> dict[str, object]:
    return {
        "Extend": extend,
        "ColorStop": [{"StopOffset": offset, "PaletteIndex": index, "Alpha": alpha} for offset, index, alpha in stops],
    }


def _linear(line: dict[str, object], x0: float = 100, x1: float = 900) -> dict[str, object]:
    return {
        "Format": otTables.PaintFormat.PaintLinearGradient,
        "ColorLine": line,
        "x0": x0,
        "y0": 0,
        "x1": x1,
        "y1": 0,
        "x2": x0,
        "y2": 800,
    }


def _layers(*layers: dict[str, object]) -> dict[str, object]:
    return {"Format": otTables.PaintFormat.PaintColrLayers, "Layers": list(layers)}


def _composite(mode: str, source: dict[str, object], backdrop: dict[str, object]) -> dict[str, object]:
    return {
        "Format": otTables.PaintFormat.PaintComposite,
        "SourcePaint": source,
        "CompositeMode": mode,
        "BackdropPaint": backdrop,
    }


def _transform(paint: dict[str, object], scale: float = 1.0, dx: float = 0.0) -> dict[str, object]:
    return {
        "Format": otTables.PaintFormat.PaintTransform,
        "Paint": paint,
        "Transform": {"xx": scale, "yx": 0, "xy": 0, "yy": scale, "dx": dx, "dy": 0},
    }


def _paints() -> dict[str, object]:
    """Return the root paint of each colour glyph of ``colr-paints``."""
    red_to_blue = _glyph("square", _linear(_line((0, RED, 1.0), (1, BLUE, 1.0))))
    deep = _glyph("square", _solid(GREEN))
    for _ in range(DEEP):
        deep = _transform(deep)
    return {
        "L": red_to_blue,
        "R": _glyph(
            "square",
            {
                "Format": otTables.PaintFormat.PaintRadialGradient,
                "ColorLine": _line((0, GREEN, 1.0), (1, BLUE, 1.0)),
                "x0": 500,
                "y0": 400,
                "r0": 0,
                "x1": 500,
                "y1": 400,
                "r1": 400,
            },
        ),
        "T": _transform(_glyph("square", _solid(GREEN)), scale=0.5),
        "K": _glyph("square", _solid(BLUE)),
        "M": _composite("multiply", _glyph("right", _solid(CYAN)), _glyph("square", _solid(RED))),
        "I": _composite("src_in", _glyph("square", _solid(BLUE)), _glyph("left", _solid(RED))),
        "O": _composite("dest_out", _glyph("left", _solid(BLUE)), _glyph("square", _solid(RED))),
        "P": _composite("xor", _glyph("left", _solid(BLUE)), _glyph("square", _solid(RED))),
        "G": {"Format": otTables.PaintFormat.PaintColrGlyph, "Glyph": "L"},
        # Layers that ``J`` shares with ``Y``, which fontTools writes once.
        "Y": _layers(_glyph("square", _solid(RED)), _glyph("right", _solid(BLUE, 0.5))),
        "J": _layers(_glyph("square", _solid(RED)), _glyph("right", _solid(BLUE, 0.5))),
        "A": _glyph("square", _linear(_line((0, RED, 1.0), (1, RED, 0.0)))),
        "F": _glyph("square", _linear(_line((0, RED, 1.0), (1, BLUE, 0.0)))),
        "N": _glyph("square", _solid(TEXT_COLOUR)),
        "Q": _glyph("square", _linear(_line((0, TEXT_COLOUR, 1.0), (1, RED, 1.0)))),
        "W": _glyph(
            "square",
            {
                "Format": otTables.PaintFormat.PaintSweepGradient,
                "ColorLine": _line((0, RED, 1.0), (1, BLUE, 1.0)),
                "centerX": 500,
                "centerY": 400,
                "startAngle": 0,
                "endAngle": 360,
            },
        ),
        "X": {"Format": otTables.PaintFormat.PaintColrGlyph, "Glyph": "X"},
        "Z": deep,
        "H": _layers(
            *(
                _glyph("square", _linear(_line((0, RED, 1.0), (1, BLUE, 0.5)), x0=100 + layer, x1=900 - layer))
                for layer in range(HEAVY)
            )
        ),
    }


def paints_face() -> bytes:
    """Return ``colr-paints``, whose COLR v1 glyphs each take one kind of paint, as ``_paints`` lists them."""
    paints = _paints()
    names = list(paints)
    # Each fallback outline is a square: W, a paint that the canvas does not
    # draw, and X, a cycle, which refuses all the same.
    fallbacks = {"W", "X"}
    # U is last, so that the glyphs before it keep their IDs.
    order = [".notdef", *names, "V", "space", *LAYERS, "U"]
    paints["V"] = paints["W"]
    paints["U"] = _glyph("space", _linear(_line((0, TEXT_COLOUR, 1.0), (1, RED, 1.0))))
    cmap = {ord(name): name for name in [*names, "V", "U"]} | {0x20: "space"}
    builder = FontBuilder(1000, isTTF=True)
    builder.setupHead(unitsPerEm=1000, created=0, modified=0)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(cmap)
    shapes = {name: LAYERS.get(name, SQUARE if name in fallbacks else None) for name in order}
    glyphs = {}
    for name, shape in shapes.items():
        pen = TTGlyphPen(None)
        if shape:
            _draw(pen, shape)
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    # A side bearing that is the outline's own keeps it where it was drawn.
    metrics = {name: (1000, min(x for x, _ in shape) if shape else 0) for name, shape in shapes.items()}
    builder.setupHorizontalMetrics(metrics | {"space": (300, 0)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Colour colr-paints", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    font = builder.font
    font["CPAL"] = buildCPAL([list(PALETTE)])
    font["COLR"] = buildCOLR(
        paints,
        version=1,
        glyphMap=font.getReverseGlyphMap(),
        clipBoxes={"K": (100, 0, 500, 800)},
    )
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def main() -> None:
    """Write every face next to this script."""
    kinds = ("colr0", "colr1", "colr-sweep", "sbix", "cbdt", "eblc", "svg", "svg-cff")
    for kind in (*kinds, "sbix-jpeg", "cbdt-raw", "sbix-damaged", "eblc-only", "colr-unclipped"):
        suffix = "otf" if kind == "svg-cff" else "ttf"
        (HERE / f"colour-{kind}.{suffix}").write_bytes(face(kind))
    for table in ("sbix", "cbdt"):
        (HERE / f"colour-bitmaps-{table}.ttf").write_bytes(bitmap_face(table, outlines=True))
        (HERE / f"colour-bitmaps-{table}-only.ttf").write_bytes(bitmap_face(table, outlines=False))
    (HERE / "colour-bitmaps-sbix-2048.ttf").write_bytes(bitmap_face("sbix", outlines=True, em=2048, many=0))
    (HERE / "colour-colr-paints.ttf").write_bytes(paints_face())


if __name__ == "__main__":
    main()
