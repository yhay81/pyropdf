# ruff: noqa: INP001, D103 - a script that writes test data, not a package.
"""Write the variable faces and the oracle of the tests of instances.

Run from the repository root:
``uv run --no-project --with fonttools python crates/pyropdf-engine/src/fonts/testdata/variable_faces.py``.
The results are committed, so the tests do not need fontTools. Each face has a
weight axis from 100 to 900 with its default at 400:

- ``variable-latin.ttf``: ``A`` grows 100 units wider at 900, and the kerning
  of ``AV`` goes from -50 at 100 through -100 at the default to -300 at 900,
  with no change to ``V``. ``MVAR`` raises the capital height by 100 and the
  ascender by 50 at 900, and 700 is a named instance, Bold.
- ``variable-composites.ttf``: ``A`` is one of 16 composites of 32 copies of a
  glyph of 500 points, which flatten to many times the size of the file.
- ``variable-overflow.ttf``: ``A`` has a point at x = 32000, which the
  variations move to 34000 at 900.
- ``variable-colour.ttf``: ``A`` is a colour glyph with an empty outline, and
  ``B`` an ordinary glyph.

``NotoSansArabic-Variable-subset-instances.txt`` holds what fontTools' instancer
writes for ``NotoSansArabic-Variable-subset.ttf``, one line for each weight and
glyph, flattened: the advance, the box of the control points, and the area. The
instance is read back from its file, since only saving rounds the coordinates.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

from fontTools.colorLib.builder import buildCOLR, buildCPAL
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.areaPen import AreaPen
from fontTools.pens.boundsPen import ControlBoundsPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.varLib.builder import buildVarData, buildVarRegionList, buildVarStore
from fontTools.varLib.instancer import instantiateVariableFont

HERE = Path(__file__).resolve().parent
AXIS = ("wght", 100, 400, 900, "Weight")
SQUARE = ((100, 0), (100, 700), (900, 700), (900, 0))
#: The weights of the oracle, on either side of the default.
ORACLE_WEIGHTS = (100, 700, 900)
#: From the default up to the top of the axis.
UP = {"wght": (0.0, 1.0, 1.0)}


def _polygon(points: tuple[tuple[int, int], ...]) -> object:
    pen = TTGlyphPen(None)
    pen.moveTo(points[0])
    for point in points[1:]:
        pen.lineTo(point)
    pen.closePath()
    return pen.glyph()


def _empty() -> object:
    return TTGlyphPen(None).glyph()


def _builder(
    family: str,
    glyphs: dict[str, object],
    cmap: dict[int, str],
    advances: dict[str, int] | None = None,
) -> FontBuilder:
    """Return a TrueType face of ``glyphs`` with the weight axis, and no variations yet."""
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(list(glyphs))
    builder.setupCharacterMap(cmap)
    builder.setupGlyf(glyphs)
    advances = advances or {}
    glyf = builder.font["glyf"]
    metrics = {}
    for name, glyph in glyphs.items():
        glyph.recalcBounds(glyf)
        metrics[name] = (advances.get(name, 1000), getattr(glyph, "xMin", 0))
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable(
        {"familyName": family, "styleName": "Regular", "psName": family.replace(" ", "") + "-Regular"}
    )
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, sCapHeight=700, sxHeight=500, usWeightClass=400)
    builder.setupPost()
    return builder


def _mvar(deltas: dict[str, int]) -> object:
    """Return an ``MVAR`` table that adds each delta at the top of the axis."""
    tags = sorted(deltas)
    regions = buildVarRegionList([UP], ["wght"])
    data = buildVarData([0], [[deltas[tag]] for tag in tags], optimize=False)
    table = otTables.MVAR()
    table.Version = 0x00010000
    table.Reserved = 0
    table.VarStore = buildVarStore(regions, [data])
    records = []
    for index, tag in enumerate(tags):
        record = otTables.MetricsValueRecord()
        record.ValueTag = tag
        record.VarIdx = index
        records.append(record)
    table.ValueRecord = records
    table.ValueRecordCount = len(records)
    table.ValueRecordSize = 8
    mvar = newTable("MVAR")
    mvar.table = table
    return mvar


def latin() -> TTFont:
    glyphs = {".notdef": _empty(), "A": _polygon(SQUARE), "V": _polygon(SQUARE), "space": _empty()}
    builder = _builder("Variable Latin", glyphs, {0x41: "A", 0x56: "V", 0x20: "space"})
    builder.setupFvar([AXIS], [{"stylename": "Bold", "location": {"wght": 700}}])
    # The right edge and the advance of A.
    builder.setupGvar(
        {
            "A": [
                TupleVariation(
                    UP,
                    [(0, 0), (0, 0), (100, 0), (100, 0), (0, 0), (100, 0), (0, 0), (0, 0)],
                )
            ]
        }
    )
    builder.addOpenTypeFeatures("feature kern { pos A V (wght=100:-50 wght=400:-100 wght=900:-300); } kern;")
    builder.font["MVAR"] = _mvar({"cpht": 100, "hasc": 50})
    return builder.font


def composites() -> TTFont:
    points = tuple(
        (round(500 + 400 * math.cos(math.tau * at / 500)), round(400 + 400 * math.sin(math.tau * at / 500)))
        for at in range(500)
    )
    glyphs = {".notdef": _empty(), "base": _polygon(points)}
    for index in range(16):
        pen = TTGlyphPen(glyphs)
        for copy in range(32):
            pen.addComponent("base", (1, 0, 0, 1, copy, index))
        glyphs[f"composite{index}"] = pen.glyph()
    builder = _builder("Variable Composites", glyphs, {0x41: "composite0"})
    builder.setupFvar([AXIS], [])
    return builder.font


def overflow() -> TTFont:
    glyphs = {".notdef": _empty(), "A": _polygon(((0, 0), (0, 700), (32000, 700), (32000, 0)))}
    builder = _builder("Variable Overflow", glyphs, {0x41: "A"})
    builder.setupFvar([AXIS], [])
    builder.setupGvar(
        {"A": [TupleVariation(UP, [(0, 0), (0, 0), (2000, 0), (2000, 0), (0, 0), (0, 0), (0, 0), (0, 0)])]}
    )
    return builder.font


def colour() -> TTFont:
    glyphs = {".notdef": _empty(), "A": _empty(), "B": _polygon(SQUARE), "layer": _polygon(SQUARE)}
    builder = _builder("Variable Colour", glyphs, {0x41: "A", 0x42: "B"})
    builder.setupFvar([AXIS], [])
    builder.font["COLR"] = buildCOLR({"A": [("layer", 0)]}, glyphMap=builder.font.getReverseGlyphMap())
    builder.font["CPAL"] = buildCPAL([[(0.0, 0.0, 1.0, 1.0)]])
    return builder.font


def oracle() -> None:
    source = HERE / "NotoSansArabic-Variable-subset.ttf"
    lines = ["# weight glyph advance x_min y_min x_max y_max area"]
    for weight in ORACLE_WEIGHTS:
        buffer = io.BytesIO()
        instantiateVariableFont(TTFont(source), {"wght": weight}).save(buffer)
        buffer.seek(0)
        instance = TTFont(buffer)
        glyphs = instance.getGlyphSet()
        for index, name in enumerate(instance.getGlyphOrder()):
            box = ControlBoundsPen(glyphs)
            area = AreaPen(glyphs)
            glyphs[name].draw(box)
            glyphs[name].draw(area)
            x_min, y_min, x_max, y_max = box.bounds or (0, 0, 0, 0)
            advance = instance["hmtx"][name][0]
            lines.append(f"{weight} {index} {advance} {x_min} {y_min} {x_max} {y_max} {abs(area.value):.1f}")
    (HERE / "NotoSansArabic-Variable-subset-instances.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def main() -> None:
    for name, build in (
        ("variable-latin", latin),
        ("variable-composites", composites),
        ("variable-overflow", overflow),
        ("variable-colour", colour),
    ):
        build().save(HERE / f"{name}.ttf")
    oracle()


if __name__ == "__main__":
    main()
