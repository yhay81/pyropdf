"""The consistency harness: an edit and a save leave a document no less valid.

Each edit of the preservation harness's ``EDITS`` is applied to each corpus
document, which is saved in full and incrementally. The structural check of
the saved document must find no defect that the original lacks, and
``qpdf --check`` must report no error or warning that it lacks. pikepdf,
which reads through its own parser, must count the same pages and report
no syntax problem that the original lacks. CI sets
``PYROPDF_REQUIRE_QPDF=1``, so that a missing qpdf fails there instead of
skipping.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pikepdf
import pytest
from corpus import ASSETS, NAMES
from test_preservation import EDITS

import pyropdf

if TYPE_CHECKING:
    from pathlib import Path

QPDF = shutil.which("qpdf")


def defects(data: bytes) -> set[tuple[int | None, str, str, str]]:
    """Return the structural defects of ``data``."""
    report = pyropdf.open(data).check()
    assert not report.truncated
    return {(d.xref, d.path, d.clause, d.kind) for d in report.defects}


def qpdf_findings(path: Path) -> set[str]:
    """Return the lines of ``qpdf --check`` that report an error or a warning."""
    assert QPDF is not None
    # qpdf exits 2 on errors and 3 on warnings.
    result = subprocess.run([QPDF, "--check", str(path)], capture_output=True, text=True, check=False)  # noqa: S603 - a fixed command on test data.
    assert result.returncode in {0, 2, 3}, result.stderr
    lines = (result.stdout + result.stderr).splitlines()
    # A finding names its object and offset, which a full save moves, before
    # its message.
    findings = {line.rsplit(": ", 1)[-1] for line in lines if "WARNING" in line or "ERROR" in line}
    if result.returncode == 2:
        findings.add("qpdf exits with errors")
    return findings


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("edit", sorted(EDITS))
@pytest.mark.parametrize("name", NAMES)
def test_an_edit_and_a_save_add_no_structural_defect(name: str, edit: str, *, incremental: bool) -> None:
    source = (ASSETS / name).read_bytes()
    doc = pyropdf.open(source)
    EDITS[edit](doc)
    assert defects(doc.to_bytes(incremental=incremental)) - defects(source) == set()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("edit", sorted(EDITS))
@pytest.mark.parametrize("name", NAMES)
def test_an_edit_and_a_save_add_no_qpdf_finding(name: str, edit: str, *, incremental: bool, tmp_path: Path) -> None:
    if QPDF is None:
        if os.environ.get("PYROPDF_REQUIRE_QPDF") == "1":
            pytest.fail("PYROPDF_REQUIRE_QPDF=1, but qpdf is not on PATH")
        pytest.skip("qpdf is not on PATH")
    doc = pyropdf.open(ASSETS / name)
    EDITS[edit](doc)
    saved = tmp_path / "saved.pdf"
    saved.write_bytes(doc.to_bytes(incremental=incremental))
    assert qpdf_findings(saved) - qpdf_findings(ASSETS / name) == set()


def pikepdf_problems(source: Path | io.BytesIO) -> tuple[int, set[str]]:
    """Return the page count that pikepdf reads and the syntax problems that it reports."""
    with pikepdf.open(source) as pdf:
        # A problem names its object and offset, which a full save moves,
        # before its message.
        return len(pdf.pages), {problem.rsplit(": ", 1)[-1] for problem in pdf.check_pdf_syntax()}


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("edit", sorted(EDITS))
@pytest.mark.parametrize("name", NAMES)
def test_pikepdf_reads_every_saved_document(name: str, edit: str, *, incremental: bool) -> None:
    doc = pyropdf.open(ASSETS / name)
    EDITS[edit](doc)
    pages, problems = pikepdf_problems(io.BytesIO(doc.to_bytes(incremental=incremental)))
    assert pages == len(doc.pages)
    assert problems - pikepdf_problems(ASSETS / name)[1] == set()
