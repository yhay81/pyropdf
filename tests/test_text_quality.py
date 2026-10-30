"""page.text(), page.words(), and page.tables() against the labeled pages of the PyMuPDF quality comparison.

The labels in ``evidence/pymupdf-comparison/quality/`` come from the pages
as Ghostscript renders them, not from either engine: anchors in reading
order, the ink boxes of chosen words, and the cells of chosen table rows.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

import pyropdf

ROOT = Path(__file__).resolve().parents[1]
LABELS = sorted((ROOT / "evidence" / "pymupdf-comparison" / "quality").glob("*.json"))
#: The share of a word's ink box that its box covers.
COVERED = 0.9
#: Pages whose labels pyropdf does not meet yet, with the issue that tracks each.
KNOWN = {"patent-us223898-p2": "#460: headings above two columns are split into the columns"}


def _normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())


def _label(path: Path) -> tuple[dict[str, Any], pyropdf.Page]:
    label = json.loads(path.read_text())
    page = pyropdf.open(ROOT / "tests" / "assets" / "real_world" / label["document"]).pages[label["page"] - 1]
    return label, page


@pytest.mark.parametrize("path", LABELS, ids=[path.stem for path in LABELS])
def test_anchors_appear_in_reading_order(path: Path, request: pytest.FixtureRequest) -> None:
    if path.stem in KNOWN:
        request.applymarker(pytest.mark.xfail(reason=KNOWN[path.stem], strict=True))
    label, page = _label(path)
    text = _normalize(page.text(unmapped="replacement"))
    positions = [text.find(_normalize(anchor)) for anchor in label["anchors"]]
    assert [anchor for anchor, at in zip(label["anchors"], positions, strict=True) if at < 0] == []
    assert positions == sorted(positions)


@pytest.mark.parametrize("path", LABELS, ids=[path.stem for path in LABELS])
def test_labeled_words_cover_their_ink(path: Path) -> None:
    label, page = _label(path)
    words = page.words(unmapped="replacement")
    for word in label["words"]:
        x0, y0, x1, y1 = word["ink"]
        best = 0.0
        for candidate in words:
            if _normalize(candidate.text) != _normalize(word["text"]):
                continue
            box = candidate.bbox
            overlap = max(min(box.x1, x1) - max(box.x0, x0), 0) * max(min(box.y1, y1) - max(box.y0, y0), 0)
            best = max(best, overlap / ((x1 - x0) * (y1 - y0)))
        assert best >= COVERED, word["text"]


@pytest.mark.parametrize("path", [path for path in LABELS if "table" in json.loads(path.read_text())])
def test_labeled_table_rows(path: Path) -> None:
    label, page = _label(path)
    [table] = page.tables()
    assert max(len(row) for row in table.rows) == label["table"]["columns"]
    rows = {row[0]: [cell or "" for cell in row] for row in table.rows if row and row[0]}
    for key, cells in label["table"]["rows"].items():
        assert rows[key] == cells
