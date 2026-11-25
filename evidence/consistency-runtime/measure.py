"""Run from the baseline or candidate checkout, with that checkout's Python and qpdf on PATH."""

# ruff: noqa: INP001 - standalone evidence script
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter
from contextlib import redirect_stdout
from functools import wraps

import pytest

READERS = {
    "defects": "test_an_edit_and_a_save_add_no_structural_defect",
    "qpdf_findings": "test_an_edit_and_a_save_add_no_qpdf_finding",
    "pikepdf_problems": "test_pikepdf_reads_every_saved_document",
}


class Measurement:
    """Record successful test IDs and optionally count calls without retaining document data."""

    def __init__(self, *, counts: bool) -> None:
        """Collect node IDs for both runs and call counts only when requested."""
        self.counts = counts
        self.calls: Counter[str] = Counter()
        self.collected: set[str] = set()
        self.passed: set[str] = set()
        self.skipped = False

    def count_calls(self, owner: object, name: str) -> None:
        """Wrap the actual operation with a counter for this process."""
        original = getattr(owner, name)

        @wraps(original)
        def counted(*args: object, **kwargs: object) -> object:
            self.calls[name] += 1
            return original(*args, **kwargs)

        setattr(owner, name, counted)

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Bind counters to the harness loaded from the selected checkout."""
        self.collected = {item.nodeid for item in session.items}
        if self.counts and session.items:
            module = session.items[0].module
            self.count_calls(type(next(iter(module.EDITS.values()))), "open")
            for name in READERS:
                self.count_calls(module, name)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Count only tests whose call actually passed."""
        if report.skipped:
            self.skipped = True
        if report.when == "call" and report.passed and not hasattr(report, "wasxfail"):
            self.passed.add(report.nodeid)

    def inspections(self) -> dict[str, dict[str, int]]:
        """Each reader test inspects its saved document once. Other calls inspect original files."""
        families = Counter(nodeid.split("::")[-1].partition("[")[0] for nodeid in self.passed)
        return {
            reader: {
                "total": self.calls[reader],
                "saved": families[family],
                "original": self.calls[reader] - families[family],
            }
            for reader, family in READERS.items()
        }


def main() -> int:
    """Report a measured pytest run as JSON on stdout, keeping pytest's progress on stderr."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", action="store_true", help="count reader inspections and edit preparations")
    args = parser.parse_args()
    if shutil.which("qpdf") is None:
        parser.error("qpdf must be on PATH to measure the complete harness")
    os.environ.pop("PYTEST_ADDOPTS", None)
    os.environ["PYROPDF_REQUIRE_QPDF"] = "1"
    measurement = Measurement(counts=args.counts)
    start = time.perf_counter()
    with redirect_stdout(sys.stderr):
        status = pytest.main(["-q", "tests/test_consistency.py"], plugins=[measurement])
    elapsed = time.perf_counter() - start
    if status or measurement.skipped or not measurement.passed or measurement.passed != measurement.collected:
        sys.stderr.write("Every collected consistency case must pass.\n")
        return status or 1
    nodeids = "\n".join(sorted(measurement.passed)).encode()
    result = {"passed": len(measurement.passed), "nodeids_sha256": hashlib.sha256(nodeids).hexdigest()}
    if args.counts:
        result.update(save_preparations=measurement.calls["open"], reader_inspections=measurement.inspections())
    else:
        result["elapsed_seconds"] = round(elapsed, 3)
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
