# ruff: noqa: INP001 - a script, not a package.
"""Compare the dumps of pylopdf and pyropdf and write the parity report.

    python compare.py PYLOPDF_DUMP PYROPDF_DUMP

It writes ``report.md`` and ``results.json`` beside this script. Each page's
output is ``equal``, ``close`` (rendering within the tolerance below), or a
difference of a kind that ``causes.toml`` explains. A difference that no
cause matches is listed as unexplained.
"""

from __future__ import annotations

import difflib
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUTPUTS = ("text", "words", "words_exact", "layout", "search", "tables_lines", "tables_text", "markdown", "render")
#: A rendered page is close when at most this fraction of its pixels differ by more than ``CHANNEL_TOLERANCE``.
PIXEL_TOLERANCE = 0.005
CHANNEL_TOLERANCE = 16
#: The largest coordinate difference, in points, that still counts as the same box.
BOX_TOLERANCE = 0.01


def _ratio(a: list[str], b: list[str]) -> float:
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


def _max_delta(a: Any, b: Any) -> float | None:  # noqa: ANN401
    """Return the largest difference between the numbers of two values of one shape, else ``None``."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b)
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        deltas = [_max_delta(x, y) for x, y in zip(a, b, strict=True)]
        return None if None in deltas else max(deltas, default=0.0)
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        return _max_delta([a[key] for key in sorted(a)], [b[key] for key in sorted(b)])
    return 0.0 if a == b else None


def _text_kind(a: str, b: str) -> tuple[str, dict[str, Any]]:
    if a.split() == b.split():
        return "whitespace", {}
    return "text", {"line_ratio": _ratio(a.splitlines(), b.splitlines())}


def _structured(a: Any, b: Any, texts: Any) -> tuple[str, dict[str, Any]]:  # noqa: ANN401
    """Classify two structured outputs: equal texts with moved numbers are ``boxes``, otherwise ``text``."""
    delta = _max_delta(a, b)
    if delta is not None:
        return ("equal", {}) if delta <= BOX_TOLERANCE else ("boxes", {"max_delta": round(delta, 3)})
    first, second = texts(a), texts(b)
    if first == second:
        return "structure", {}
    return "text", {"count": [len(first), len(second)], "ratio": _ratio(first, second)}


def _span_texts(layout: dict[str, Any]) -> list[str]:
    return [span["text"] for block in layout["blocks"] for line in block.get("lines", ()) for span in line["spans"]]


def _render(a: dict[str, int], b: dict[str, int], first: Path, second: Path) -> tuple[str, dict[str, Any]]:
    if (a["width"], a["height"]) != (b["width"], b["height"]):
        return "size", {"sizes": [[a["width"], a["height"]], [b["width"], b["height"]]]}
    x, y = first.read_bytes(), second.read_bytes()
    for metadata, samples in ((a, x), (b, y)):
        if metadata["width"] < 1 or metadata["height"] < 1 or metadata["stride"] < metadata["width"] * 3:
            msg = "invalid RGB raster dimensions or stride"
            raise ValueError(msg)
        if len(samples) != metadata["stride"] * metadata["height"]:
            msg = "raster byte count does not match its dimensions and stride"
            raise ValueError(msg)
    if x == y:
        return "equal", {}
    width, height = a["width"], a["height"]
    differing = total = 0
    for row in range(height):
        xs = x[row * a["stride"] : row * a["stride"] + width * 3]
        ys = y[row * b["stride"] : row * b["stride"] + width * 3]
        for pixel in range(width):
            channels = range(pixel * 3, pixel * 3 + 3)
            worst = max(abs(xs[i] - ys[i]) for i in channels)
            total += worst
            differing += worst > CHANNEL_TOLERANCE
    fraction = differing / (width * height)
    detail = {"differing": round(fraction, 5), "mean": round(total / (width * height), 3)}
    return ("close" if fraction <= PIXEL_TOLERANCE else "pixels"), detail


def _compare(output: str, a: Any, b: Any, rasters: tuple[Path, Path]) -> tuple[str, dict[str, Any]]:  # noqa: ANN401, PLR0911 - one return per output
    errors = [isinstance(value, dict) and "error" in value for value in (a, b)]
    if any(errors):
        return "error", {
            "pylopdf": a.get("error") if errors[0] else None,
            "pyropdf": b.get("error") if errors[1] else None,
        }
    if output == "render":
        return _render(a, b, *rasters)
    if a == b:
        return "equal", {}
    if output in ("text", "markdown"):
        return _text_kind(a, b)
    if output in ("words", "words_exact"):
        return _structured(a, b, lambda words: [word[0] for word in words])
    if output == "search":
        return _structured(a, b, lambda hits: [(text, len(rects)) for text, rects in hits])
    if output == "layout":
        return _structured(a, b, _span_texts)
    if output.startswith("tables"):
        return _structured(a, b, lambda tables: [table["rows"] for table in tables])
    return _render(a, b, *rasters)


def _causes() -> list[dict[str, Any]]:
    return tomllib.loads((HERE / "causes.toml").read_text())["cause"]


def _cause(causes: list[dict[str, Any]], document: str, output: str, kind: str, page: int | None) -> str | None:
    for cause in causes:
        if (
            output in cause["outputs"]
            and kind in cause["kinds"]
            and cause.get("documents", [document]).count(document)
            and (page is None or "pages" not in cause or page in cause["pages"])
        ):
            return cause["id"]
    return None


def main() -> None:
    """Compare the two dumps that the arguments name and write the report."""
    first_dir, second_dir = map(Path, sys.argv[1:])
    causes = _causes()
    results: dict[str, Any] = {}
    for path in sorted(first_dir.glob("*.json")):
        a, b = (json.loads((folder / path.name).read_text()) for folder in (first_dir, second_dir))
        pages = []
        for index, (x, y) in enumerate(zip(a["pages"], b["pages"], strict=True)):
            rasters = (first_dir / f"{path.stem}-{index}.rgb", second_dir / f"{path.stem}-{index}.rgb")
            pages.append({output: _compare(output, x[output], y[output], rasters) for output in OUTPUTS})
        document = _compare("markdown", a["markdown"], b["markdown"], (Path(), Path()))
        results[path.stem] = {"pages": pages, "markdown": document}
    (HERE / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
    (HERE / "report.md").write_text(_report(results, causes))


def _report(results: dict[str, Any], causes: list[dict[str, Any]]) -> str:
    lines = [
        "# Phase 1 parity report",
        "",
        "Generated by `compare.py` from the dumps of `run.py`; see README.md for the causes.",
        (
            f"Rendering is close when at most {PIXEL_TOLERANCE:.1%} of pixels differ by more than "
            f"{CHANNEL_TOLERANCE} in a channel, at 72 dpi."
        ),
        "",
        "## Pages by document and output",
        "",
        "Each cell counts the pages that are equal (or close, for rendering) out of all pages,",
        "then the causes of the rest.",
        "",
        "| Document | Pages | " + " | ".join(OUTPUTS) + " | document markdown |",
        "| --- | --- | " + " | ".join("---" for _ in OUTPUTS) + " | --- |",
    ]
    unexplained: list[str] = []
    close_rasters: list[str] = []
    for document, result in results.items():
        cells = []
        for output in OUTPUTS:
            same = 0
            found: dict[str, int] = {}
            for index, page in enumerate(result["pages"]):
                kind, detail = page[output]
                if kind in ("equal", "close"):
                    same += 1
                    if kind == "close":
                        cause = _cause(causes, document, output, kind, index)
                        note = f"- {document} page {index + 1}: {cause or 'unexplained'} {json.dumps(detail)}"
                        close_rasters.append(note)
                        if cause is None:
                            unexplained.append(note)
                    continue
                cause = _cause(causes, document, output, kind, index)
                if cause is None:
                    unexplained.append(
                        f"- {document} page {index + 1}, {output}: {kind} {json.dumps(detail, ensure_ascii=False)}"
                    )
                    cause = "unexplained"
                found[cause] = found.get(cause, 0) + 1
            notes = ", ".join(f"{cause} {count}" for cause, count in sorted(found.items()))
            cells.append(f"{same}/{len(result['pages'])}" + (f" ({notes})" if notes else ""))
        kind, detail = result["markdown"]
        whole = "equal"
        if kind != "equal":
            whole = _cause(causes, document, "document_markdown", kind, None) or "unexplained"
            if whole == "unexplained":
                unexplained.append(f"- {document}, document markdown: {kind} {json.dumps(detail, ensure_ascii=False)}")
        lines.append(f"| {document} | {len(result['pages'])} | " + " | ".join(cells) + f" | {whole} |")
    lines += ["", "## Rendering within tolerance", ""]
    lines += close_rasters or ["None."]
    lines += ["", "## Unexplained differences", ""]
    lines += unexplained or ["None."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
