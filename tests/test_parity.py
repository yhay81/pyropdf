"""Differential parity with frozen pylopdf (DESIGN.md §12 and §13).

``evidence/phase1-parity`` holds the harness and its report. The report test
runs everywhere. The subset test redumps three corpus documents with both
engines, and runs only when ``PYROPDF_PARITY_PYLOPDF`` names the Python of an
environment with frozen pylopdf, such as the ``target/parity/pylopdf-env``
that ``run.py`` builds.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from corpus import ASSETS, NAMES

import pyropdf

if TYPE_CHECKING:
    from types import ModuleType

HARNESS = Path(__file__).resolve().parents[1] / "evidence" / "phase1-parity"
SUBSET = ("pdf20-simple.pdf", "pdfium-type3.pdf", "pdfium-links-highlights-annots.pdf")


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
    compare = _compare()
    metadata = json.loads((HARNESS / "metadata.json").read_text())
    assert metadata["pylopdf_commit"] == "ebb01d3c4c742046e8ce7fd72957cd15a2933321"
    assert set(metadata["corpus_sha256"]) == set(NAMES)
    for name in NAMES:
        assert metadata["corpus_sha256"][name] == hashlib.sha256((ASSETS / name).read_bytes()).hexdigest()
        result = results[Path(name).stem]
        with pyropdf.open(ASSETS / name) as document:
            assert len(result["pages"]) == len(document.pages)
        assert all(set(page) == set(compare.OUTPUTS) for page in result["pages"])
        assert "markdown" in result
    assert compare._report(results, compare._causes()) == report  # noqa: SLF001
    assert report.split("## Unexplained differences\n\n", 1)[1] == "None.\n"


@pytest.mark.skipif("PYROPDF_PARITY_PYLOPDF" not in os.environ, reason="needs an environment with frozen pylopdf")
def test_a_subset_redumps_with_only_the_documented_differences(tmp_path: Path) -> None:
    files = [str(ASSETS / name) for name in SUBSET]
    for engine, python in (("pylopdf", os.environ["PYROPDF_PARITY_PYLOPDF"]), ("pyropdf", sys.executable)):
        subprocess.run([python, HARNESS / "dump.py", engine, tmp_path / engine, *files], check=True)  # noqa: S603
    compare = _compare()
    # Fixing a documented difference to equality is an improvement.
    expected = json.loads((HARNESS / "results.json").read_text())

    for name in SUBSET:
        stem = name.removesuffix(".pdf")
        first, second = (
            json.loads((tmp_path / engine / f"{stem}.json").read_text()) for engine in ("pylopdf", "pyropdf")
        )
        assert len(first["pages"]) == len(expected[stem]["pages"])
        kind, _ = compare._compare("markdown", first["markdown"], second["markdown"], (Path(), Path()))  # noqa: SLF001
        assert kind in {"equal", expected[stem]["markdown"][0]}
        for index, (a, b) in enumerate(zip(first["pages"], second["pages"], strict=True)):
            rasters = (tmp_path / "pylopdf" / f"{stem}-{index}.rgb", tmp_path / "pyropdf" / f"{stem}-{index}.rgb")
            for output in compare.OUTPUTS:
                kind, _ = compare._compare(output, a[output], b[output], rasters)  # noqa: SLF001
                assert kind in {"equal", expected[stem]["pages"][index][output][0]}, (stem, index, output)


@pytest.mark.parametrize(("value", "expected"), [(255, "pixels"), (1, "close"), (0, "equal")])
def test_render_comparison_reads_pixels_with_identical_metadata(tmp_path: Path, value: int, expected: str) -> None:
    first, second = tmp_path / "first.rgb", tmp_path / "second.rgb"
    first.write_bytes(bytes([0, 0, 0]))
    second.write_bytes(bytes([value, value, value]))
    metadata = {"width": 1, "height": 1, "stride": 3}
    kind, _ = _compare()._compare("render", metadata, metadata, (first, second))  # noqa: SLF001
    assert kind == expected


def test_matching_failures_are_reported_as_errors() -> None:
    failure = {"error": "RuntimeError: no output"}
    kind, detail = _compare()._compare("text", failure, failure, (Path(), Path()))  # noqa: SLF001
    assert kind == "error"
    assert detail == {"pylopdf": failure["error"], "pyropdf": failure["error"]}


def test_render_comparison_refuses_truncated_equal_rasters(tmp_path: Path) -> None:
    first, second = tmp_path / "first.rgb", tmp_path / "second.rgb"
    first.write_bytes(b"xx")
    second.write_bytes(b"xx")
    metadata = {"width": 1, "height": 1, "stride": 3}
    with pytest.raises(ValueError, match="raster byte count"):
        _compare()._compare("render", metadata, metadata, (first, second))  # noqa: SLF001
