"""Collect the test and validator outcomes of one CI checkout for a release claim."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
MAP = Path("docs/spec-map.json")
# These suites run against every corpus edit and save mode. The named reader
# tests actually invoke the corresponding tool and skip if it is unavailable.
SUITES = {
    "preservation": "tests/test_preservation.py::",
    "consistency": "tests/test_consistency.py::",
    "qpdf": "tests/test_consistency.py::test_an_edit_and_a_save_add_no_qpdf_finding[",
    "pikepdf": "tests/test_consistency.py::test_pikepdf_reads_every_saved_document[",
    "poppler": "tests/test_font_subset_poppler.py::",
    "pyhanko": "tests/test_verify.py::test_pyhanko_signatures_verify",
}
VALIDATORS = ("verapdf", "arlington", "pdfjs")


def digest(path: Path) -> str:
    """Identify the exact map used by the checkout."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_junit(report: Path, revision: str) -> dict[str, str]:
    """Return pytest node IDs, retaining failed teardowns and skipped parameters."""
    tree = ET.parse(report)  # noqa: S314 - pytest's local CI output
    suites = list(tree.iter("testsuite"))
    if not suites or any(suite.get("name") != f"pyropdf-{revision}" for suite in suites):
        msg = f"pytest did not run at the expected revision {revision}"
        raise ValueError(msg)
    results: dict[str, str] = {}
    for case in tree.iter("testcase"):
        module, name = case.attrib["classname"], case.attrib["name"]
        parts = module.split(".")
        at = next((i for i, part in enumerate(parts) if part.startswith("test_")), None)
        if at is None:
            # Collection-level skips/errors have no test node ID.
            continue
        file = "/".join(parts[: at + 1]) + ".py"
        node = "::".join([file, *parts[at + 1 :], name])
        if case.find("failure") is not None or case.find("error") is not None:
            results[node] = "failed"
        elif case.find("skipped") is not None:
            if results.get(node) != "failed":
                results[node] = "skipped"
        else:
            results.setdefault(node, "passed")
    return results


def passed(results: list[str]) -> bool:
    """All selected cases must exist and pass, including every selected parameter."""
    return bool(results) and all(result == "passed" for result in results)


def suite_outcomes(tests: dict[str, str]) -> dict[str, str]:
    """Resolve the harness and reader verdicts from their observed cases."""
    return {
        name: "passed"
        if passed([result for node, result in tests.items() if node.startswith(prefix)])
        else "incomplete"
        for name, prefix in SUITES.items()
    }


def test_outcomes(tests: dict[str, str]) -> dict[str, bool]:
    """Index whole parametrized tests and their individual cases."""
    by_test: dict[str, list[str]] = defaultdict(list)
    for node, result in tests.items():
        by_test[node.partition("[")[0]].append(result)
    return {node: result == "passed" for node, result in tests.items()} | {
        name: passed(results) for name, results in by_test.items()
    }


def main() -> None:
    """Write one release evidence file from the complete pytest run and CI steps."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    parser.add_argument("output", type=Path)
    outcomes = ("success", "failure", "skipped", "cancelled")
    parser.add_argument("--verapdf", required=True, choices=outcomes)
    parser.add_argument("--pdfjs", required=True, choices=outcomes)
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()  # noqa: S607
    try:
        tests = read_junit(args.junit, revision)
    except (ValueError, ET.ParseError) as error:
        parser.error(str(error))
    checks = suite_outcomes(tests)
    # One command validates both model families with two separately pinned jars.
    checks.update(
        verapdf="passed" if args.verapdf == "success" else args.verapdf,
        arlington="passed" if args.verapdf == "success" else args.verapdf,
        pdfjs="passed" if args.pdfjs == "success" else args.pdfjs,
    )
    evidence = {"revision": revision, "map_sha256": digest(ROOT / MAP), "tests": tests, "checks": checks}
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
