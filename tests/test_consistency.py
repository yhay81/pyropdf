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
import re
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

# Group by source first so the three readers can reuse their original findings.
# Within each source pytest prepares one saved document for all three checks.
pytestmark = [
    pytest.mark.parametrize("source", [ASSETS / name for name in NAMES], ids=NAMES, scope="session"),
    pytest.mark.parametrize("edit", sorted(EDITS), scope="module"),
    pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"], scope="module"),
]

#: qpdf 11 reads every object of an object stream into its xref table, so an
#: object that a later revision frees gets an entry of no type, which
#: ``--check`` reports; qpdf 12 does not.
_FREED_IN_STREAM = re.compile(r"object (\d+)/\d+ has unexpected xref entry type")


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


@pytest.fixture(scope="module")
def saved_document(source: Path, edit: str, *, incremental: bool) -> tuple[bytes, int]:
    """Share the saved bytes and expected page count for just this edit and mode."""
    with EDITS[edit].open(source) as doc:
        EDITS[edit].apply(doc)
        return doc.to_bytes(incremental=incremental), len(doc.pages)


@pytest.fixture(scope="module")
def original_defects(source: Path) -> set[tuple[int | None, str, str, str]]:
    return defects(source.read_bytes())


@pytest.fixture(scope="module")
def original_qpdf_findings(source: Path) -> set[str] | None:
    return qpdf_findings(source) if QPDF is not None else None


def test_an_edit_and_a_save_add_no_structural_defect(
    saved_document: tuple[bytes, int],
    original_defects: set[tuple[int | None, str, str, str]],
) -> None:
    data, _ = saved_document
    assert defects(data) - original_defects == set()


@pytest.mark.skipif(QPDF is None and os.environ.get("PYROPDF_REQUIRE_QPDF") != "1", reason="qpdf is not on PATH")
def test_an_edit_and_a_save_add_no_qpdf_finding(
    saved_document: tuple[bytes, int],
    original_qpdf_findings: set[str] | None,
    tmp_path: Path,
) -> None:
    assert original_qpdf_findings is not None, "PYROPDF_REQUIRE_QPDF=1, but qpdf is not on PATH"
    data, _ = saved_document
    saved = tmp_path / "saved.pdf"
    saved.write_bytes(data)
    assert saved_qpdf_findings(saved) - original_qpdf_findings == set()


def saved_qpdf_findings(saved: Path) -> set[str]:
    """Ignore qpdf 11's stale object-stream entries only when the saved document actually freed them."""
    live = set(pyropdf.open(saved).objects)
    return {
        finding
        for finding in qpdf_findings(saved)
        if not ((match := _FREED_IN_STREAM.fullmatch(finding)) and int(match[1]) not in live)
    }


def pikepdf_problems(source: Path | io.BytesIO) -> tuple[int, set[str]]:
    """Return the page count that pikepdf reads and the syntax problems that it reports."""
    with pikepdf.open(source) as pdf:
        # A problem names its object and offset, which a full save moves,
        # before its message.
        return len(pdf.pages), {problem.rsplit(": ", 1)[-1] for problem in pdf.check_pdf_syntax()}


@pytest.fixture(scope="module")
def original_pikepdf_problems(source: Path) -> set[str]:
    return pikepdf_problems(source)[1]


def test_pikepdf_reads_every_saved_document(
    saved_document: tuple[bytes, int], original_pikepdf_problems: set[str]
) -> None:
    data, expected_pages = saved_document
    pages, problems = pikepdf_problems(io.BytesIO(data))
    assert pages == expected_pages
    assert problems - original_pikepdf_problems == set()
