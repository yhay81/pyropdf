"""page.text(), page.words(), page.tables(), and page.ocr() against labeled ground-truth pages.

The labels in ``evidence/pymupdf-comparison/quality/`` come from the pages
as Ghostscript renders them, not from either engine: anchors in reading
order, the ink boxes of chosen words, and the cells of chosen table rows.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

import pyropdf

ROOT = Path(__file__).resolve().parents[1]
LABELS = sorted((ROOT / "evidence" / "pymupdf-comparison" / "quality").glob("*.json"))
NATIVE_LABELS = [
    path for path in LABELS if json.loads(path.read_text(encoding="utf-8")).get("mode", "native") == "native"
]
OCR_LABELS = [path for path in LABELS if json.loads(path.read_text(encoding="utf-8")).get("mode") == "ocr"]
TABLE_LABELS = [path for path in NATIVE_LABELS if "table" in json.loads(path.read_text(encoding="utf-8"))]
#: The share of a word's ink box that its box covers.
COVERED = 0.9
#: Pages whose reading-order anchors pyropdf does not meet yet, with the issue that tracks each.
KNOWN: dict[str, str] = {}
#: Pages whose labeled table rows pyropdf does not meet yet, with the issue that tracks each.
KNOWN_TABLES: dict[str, str] = {}
REDISTRIBUTABLE = re.compile(
    r"public[ -]domain|CC BY 4\.0|Government Standard Terms of Use 2\.0|Public Data License 1\.0|LPPL|BSD 3-Clause",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())


def _label(path: Path) -> tuple[dict[str, Any], pyropdf.Page]:
    label = json.loads(path.read_text(encoding="utf-8"))
    page = pyropdf.open(ROOT / "tests" / "assets" / "real_world" / label["document"]).pages[label["page"] - 1]
    return label, page


@pytest.mark.parametrize("path", NATIVE_LABELS, ids=[path.stem for path in NATIVE_LABELS])
def test_anchors_appear_in_reading_order(path: Path, request: pytest.FixtureRequest) -> None:
    if path.stem in KNOWN:
        request.applymarker(pytest.mark.xfail(reason=KNOWN[path.stem], strict=True))
    label, page = _label(path)
    text = _normalize(page.text(unmapped="replacement"))
    positions = [text.find(_normalize(anchor)) for anchor in label["anchors"]]
    assert [anchor for anchor, at in zip(label["anchors"], positions, strict=True) if at < 0] == []
    assert positions == sorted(positions)


@pytest.mark.parametrize("path", NATIVE_LABELS, ids=[path.stem for path in NATIVE_LABELS])
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


@pytest.mark.parametrize("path", TABLE_LABELS, ids=[path.stem for path in TABLE_LABELS])
def test_labeled_table_rows(path: Path, request: pytest.FixtureRequest) -> None:
    if path.stem in KNOWN_TABLES:
        request.applymarker(pytest.mark.xfail(reason=KNOWN_TABLES[path.stem], strict=True))
    label, page = _label(path)
    expected_cols = label["table"]["columns"]
    expected_rows: dict[str, list[str]] = label["table"]["rows"]
    tables = page.tables()
    assert tables
    matching = [
        table
        for table in tables
        if max(len(row) for row in table.rows) == expected_cols
        and all(
            _normalize(key) in {_normalize(row[0]) for row in table.rows if row and row[0]} for key in expected_rows
        )
    ]
    assert matching, f"no {expected_cols}-column table contained expected rows {list(expected_rows)}"
    table = matching[0]
    rows = {_normalize(row[0]): [cell or "" for cell in row] for row in table.rows if row and row[0]}
    for key, cells in expected_rows.items():
        got = rows[_normalize(key)]
        assert [_normalize(cell) for cell in got] == [_normalize(cell) for cell in cells]


@pytest.mark.parametrize("path", OCR_LABELS, ids=[path.stem for path in OCR_LABELS])
def test_ocr_anchors_words_and_negative_controls(path: Path) -> None:
    label, page = _label(path)
    ocr_words = page.ocr()
    if "max_spurious_words" in label:
        assert len(ocr_words) <= label["max_spurious_words"]
    if label["anchors"]:
        text = _normalize("\n".join(word.text for word in ocr_words))
        positions = [text.find(_normalize(anchor)) for anchor in label["anchors"]]
        assert [anchor for anchor, at in zip(label["anchors"], positions, strict=True) if at < 0] == []
        assert positions == sorted(positions)
    for word in label["words"]:
        x0, y0, x1, y1 = word["ink"]
        best = 0.0
        for candidate in ocr_words:
            if _normalize(candidate.text) != _normalize(word["text"]):
                continue
            box = candidate.bbox
            overlap = max(min(box.x1, x1) - max(box.x0, x0), 0) * max(min(box.y1, y1) - max(box.y0, y0), 0)
            best = max(best, overlap / ((x1 - x0) * (y1 - y0)))
        assert best >= COVERED, word["text"]


def test_every_quality_label_document_has_license_record() -> None:
    readme = (ROOT / "tests" / "assets" / "real_world" / "README.md").read_text(encoding="utf-8")
    table_rows = [line for line in readme.splitlines() if line.startswith("| `")]
    licenses: dict[str, str] = {}
    for row in table_rows:
        parts = [cell.strip() for cell in row.strip("|").split("|")]
        licenses[parts[0].strip("`")] = parts[2]
    for path in LABELS:
        label = json.loads(path.read_text(encoding="utf-8"))
        doc_rel = label["document"]
        assert (ROOT / "tests" / "assets" / "real_world" / doc_rel).is_file(), doc_rel
        assert doc_rel in licenses, f"{doc_rel} missing from tests/assets/real_world/README.md"
        assert REDISTRIBUTABLE.search(licenses[doc_rel]), (doc_rel, licenses[doc_rel])


def test_compare_pymupdf4llm_scoring_without_agpl_dependencies() -> None:
    script = ROOT / "evidence" / "pymupdf-comparison" / "compare_pymupdf4llm.py"
    spec = importlib.util.spec_from_file_location("compare_pymupdf4llm", script)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    labels = {
        "sample-p1": {
            "document": "sample.pdf",
            "page": 1,
            "checked": False,
            "anchors": ["Title", "RowA"],
            "table": {"columns": 2, "rows": {"RowA": ["RowA", "100"]}},
        }
    }
    obs = [
        {
            "version": "pyropdf 0.4.1",
            "pages": {
                "sample-p1": {
                    "markdown": "# Title\n\n| Key | Val |\n| --- | --- |\n| RowA | 100 |\n",
                    "chunks": ["# Title\nRowA 100"],
                    "markdown_ms": 1.2,
                    "chunks_ms": 1.5,
                }
            },
        }
    ]
    payload, md = mod.build_report("deadbeef", obs, labels)
    scored = payload["pages"]["sample-p1"]["pyropdf 0.4.1"]
    assert scored["anchors"]["found"] == 2
    assert scored["anchors"]["in_order"] == 1
    assert scored["table"]["cells_right"] == 2
    assert scored["headings"] == 1
    assert "| pyropdf 0.4.1 | 2/2 | 1/1 | 2/2 | 2/2 |" in md
