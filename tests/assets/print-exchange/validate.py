"""Validate print jobs and isolated controls with Kura 1.4.1, phpdftk and Poppler."""

# ruff: noqa: S603, T201 - executes the specified validator tools and prints the checked matrix.
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import pyropdf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from print_exchange_fixtures import break_job, edit_job, new_job  # noqa: E402


def validate(path: Path, level: str, args: argparse.Namespace) -> tuple[set[str], set[str]]:
    """Run the two independently implemented conformance checkers without modifying the source."""
    kura = subprocess.run([args.kura, "--check", "--level", level, path], capture_output=True, check=False)
    k = json.loads(kura.stdout)
    assert k["engine"] == "BentoPDF Kura Engine 1.4.1"
    assert k["mode"] == "check"
    assert k["ok"]
    assert kura.returncode == int(not k["compliant"])
    php = subprocess.run([args.php, HERE / "check.php", args.autoload, path, level], capture_output=True, check=True)
    p = json.loads(php.stdout)
    assert k["compliant"] == (not k["issues"])
    assert p["compliant"] == (not p["rules"])
    return {item["code"] for item in k["issues"]}, set(p["rules"])


def render(path: Path, pdftoppm: str) -> tuple[bytes, ...]:
    """Render both 100-point pages as RGB at 72 dpi with Poppler."""
    rasters = []
    for page in ("1", "2"):
        command = [pdftoppm, "-r", "72", "-f", page, "-l", page, "-singlefile", path]
        ppm = subprocess.run(command, capture_output=True, check=True).stdout
        assert ppm.startswith(b"P6\n100 100\n255\n")
        rasters.append(ppm)
    assert rasters[0] != rasters[1]
    return tuple(rasters)


# External validators cover different subsets of the ISO rules. In particular,
# phpdftk detects the missing VT root, while Kura also exercises leaf structure.
RULES = {
    "no-intent": ({"OUTPUT_INTENT_ADDED"}, {"6.2.2"}),
    "no-icc": ({"OUTPUT_INTENT_ADDED"}, {"6.2.2"}),
    "duplicate-intent": ({"OUTPUT_INTENTS_PRUNED"}, set()),
    "no-root": ({"VT_SINGLE_PART"}, {"6.1"}),
    "no-leaf": ({"VT_DPART_REBUILT", "VT_SINGLE_PART"}, set()),
    "wrong-leaf": ({"VT_DPART_REBUILT", "VT_SINGLE_PART"}, set()),
    "flat-children": ({"VT_DPART_REBUILT", "VT_SINGLE_PART"}, set()),
    "wrong-x": ({"XMP_NONCONFORMING"}, set()),
}


def matrix(root: Path, args: argparse.Namespace) -> Counter[str]:
    """Require the positive and negative control results for both persistence modes."""
    counts: Counter[str] = Counter()
    for level in ("x4", "vt1"):
        original = root / f"{level}.pdf"
        with new_job(level) as doc:
            doc.save(original)
        assert validate(original, level, args) == (set(), set())
        counts["created"] += 1
        pixels = render(original, args.pdftoppm)
        for incremental in (False, True):
            mode = "incremental" if incremental else "full"
            saved = root / f"{level}-{mode}.pdf"
            with pyropdf.open(original) as doc:
                edit_job(doc, level)
                doc.save(saved, incremental=incremental)
            assert validate(saved, level, args) == (set(), set())
            assert render(saved, args.pdftoppm) == pixels
            counts["edited"] += 1
            for case, expected in RULES.items():
                if level == "x4" and case not in ("no-intent", "no-icc", "duplicate-intent"):
                    continue
                with pyropdf.open(original) as doc:
                    break_job(doc, case)
                    doc.save(saved, incremental=incremental)
                observed = validate(saved, level, args)
                assert observed == expected, (level, mode, case, observed)
                counts["malformed"] += 1
                if level == "vt1" and case in ("no-root", "no-leaf", "wrong-leaf", "flat-children"):
                    assert validate(saved, "x4", args) == (set(), set())
                    counts["underlying X-4 retained"] += 1
    return counts


def structure_matrix(root: Path, args: argparse.Namespace) -> int:
    """Run the ICC and nested-DPart controls, including known differences between the two validators."""
    from test_print_profile_structures import (  # noqa: PLC0415 - the tests directory is installed above.
        HIERARCHY_CASES,
        ICC_CASES,
        MEDIA,
        _change_hierarchy,
        break_profile,
        hierarchy_pdf,
        independent_hierarchy,
        independent_icc,
        media_pdf,
    )

    rebuilt = {"VT_DPART_REBUILT", "VT_SINGLE_PART"}
    positives = [(media_pdf(model), "x4", set()) for model, *_ in MEDIA]
    positives.extend([(hierarchy_pdf(), "vt1", set()), (hierarchy_pdf(single_leaf=True), "vt1", rebuilt)])
    path = root / "structure.pdf"
    comparisons = 0
    for data, level, expected in positives:
        for incremental in (False, True):
            with pyropdf.open(data) as doc:
                doc.catalog["OutputIntents"][0]["Info"] = "Verified print condition"
                doc.save(path, incremental=incremental)
            assert (independent_icc if level == "x4" else independent_hierarchy)(path.read_bytes())
            assert validate(path, level, args) == (expected, set())
            comparisons += 1

    icc_rules = {
        "input-profile": {"OUTPUT_INTENT_ADDED"},
        "scanner": {"OUTPUT_INTENT_ADDED"},
        "display": {"OUTPUT_INTENT_ADDED"},
        "bad-signature": {"ICC_PROFILE_REPLACED"},
        "short-header": {"ICC_PROFILE_REPLACED"},
        "unrecognized-model": {"ICC_PROFILE_REPLACED"},
        "wrong-components": {"ICC_PROFILE_REPLACED", "OUTPUT_INTENT_ADDED"},
        "boolean-components": set(),
        "missing-components": set(),
    }
    for case in [*HIERARCHY_CASES, *ICC_CASES]:
        if case in HIERARCHY_CASES:
            data, level, change, independent = hierarchy_pdf(), "vt1", _change_hierarchy, independent_hierarchy
            expected = set() if case in ("nonfinal-short", "bad-name-token") else rebuilt
        else:
            data, level, change, independent = media_pdf("CMYK"), "x4", break_profile, independent_icc
            expected = icc_rules[case]
        for incremental in (False, True):
            with pyropdf.open(data) as doc:
                change(doc, case)
                doc.save(path, incremental=incremental)
            assert not independent(path.read_bytes()), case
            assert validate(path, level, args) == (expected, set()), (level, incremental, case)
            comparisons += 1
    return comparisons


def producer_inputs(source_dir: Path, root: Path) -> None:
    """Read the publisher originals, edit the job and verify the two saves independently of their producer."""
    samples = (
        ("starter_pdfx4.pdf", 1, "61e2622f5a5b81bd9e0f592951db0b2a7a8d362eca0ba6c8845ace3ac9b4874b"),
        ("starter_pdfvt1.pdf", 100, "8d2acc725edd75ed4cf818c357d286a6c70545a553ed0de16c9eb63ca3d65efc"),
        ("Ghent_PDF-Output-Test-V50_ALL_X4.pdf", 6, "6d9686303ea9049350bc1b3253c22a3cc8578bd7dfcc6afeaef89cbbe37d4cbe"),
    )
    for filename, pages, digest in samples:
        source = source_dir / filename
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        expected_vt = "PDF/VT-1" if filename == "starter_pdfvt1.pdf" else None
        with pyropdf.open(source) as doc:
            assert (doc.profiles.pdfx, doc.profiles.pdfvt) == ("PDF/X-4", expected_vt)
            assert doc.profiles.output_intents[0].profile is not None
            assert len(doc.pages) == pages
            assert not [item for item in doc.check().defects if item.kind == "profile"]
            if doc.profiles.pdfvt == "PDF/VT-1":
                parts = doc.parts.root
                assert parts is not None
                assert doc.parts.record_level == 1
                assert doc.parts.node_names == ("root", "recipient")
                assert len(parts.children) == pages
                final = parts.children[-1]
                metadata = final.dpm
                assert metadata is not None
                recipient = metadata["CIP4_Root"]["CIP4_Recipient"]
                assert recipient["CIP4_UniqueID"] == "ID_99"
                recipient["CIP4_UniqueID"] = "Revised recipient"
                final.dpm = metadata
            else:
                doc.catalog["OutputIntents"][0]["OutputCondition"] = "Revised press condition"
            for incremental in (False, True):
                saved = root / "publisher-edit.pdf"
                doc.save(saved, incremental=incremental)
                with pyropdf.open(saved) as reopened:
                    assert (reopened.profiles.pdfx, reopened.profiles.pdfvt) == ("PDF/X-4", expected_vt)
                    assert not [item for item in reopened.check().defects if item.kind == "profile"]
                    assert len(reopened.pages) == pages
                    if reopened.profiles.pdfvt == "PDF/VT-1":
                        parts = reopened.parts.root
                        assert parts is not None
                        values = [part.dpm for part in (parts.children[0], parts.children[-1])]
                        ids = [dpm["CIP4_Root"]["CIP4_Recipient"]["CIP4_UniqueID"] for dpm in values if dpm]
                        assert ids == ["ID_0", "Revised recipient"]
                    else:
                        assert reopened.profiles.output_intents[0].condition == "Revised press condition"


def main() -> None:
    """Run the entire formal matrix in a temporary directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kura", required=True, help="Path to the Kura 1.4.1 command-line tool")
    parser.add_argument("--php", default="php")
    parser.add_argument("--autoload", required=True, help="Path to the pinned phpdftk checkout's Composer autoloader")
    parser.add_argument("--pdftoppm", default="pdftoppm")
    parser.add_argument(
        "--sources", type=Path, help="Directory containing the three original publisher PDFs in the README"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        counts = matrix(Path(temporary), args)
        structures = structure_matrix(Path(temporary), args)
        print(f"{structures} ICC and hierarchy comparisons matched their expected independent outcomes")
        if args.sources:
            producer_inputs(args.sources, Path(temporary))
            print("Verified all three publisher inputs and their six edited saves")
    print(f"{sum(counts.values())} profile comparisons matched in both validators: {dict(counts)}")


if __name__ == "__main__":
    main()
