# ruff: noqa: INP001, S101 - fixture for the release workflow.
"""A missing or unsuccessful operation must stay in the same report as a slower success."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import release
from release_report import quality_rows, timing_rows


class ComparisonFixture(unittest.TestCase):
    """Run without either engine installed."""

    def test_full_workload_denominators_and_quality_losses(self) -> None:
        """Retain slower, failed, refused and unmeasured cells and exclude them only from paired sums."""
        corpus = ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]
        success = {"status": "ok", "samples_ms": [4, 1]}
        quality = {
            "status": "ok",
            "anchors": {"found": 1, "anchors": 2, "in_order": 0, "pairs": 0},
            "words": {"placed": 0, "words": 1, "mean_iou": 0.2},
        }
        raw = {
            "corpus": corpus,
            "labels": ["a-p1", "b-p2"],
            "settings": {"operations": ["render_72"]},
            "engines": {
                "pymupdf": {"timing": {p: {"render_72": success} for p in corpus}, "quality": {}},
                "pyropdf": {
                    "timing": {
                        "a.pdf": {"render_72": {"status": "ok", "samples_ms": [9, 5]}},
                        "b.pdf": {"render_72": {"status": "failed", "reason": "RuntimeError"}},
                        "c.pdf": {"render_72": {"status": "refused", "reason": "LimitError"}},
                    },
                    "quality": {"a-p1": quality, "b-p2": {"status": "failed", "reason": "collector exit"}},
                },
            },
        }
        rows = timing_rows(raw)
        assert "| render_72 | 1/4 | 1.000 | 5.000 | 5.00 |" in rows
        assert "| a.pdf | render_72 | 1.000 | 5.000 | 5.00 |" in rows
        assert "| b.pdf | render_72 | 1.000 | failed RuntimeError | — |" in rows
        assert "| c.pdf | render_72 | 1.000 | refused LimitError | — |" in rows
        assert "| d.pdf | render_72 | 1.000 | unmeasured missing observation | — |" in rows
        scores = quality_rows(raw)
        assert "| a-p1 | pyropdf | 1/2 | 0/0 | 0/1 | 0.200 | — |" in scores
        assert "| b-p2 | pyropdf | failed: collector exit | — | — | — | — |" in scores
        assert "| **Total (1/2 pages)** | pyropdf | 1/2 | 0/0 | 0/1 | 0.200 | 0/0 |" in scores

    def test_dead_worker_preserves_prior_results_and_all_planned_operations(self) -> None:
        """The interrupted operation fails, and later operations remain explicitly unmeasured."""
        output = '["open", {"status": "ok", "samples_ms": [5]}]\n["text", {'
        with patch.object(release, "observe", return_value=(output, "worker interrupted")):
            measured = release.timing(Path("python"), "pyropdf", Path("fixture.pdf"))
        assert tuple(measured) == release.bench.OPERATIONS
        assert measured["open"] == {"status": "ok", "samples_ms": [5]}
        assert measured["text"] == {"status": "failed", "reason": "worker interrupted"}
        assert measured["save"]["status"] == "unmeasured"
