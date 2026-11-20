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


# Latent profile consequences tracked in #848 are constrained to the exact
# operation, profile, mode and rule. In particular, a full save invalidates
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
                case.apply(doc)
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


def arlington(path: Path, flavour: str) -> str:
    """Validate the effective version, including a catalog promotion in a later revision."""
    with path.open("rb") as source:
        header = source.read(8)[5:8].decode("ascii")
    with pyropdf.open(path) as doc:
        declared = doc.catalog.get("Version")
        extensions = doc.catalog.get("Extensions")
        pades = extensions.get("ETSI_PAdES") if isinstance(extensions, pyropdf.Dictionary) else None
        declares_pades = (
            isinstance(pades, pyropdf.Dictionary)
            and pades.get("BaseVersion") == pyropdf.Name("1.7")
            and pades.get("ExtensionLevel") == PADES_LEVEL
        )
    # Valid PDF version names are ordered by their one-digit components.
    effective = (
        max(header, str(declared))
        if isinstance(declared, pyropdf.Name) and re.fullmatch(r"1\.[0-7]|2\.0", str(declared))
        else header
    )
    version = "arlington" + effective
    names = [EXTENSIONS[flavour]] if flavour in EXTENSIONS else []
    if declares_pades:
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


def validate(jar: Path, flavour: str, paths: list[Path]) -> dict[str, bool]:
    """Return whether veraPDF accepts each of ``paths`` under ``flavour``, by file name."""
    command = cli(jar, flavour)
    result = subprocess.run([*command, *map(str, paths)], capture_output=True, text=True, check=False)  # noqa: S603 - a fixed command on generated files.
    verdicts: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        verdict, _, rest = line.partition(" ")
        # The Arlington profiles report a deviation from the model as DEVIATION.
        if verdict in {"PASS", "FAIL", "DEVIATION"}:
            verdicts[Path(rest.rpartition(" ")[0]).name] = verdict == "PASS"
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
            # The text header ends with its flavour; paths may contain spaces.
            source = Path(line.partition(" ")[2].rpartition(" ")[0])
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


def run(jar: Path, files: dict[Path, str]) -> list[tuple[str, Path]]:
    """Verify the verdict and the complete failed-rule set under each flavour."""
    failures = []
    for flavour, paths in by_flavour(files).items():
        verdicts = validate(jar, flavour, paths)
        reports = detailed_reports(jar, flavour, [path for path in paths if not verdicts[path.name]])
        for path in paths:
            accepted = verdicts[path.name]
            key = f"{flavour} {path.name}"
            expected = EXPECTED.get(key)
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


def main() -> int:
    """Validate the corpus and its saves, and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jar", type=Path, required=True, help="veraPDF's greenfield-apps jar")
    parser.add_argument("--arlington", type=Path, help="veraPDF's greenfield-apps-arlington jar")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        files = saves(Path(directory))
        files.update(language_saves(Path(directory)))
        failures = run(args.jar.resolve(), files)
        failures += run(args.jar.resolve(), dual_archival(files))
        failures += controls(args.jar.resolve(), Path(directory))
        if args.arlington:
            models = {path: arlington(path, flavour) for path, flavour in files.items()}
            failures += run(args.arlington.resolve(), models)
            failures += run(args.arlington.resolve(), pades_comparisons(models))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
