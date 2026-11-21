"""Keep intentional PDF/UA failures narrow while adding save-harness operations."""

from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
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

    for edit in ("action_triggers", "edit_interaction", "ocr_layer", "page_records"):
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


@pytest.mark.parametrize("level", ["1b", "2b", "3b", "4", "4e", "4f"])
def test_archival_appearance_reports_only_the_annotation_with_an_alternate(level: str) -> None:
    path = ROOT / "tests/assets/pdfa" / f"pdfa-{level}.pdf"
    with pyropdf.open(path) as doc:
        note = doc.pages[0].annotations.add(pyropdf.Note((4, 8), contents="Review"))
        normal = note.appearance_stream()
        assert normal is not None
        note.set_appearance("normal", normal)
        assert doc.changes(incremental=True).claims == ()
        note.set_appearance("down", normal)
        for incremental in (False, True):
            (change,) = doc.changes(incremental=incremental).claims
            assert change.claim == f"PDF/A-{level.upper()}"
            assert change.objects == (note.xref,)
            assert change.reason == "PDF/A permits only the normal annotation appearance"
        del note.obj["AP"]["D"]
        assert doc.changes(incremental=True).claims == ()


@pytest.mark.parametrize("incremental", [False, True])
def test_a_normal_line_caption_breaks_archival_font_embedding_on_its_annotation(*, incremental: bool) -> None:
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-2b.pdf") as original:
        line = original.pages[0].annotations.add(pyropdf.LineAnnotation((16, 20), (80, 20), contents="Measured"))
        line.line_details = pyropdf.LineDetails(caption=False)
        baseline = original.to_bytes()
    with pyropdf.open(baseline) as doc:
        line = doc.pages[0].annotations[0]
        assert doc.changes(incremental=incremental).claims == ()
        line.line_details = pyropdf.LineDetails(caption=True)
        (change,) = doc.changes(incremental=incremental).claims
        assert change.claim == "PDF/A-2B"
        assert change.objects == (line.xref,)
        assert "font without an embedded program" in change.reason
        saved = doc.to_bytes(incremental=incremental)
    with pyropdf.open(saved) as reopened:
        reopened.pages[0].rotation = 90
        assert reopened.changes(incremental=incremental).claims == ()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"BT /F 10 Tf /Other 10 Tf ( ) Tj ET", {"Other"}),
        (b"BT /F 10 Tf ( ) Tj /Other 10 Tf ( ) Tj ET", {"F", "Other"}),
        (b"BT /F 10 Tf q /Other 10 Tf Q ( ) Tj ET", {"F"}),
    ],
)
def test_appearance_names_only_embedded_programs_actually_used(content: bytes, expected: set[str]) -> None:
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-2b.pdf") as source:
        data = VERAPDF.ALTERNATES["prepare"](source, "appearance-selected")
    with pyropdf.open(data) as doc:
        widget = doc.pages[0].obj["Annots"][0]
        del widget["Type"]  # An annotation's Type entry is optional.
        ink = widget["AP"]["N"]["Yes"]
        ink["Resources"]["Font"]["Other"] = VERAPDF.ALTERNATES["simple"](doc)
        ink.write(content)
        data = doc.to_bytes()
    with pyropdf.open(data) as doc:
        widget = doc.pages[0].obj["Annots"][0]
        fonts = widget["AP"]["N"]["Yes"]["Resources"]["Font"]
        responsible = {fonts[name]["FontDescriptor"].xref for name in expected}
        for name in ("F", "Other"):
            del fonts[name]["FontDescriptor"]["FontFile2"]
        widget["Contents"] = pyropdf.String(b"Changed label")
        ink = widget["AP"]["N"]["Yes"]
        ink["PrivateNote"] = pyropdf.String(b"Unrelated change")
        ink.write(b"q 0 0 3 3 re f Q " + ink.read())
        for incremental in (False, True):
            (change,) = doc.changes(incremental=incremental).claims
            assert change.claim == "PDF/A-2B"
            assert set(change.objects) == responsible


def test_an_existing_unembedded_font_does_not_hide_a_second_descriptor_loss() -> None:
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-2b.pdf") as source:
        data = VERAPDF.ALTERNATES["prepare"](source, "appearance-selected")
    with pyropdf.open(data) as doc:
        page = doc.pages[0].obj
        ink = page["Annots"][0]["AP"]["N"]["Yes"]
        fonts = ink["Resources"]["Font"]
        fonts["Other"] = VERAPDF.ALTERNATES["simple"](doc)
        del fonts["F"]["FontDescriptor"]["FontFile2"]
        page["Resources"]["Font"] = fonts
        del ink["Resources"]  # The Form inherits these fonts from its page.
        ink.write(b"BT /F 10 Tf ( ) Tj /Other 10 Tf ( ) Tj ET")
        data = doc.to_bytes()
    with pyropdf.open(data) as doc:
        desc = doc.pages[0].obj["Resources"]["Font"]["Other"]["FontDescriptor"]
        del desc["FontFile2"]
        for incremental in (False, True):
            (change,) = doc.changes(incremental=incremental).claims
            assert change.objects == (desc.xref,)


def test_unrelated_widget_edit_avoids_decoding_an_oversized_appearance() -> None:
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-2b.pdf") as source:
        data = VERAPDF.ALTERNATES["prepare"](source, "appearance-selected")
    with pyropdf.open(data) as doc:
        ink = doc.pages[0].obj["Annots"][0]["AP"]["N"]["Yes"]
        ink.write(ink.read() + b" " * 5_000)
        data = doc.to_bytes()
    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_page_content_size=2_048)) as doc:
        widget = doc.pages[0].obj["Annots"][0]
        widget["Contents"] = pyropdf.String(b"Updated description")
        assert doc.changes(incremental=True).claims == ()
        del widget["AP"]["N"]["Yes"]["Resources"]["Font"]["F"]["FontDescriptor"]["FontFile2"]
        with pytest.raises(pyropdf.LimitError) as refusal:
            doc.changes(incremental=True)
        assert refusal.value.code == "page_content_size"


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


@pytest.mark.parametrize("suffix", ["", " extensions: ISO_19005_3", " extensions: ISO_19005_3, ETSI_PAdES"])
def test_arlington_summary_and_verbose_reports_retain_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    accepted = tmp_path / "contains extensions: sample.pdf"
    rejected = tmp_path / "short identifier.pdf"
    jar = tmp_path / "validator.jar"
    flavour = "arlington1.7+ISO_19005_3"
    rule = "TrailerIDArray-1-StringByte-9"
    output = f"PASS {accepted} arlington1.7{suffix}\nDEVIATION {rejected} arlington1.7{suffix}\n  DEVIATION {rule}\n"
    captured = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
    monkeypatch.setattr(VERAPDF.subprocess, "run", lambda *_args, **_kwargs: captured)
    assert VERAPDF.validate(jar, flavour, [accepted, rejected]) == {accepted.name: True, rejected.name: False}
    reports = VERAPDF.detailed_reports(jar, flavour, [accepted, rejected])
    assert {path: VERAPDF.failed_rules(report) for path, report in reports.items()} == {
        accepted: set(),
        rejected: {rule},
    }
    absent = tmp_path / "missing control.pdf"
    with pytest.raises(SystemExit, match="missing control"):
        VERAPDF.validate(jar, flavour, [accepted, rejected, absent])
    with pytest.raises(ValueError, match="incomplete verbose"):
        VERAPDF.detailed_reports(jar, flavour, [accepted, rejected, absent])


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
            "appearance-unselected": {"4", "ua2"},
            "appearance-unused": set(),
            "appearance-invisible": set(),
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


@pytest.mark.parametrize("location", ["page", "appearance"])
@pytest.mark.parametrize("damage", ["bad-filter", "truncated", "format", "descriptor"])
def test_damaged_program_has_the_exact_claims_and_owner(location: str, damage: str) -> None:
    a = VERAPDF.ALTERNATES
    with pyropdf.open(ROOT / "tests/assets/pdfua/pdfua-2-pdfa-4.pdf") as original:
        data = a["prepare"](original, f"program-{location}-empty")
    with pyropdf.open(data) as doc:
        page = doc.pages[0].obj
        program = a["embedded_program"](page, location)
        owner = program.xref
        if damage == "bad-filter":
            program.write(b"invalid zlib data", filter=None)
            program["Filter"] = pyropdf.Name("FlateDecode")
        elif damage == "truncated":
            program.write(program.read()[:64])
        else:
            if location == "page":
                descriptor, _ = a["nested_font_entry"](page, "nested-simple-font")
            else:
                descriptor = page["Annots"][0]["AP"]["N"]["Yes"]["Resources"]["Font"]["F"]["FontDescriptor"]
            owner = descriptor.xref
            if damage == "descriptor":
                descriptor["FontFile2"] = a["stream"](doc, b"fontdata")
            else:
                del descriptor["FontFile2"]
                descriptor["FontFile3"] = program
                program["Subtype"] = pyropdf.Name("Type1C")
        page["Rotate"] = 90
        for incremental in (True, False):
            changes = doc.changes(incremental=incremental).claims
            assert {c.claim for c in changes} == {"PDF/A-4", "PDF/UA-2"}
            for change in changes:
                assert owner in change.objects
                assert set(change.objects) <= {owner, program.xref}
                assert change.reason == "a painted font lost its readable embedded program"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("nested-unused-font", False),
        ("nested-empty-show", True),
        ("appearance-empty", True),
        ("appearance-select-only", False),
        ("appearance-invisible", False),
        ("appearance-graphics-state", True),
    ],
)
def test_program_read_follows_the_text_graphics_state(case: str, *, expected: bool) -> None:
    a = VERAPDF.ALTERNATES
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-2b.pdf") as original:
        data = a["prepare"](original, case)
    with pyropdf.open(data) as doc:
        location = "appearance" if case.startswith("appearance") else "page"
        program = a["embedded_program"](doc.pages[0].obj, location)
        program.write(b"fontdata")
        changes = doc.changes(incremental=True).claims
        assert [c.objects for c in changes] == ([(program.xref,)] if expected else [])


def test_reencoding_a_valid_program_and_altering_an_existing_defect() -> None:
    a = VERAPDF.ALTERNATES
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-2b.pdf") as original:
        data = a["prepare"](original, "program-page-empty")
    with pyropdf.open(data) as doc:
        program = a["embedded_program"](doc.pages[0].obj, "page")
        program.write(program.read(), filter=None)
        assert doc.changes(incremental=True).claims == ()
        program.write(b"originally bad")
        already_invalid = doc.to_bytes()
    with pyropdf.open(already_invalid) as doc:
        program = a["embedded_program"](doc.pages[0].obj, "page")
        program.write(b"still bad")
        doc.catalog["Private"] = True
        assert doc.changes(incremental=True).claims == ()


def test_program_validation_obeys_the_stream_decoding_budget() -> None:
    a = VERAPDF.ALTERNATES
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-2b.pdf") as original:
        data = a["prepare"](original, "program-appearance-empty")
    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_decompressed_size=4_500)) as doc:
        program = a["embedded_program"](doc.pages[0].obj, "appearance")
        program.write(b"invalid")
        with pytest.raises(pyropdf.LimitError) as exc:
            doc.changes(incremental=True)
        assert exc.value.code == "decompressed_size"
        doc.history.undo()
        doc.catalog["Private"] = True
        assert doc.changes(incremental=True).claims == ()


@pytest.mark.parametrize(
    ("subtype", "slot", "format_", "file"),
    [
        ("Type1", "FontFile", None, "usrguide.pdf"),
        ("Type1", "FontFile3", "Type1C", "bill-hr815.pdf"),
        ("CIDFontType0", "FontFile3", "CIDFontType0C", "bill-hr815.pdf"),
        ("CIDFontType0", "FontFile3", "CIDFontType0C", "NotoSansJP-subset.otf"),
        ("Type1", "FontFile3", "OpenType", "colour-svg-cff.otf"),
        ("CIDFontType0", "FontFile3", "OpenType", "colour-svg-cff.otf"),
        ("CIDFontType0", "FontFile3", "OpenType", "NotoSansJP-subset.otf"),
        ("CIDFontType2", "FontFile3", "OpenType", "NotoSansHebrew-subset.ttf"),
    ],
)
def test_report_opens_embedded_programs_in_their_declared_formats(
    subtype: str, slot: str, format_: str | None, file: str
) -> None:
    if file.endswith(".pdf"):
        with pyropdf.open(ROOT / "tests/assets/real_world" / file) as doc:
            embedded = doc.font_program(doc.pages[0].fonts()[0])
            assert embedded is not None
            data = embedded.data
    else:
        data = (ROOT / "crates/pyropdf-engine/src/fonts/testdata" / file).read_bytes()
        if format_ == "CIDFontType0C":
            records = struct.iter_unpack(">4sIII", data[12 : 12 + 16 * int.from_bytes(data[4:6], "big")])
            _, _, offset, size = next(r for r in records if r[0] == b"CFF ")
            data = data[offset : offset + size]
    a = VERAPDF.ALTERNATES
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-2b.pdf") as original:
        baseline = a["prepare"](original, "appearance-selected")
    with pyropdf.open(baseline) as doc:
        font = doc.pages[0].obj["Annots"][0]["AP"]["N"]["Yes"]["Resources"]["Font"]["F"]
        desc = font["FontDescriptor"]
        del desc["FontFile2"]
        entries = {"Subtype": pyropdf.Name(format_)} if format_ else {}
        program = a["stream"](doc, data, entries)
        desc[slot] = program
        if subtype.startswith("CID"):
            font["Subtype"] = pyropdf.Name("Type0")
            font["DescendantFonts"] = [a["indirect"](doc, {"Subtype": pyropdf.Name(subtype), "FontDescriptor": desc})]
            del font["FontDescriptor"]
        else:
            font["Subtype"] = pyropdf.Name(subtype)
        source = doc.to_bytes()
        xref = program.xref
    with pyropdf.open(source) as doc:
        program = doc.objects[xref]
        program.write(data, filter=None)
        assert doc.changes(incremental=True).claims == ()
        program.write(data[:12])
        (change,) = doc.changes(incremental=True).claims
        assert (change.claim, change.objects) == ("PDF/A-2B", (xref,))


@pytest.mark.parametrize("indirect_map", [False, True])
def test_replacing_a_direct_fonts_program_is_attributed_to_the_containing_resource(*, indirect_map: bool) -> None:
    helpers = VERAPDF.ALTERNATES
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-2b.pdf") as source:
        data = helpers["prepare"](source, "nested-simple-font")
    with pyropdf.open(data) as doc:
        inner = doc.pages[0].obj["Resources"]["XObject"]["Outer"]["Resources"]["XObject"]["Inner"]
        font = inner["Resources"]["Font"]["F"]
        direct = dict(font)
        direct["FontDescriptor"] = dict(font["FontDescriptor"])
        entries = {"F": direct}
        inner["Resources"]["Font"] = helpers["indirect"](doc, entries) if indirect_map else entries
        baseline = doc.to_bytes()
    with pyropdf.open(baseline) as doc:
        inner = doc.pages[0].obj["Resources"]["XObject"]["Outer"]["Resources"]["XObject"]["Inner"]
        fonts = inner["Resources"]["Font"]
        fonts["F"]["FontDescriptor"]["FontFile2"] = helpers["stream"](doc, b"broken program")
        (change,) = doc.changes(incremental=True).claims
        assert change.objects == (fonts.xref if indirect_map else inner.xref,)


@pytest.mark.parametrize("shared", [False, True])
def test_page_and_appearance_charge_their_shared_font_once(*, shared: bool) -> None:
    helpers = VERAPDF.ALTERNATES
    with pyropdf.open(ROOT / "tests/assets/pdfa/pdfa-2b.pdf") as doc:
        helpers["prepare_appearance"](doc, "appearance-selected")
        page = doc.pages[0].obj
        face = helpers["simple"](doc)
        if shared:
            appearance = page["Annots"][0]["AP"]["N"]["Yes"]["Resources"]["Font"]["F"]
            appearance["FontDescriptor"]["FontFile2"] = face["FontDescriptor"]["FontFile2"]
        helpers["nest"](doc, b"BT /F 10 Tf ( ) Tj ET", {"Font": {"F": face}})
        source = doc.to_bytes()
    # The previously valid program is 6,408 decoded bytes. This budget also
    # fits page/appearance operators and metadata, and only one such program.
    with pyropdf.open(source, limits=pyropdf.DocumentLimits(max_total_decompressed_size=10_000)) as doc:
        program = helpers["embedded_program"](doc.pages[0].obj, "page")
        program.write(b"invalid")
        helpers["embedded_program"](doc.pages[0].obj, "appearance").write(b"invalid")
        if shared:
            (change,) = doc.changes(incremental=True).claims
            assert change.objects == (program.xref,)
        else:
            with pytest.raises(pyropdf.LimitError) as exc:
                doc.changes(incremental=True)
            assert exc.value.code == "total_decompressed_size"
