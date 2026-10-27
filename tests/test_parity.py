"""Differential parity with frozen pylopdf (DESIGN.md §12 and §13).

``evidence/phase1-parity`` holds the harness and its report. The report test
runs everywhere. The subset test redumps two corpus documents with both
engines, and runs only when ``PYROPDF_PARITY_PYLOPDF`` names the Python of an
environment with frozen pylopdf, such as the ``target/parity/pylopdf-env``
that ``run.py`` builds.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from corpus import ASSETS, NAMES

if TYPE_CHECKING:
    from types import ModuleType

HARNESS = Path(__file__).resolve().parents[1] / "evidence" / "phase1-parity"
SUBSET = ("pdf20-simple.pdf", "pdfium-type3.pdf")


def _compare() -> ModuleType:
    spec = importlib.util.spec_from_file_location("parity_compare", HARNESS / "compare.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_report_covers_the_corpus_and_explains_every_difference() -> None:
    results = json.loads((HARNESS / "results.json").read_text())
    report = (HARNESS / "report.md").read_text()

    assert sorted(results) == sorted(name.removesuffix(".pdf") for name in NAMES)
    for result in results.values():
        assert all(set(page) == set(_compare().OUTPUTS) for page in result["pages"])
    assert report.split("## Unexplained differences\n\n", 1)[1] == "None.\n"


@pytest.mark.skipif("PYROPDF_PARITY_PYLOPDF" not in os.environ, reason="needs an environment with frozen pylopdf")
def test_a_subset_redumps_with_only_the_documented_differences(tmp_path: Path) -> None:
    files = [str(ASSETS / name) for name in SUBSET]
    for engine, python in (("pylopdf", os.environ["PYROPDF_PARITY_PYLOPDF"]), ("pyropdf", sys.executable)):
        subprocess.run([python, HARNESS / "dump.py", engine, tmp_path / engine, *files], check=True)  # noqa: S603
    compare = _compare()
    expected = json.loads((HARNESS / "results.json").read_text())

    for name in SUBSET:
        stem = name.removesuffix(".pdf")
        first, second = (
            json.loads((tmp_path / engine / f"{stem}.json").read_text()) for engine in ("pylopdf", "pyropdf")
        )
        for index, (a, b) in enumerate(zip(first["pages"], second["pages"], strict=True)):
            rasters = (tmp_path / "pylopdf" / f"{stem}-{index}.rgb", tmp_path / "pyropdf" / f"{stem}-{index}.rgb")
            for output in compare.OUTPUTS:
                kind, _ = compare._compare(output, a[output], b[output], rasters)  # noqa: SLF001
                assert kind == expected[stem]["pages"][index][output][0], (stem, index, output)
