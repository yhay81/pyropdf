"""Keep intentional PDF/UA failures narrow while adding save-harness operations."""

from __future__ import annotations

import importlib.util
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
