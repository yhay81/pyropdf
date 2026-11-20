"""Validate the PDF/A and PDF/UA corpora, and every save of every edit of them, with veraPDF.

CI runs this with veraPDF's greenfield-apps and greenfield-apps-arlington
jars, which it downloads pinned by their SHA-256:

    uv run python tools/verapdf.py --jar greenfield-apps-1.28.2.jar --arlington greenfield-apps-arlington-1.28.2.jar

Each fixture of tests/assets/pdfa and tests/assets/pdfua is validated under
its claimed flavour, as is each edit of the preservation harness's EDITS
applied to it and saved in full and incrementally. With ``--arlington``,
each of those files is also checked against the Arlington PDF Model of the
PDF version in its header, with the extensions that its PDF/A part adds to
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
}

DUAL = "pdfua-2-pdfa-4"
UA2_ANNOTATIONS = {
    edit: ({"8.10.2.3-1", "8.9.3.3-1"} if edit.endswith("field") else {"8.9.2.3-1", "8.9.3.3-1"})
    for edit in UNTAGGED_RULES
}

# Every expected rejection of an edit is scoped to the source, profile,
# operation, save mode and exact failed rules. A new rule or a surprise pass fails CI.
EXPECTED: dict[str, tuple[str, set[str]]] = {}


def expect(source: str, flavour: str, edits: dict[str, set[str]], issue: str) -> None:
    """Record both saves of the named operations under one source and profile."""
    for edit, rules in edits.items():
        for mode in ("full", "incremental"):
            EXPECTED[f"{flavour} {source}-{edit}-{mode}.pdf"] = (issue, rules)


for variant in ("1a", "1b", "2a", "2b", "2u", "4"):
    rules = {"1": {"6.1.11-1", "6.1.11-2"}, "2": {"6.8-5"}, "4": {"6.9-3"}}[variant[0]]
    expect(f"pdfa-{variant}", variant, {"add_attachment": rules, "write_attachment": rules}, "#301")
expect(DUAL, "4", {"add_attachment": {"6.9-3"}, "write_attachment": {"6.9-3"}}, "#301")
expect("pdfua-1", "ua1", UNTAGGED_RULES, "#794")
expect("pdfua-1", "ua1", {"remove-language": {"7.2-22", "7.2-33"}}, "#847")
for source in ("pdfua-2", DUAL):
    expect(source, "ua2", UA2_ANNOTATIONS, "#848")
    expect(source, "ua2", {"add_attachment": {"8.14.1-1"}, "write_attachment": {"8.14.1-1"}}, "#848")
    expect(source, "ua2", {"delete_page": {"8.2.5.2-1"}}, "#848")


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
    """Return the Arlington flavour of the PDF version in the header of ``path``, with ``flavour``'s extensions."""
    version = "arlington" + path.read_bytes()[5:8].decode("ascii")
    return f"{version}+{EXTENSIONS[flavour]}" if flavour in EXTENSIONS else version


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
            verdicts[Path(rest.split(" ")[0]).name] = verdict == "PASS"
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


def failed_rules(report: str) -> set[str]:
    """Read the rule identifiers from a verbose veraPDF text report."""
    return set(re.findall(r"^\s+FAIL (\S+)\s*$", report, re.MULTILINE))


def run(jar: Path, files: dict[Path, str]) -> list[tuple[str, Path]]:
    """Validate ``files`` under their flavours; print each verdict and return the unexpected ones."""
    failures = []
    for flavour, paths in by_flavour(files).items():
        verdicts = validate(jar, flavour, paths)
        for path in paths:
            accepted = verdicts[path.name]
            key = f"{flavour} {path.name}"
            expected = EXPECTED.get(key)
            note = f" ({expected[0]})" if expected else ""
            sys.stdout.write(f"{'PASS' if accepted else 'FAIL'} {key}{note}\n")
            if accepted == bool(expected) or (expected and failed_rules(details(jar, flavour, path)) != expected[1]):
                failures.append((flavour, path))
    for flavour, path in failures:
        sys.stdout.write(f"\nunexpected verdict for {path.name} under {flavour}:\n{details(jar, flavour, path)}\n")
    return failures


def dual_archival(files: dict[Path, str]) -> dict[Path, str]:
    """Include the same source and every save in the independent PDF/A-4 run."""
    return {path: "4" for path in files if path.stem == DUAL or path.stem.startswith(DUAL + "-")}


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
            failures += run(
                args.arlington.resolve(), {path: arlington(path, flavour) for path, flavour in files.items()}
            )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
