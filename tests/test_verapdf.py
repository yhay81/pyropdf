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
def test_profile_structure_and_edit_reports(profile: str) -> None:
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

    for edit in ("action_triggers", "ocr_layer", "page_records"):
        with pyropdf.open(path) as edited:
            case = EDITS[edit]
            case.prepare(edited)
            case.apply(edited)
            for incremental in (False, True):
                VERAPDF.check_matrix_claims(edited, path, VERAPDF.FIXTURES[path], edit, incremental=incremental)

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
                if "::test_profile_structure_and_edit_reports" in test
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


def test_archival_media_keys_report_only_the_changed_owner(tmp_path: Path) -> None:
    files = VERAPDF.archival_media_controls(tmp_path)
    for mode in ("full", "incremental"):
        assert files[tmp_path / f"media-complete-{mode}.pdf"] == "4e"
        dual_prefix = f"{VERAPDF.DUAL}-media"
        assert files[tmp_path / f"{dual_prefix}-complete-{mode}.pdf"] == "ua2"
        for key, rules in VERAPDF.ARCHIVAL_MEDIA_RULES.items():
            path = tmp_path / f"media-{key}-{mode}.pdf"
            assert files[path] == "4e"
            assert VERAPDF.EXPECTED[f"4e {path.name}"][1] == rules
            dual = tmp_path / f"{dual_prefix}-{key}-{mode}.pdf"
            assert VERAPDF.dual_archival(files)[dual] == "4"
            assert VERAPDF.EXPECTED[f"4 {dual.name}"][1] == {"6.3.1-1", "6.9-3"} | rules
            assert VERAPDF.EXPECTED[f"ua2 {dual.name}"][1] == {"8.9.3.3-1", "8.9.2.4.19-2"}


def test_verapdf_rules_include_full_iso_table_names_and_model_deviations() -> None:
    report = (
        "FAIL output.pdf ua2\n"
        "  FAIL 8.2.5.2-1\n"
        "  FAIL Table 5. StructTreeRoot-Document-1\n"
        "DEVIATION other.pdf arlington2.0\n"
        "  DEVIATION VRI-OCSP-Array-9\n"
    )
    assert VERAPDF.failed_rules(report) == {"8.2.5.2-1", "Table 5. StructTreeRoot-Document-1", "VRI-OCSP-Array-9"}


@pytest.mark.parametrize(("level", "extensions"), [(1, "ISO_19005_3"), (2, "ISO_19005_3,ETSI_PAdES")])
def test_arlington_selects_the_declared_pades_extension(tmp_path: Path, level: int, extensions: str) -> None:
    source = ROOT / "tests/assets/pdfa/pdfa-3b.pdf"
    result = tmp_path / "document with spaces.pdf"
    with pyropdf.open(source) as doc:
        doc.catalog["Extensions"] = {"ETSI_PAdES": {"BaseVersion": pyropdf.Name("1.7"), "ExtensionLevel": level}}
        doc.save(result)
    assert VERAPDF.arlington(result, "3b") == f"arlington1.7+{extensions}"


def test_supplemental_pades_validation_keeps_native_and_archival_checks_distinct() -> None:
    dss = Path("pdfa-3b-security_store-full.pdf")
    signature = Path("pdfa-4-sign_revision-incremental.pdf")
    models = {
        dss: "arlington2.0+ISO_19005_3",
        signature: "arlington2.0",
        Path("pdfa-1b-sign_revision-full.pdf"): "arlington1.7+ETSI_PAdES",
        Path("pdfua-2-none-full.pdf"): "arlington2.0",
    }
    assert VERAPDF.pades_comparisons(models) == {
        dss: "arlington2.0+ISO_19005_3,ETSI_PAdES",
        signature: "arlington2.0+ETSI_PAdES",
    }


def test_dual_profile_loss_and_controls_report_the_responsible_objects(tmp_path: Path) -> None:
    files = VERAPDF.profile_saves(tmp_path)
    for edit, profiles in VERAPDF.FOCUSED.items():
        for mode in ("full", "incremental"):
            name = f"{VERAPDF.DUAL}-{edit}-{mode}.pdf"
            assert files[tmp_path / name] == "ua2"
            for profile in ("ua2", "4"):
                expected = VERAPDF.EXPECTED.get(f"{profile} {name}")
                assert (expected[1] if expected else set()) == profiles.get(profile, set())


@pytest.mark.parametrize(("edit", "apply"), [("add_attachment", False), ("none", True)])
def test_pre_save_comparison_rejects_both_missed_and_spurious_claims(edit: str, *, apply: bool) -> None:
    source = SOURCE.with_name(f"{VERAPDF.DUAL}.pdf")
    with pyropdf.open(source) as doc:
        if apply:
            EDITS["add_attachment"].apply(doc)
        with pytest.raises(ValueError, match="expected"):
            VERAPDF.check_matrix_claims(doc, source, "ua2", edit, incremental=True)


@pytest.mark.spec("{profile}", "interpret", "edit", "preserve")
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
def test_alternate_profile_representations(profile: str, tmp_path: Path) -> None:
    source = ROOT / "tests" / "assets" / profile.split("-", maxsplit=1)[0] / f"{profile}.pdf"
    files = VERAPDF.alternate_saves(source, VERAPDF.FIXTURES[source], tmp_path)
    assert files
    for case in VERAPDF.ALTERNATES["cases"](VERAPDF.FIXTURES[source], dual=False):
        assert tmp_path / f"{source.stem}-{case}-input.pdf" in files
        for mode in ("full", "incremental"):
            for operation in ("edit", "valid-control"):
                assert tmp_path / f"{source.stem}-{case}-{operation}-{mode}.pdf" in files


@pytest.mark.spec("pdfa", "interpret", "edit", "preserve")
@pytest.mark.spec("pdfua", "interpret", "edit", "preserve")
def test_dual_alternate_resources_report_only_the_claim_that_newly_fails(tmp_path: Path) -> None:
    source = ROOT / "tests/assets/pdfua/pdfua-2-pdfa-4.pdf"
    files = VERAPDF.alternate_saves(source, "ua2", tmp_path)
    assert set(VERAPDF.dual_archival(files)) == set(files)
    for mode in ("full", "incremental"):
        for case, profiles in {
            "page-intent": {"4"},
            "parent-tree-slot": {"ua2"},
            "nested-composite-font": {"4", "ua2"},
            "nested-default-rgb": set(),
        }.items():
            name = f"{source.stem}-{case}-edit-{mode}.pdf"
            assert {f for f in ("ua2", "4") if f"{f} {name}" in VERAPDF.EXPECTED} == profiles


@pytest.mark.parametrize("case", ["inherited-default-rgb", "parent-tree-slot", "named-properties"])
def test_unrelated_rotation_does_not_own_a_resource_failure(case: str) -> None:
    with pyropdf.open(ROOT / "tests/assets/pdfua/pdfua-2-pdfa-4.pdf") as source:
        data = VERAPDF.ALTERNATES["prepare"](source, case)
    with pyropdf.open(data) as doc:
        responsible = VERAPDF.ALTERNATES["edit"](doc, case)
        doc.pages[0].rotation = 90
        for incremental in (True, False):
            (change,) = doc.changes(incremental=incremental).claims
            assert change.objects == (responsible,)


def test_nested_form_reads_use_the_change_reports_operation_budget() -> None:
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-4.pdf") as source:
        data = VERAPDF.ALTERNATES["prepare"](source, "nested-rgb")
    with pyropdf.open(data) as prepared:
        outer = prepared.pages[0].obj["Resources"]["XObject"]["Outer"]
        inner = outer["Resources"]["XObject"]["Inner"]
        inner.write(inner.read() + b" " * 10_000)
        data = prepared.to_bytes()
    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_page_content_size=5_000)) as doc:
        del doc.catalog["OutputIntents"]
        with pytest.raises(pyropdf.LimitError) as failure:
            doc.changes(incremental=True)
        assert failure.value.code == "page_content_size"
        doc.history.undo()
        assert doc.changes(incremental=True).claims == ()


def test_unrelated_catalog_edit_does_not_decode_the_nested_archival_form() -> None:
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-4.pdf") as source:
        data = VERAPDF.ALTERNATES["prepare"](source, "nested-rgb")
    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_page_content_size=1)) as doc:
        doc.catalog["Unrelated"] = True
        assert doc.changes(incremental=True).claims == ()
