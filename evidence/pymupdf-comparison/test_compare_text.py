# ruff: noqa: D103, INP001, S101 - standalone evidence tests.
"""Validate the aggregation and input checks in the comparison evidence."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from compare_text import compare

if TYPE_CHECKING:
    from pathlib import Path


def _page(text: str, normalized: str, words: str, chars: int, count: int) -> dict[str, str | int]:
    return {"text": text, "normalized_text": normalized, "word_multiset": words, "characters": chars, "words": count}


def _dump(path: Path, engine: str, pages: list[dict], digest: str = "same") -> None:
    path.write_text(
        json.dumps({"engine": engine, "python": "test", "files": {"fixture.pdf": {"sha256": digest, "pages": pages}}}),
        encoding="utf-8",
    )


def test_comparison_counts_failures_and_unequal_page_counts(tmp_path: Path) -> None:
    left, right, out = (tmp_path / name for name in ("left.json", "right.json", "result.json"))
    _dump(left, "PyMuPDF", [_page("a", "same", "w", 4, 1), _page("b", "b", "x", 2, 1), {"error": "ValueError"}])
    _dump(right, "pyropdf", [_page("c", "same", "w", 3, 1), _page("b", "b", "y", 2, 2)])
    compare(str(left), str(right), str(out), "commit")
    result = json.loads(out.read_text(encoding="utf-8"))
    row = result["files"]["fixture.pdf"]
    assert result["pyropdf_main"] == "commit"
    assert (row["pymupdf_pages"], row["pyropdf_pages"], row["compared_pages"]) == (3, 2, 2)
    assert (row["exact_text_matches"], row["normalized_text_matches"], row["word_multiset_matches"]) == (1, 2, 1)
    assert (row["pymupdf_page_errors"], row["pyropdf_page_errors"]) == (1, 0)
    assert (row["pymupdf_characters"], row["pyropdf_characters"]) == (6, 5)


def test_comparison_refuses_different_input_bytes(tmp_path: Path) -> None:
    left, right, out = (tmp_path / name for name in ("left.json", "right.json", "result.json"))
    _dump(left, "PyMuPDF", [], digest="first")
    _dump(right, "pyropdf", [], digest="second")
    with pytest.raises(ValueError, match="different input bytes"):
        compare(str(left), str(right), str(out), "commit")
    assert not out.exists()
