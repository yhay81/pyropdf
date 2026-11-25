"""The short test run keeps each coverage axis and leaves other tests selected."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from corpus import NAMES
from test_preservation import EDITS

TESTS = Path(__file__).resolve().parent
MATRICES = (
    "tests/test_preservation.py::test_an_edit_and_a_save_keep_every_untouched_object",
    "tests/test_consistency.py::test_an_edit_and_a_save_add_no_structural_defect",
    "tests/test_consistency.py::test_an_edit_and_a_save_add_no_qpdf_finding",
    "tests/test_consistency.py::test_pikepdf_reads_every_saved_document",
)


def test_quick_corpus_keeps_both_save_modes_on_each_selected_pair() -> None:
    control = "tests/test_preservation.py::test_each_edit_rejects_a_no_op[add_annotation]"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--quick-corpus",
        "--collect-only",
        "-q",
        "--color=no",
        *MATRICES,
        control,
    ]
    result = subprocess.run(  # noqa: S603 - collect fixed repository tests with this interpreter.
        command, cwd=TESTS.parent, env={**os.environ, "PYTEST_ADDOPTS": ""}, capture_output=True, text=True, check=True
    )
    collected = {line for line in result.stdout.splitlines() if line.startswith("tests/")}

    all_edit_sources = {"pdf20-simple.pdf", "pdfium-links-highlights-annots.pdf"}
    assert all_edit_sources < set(NAMES)
    assert "rotate" in EDITS
    expected = {
        f"{test}[{source}-{edit}-{mode}]"
        for test in MATRICES
        for source in NAMES
        for edit in (EDITS if source in all_edit_sources else ("rotate",))
        for mode in ("full", "incremental")
    }
    assert collected == expected | {control}
