"""Validate the PDF/A and PDF/UA corpora, and every save of every edit of them, with veraPDF.

CI runs this with veraPDF's greenfield-apps and greenfield-apps-arlington
jars, which it downloads pinned by their SHA-256:

    uv run python tools/verapdf.py --jar greenfield-apps-1.28.2.jar --arlington greenfield-apps-arlington-1.28.2.jar

Each fixture of tests/assets/pdfa and tests/assets/pdfua is validated under
its claimed flavour, as is each edit of the preservation harness's EDITS
applied to it and saved in full and incrementally. With ``--arlington``,
each of those files is also checked against the Arlington PDF Model of the
effective PDF version, with the extensions that its PDF/A part adds to
that version. A file that veraPDF rejects must be listed in
``EXPECTED`` with its exact rules and tracking issue; the script exits with status 1
on a rejection that is not listed, and on a listed one that passes.
"""

from __future__ import annotations

import argparse
import re
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

import pyropdf
from pyropdf._objects import _direct_value

ROOT = Path(__file__).resolve().parent.parent
# The preservation harness imports its sibling test modules.
sys.path.insert(0, str(ROOT / "tests"))
EDITS = runpy.run_path(str(ROOT / "tests" / "test_preservation.py"))["EDITS"]

#: Each fixture, by path, with veraPDF's flavour, from the generator of each corpus.
FIXTURES: dict[Path, str] = {
    corpus / name: flavour
    for corpus in (ROOT / "tests" / "assets" / "pdfa", ROOT / "tests" / "assets" / "pdfua")
    for name, (_, _, flavour) in runpy.run_path(str(corpus / "generate.py"))["FLAVOURS"].items()
}

#: Rules caused solely by deliberately leaving a new annotation/widget untagged.
UNTAGGED_RULES = {
    "add_annotation": {"7.18.1-1", "7.18.3-1"},
    "edit_annotation": {"7.18.1-1", "7.18.3-1"},
    "add_field": {"7.18.3-1", "7.18.4-1"},
    "fill_field": {"7.18.3-1", "7.18.4-1"},
    "import_form_data": {"7.18.3-1", "7.18.4-1"},
}

DUAL = "pdfua-2-pdfa-4"
UA2_ANNOTATIONS = {
    edit: (
        {"8.10.2.3-1", "8.9.3.3-1"}
        if edit in {"add_field", "fill_field", "import_form_data"}
        else {"8.9.2.3-1", "8.9.3.3-1"}
    )
    for edit in UNTAGGED_RULES
}

# Every expected rejection of an edit is scoped to the source, profile,
# operation, save mode and exact failed rules. A new rule or a surprise pass fails CI.
EXPECTED: dict[str, tuple[str, set[str]]] = {}


def expect(
    source: str,
    flavour: str,
    edits: dict[str, set[str]],
    issue: str,
    *,
    modes: tuple[str, ...] = ("full", "incremental"),
) -> None:
    """Record the named operations and save modes under one source and profile."""
    for edit, rules in edits.items():
        for mode in modes:
            EXPECTED[f"{flavour} {source}-{edit}-{mode}.pdf"] = (issue, rules)


# The rule sets checked in #848 identify the exact operation, profile and mode. In particular, a full save invalidates
# an existing signature, while an incremental save must keep its byte range.
SIGNATURE_FIELDS = ("signature_field", "sign_revision", "timestamp_revision")
ATTACHMENT_EDITS = ("add_attachment", "write_attachment", "collection_item", "related_files")
for source_path, profile in [*FIXTURES.items(), (Path(DUAL), "4")]:
    source = source_path.stem
    part = profile[0]
    if part not in "1234":
        continue
    edit_rules = {"action_triggers": {"6.6.1-3" if part == "1" else "6.4.1-1"}}
    if part == "1":
        edit_rules.update(
            annotation_relations={"6.5.3-2"},
            canvas_resources={"6.1.13-1", "6.4-3"},
            file_specification={"6.1.11-1", "6.5.2-1"},
            optional_content={"6.1.13-1"},
        )
        edit_rules.update({key: {"6.9-2"} for key in SIGNATURE_FIELDS})
    else:
        edit_rules["page_records"] = {"6.11-2" if part == "4" else "6.10-2"}
    if part == "2":
        edit_rules["file_specification"] = {"6.8-5"}
    if profile == "4":
        edit_rules["file_specification"] = {"6.3.1-1", "6.9-3"}
    if profile == "4e":
        edit_rules["file_specification"] = {"6.3.1-1"}
    expect(source, profile, edit_rules, "#848")
    if part in "12" or profile == "4":
        attachment_rules = {"1": {"6.1.11-1", "6.1.11-2"}, "2": {"6.8-5"}, "4": {"6.9-3"}}[part]
        expect(source, profile, dict.fromkeys(ATTACHMENT_EDITS, attachment_rules), "#301")
    if part in "23":
        expect(
            source,
            profile,
            {key: {"6.4.3-1"} for key in ("sign_revision", "timestamp_revision")},
            "#848",
            modes=("full",),
        )

expect("pdfua-1", "ua1", UNTAGGED_RULES, "#794")
expect("pdfua-1", "ua1", {"remove-language": {"7.2-22", "7.2-33"}}, "#847")
expect(
    "pdfua-1",
    "ua1",
    {
        "action_triggers": UNTAGGED_RULES["add_field"],
        "annotation_relations": {"7.18.1-1", "7.18.1-2", "7.18.3-1"},
        "file_specification": UNTAGGED_RULES["add_annotation"],
        "ocr_layer": {"7.1-3"},
        "radio_groups": {"7.18.1-3", "7.18.3-1", "7.18.4-1"},
        **{key: {"7.18.1-2", "7.18.3-1", "7.18.5-1", "7.18.5-2"} for key in ("page_copies", "remove_rotation")},
        **{key: {"7.18.3-1"} for key in SIGNATURE_FIELDS},
    },
    "#848",
)
for source in ("pdfua-2", DUAL):
    expect(
        source,
        "ua2",
        {
            **UA2_ANNOTATIONS,
            **{key: {"8.14.1-1"} for key in ATTACHMENT_EDITS},
            **dict.fromkeys((*SIGNATURE_FIELDS, "action_triggers", "radio_groups"), UA2_ANNOTATIONS["add_field"]),
            **dict.fromkeys(("annotation_relations", "file_specification"), UA2_ANNOTATIONS["add_annotation"]),
            **{key: {"8.2.5.20-1", "8.9.3.3-1"} for key in ("page_copies", "remove_rotation")},
            **{key: {"8.8-1"} for key in ("outline", "shared_action_trigger")},
            "delete_page": {"8.2.5.2-1", "Table 5. StructTreeRoot-Document-1"},
            "ocr_layer": {"8.2.2-1"},
        },
        "#848",
    )


def rich_media_saves(directory: Path) -> dict[Path, str]:
    """Check the split asset tree and shared configurations with and without a named activation script."""
    fixture = runpy.run_path(str(ROOT / "tests/test_rich_media_settings.py"))

    files = {}
    for scripts in (False, True):
        with pyropdf.open(fixture["rich_settings_pdf"](scripts=scripts)) as doc:
            fixture["edit_rich_fixture"](doc)
            for incremental in (False, True):
                mode = "incremental" if incremental else "full"
                variant = "script" if scripts else "plain"
                path = directory / f"rich-media-{variant}-{mode}.pdf"
                doc.save(path, incremental=incremental)
                files[path] = "arlington2.0"
    with pyropdf.open(fixture["legacy_pdf"]()) as doc:
        with doc.pages[0].annotations[0].edit_rich_media() as media:
            params = media.RichMediaContent.Configurations[0].Instances[0].Params
            if params is None:
                msg = "the Adobe extension fixture needs its Flash instance parameters"
                raise ValueError(msg)
            params.FlashVars = "mode=stored"
        for incremental in (False, True):
            mode = "incremental" if incremental else "full"
            path = directory / f"rich-media-legacy-{mode}.pdf"
            doc.save(path, incremental=incremental)
            files[path] = "arlington1.7+ADBE_Extn3"
    return files


# 1.28.2 rejects a script that is present in the asset tree. 1.30.4 accepts
# these same named files and rejects a script whose final name is removed.
expect("rich-media", "arlington2.0", {"script": {"ArrayOfIndirectFileSpecificationsEntry-Dictionary-9"}}, "#914")


def language_saves(directory: Path) -> dict[Path, str]:
    """Write the UA-1 catalog language loss and a control with explicit structure and metadata languages."""
    source = ROOT / "tests/assets/pdfua/pdfua-1.pdf"
    files = {}
    for local_language in (False, True):
        with pyropdf.open(source) as doc:
            if local_language:
                doc.catalog["StructTreeRoot"]["K"][0]["Lang"] = pyropdf.String(b"en-US")
                packet = doc.xmp
                if packet is None:
                    msg = "the PDF/UA-1 fixture must have an XMP title"
                    raise ValueError(msg)
                doc.xmp = packet.replace('xml:lang="x-default"', 'xml:lang="en-US"')
            del doc.language
            for incremental in (False, True):
                claims = doc.changes(incremental=incremental).claims
                expected = [] if local_language else [("PDF/UA-1", (doc.catalog.xref,))]
                if [(claim.claim, claim.objects) for claim in claims] != expected:
                    msg = f"unexpected pre-save language report: {claims}"
                    raise ValueError(msg)
                edit = "local-language" if local_language else "remove-language"
                mode = "incremental" if incremental else "full"
                path = directory / f"pdfua-1-{edit}-{mode}.pdf"
                doc.save(path, incremental=incremental)
                files[path] = "ua1"
    return files


#: Arlington extensions of the PDF version for each fixture.
EXTENSIONS = dict.fromkeys(("3a", "3b", "3u"), "ISO_19005_3")
PADES_LEVEL = 2

# The base 2.0 model rejects these standard VRI and CAdES cases (#883).
# Check both base and extension verdicts on the same bytes.
for source_path, profile in FIXTURES.items():
    extension = "+ISO_19005_3" if profile in EXTENSIONS else ""
    version = "2.0" if profile.startswith("4") or profile == "ua2" else "1.4" if profile.startswith("1") else "1.7"
    # AnnotWidget/Parent links to generic Field in the pinned model. The same
    # parent qualifies as FieldBtnRadio from AcroForm/Fields; the raw field
    # flags, parent links and default appearance are checked on each save.
    expect(
        source_path.stem, "arlington" + version + extension, {"radio_groups": {"Field-1", "Field-Ff-Bitmask-9"}}, "#879"
    )
    model = "arlington2.0" + extension
    expect(source_path.stem, model, {"security_store": {"VRI-OCSP-Array-9"}}, "#926")
    if profile.startswith("4") or profile == "ua2":
        expect(source_path.stem, model, {"sign_revision": {"Signature-SubFilter-Name-ETSI.CAdES.detached-27"}}, "#927")


def saves(directory: Path) -> dict[Path, str]:
    """Write every save of every edit of every fixture into ``directory``; return all files with their flavour."""
    out = dict(FIXTURES)
    for source, flavour in FIXTURES.items():
        for edit, case in sorted(EDITS.items()):
            for incremental in (False, True):
                doc = pyropdf.open(source)
                case.prepare(doc)
                if edit in {"sign_revision", "timestamp_revision"}:
                    # Signing publishes a revision and adopts it as the new base.
                    check_matrix_claims(doc, source, flavour, "signature_field", incremental=True)
                case.apply(doc)
                check_matrix_claims(doc, source, flavour, edit, incremental=incremental)
                check_claims(doc, flavour, edit, incremental=incremental)
                mode = "incremental" if incremental else "full"
                path = directory / f"{source.stem}-{edit}-{mode}.pdf"
                path.write_bytes(doc.to_bytes(incremental=incremental))
                with pyropdf.open(path) as reopened:
                    case.check(reopened)
                out[path] = flavour
    return out


def check_claims(doc: pyropdf.Document, flavour: str, edit: str, *, incremental: bool) -> None:
    """Require the deliberately untagged annotation to be reported before saving."""
    if flavour != "ua1" or edit not in UNTAGGED_RULES:
        return
    page = doc.pages[0]
    items = page.annotations if edit.endswith("annotation") else page.widgets
    expected = {item.xref for item in items}
    claims = doc.changes(incremental=incremental).claims
    if len(claims) != 1 or claims[0].claim != "PDF/UA-1" or set(claims[0].objects) != expected or not expected:
        msg = f"{edit} must report only the untagged annotations {expected}, got {claims}"
        raise ValueError(msg)


def remove_signature_saves(source: Path, directory: Path) -> dict[Path, str]:
    """Check that removing the signed field clears the archival byte-range warning."""
    files = {}
    with pyropdf.open(source) as doc:
        del doc.form[doc.signatures[0].field]
        for incremental in (False, True):
            if doc.changes(incremental=incremental).claims:
                msg = "removing the signed field must clear the archival byte-range warning"
                raise ValueError(msg)
            mode = "incremental" if incremental else "full"
            path = directory / f"pdfa-2b-removed-signature-{mode}.pdf"
            doc.save(path, incremental=incremental)
            files[path] = "2b"
    return files


def claim_name(flavour: str) -> str:
    """Return the public claim identity for a forced veraPDF flavour."""
    return f"PDF/UA-{flavour[2:]}" if flavour.startswith("ua") else f"PDF/A-{flavour.upper()}"


def check_matrix_claims(doc: pyropdf.Document, source: Path, flavour: str, edit: str, *, incremental: bool) -> None:
    """Require exactly the claims for which this save introduces an independently checked rule failure."""
    mode = "incremental" if incremental else "full"
    expected = set()
    adopted = edit in {"sign_revision", "timestamp_revision"} and bool(doc.signatures)
    for profile in (flavour, "4") if source.stem == DUAL else (flavour,):
        prefix = f"{profile} {source.stem}-{edit}-"
        rules = EXPECTED.get(prefix + mode + ".pdf", ("", set()))[1]
        # The no-edit incremental bytes are exactly the newly adopted signed revision,
        # as checked by the shared edit. Its defects predate the next save.
        baseline = EXPECTED.get(prefix + "incremental.pdf", ("", set()))[1] if adopted else set()
        if rules - baseline:
            expected.add(claim_name(profile))
    claims = doc.changes(incremental=incremental).claims
    if {change.claim for change in claims} != expected or any(
        not change.reason or (not change.objects and not (adopted and not incremental)) for change in claims
    ):
        msg = f"{source.stem} {edit} {mode} expected {expected}, got {claims}"
        raise ValueError(msg)


# Targeted edits to the dual-claim fixture. Both forced profiles are verified
# on each prepared input and each saved output.
FOCUSED = {
    "archival-revision-loss": {"4": {"6.7.3-5"}},
    "accessible-revision-loss": {"ua2": {"5-5"}},
    "metadata-loss": {"ua2": {"8.11.1-2"}, "4": {"6.7.2.1-1"}},
    "language-loss": {"ua2": {"8.4.4-1"}},
    "alt-loss": {"ua2": {"8.2.5.28.2-1"}},
    "parent-tree-loss": {"ua2": {"8.2.2-1"}},
    "output-intent-loss": {"4": {"6.2.4.3-2"}},
    "indirect-intent-loss": {"4": {"6.2.4.3-2"}},
    "unused-font-loss": {},
    "font-loss": {"ua2": {"8.4.5.5.1-1"}, "4": {"6.2.10.4.1-1"}},
    "undo-loss": {},
    "rollback-loss": {},
    "refused-rotation": {},
    "existing-alt-loss": {"ua2": {"8.2.5.28.2-1"}},
}
for operation, profiles in FOCUSED.items():
    for profile, ids in profiles.items():
        expect(DUAL, profile, {operation: ids}, "#848")
EXPECTED[f"ua2 {DUAL}-existing-alt-input.pdf"] = ("#848", {"8.2.5.28.2-1"})


def prepare_profile_case(doc: pyropdf.Document, edit: str) -> None:
    """Prepare the independently validated baseline for an indirect dependency or existing defect."""
    if edit in {"font-loss", "unused-font-loss"}:
        original = doc.pages[0].obj["Contents"].xref
        EDITS["canvas_text"].apply(doc)
        if edit == "unused-font-loss":
            doc.pages[0].obj["Contents"] = doc.objects[original]
    elif edit == "indirect-intent-loss":
        intents = doc.catalog["OutputIntents"]
        intents[0] = doc.objects[doc.objects.add(dict(intents[0]))]
    elif edit == "existing-alt-loss":
        del doc.catalog["StructTreeRoot"]["K"][0]["K"][0]["Alt"]


def apply_profile_case(doc: pyropdf.Document, edit: str) -> int | None:
    """Apply one edit and return the responsible indirect object when conformance changes."""
    cat = doc.catalog
    if edit in {"archival-revision-loss", "accessible-revision-loss"}:
        field = (
            b"<pdfaid:rev>2020</pdfaid:rev>" if edit == "archival-revision-loss" else b"<pdfuaid:rev>2024</pdfuaid:rev>"
        )
        metadata = cat["Metadata"]
        metadata.write(metadata.read().replace(field, b""))
        return metadata.xref
    if edit in {"metadata-loss", "language-loss", "output-intent-loss"}:
        key = {"metadata-loss": "Metadata", "language-loss": "Lang", "output-intent-loss": "OutputIntents"}[edit]
        del cat[key]
    elif edit == "alt-loss":
        figure = cat["StructTreeRoot"]["K"][0]["K"][0]
        del figure["Alt"]
        return figure.xref
    elif edit == "parent-tree-loss":
        root = cat["StructTreeRoot"]
        del root["ParentTree"]
        return root.xref
    elif edit == "indirect-intent-loss":
        intent = cat["OutputIntents"][0]
        del intent["DestOutputProfile"]
        return intent.xref
    elif edit in {"font-loss", "unused-font-loss"}:
        for number in doc.objects:
            value = doc.objects[number]
            if isinstance(value, pyropdf.Dictionary) and value.get("Type") == pyropdf.Name("FontDescriptor"):
                key = next(key for key in ("FontFile", "FontFile2", "FontFile3") if key in value)
                del value[key]
                return number
        msg = "the embedded-text control needs a font descriptor"
        raise ValueError(msg)
    else:
        apply_profile_control(doc, edit)
    return cat.xref


def apply_profile_control(doc: pyropdf.Document, edit: str) -> None:
    """Keep the saved report clear after undo, rollback, refusal, or an unrelated edit."""
    if edit == "undo-loss":
        del doc.catalog["Metadata"]
        doc.history.undo()
    elif edit in {"rollback-loss", "refused-rotation"}:
        try:
            with doc.transaction():
                if edit == "rollback-loss":
                    del doc.catalog["Metadata"]
                doc.pages[0].rotation = 45
        except ValueError:
            pass
        else:
            msg = "the invalid page rotation should be refused"
            raise AssertionError(msg)
    elif edit == "existing-alt-loss":
        doc.pages[0].rotation = 90
    else:
        raise KeyError(edit)


def profile_saves(directory: Path) -> dict[Path, str]:
    """Match both pre-save claim identities and the responsible object to the per-profile validator cases."""
    source = ROOT / f"tests/assets/pdfua/{DUAL}.pdf"
    files = {}
    for edit, rules in FOCUSED.items():
        baseline = source
        if edit in {"font-loss", "unused-font-loss", "indirect-intent-loss", "existing-alt-loss"}:
            baseline = directory / f"{DUAL}-{edit.removesuffix('-loss')}-input.pdf"
            with pyropdf.open(source) as prepared:
                prepare_profile_case(prepared, edit)
                prepared.save(baseline)
            files[baseline] = "ua2"
        with pyropdf.open(baseline) as doc:
            owner = apply_profile_case(doc, edit)
            expected = {} if edit == "existing-alt-loss" else {claim_name(f): (owner,) for f in rules}
            for incremental in (False, True):
                report = doc.changes(incremental=incremental).claims
                if {change.claim: change.objects for change in report} != expected or any(not c.reason for c in report):
                    msg = f"{edit} expected {expected}, got {report}"
                    raise ValueError(msg)
                mode = "incremental" if incremental else "full"
                path = directory / f"{DUAL}-{edit}-{mode}.pdf"
                doc.save(path, incremental=incremental)
                files[path] = "ua2"
    return files


def declares_pades(extensions: object) -> bool:
    """Recognize the PDF 1.7 declarations registered for PAdES signatures."""
    if not isinstance(extensions, pyropdf.Dictionary) or extensions.is_indirect:
        return False
    for prefix, required in (("ESIC", PADES_LEVEL), ("ADBE", 8)):
        entry = extensions.get(prefix)
        if (
            isinstance(entry, pyropdf.Dictionary)
            and not entry.is_indirect
            and _direct_value(entry, "BaseVersion") == "1.7"
        ):
            level = _direct_value(entry, "ExtensionLevel")
            if type(level) is int and level >= required:
                return True
    return False


def arlington(path: Path, flavour: str) -> str:
    """Validate the effective version, including a catalog promotion in a later revision."""
    with path.open("rb") as source:
        header = source.read(8)[5:8].decode("ascii")
    with pyropdf.open(path) as doc:
        declared = doc.catalog.get("Version")
        pades = declares_pades(doc.catalog.get("Extensions"))
    # Valid PDF version names are ordered by their one-digit components.
    effective = (
        max(header, str(declared))
        if isinstance(declared, pyropdf.Name) and re.fullmatch(r"1\.[0-7]|2\.0", str(declared))
        else header
    )
    version = "arlington" + effective
    names = [EXTENSIONS[flavour]] if flavour in EXTENSIONS else []
    if pades:
        names.append("ETSI_PAdES")
    return f"{version}+{','.join(names)}" if names else version


def by_flavour(files: dict[Path, str]) -> dict[str, list[Path]]:
    """Group ``files`` by their flavour."""
    out: dict[str, list[Path]] = {}
    for path, flavour in files.items():
        out.setdefault(flavour, []).append(path)
    return out


def cli(jar: Path, flavour: str) -> list[str]:
    """Return the veraPDF command line that validates under ``flavour`` and reports in text.

    An Arlington flavour names its model extensions after a ``+``.
    """
    # The wrapper, unlike VeraPdfCli itself, installs the validation foundry.
    wrapper = "org.verapdf.apps.GreenfieldCliWrapper"
    flavour, _, extensions = flavour.partition("+")
    command = ["java", "-cp", str(jar), wrapper, "--flavour", flavour, "--format", "text"]
    return [*command, "--extensions", extensions] if extensions else command


def verdict_path(header: str) -> Path:
    """Extract the path before the flavour and optional Arlington extension list."""
    return Path(re.sub(r" \S+(?: extensions: [\w.-]+(?:,\s*[\w.-]+)*)?$", "", header))


def validate(jar: Path, flavour: str, paths: list[Path]) -> dict[str, bool]:
    """Return whether veraPDF accepts each of ``paths`` under ``flavour``, by file name."""
    command = cli(jar, flavour)
    result = subprocess.run([*command, *map(str, paths)], capture_output=True, text=True, check=False)  # noqa: S603 - a fixed command on generated files.
    verdicts: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        verdict, _, rest = line.partition(" ")
        # The Arlington profiles report a deviation from the model as DEVIATION.
        if verdict in {"PASS", "FAIL", "DEVIATION"}:
            verdicts[verdict_path(rest).name] = verdict == "PASS"
    missing = [path.name for path in paths if path.name not in verdicts]
    if missing:
        sys.stderr.write(f"{result.stdout}\n{result.stderr}\n")
        msg = f"veraPDF reported no verdict for {missing}"
        raise SystemExit(msg)
    return verdicts


def details(jar: Path, flavour: str, path: Path) -> str:
    """Return veraPDF's text report of the rules that ``path`` fails."""
    command = cli(jar, flavour)
    result = subprocess.run([*command, "-v", str(path)], capture_output=True, text=True, check=False)  # noqa: S603 - a fixed command on a generated file.
    return result.stdout


def detailed_reports(jar: Path, flavour: str, paths: list[Path]) -> dict[Path, str]:
    """Read exact rule IDs for a group in one validator invocation."""
    if not paths:
        return {}
    if len(paths) == 1:
        path = paths[0]
        return {path: details(jar, flavour, path)}
    command = [*cli(jar, flavour), "-v", *map(str, paths)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603 - test corpus paths.
    reports: dict[Path, list[str]] = {}
    current: list[str] | None = None
    for line in result.stdout.splitlines():
        if line.startswith(("PASS ", "FAIL ", "DEVIATION ")):
            source = verdict_path(line.partition(" ")[2])
            current = reports.setdefault(source, [])
        if current is not None:
            current.append(line)
    if set(reports) != set(paths):
        msg = f"incomplete verbose {flavour} report: {result.stderr}"
        raise ValueError(msg)
    return {path: "\n".join(lines) for path, lines in reports.items()}


def failed_rules(report: str) -> set[str]:
    """Read numeric, ISO table, and Arlington rule identifiers, including spaces."""
    return set(re.findall(r"^[ \t]+(?:FAIL|DEVIATION) ([^\r\n]+)$", report, re.MULTILINE))


def run(
    jar: Path, files: dict[Path, str], *, outcomes: dict[str, tuple[str, set[str]]] | None = None
) -> list[tuple[str, Path]]:
    """Verify the verdict and the complete failed-rule set under each flavour."""
    failures = []
    outcomes = EXPECTED if outcomes is None else outcomes
    for flavour, paths in by_flavour(files).items():
        verdicts = validate(jar, flavour, paths)
        reports = detailed_reports(jar, flavour, [path for path in paths if not verdicts[path.name]])
        for path in paths:
            accepted = verdicts[path.name]
            key = f"{flavour} {path.name}"
            expected = outcomes.get(key)
            note = f" ({expected[0]})" if expected else ""
            sys.stdout.write(f"{'PASS' if accepted else 'FAIL'} {key}{note}\n")
            report = reports.get(path, "")
            if accepted == bool(expected) or (expected and failed_rules(report) != expected[1]):
                failures.append((flavour, path))
                sys.stdout.write(f"unexpected verdict for {key}\n{report}\n")
    return failures


def dual_archival(files: dict[Path, str]) -> dict[Path, str]:
    """Include the same source and every save in the independent PDF/A-4 run."""
    return {path: "4" for path in files if path.stem == DUAL or path.stem.startswith(DUAL + "-")}


ARCHIVAL_MEDIA_RULES = {
    "flags": {"6.3.2-1"},
    "flags-null": {"6.3.2-1"},
    "flags-no-print": {"6.3.2-2"},
    "flags-toggle-no-view": {"6.3.2-2"},
    "filename": {"6.9-2"},
    "unicode-filename": {"6.9-2"},
    "unicode-filename-name": {"6.9-2"},
    "relationship": {"6.9-4"},
    "relationship-string": {"6.9-4"},
}
expect("media", "4e", ARCHIVAL_MEDIA_RULES, "#967")
# A complete untagged RichMedia annotation and its embedded U3D already
# violate these rules on the plain A-4 / UA-2 source. The edits below add
# a rule only to the archival verdict.
for case in ("complete", "flags-numeric", *ARCHIVAL_MEDIA_RULES):
    expect(f"{DUAL}-media", "ua2", {case: {"8.9.3.3-1", "8.9.2.4.19-2"}}, "#967")
    expect(f"{DUAL}-media", "4", {case: {"6.3.1-1", "6.9-3"} | ARCHIVAL_MEDIA_RULES.get(case, set())}, "#967")


def archival_media_source(source: Path) -> bytes:
    """Establish the baseline with complete engineering annotation flags and file keys."""
    spec = pyropdf.RichMedia(
        rect=pyropdf.Rect(10, 10, 30, 30),
        kind=pyropdf.MediaKind.THREE_D,
        asset=pyropdf.MediaAsset(filename="model.u3d", content_type="model/u3d", data=b"stored asset"),
    )
    with pyropdf.open(source) as doc:
        annotation = doc.pages[0].annotations.add(spec)
        file = annotation.obj["RichMediaContent"]["Assets"]["Names"][1]
        annotation.flags = pyropdf.AnnotationFlags.PRINT
        file["UF"] = "model.u3d"
        file["AFRelationship"] = pyropdf.Name("Data")
        return doc.to_bytes()


def archival_media_controls(directory: Path) -> dict[Path, str]:
    """Compare the changed owner and both saved verdicts against each source's own baseline."""
    remove = object()
    edits = {
        "complete": ("", None),
        # The structure checker requires an integer. veraPDF tests the
        # integer portion of numeric values for the archival flag bits.
        "flags-numeric": ("F", 4.5),
        "flags": ("F", remove),
        "flags-null": ("F", None),
        "flags-no-print": ("F", 0),
        "flags-toggle-no-view": ("F", int(pyropdf.AnnotationFlags.PRINT | pyropdf.AnnotationFlags.TOGGLE_NO_VIEW)),
        "filename": ("F", remove),
        "unicode-filename": ("UF", remove),
        "unicode-filename-name": ("UF", pyropdf.Name("model.u3d")),
        "relationship": ("AFRelationship", remove),
        "relationship-string": ("AFRelationship", "Data"),
    }
    files = {}
    inputs = [
        (ROOT / "tests/assets/pdfa/pdfa-4e.pdf", "media", "4e", "PDF/A-4E"),
        (ROOT / f"tests/assets/pdfua/{DUAL}.pdf", f"{DUAL}-media", "ua2", "PDF/A-4"),
    ]
    for source, prefix, profile, archival_claim in inputs:
        original = archival_media_source(source)
        for case, (field, value) in edits.items():
            with pyropdf.open(original) as doc:
                annotation = doc.pages[0].annotations[-1]
                file = annotation.obj["RichMediaContent"]["Assets"]["Names"][1]
                owner = annotation.obj if case.startswith("flags") else file
                if value is remove:
                    del owner[field]
                elif field:
                    owner[field] = value
                for incremental in (False, True):
                    claims = doc.changes(incremental=incremental).claims
                    expected = [(archival_claim, (owner.xref,))] if case in ARCHIVAL_MEDIA_RULES else []
                    if [(item.claim, item.objects) for item in claims] != expected:
                        msg = f"unexpected {prefix} report for {case}: {claims}"
                        raise ValueError(msg)
                    mode = "incremental" if incremental else "full"
                    path = directory / f"{prefix}-{case}-{mode}.pdf"
                    doc.save(path, incremental=incremental)
                    with pyropdf.open(path) as reopened:
                        reopened.pages[0].rotation = 90
                        if reopened.changes(incremental=incremental).claims:
                            msg = f"an unrelated edit reported the existing {case} defect"
                            raise ValueError(msg)
                    files[path] = profile
    return files


# The shared Screen case embeds a clip and a rendition action without tagging
# the annotation. Each profile restricts a different part of that graph.
SCREEN_RULES = {
    "1": {"6.1.11-1", "6.5.2-1", "6.5.3-2", "6.6.1-1"},
    "2": {"6.3.1-1", "6.3.2-1", "6.5.1-1", "6.8-2", "6.8-5"},
    "3": {"6.3.1-1", "6.3.2-1", "6.5.1-1", "6.8-2", "6.8-3", "6.8-4"},
    "4": {"6.3.1-1", "6.3.2-1", "6.6.1-1", "6.9-2", "6.9-4"},
    "ua1": {"7.11-1", "7.18.1-1", "7.18.1-2", "7.18.3-1", "7.18.6.2-2"},
    "ua2": {"8.9.2.4.12-1", "8.9.3.3-1"},
}
for source_path, variant in [*FIXTURES.items(), *dual_archival(FIXTURES).items()]:
    rules = SCREEN_RULES[variant if variant.startswith("ua") else variant[0]]
    if variant == "4":
        rules = rules | {"6.9-3"}  # The base profile also restricts the embedded file's type.
    expect(source_path.stem, variant, {"add_screen": rules}, "#848")


# The stored RichMedia case adds its instance file and an untagged annotation.
# PDF/A-4e permits the annotation subtype. Base PDF/A-4 also restricts the file type.
RICH_MEDIA_RULES = {
    "1": {"6.1.11-1", "6.5.2-1", "6.5.3-2"},
    "2": {"6.3.1-1", "6.3.2-1", "6.8-2", "6.8-5"},
    "3": {"6.3.1-1", "6.3.2-1", "6.8-2", "6.8-3", "6.8-4"},
    "4": {"6.3.1-1", "6.3.2-1", "6.9-2", "6.9-3", "6.9-4"},
    "4e": {"6.3.2-1", "6.9-2", "6.9-4"},
    "4f": {"6.3.1-1", "6.3.2-1", "6.9-2", "6.9-4"},
    "ua1": {"7.11-1", "7.18.1-1", "7.18.1-2", "7.18.3-1"},
    "ua2": {"8.9.2.4.19-2", "8.9.3.3-1"},
}
for source_path, variant in [*FIXTURES.items(), *dual_archival(FIXTURES).items()]:
    key = variant if variant in RICH_MEDIA_RULES else variant[0]
    expect(source_path.stem, variant, {"rich_media_settings": RICH_MEDIA_RULES[key]}, "#914")


def pades_comparisons(models: dict[Path, str]) -> dict[Path, str]:
    """Check the same native 2.0 DSS and CAdES files under the PAdES extension."""
    suffixes = tuple(
        f"-{edit}-{mode}" for edit in ("security_store", "sign_revision") for mode in ("full", "incremental")
    )
    return {
        path: model + ("," if "+" in model else "+") + "ETSI_PAdES"
        for path, model in models.items()
        if model.partition("+")[0] == "arlington2.0" and "ETSI_PAdES" not in model and path.stem.endswith(suffixes)
    }


def signature_widget_controls(jar: Path, directory: Path) -> list[tuple[str, Path]]:
    """Check the separate page and widget rules on an independently sourced PDF/UA-2 file."""
    source = ROOT / "tests/assets/profile-sources/ua2-pass.pdf"
    signing = runpy.run_path(str(ROOT / "tests/test_preservation_operations.py"))["local_pki"]()
    files = {}
    outcomes = {}
    cases = (
        (True, True, set()),
        (True, False, {"8.10.2.3-1"}),
        (False, True, {"8.9.3.3-1"}),
        (False, False, {"8.9.3.3-1", "8.10.2.3-1"}),
    )
    for tabs, described, rules in cases:
        with pyropdf.open(source) as doc:
            number = doc.prepare_signature("Review")
            if described:
                doc.objects[number]["Contents"] = "Approval by the reviewer"
            if not tabs:
                del doc.pages[0].obj["Tabs"]
            saves = {"full": doc.to_bytes(), "incremental": doc.to_bytes(incremental=True)}
        with pyropdf.open(saves["incremental"]) as prepared:
            saves["signed"] = prepared.sign("Review", signing.signer)
            if (
                not saves["signed"].startswith(saves["incremental"])
                or prepared.form["Review"].xref != number
                or not prepared.signatures[-1].verify(signing.trust)
            ):
                msg = "signing the prepared widget changed the original revision or failed verification"
                raise ValueError(msg)
        for mode, data in saves.items():
            path = directory / f"signature-widget-tabs-{tabs}-described-{described}-{mode}.pdf"
            path.write_bytes(data)
            files[path] = "ua2"
            if rules:
                outcomes[f"ua2 {path.name}"] = ("#1094", rules)
    with pyropdf.open(source) as doc:
        number = doc.prepare_signature("Review")
        doc.objects[number]["Rect"] = [14, 17, 48, 31]
        visible = doc.to_bytes()
    with pyropdf.open(visible) as doc:
        doc.objects[number]["Rect"] = [14, 17, 14, 17]
        sizes = {"visible": visible, "zero-full": doc.to_bytes(), "zero-incremental": doc.to_bytes(incremental=True)}
    for size, data in sizes.items():
        path = directory / f"signature-widget-undescribed-{size}.pdf"
        path.write_bytes(data)
        files[path] = "ua2"
        outcomes[f"ua2 {path.name}"] = ("#1094", {"8.10.2.3-1"})
    return run(jar, files, outcomes=outcomes)


def artifact_widget_label_controls(jar: Path, directory: Path) -> list[tuple[str, Path]]:
    """Verify the actual label in a signature widget's reciprocal structure relationships."""
    fixtures = runpy.run_path(str(ROOT / "tests/test_changes.py"))
    files = {}
    outcomes = {}
    for label, has_label in fixtures["WIDGET_LABELS"].items():
        original = fixtures["tagged_signature_widget"](label)
        with pyropdf.open(original) as doc:
            del doc.form["Review"].obj["Contents"]
            saves = {"original": original, "full": doc.to_bytes(), "incremental": doc.to_bytes(incremental=True)}
        for mode, data in saves.items():
            path = directory / f"artifact-widget-{label}-{mode}.pdf"
            path.write_bytes(data)
            files[path] = "ua2"
            if mode != "original" and not has_label:
                outcomes[f"ua2 {path.name}"] = ("#1094", {"8.10.2.3-1"})

    with pyropdf.open(fixtures["tagged_signature_widget"]("legacy")) as doc:
        del doc.form["Review"].obj["Contents"]
        labeled = directory / "widget-role-map-original.pdf"
        labeled.write_bytes(doc.to_bytes())
        files[labeled] = "ua2"
    with pyropdf.open(labeled) as doc:
        doc.catalog["StructTreeRoot"]["RoleMap"] = {"ReviewLabel": pyropdf.Name("Span")}
        for mode, incremental in (("full", False), ("incremental", True)):
            path = directory / f"widget-role-map-{mode}.pdf"
            path.write_bytes(doc.to_bytes(incremental=incremental))
            files[path] = "ua2"
            outcomes[f"ua2 {path.name}"] = ("#1094", {"8.10.2.3-1"})
    return run(jar, files, outcomes=outcomes)


def negative_control(source: Path, flavour: str) -> tuple[bytes, str]:
    """Break one requirement, leaving the XMP claim, xref offsets and stream lengths intact."""
    if flavour.startswith("ua"):
        key = b"/Alt ("
        rule = "7.3-1" if flavour == "ua1" else "8.2.5.28.2-1"
    elif flavour == "4f":
        key, rule = b"/EmbeddedFiles ", "6.9-5"
    else:
        key = b"/OutputIntents "
        rule = "6.2.3.3-1" if flavour.startswith("1") else "6.2.4.3-2"
    data = source.read_bytes()
    if data.count(key) != 1:
        msg = f"{source} needs exactly one {key!r} for its negative control"
        raise ValueError(msg)
    return data.replace(key, b"/Z" + key[2:]), rule


def controls(jar: Path, directory: Path) -> list[tuple[str, Path]]:
    """Require the targeted rule to fail under each explicitly selected profile."""
    failures = []
    for source, flavour in [*FIXTURES.items(), *dual_archival(FIXTURES).items()]:
        data, rule = negative_control(source, flavour)
        path = directory / f"{source.stem}-without-required-key-{flavour}.pdf"
        path.write_bytes(data)
        report = details(jar, flavour, path)
        if not report.startswith(f"FAIL {path} ") or failed_rules(report) != {rule}:
            sys.stderr.write(f"expected {flavour} {rule} for the negative control, got {report}\n")
            failures.append((flavour, path))
    return failures


def source_matrix(jar: Path, directory: Path) -> list[tuple[str, Path]]:
    """Validate independent sources, negative controls and newly authored files in both save modes."""
    fixtures = runpy.run_path(str(ROOT / "tests/test_profile_sources.py"))
    negative = {
        "1a": "6.8.2.2-1",
        "1b": "6.2.3.3-2",
        "2a": "6.7.3.4-1",
        "2b": "6.1.13-11",
        "2u": "6.2.11.7.2-1",
        "3a": "6.7.3.4-1",
        "3b": "6.8-4",
        "3u": "6.2.11.7.2-1",
        "4": "6.2.4.3-2",
        "4e": "6.7.3-3",
        "4f": "6.9-5",
        "ua1": "7.1-3",
        "ua2": "8.4.5.7-2",
    }
    files = {}
    outcomes = {}
    for flavour, rule in negative.items():
        inputs = {k: fixtures["source"](flavour, k).read_bytes() for k in ("pass", "fail")}
        if flavour not in ("4", "ua2"):  # The dual PDF/A-4 and PDF/UA-2 authoring workflow belongs to #846.
            profile = "pdfua-1" if flavour == "ua1" else f"pdfa-{flavour}"
            with fixtures["authored"](profile) as doc:
                inputs["authored"] = doc.to_bytes()
        for kind, original in inputs.items():
            with pyropdf.open(original) as doc:
                doc.pages[0].rotation = 90
                saves = {"original": original, "full": doc.to_bytes(), "incremental": doc.to_bytes(incremental=True)}
            for mode, data in saves.items():
                path = directory / f"independent-{flavour}-{kind}-{mode}.pdf"
                path.write_bytes(data)
                files[path] = flavour
                if kind == "fail":
                    outcomes[f"{flavour} {path.name}"] = ("source corpus negative control", {rule})
    return run(jar, files, outcomes=outcomes)


def archival_colour_controls(jar: Path, directory: Path) -> list[tuple[str, Path]]:
    """Compare selected colours, actual painting, local defaults and transparency blending spaces."""
    fixtures = runpy.run_path(str(ROOT / "tests/test_archival_output_intents.py"))
    files = {}
    outcomes = {}
    for flavour, case, _device, rule in fixtures["CONTROLS"]:
        with fixtures["control"](flavour, case) as doc:
            original = doc.to_bytes()
        with pyropdf.open(original) as doc:
            doc.pages[0].rotation = 90
            saves = {"original": original, "full": doc.to_bytes(), "incremental": doc.to_bytes(incremental=True)}
        for mode, data in saves.items():
            path = directory / f"archival-colour-{case}-{mode}.pdf"
            path.write_bytes(data)
            files[path] = flavour
            if rule is not None:
                outcomes[f"{flavour} {path.name}"] = ("device colour use", {rule})
    return run(jar, files, outcomes=outcomes)


def main() -> int:
    """Validate the corpus and its saves, and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jar", type=Path, required=True, help="veraPDF's greenfield-apps jar")
    parser.add_argument("--arlington", type=Path, help="veraPDF's greenfield-apps-arlington jar")
    parser.add_argument(
        "--profile-sources-only", action="store_true", help="run the per-flavour source and authoring matrix"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        source_failures = source_matrix(args.jar.resolve(), Path(directory))
        if args.profile_sources_only:
            return int(bool(source_failures))
        files = saves(Path(directory))
        files.update(language_saves(Path(directory)))
        files.update(remove_signature_saves(Path(directory) / "pdfa-2b-sign_revision-incremental.pdf", Path(directory)))
        targeted = profile_saves(Path(directory))
        failures = source_failures + archival_colour_controls(args.jar.resolve(), Path(directory))
        failures += run(args.jar.resolve(), targeted)
        failures += run(args.jar.resolve(), dual_archival(targeted))
        failures += run(args.jar.resolve(), files)
        failures += run(args.jar.resolve(), dual_archival(files))
        media = archival_media_controls(Path(directory))
        failures += run(args.jar.resolve(), media)
        failures += run(args.jar.resolve(), dual_archival(media))
        failures += controls(args.jar.resolve(), Path(directory))
        failures += signature_widget_controls(args.jar.resolve(), Path(directory))
        failures += artifact_widget_label_controls(args.jar.resolve(), Path(directory))
        if args.arlington:
            models = {path: arlington(path, flavour) for path, flavour in files.items()}
            models.update(rich_media_saves(Path(directory)))
            failures += run(args.arlington.resolve(), models)
            failures += run(args.arlington.resolve(), pades_comparisons(models))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
