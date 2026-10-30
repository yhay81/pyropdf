# ruff: noqa: INP001, S603, S607 - a script that runs Ghostscript, which the README requires.
"""Score each engine's text, reading order, word boxes, and tables against labeled pages.

The labels in ``quality/`` are the ground truth, not either engine. Run the
collectors in each engine's environment, then score both results::

    python quality.py collect pymupdf /tmp/mupdf.json
    python quality.py collect pymupdf-sort /tmp/mupdf-sort.json
    python quality.py collect pyropdf /tmp/pyropdf.json
    python quality.py score results/quality COMMIT /tmp/mupdf.json /tmp/mupdf-sort.json /tmp/pyropdf.json

``pymupdf-sort`` reads with ``get_text(sort=True)``, which orders blocks by
position instead of the content stream: an option, where the default is an
API choice rather than an engine error.

``python quality.py ink`` measures each labeled word's ink box again with
Ghostscript, inside the seed box that the label gives, and rewrites the
labels; ``python quality.py crops DIR`` writes each labeled word's crop, for
checking a label by eye.
"""

from __future__ import annotations

import itertools
import json
import platform
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LABELS = sorted((HERE / "quality").glob("*.json"))
CORPUS = ROOT / "tests" / "assets" / "real_world"
#: The resolution of the Ghostscript renderings that ink boxes come from.
INK_DPI = 288
#: The gray level below which a pixel is ink.
INK_LEVEL = 160
#: The share of a seed's row or column above which dark pixels form a rule, not a glyph.
RULE = 0.9
#: The largest gap, in points, between the rows of one line's ink, as between an i and its dot.
GAP = 1.5
#: The share of a word's ink box that an engine's box covers for the word to count as placed.
COVERED = 0.9


def normalize(text: str) -> str:
    """Return ``text`` in NFKC without whitespace, which the engines place differently between CJK and digits."""
    return "".join(unicodedata.normalize("NFKC", text).split())


def collect(engine: str, out: str) -> None:
    """Write the text, words, and tables of every labeled page as ``engine`` reads them."""
    result: dict[str, Any] = {"engine": engine, "python": platform.python_version(), "pages": {}}
    for path in LABELS:
        label = json.loads(path.read_text())
        source = str(CORPUS / label["document"])
        index = label["page"] - 1
        if engine in ("pymupdf", "pymupdf-sort"):
            import pymupdf  # noqa: PLC0415

            sort = engine == "pymupdf-sort"
            result["version"] = f"PyMuPDF {pymupdf.__version__}" + (" sort=True" if sort else "")
            with pymupdf.open(source) as doc:
                page = doc[index]
                text = page.get_text(sort=sort)
                words = [[w[4], *w[:4]] for w in page.get_text("words", sort=sort)]
                tables = [table.extract() for table in page.find_tables().tables] if "table" in label else []
        else:
            import pyropdf  # noqa: PLC0415

            result["version"] = f"pyropdf {pyropdf.__version__}"
            page = pyropdf.open(source).pages[index]
            text = page.text()
            words = [[w.text, *w.bbox] for w in page.words()]
            tables = [[list(row) for row in table.rows] for table in page.tables()] if "table" in label else []
        result["pages"][path.stem] = {"text": text, "words": words, "tables": tables}
    Path(out).write_text(json.dumps(result, ensure_ascii=False))


def _render(document: str, page: int) -> tuple[int, int, bytes]:
    """Return the width, height, and 8-bit gray samples of a page that Ghostscript renders."""
    data = subprocess.run(
        [
            "gs",
            "-q",
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=pgmraw",
            f"-r{INK_DPI}",
            f"-dFirstPage={page}",
            f"-dLastPage={page}",
            "-sOutputFile=-",
            str(CORPUS / document),
        ],
        check=True,
        capture_output=True,
    ).stdout
    # The header is "P5", a comment line, the width and height, and the largest sample.
    magic, rest = data.split(b"\n", 1)
    while rest.startswith(b"#"):
        rest = rest.split(b"\n", 1)[1]
    size, _, samples = rest.split(b"\n", 2)
    width, height = size.split()
    if magic != b"P5":
        msg = f"Ghostscript wrote no PGM: {magic[:20]!r}"
        raise ValueError(msg)
    return int(width), int(height), samples


def _ink(raster: tuple[int, int, bytes], seed: list[float]) -> list[float] | None:
    """Return the box, in points from the top left, of the ink inside ``seed``."""
    width, height, samples = raster
    scale = INK_DPI / 72
    x0, y0 = max(0, int(seed[0] * scale)), max(0, int(seed[1] * scale))
    x1, y1 = min(width, int(seed[2] * scale) + 1), min(height, int(seed[3] * scale) + 1)
    dark = [[samples[y * width + x] < INK_LEVEL for x in range(x0, x1)] for y in range(y0, y1)]
    # Rules of a table or a form field that cross the seed are not the word's ink.
    rows = [sum(row) < RULE * len(row) for row in dark]
    columns = [sum(row[x] for row in dark) < RULE * len(dark) for x in range(x1 - x0)]
    inked = [any(on and columns[x] for x, on in enumerate(row)) and rows[y] for y, row in enumerate(dark)]
    # Keep the rows around the seed's middle: the line above or below can reach into it.
    middle = len(inked) // 2
    gap = int(GAP * scale)
    top = bottom = next((y for y in sorted(range(len(inked)), key=lambda y: abs(y - middle)) if inked[y]), None)
    if top is None:
        return None
    while any(inked[max(top - gap, 0) : top]):
        top = max(y for y in range(max(top - gap, 0), top) if inked[y])
    while any(inked[bottom + 1 : bottom + 1 + gap]):
        bottom = min(y for y in range(bottom + 1, min(bottom + 1 + gap, len(inked))) if inked[y])
    xs, ys = [], []
    for y in range(top, bottom + 1):
        for x, on in enumerate(dark[y]):
            if on and columns[x] and rows[y]:
                xs.append(x0 + x)
                ys.append(y0 + y)
    return [
        round(min(xs) / scale, 2),
        round(min(ys) / scale, 2),
        round((max(xs) + 1) / scale, 2),
        round((max(ys) + 1) / scale, 2),
    ]


def ink() -> None:
    """Measure each labeled word's ink box inside its seed box and write it into the label."""
    for path in LABELS:
        label = json.loads(path.read_text())
        raster = _render(label["document"], label["page"])
        for word in label["words"]:
            word["ink"] = _ink(raster, word["seed"])
        path.write_text(json.dumps(label, ensure_ascii=False, indent=1) + "\n")


def crops(directory: str) -> None:
    """Write a PGM crop of each labeled word's ink box, with a margin, for checking the labels by eye."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    scale = INK_DPI / 72
    for path in LABELS:
        label = json.loads(path.read_text())
        width, _, samples = _render(label["document"], label["page"])
        for number, word in enumerate(label["words"]):
            x0, y0, x1, y1 = (int(v * scale) for v in word["ink"])
            x0, y0, x1, y1 = x0 - 8, y0 - 8, x1 + 8, y1 + 8
            rows = b"".join(samples[y * width + x0 : y * width + x1] for y in range(y0, y1))
            (out / f"{path.stem}-{number}.pgm").write_bytes(b"P5 %d %d 255\n" % (x1 - x0, y1 - y0) + rows)


def _anchors(label: dict[str, Any], text: str) -> dict[str, Any]:
    """Score the anchors: how many the text holds, and how many neighbors keep their labeled order."""
    flat = normalize(text)
    positions = [flat.find(normalize(anchor)) for anchor in label["anchors"]]
    found = [(anchor, position) for anchor, position in zip(label["anchors"], positions, strict=True) if position >= 0]
    pairs = list(itertools.pairwise(found))
    inversions = [(first, second) for (first, a), (second, b) in pairs if a > b]
    missing = [anchor for anchor, position in zip(label["anchors"], positions, strict=True) if position < 0]
    return {
        "anchors": len(positions),
        "found": len(found),
        "in_order": len(pairs) - len(inversions),
        "pairs": len(pairs),
        "missing": missing,
        "inversions": inversions,
    }


def _area(box: list[float]) -> float:
    return max(box[2] - box[0], 0) * max(box[3] - box[1], 0)


def _overlap(a: list[float], b: list[float]) -> list[float]:
    return [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]


def _words(label: dict[str, Any], words: list[list[Any]]) -> dict[str, Any]:
    """Score the labeled words: whether a word of the same text covers its ink, and the IoU of the best."""
    placed, ious, failures = 0, [], []
    for word in label["words"]:
        best_cover, best_iou = 0.0, 0.0
        for text, *box in words:
            if normalize(text) != normalize(word["text"]):
                continue
            inter = _area(_overlap(box, word["ink"]))
            cover = inter / _area(word["ink"])
            iou = inter / (_area(box) + _area(word["ink"]) - inter)
            best_cover, best_iou = max((best_cover, best_iou), (cover, iou))
        if best_cover >= COVERED:
            placed += 1
        else:
            failures.append(word["text"])
        ious.append(best_iou)
    return {
        "words": len(label["words"]),
        "placed": placed,
        "mean_iou": sum(ious) / len(ious) if ious else None,
        "failures": failures,
    }


def _table(label: dict[str, Any], tables: list[list[list[Any]]]) -> dict[str, Any]:
    """Score the labeled rows: the cells of the row whose first cell names it, in the table that holds most rows."""
    expected = label["table"]
    best: dict[str, Any] = {
        "columns": None,
        "rows": len(expected["rows"]),
        "rows_found": 0,
        "cells": 0,
        "cells_right": 0,
    }
    for table in tables:
        rows = {normalize(str(row[0] or "")): row for row in table if row}
        found = [key for key in expected["rows"] if normalize(key) in rows]
        right = sum(
            1
            for key in found
            for want, got in zip(expected["rows"][key], rows[normalize(key)], strict=False)
            if normalize(want) == normalize(str(got or ""))
        )
        if (len(found), right) > (best["rows_found"], best["cells_right"]):
            columns = max(len(row) for row in table)
            best = {**best, "columns": columns, "rows_found": len(found), "cells_right": right}
    best["cells"] = sum(len(cells) for cells in expected["rows"].values())
    best["expected_columns"] = expected["columns"]
    return best


def score(stem: str, commit: str, *names: str) -> None:
    """Score the engines' results against the labels and write ``stem``.json and ``stem``.md."""
    results = [json.loads(Path(name).read_text()) for name in names]
    pages: dict[str, Any] = {}
    for path in LABELS:
        label = json.loads(path.read_text())
        pages[path.stem] = {"checked": label["checked"]}
        for result in results:
            page = result["pages"][path.stem]
            entry = {"anchors": _anchors(label, page["text"]), "words": _words(label, page["words"])}
            if "table" in label:
                entry["table"] = _table(label, page["tables"])
            pages[path.stem][result["version"]] = entry
    versions = [result["version"] for result in results]
    Path(f"{stem}.json").write_text(
        json.dumps({"commit": commit, "engines": versions, "pages": pages}, ensure_ascii=False, indent=1) + "\n"
    )
    Path(f"{stem}.md").write_text("\n".join(_markdown(pages, versions, commit)) + "\n")


def _row(name: str, version: str, entry: dict[str, Any]) -> str:
    a, w, t = entry["anchors"], entry["words"], entry.get("table")
    iou = "" if w["mean_iou"] is None else f"{w['mean_iou']:.2f}"
    table = "| |"
    if t:
        table = (
            f"{t['rows_found']}/{t['rows']} | {t['cells_right']}/{t['cells']} | {t['columns']}/{t['expected_columns']}"
        )
    return (
        f"| {name} | {version} | {a['found']}/{a['anchors']} | {a['in_order']}/{a['pairs']} "
        f"| {w['placed']}/{w['words']} | {iou} | {table} |"
    )


def _markdown(pages: dict[str, Any], versions: list[str], commit: str) -> list[str]:
    """Return the lines of the scores' table and of each failure."""
    lines = [
        f"Labels: `quality/*.json`; pyropdf commit {commit}.",
        "",
        (
            "| Page | Engine | Anchors found | Anchor pairs in order | Words placed | Mean word IoU "
            "| Table rows | Cells right | Columns |"
        ),
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    lines += [_row(name, version, page[version]) for name, page in pages.items() for version in versions]
    lines += ["", "## Failures", ""]
    for name, page in pages.items():
        for version in versions:
            a, w = page[version]["anchors"], page[version]["words"]
            lines += [f"- {name}, {version}: anchor not found: `{anchor}`" for anchor in a["missing"]]
            lines += [f"- {name}, {version}: `{second}` comes before `{first}`" for first, second in a["inversions"]]
            lines += [f"- {name}, {version}: no word `{word}` covers its ink" for word in w["failures"]]
    return lines


def main() -> None:
    """Run the command that the arguments name."""
    command, *rest = sys.argv[1:]
    {"collect": collect, "ink": ink, "crops": crops, "score": score}[command](*rest)


if __name__ == "__main__":
    main()
