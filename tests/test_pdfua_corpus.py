"""The generated PDF/UA corpus of tests/assets/pdfua, which veraPDF validates in CI (tools/verapdf.py)."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

import pyropdf

PDFUA = Path(__file__).resolve().parent / "assets" / "pdfua"
GENERATOR = runpy.run_path(str(PDFUA / "generate.py"))
FLAVOURS: dict[str, tuple[str, str, str]] = GENERATOR["FLAVOURS"]


@pytest.mark.parametrize("name", sorted(FLAVOURS))
def test_each_fixture_is_what_the_generator_writes(name: str) -> None:
    version, claim, _ = FLAVOURS[name]
    assert (PDFUA / name).read_bytes() == GENERATOR["write"](version, claim), "run tests/assets/pdfua/generate.py"


@pytest.mark.parametrize("name", sorted(FLAVOURS))
@pytest.mark.spec("pdfua", "preserve")
def test_each_fixture_claims_its_part_and_passes_the_structural_check(name: str) -> None:
    doc = pyropdf.open(PDFUA / name)
    assert doc.profiles.pdfua == f"PDF/UA-{FLAVOURS[name][2].removeprefix('ua')}"
    assert doc.profiles.pdfa == ("PDF/A-4" if name == "pdfua-2-pdfa-4.pdf" else None)
    assert doc.catalog["MarkInfo"]["Marked"] is True
    assert doc.check().defects == ()
