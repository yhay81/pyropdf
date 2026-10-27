# ruff: noqa: INP001 - a script, not a package.
"""Dump the reading outputs of pylopdf or pyropdf for each page of some PDFs.

Run it with the Python of either engine:

    python dump.py ENGINE OUT FILE...

ENGINE is ``pylopdf`` or ``pyropdf``. For each FILE it writes
``OUT/<name>.json`` and, per page, ``OUT/<name>-<page>.rgb``: the samples of
the page rendered at 72 dpi as opaque RGB. The JSON holds, per page, the text;
the words, with and without exact boxes; the layout's dictionary, with
characters and styles; the search boxes of the page's first 20 distinct
words; the tables of both strategies; and the Markdown. It also holds the
Markdown of the whole document. An output that raises
holds ``{"error": "<type>: <message>"}`` instead. The script uses only the API
that both engines share, so the same code reads both.
"""

from __future__ import annotations

import importlib
import json
import sys
import warnings
from pathlib import Path
from typing import Any

#: The decimals that numbers keep, so that a dump does not record float noise.
DECIMALS = 3
#: The distinct words of a page that the dump searches for.
SEARCHED_WORDS = 20


def _round(value: Any) -> Any:  # noqa: ANN401 - a dump holds any JSON value.
    """Round every float in a JSON-like value."""
    if isinstance(value, float):
        return round(value, DECIMALS) + 0.0
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(item) for item in value]
    return value


def _guard(read: Any) -> Any:  # noqa: ANN401
    """Return the result of ``read()``, or the error it raised."""
    try:
        return _round(read())
    except Exception as error:  # noqa: BLE001 - the dump records any failure.
        return {"error": f"{type(error).__name__}: {error}"}


def _tables(page: Any, strategy: str) -> Any:  # noqa: ANN401
    return [
        {
            "bbox": tuple(table.bbox),
            "rows": table.rows,
            "cells": [[cell and tuple(cell) for cell in row] for row in table.cells],
        }
        for table in page.tables(strategy=strategy)
    ]


def _search(page: Any) -> list[Any]:  # noqa: ANN401
    """Return the boxes of each of the page's first 20 distinct words."""
    texts = list(dict.fromkeys(word.text for word in page.words()))[:SEARCHED_WORDS]
    return [(text, [tuple(rect) for rect in page.search(text, max_hits=None)]) for text in texts]


def _page(page: Any, raster: Path) -> dict[str, Any]:  # noqa: ANN401
    """Return the outputs of one page, and write its raster."""

    def render() -> dict[str, int]:
        pixmap = page.pixmap(dpi=72)
        raster.write_bytes(bytes(pixmap))
        return {"width": pixmap.width, "height": pixmap.height, "stride": pixmap.stride}

    return {
        "text": _guard(page.text),
        "words": _guard(lambda: [(word.text, tuple(word.bbox)) for word in page.words()]),
        "words_exact": _guard(lambda: [(word.text, tuple(word.bbox)) for word in page.words(exact_bboxes=True)]),
        "layout": _guard(lambda: page.layout(chars=True, styles=True).to_dict()),
        "search": _guard(lambda: _search(page)),
        "tables_lines": _guard(lambda: _tables(page, "lines")),
        "tables_text": _guard(lambda: _tables(page, "text")),
        "markdown": _guard(page.markdown),
        "render": _guard(render),
    }


def main() -> None:
    """Dump the files that the arguments name."""
    engine_name, out, *files = sys.argv[1:]
    engine = importlib.import_module(engine_name)
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings.simplefilter("ignore")
    for file in map(Path, files):
        document = engine.open(file.read_bytes())
        pages = [_page(page, out_dir / f"{file.stem}-{index}.rgb") for index, page in enumerate(document.pages)]
        dump = {"pages": pages, "markdown": _guard(document.markdown)}
        (out_dir / f"{file.stem}.json").write_text(json.dumps(dump, ensure_ascii=False))


if __name__ == "__main__":
    main()
