"""Keep intentional PDF/UA failures narrow while adding save-harness operations."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from test_preservation import EDITS

import pyropdf

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("verapdf", ROOT / "tools" / "verapdf.py")
assert SPEC is not None
assert SPEC.loader is not None
VERAPDF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERAPDF)
SOURCE = ROOT / "tests" / "assets" / "pdfua" / "pdfua-1.pdf"


@pytest.mark.parametrize("edit", sorted(VERAPDF.UNTAGGED_RULES))
@pytest.mark.parametrize("incremental", [False, True])
def test_intentional_untagged_annotations_require_a_change_report(edit: str, *, incremental: bool) -> None:
    doc = pyropdf.open(SOURCE)
    EDITS[edit].prepare(doc)
    EDITS[edit].apply(doc)
    VERAPDF.check_claims(doc, "ua1", edit, incremental=incremental)

    # Reopening loses the edit history. The exemption must not silently
    # accept a harness that prepared the annotation outside the report.
    reopened = pyropdf.open(doc.to_bytes(incremental=incremental))
    with pytest.raises(ValueError, match="must report only the untagged annotations"):
        VERAPDF.check_claims(reopened, "ua1", edit, incremental=incremental)


@pytest.mark.parametrize(
    ("accepted", "change", "fails"),
    [
        (False, None, False),
        (False, "extra", True),
        (False, "missing", True),
        (True, None, True),
    ],
)
@pytest.mark.parametrize("edit", ["add_annotation", "add_field"])
def test_expected_annotation_failure_rejects_different_rules(
    monkeypatch: pytest.MonkeyPatch, edit: str, *, accepted: bool, change: str | None, fails: bool
) -> None:
    path = Path(f"pdfua-1-{edit}-full.pdf")
    rules = set(VERAPDF.UNTAGGED_RULES[edit])
    if change == "extra":
        rules.add("7.21.5-1")
    elif change == "missing":
        rules.remove("7.18.3-1")
    monkeypatch.setattr(VERAPDF, "validate", lambda *_args: {path.name: accepted})
    report = f"FAIL {path} ua1\n" + "".join(f"  FAIL {rule}\n" for rule in sorted(rules))
    monkeypatch.setattr(VERAPDF, "details", lambda *_args: report)

    assert bool(VERAPDF.run(Path("unused.jar"), {path: "ua1"})) == fails


def test_saved_inventory_keeps_annotation_preparation_in_the_change_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(VERAPDF, "FIXTURES", {SOURCE: "ua1"})
    monkeypatch.setattr(VERAPDF, "EDITS", {edit: EDITS[edit] for edit in VERAPDF.UNTAGGED_RULES})

    files = VERAPDF.saves(tmp_path)

    assert len(files) == 1 + len(VERAPDF.UNTAGGED_RULES) * 2
    for path in files.keys() - {SOURCE}:
        edit = next(edit for edit in VERAPDF.UNTAGGED_RULES if edit in path.name)
        EDITS[edit].check(pyropdf.open(path))


@pytest.mark.spec("{profile}", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize(
    "profile",
    [
        "pdfa-1a",
        "pdfa-1b",
        "pdfa-2a",
        "pdfa-2b",
        "pdfa-2u",
        "pdfa-3a",
        "pdfa-3b",
        "pdfa-3u",
        "pdfa-4",
        "pdfa-4e",
        "pdfa-4f",
        "pdfua-1",
        "pdfua-2",
    ],
)
def test_profile_structure_and_page_rotation(profile: str) -> None:
    family, level = profile.split("-")
    path = ROOT / "tests" / "assets" / family / f"{profile}.pdf"
    original = pyropdf.open(path)
    claim = f"PDF/{'A' if family == 'pdfa' else 'UA'}-{level.upper()}"
    profiles = original.profiles
    assert (profiles.pdfa if family == "pdfa" else profiles.pdfua) == claim
    assert original.check().defects == ()

    content = list(original.pages[0].operations)
    for incremental in (False, True):
        doc = pyropdf.open(path)
        doc.pages[0].rotation = 90
        saved = pyropdf.open(doc.to_bytes(incremental=incremental))
        assert saved.profiles == profiles
        assert saved.pages[0].rotation == 90
        assert list(saved.pages[0].operations) == content
        assert saved.check().defects == ()

    # A missing required entry must be reported even with the original XMP claim.
    key = "OutputIntents" if family == "pdfa" else "StructTreeRoot"
    del original.catalog[key]
    assert (original.profiles.pdfa if family == "pdfa" else original.profiles.pdfua) == claim
    assert any(defect.kind == "profile" and defect.path == key for defect in original.check().defects)


def test_the_profile_map_names_only_the_matching_parameter_case() -> None:
    spec_map = json.loads((ROOT / "docs" / "spec-map.json").read_text(encoding="utf-8"))
    features = {feature["feature"]: feature for feature in spec_map["features"]}
    for fixture in VERAPDF.FIXTURES:
        if fixture.stem == VERAPDF.DUAL:
            continue
        row = features[fixture.stem]
        for dimension in ("read", "interpret", "edit", "preserve"):
            assert any(test.endswith(f"[{fixture.stem}]") for test in row[dimension]["tests"])
            assert all(
                test.endswith(f"[{fixture.stem}]")
                for test in row[dimension]["tests"]
                if "::test_profile_structure_and_page_rotation" in test
            )


@pytest.mark.parametrize("incremental", [False, True])
def test_dual_claim_screen_has_distinct_accessibility_and_archival_rules(*, incremental: bool) -> None:
    mode = "incremental" if incremental else "full"
    name = f"pdfua-2-pdfa-4-add_screen-{mode}.pdf"
    assert VERAPDF.EXPECTED[f"ua2 {name}"] == ("#848", {"8.9.2.4.12-1", "8.9.3.3-1"})
    assert VERAPDF.EXPECTED[f"4 {name}"] == ("#848", {"6.3.1-1", "6.3.2-1", "6.6.1-1", "6.9-2", "6.9-3", "6.9-4"})


@pytest.mark.parametrize("incremental", [False, True])
def test_arlington_uses_the_promoted_screen_edition(tmp_path: Path, *, incremental: bool) -> None:
    original = ROOT / "tests/assets/pdfa/pdfa-1b.pdf"
    assert VERAPDF.arlington(original, "1b") == "arlington1.4"
    with pyropdf.open(original) as doc:
        EDITS["add_screen"].apply(doc)
        output = tmp_path / "screen.pdf"
        doc.save(output, incremental=incremental)
    assert VERAPDF.arlington(output, "1b") == "arlington1.5"
