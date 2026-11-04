# ruff: noqa: INP001, T201, D103, PLR2004, C901 - measurement scripts.
"""Count which COLR v1 features each base glyph of a face needs (issue #598).

Usage: uv run --with fonttools python colr_features.py FACE OUT.json

A glyph's class is the hardest thing it needs in a PDF writer: only solid
layers; axial or radial shadings; shadings whose stops have alpha, which need
soft masks; or ``PaintComposite``, which needs transparency groups and blend
modes. Shared ``PaintColrLayers`` are followed; the walk is bounded by depth.
"""

import collections
import json
import sys
from pathlib import Path

from fontTools.ttLib import TTFont


def main() -> None:
    font = TTFont(sys.argv[1], lazy=True)
    table = font["COLR"].table
    palette = font["CPAL"].palettes[0]
    records = {record.BaseGlyph: record for record in table.BaseGlyphList.BaseGlyphPaintRecord}
    layers = table.LayerList.Paint
    memo: dict[int, collections.Counter] = {}

    def alpha(stop: object) -> bool:
        return palette[stop.PaletteIndex].alpha < 255 or stop.Alpha < 1

    def walk(paint: object, depth: int) -> collections.Counter:
        found: collections.Counter = collections.Counter()
        kind = paint.getFormatName()
        found[kind] += 1
        if depth > 64:
            found["depth limit"] += 1
            return found
        if kind == "PaintColrLayers":
            for index in range(paint.FirstLayerIndex, paint.FirstLayerIndex + paint.NumLayers):
                if index not in memo:
                    memo[index] = walk(layers[index], depth + 1)
                found.update(memo[index])
        if kind == "PaintColrGlyph":
            found.update(walk(records[paint.Glyph].Paint, depth + 1))
        if kind in {"PaintLinearGradient", "PaintRadialGradient", "PaintSweepGradient"} and any(
            alpha(stop) for stop in paint.ColorLine.ColorStop
        ):
            found["alpha stops"] += 1
        if kind == "PaintSolid" and alpha(paint):
            found["alpha solid"] += 1
        for name in ("Paint", "SourcePaint", "BackdropPaint"):
            child = getattr(paint, name, None)
            if child is not None:
                found.update(walk(child, depth + 1))
        return found

    classes: collections.Counter = collections.Counter()
    totals: collections.Counter = collections.Counter()
    counts = []
    for record in records.values():
        found = walk(record.Paint, 0)
        totals.update(found)
        counts.append(found["PaintGlyph"])
        if found["PaintComposite"]:
            classes["composite"] += 1
        elif found["alpha stops"]:
            classes["shadings with alpha stops"] += 1
        elif found["PaintLinearGradient"] or found["PaintRadialGradient"] or found["PaintSweepGradient"]:
            classes["shadings, opaque"] += 1
        else:
            classes["solid layers"] += 1
    counts.sort()
    result = {
        "base_glyphs": len(records),
        "classes": classes.most_common(),
        "paint_formats": totals.most_common(),
        "paint_glyph_median": counts[len(counts) // 2],
        "paint_glyph_p95": counts[int(len(counts) * 0.95)],
        "paint_glyph_max": counts[-1],
    }
    Path(sys.argv[2]).write_text(json.dumps(result, indent=1) + "\n", "utf-8")
    print(result)


if __name__ == "__main__":
    main()
