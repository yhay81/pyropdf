"""The generated PDF/A corpus of tests/assets/pdfa, which veraPDF validates in CI (tools/verapdf.py)."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

import pyropdf

PDFA = Path(__file__).resolve().parent / "assets" / "pdfa"
GENERATOR = runpy.run_path(str(PDFA / "generate.py"))
FLAVOURS: dict[str, tuple[str, str, str]] = GENERATOR["FLAVOURS"]


@pytest.mark.parametrize("name", sorted(FLAVOURS))
def test_each_fixture_is_what_the_generator_writes(name: str) -> None:
    version, claim, _ = FLAVOURS[name]
    assert (PDFA / name).read_bytes() == GENERATOR["write"](version, claim), "run tests/assets/pdfa/generate.py"


@pytest.mark.parametrize("name", sorted(FLAVOURS))
def test_each_fixture_claims_its_flavour_and_passes_the_structural_check(name: str) -> None:
    doc = pyropdf.open(PDFA / name)
    flavour = FLAVOURS[name][2].upper()
    assert doc.profiles.pdfa == f"PDF/A-{flavour}"
    assert doc.profiles.output_intents[0].subtype == "GTS_PDFA1"
    assert doc.profiles.output_intents[0].profile == 6
    assert doc.check().defects == ()


def test_the_icc_profile_declares_its_size_and_a_display_rgb_class() -> None:
    profile = GENERATOR["icc"]()
    assert int.from_bytes(profile[:4]) == len(profile)
    assert profile[12:24] == b"mntrRGB XYZ "
    assert profile[36:40] == b"acsp"
