"""Time full or quick pytest from the repository root and compare their JUnit reports."""

# ruff: noqa: INP001 - standalone evidence script
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from xml.etree import ElementTree as ET

GRIDS = (
    "tests.test_consistency::test_an_edit_and_a_save_add_no_structural_defect",
    "tests.test_consistency::test_an_edit_and_a_save_add_no_qpdf_finding",
    "tests.test_consistency::test_pikepdf_reads_every_saved_document",
    "tests.test_preservation::test_an_edit_and_a_save_keep_every_untouched_object",
)
REPRESENTATIVE = {"pdf20-simple.pdf", "pdfium-links-highlights-annots.pdf"}


def read_results(report: Path) -> dict[str, str]:
    """Read every reported ID, including collection skips, and retain its outcome."""
    results = {}
    for case in ET.parse(report).iter("testcase"):  # noqa: S314 - locally generated JUnit report
        node = f"{case.get('classname', '')}::{case.attrib['name']}"
        if node in results:
            msg = f"Duplicate JUnit case {node} in {report}"
            raise ValueError(msg)
        results[node] = next((tag for tag in ("error", "failure", "skipped") if case.find(tag) is not None), "passed")
    if not results:
        msg = f"Empty JUnit report {report}"
        raise ValueError(msg)
    return results


def measure(mode: str, report: Path) -> int:
    """Measure one fresh pytest process, retaining the actual report and exit status."""
    if shutil.which("qpdf") is None:
        sys.stderr.write("qpdf is required on PATH.\n")
        return 1
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["PYROPDF_REQUIRE_QPDF"] = "1"
    report.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=report.parent) as scratch:
        fresh = Path(scratch) / "pytest.xml"
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={fresh}"]
        if mode == "quick":
            command.append("--quick-corpus")
        start = perf_counter()
        status = subprocess.run(command, env=env, stdout=sys.stderr, check=False).returncode  # noqa: S603
        elapsed = round(perf_counter() - start, 3)
        if not fresh.is_file():
            sys.stderr.write("pytest did not produce a JUnit report.\n")
            return status or 1
        fresh.replace(report)
    results = read_results(report)
    summary = {
        "mode": mode,
        "elapsed_seconds": elapsed,
        "reported": len(results),
        "outcomes": Counter(results.values()),
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return status


def compare(full_report: Path, quick_report: Path) -> None:
    """Check each recorded grid against the selection rule and compare all other IDs and outcomes."""
    full, quick = read_results(full_report), read_results(quick_report)
    expected = full.copy()
    counts = {}
    for family in GRIDS:
        cases = [node for node in full if node.startswith(f"{family}[")]
        kept = 0
        for node in cases:
            source, separator, edit_mode = node.removeprefix(f"{family}[").removesuffix("]").partition(".pdf-")
            edit, mode = edit_mode.rsplit("-", 1)
            if not separator or mode not in {"full", "incremental"} or full[node] != "passed":
                msg = f"Unexpected exhaustive grid case or outcome: {node} ({full[node]})"
                raise ValueError(msg)
            if f"{source}.pdf" in REPRESENTATIVE or edit == "rotate":
                kept += 1
            else:
                del expected[node]
        if not 0 < kept < len(cases):
            msg = f"Missing representative or exhaustive cases for {family}"
            raise ValueError(msg)
        counts[family] = {"full": len(cases), "quick": kept}
    if expected != quick:
        missing = sorted(expected.keys() - quick.keys())
        extra = sorted(quick.keys() - expected.keys())
        changed = sorted(node for node in expected.keys() & quick.keys() if expected[node] != quick[node])
        msg = f"JUnit mismatch: missing={missing[:3]}, extra={extra[:3]}, changed={changed[:3]}"
        raise ValueError(msg)
    unrelated = len(full) - sum(pair["full"] for pair in counts.values())
    sys.stdout.write(json.dumps({"grids": counts, "other_cases": unrelated}, indent=2) + "\n")


def main() -> int:
    """Run a measurement or verify two existing reports."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("full", "quick"):
        commands.add_parser(mode).add_argument("report", type=Path)
    comparison = commands.add_parser("compare")
    comparison.add_argument("full_report", type=Path)
    comparison.add_argument("quick_report", type=Path)
    args = parser.parse_args()
    if args.mode == "compare":
        compare(args.full_report, args.quick_report)
        return 0
    return measure(args.mode, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
