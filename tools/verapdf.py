"""Validate the PDF/A and PDF/UA corpora, and every save of every edit of them, with veraPDF.

CI runs this with veraPDF's greenfield-apps and greenfield-apps-arlington
jars, which it downloads pinned by their SHA-256:

    uv run python tools/verapdf.py --jar greenfield-apps-1.28.1.jar --arlington greenfield-apps-arlington-1.26.6.jar

Each fixture of tests/assets/pdfa and tests/assets/pdfua is validated under
its claimed flavour, as is each edit of the preservation harness's EDITS
applied to it and saved in full and incrementally. With ``--arlington``,
each of those files is also checked against the Arlington PDF Model of the
PDF version in its header, with the extensions that its PDF/A part adds to
that version. A file that veraPDF rejects must be listed in
``REJECTED`` with the issue that tracks it; the script exits with status 1
on a rejection that is not listed, and on a listed one that passes.
"""

from __future__ import annotations

import argparse
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

#: Verdicts that differ from acceptance, by flavour and file name, with the issue that tracks each.
REJECTED: dict[str, str] = {
    # PDF/A-1 forbids embedded files, and PDF/A-2 and PDF/A-4 allow only
    # embedded PDF/A files; doc.changes() reports these claims broken.
    **{
        f"{part} pdfa-{part}-{edit}_attachment-{mode}.pdf": "#301"
        for part in ("1b", "2b", "2u", "4")
        for edit in ("add", "write")
        for mode in ("full", "incremental")
    },
    # A deleted attachment leaves its file specification and embedded file
    # behind, and veraPDF checks every object.
    **{
        f"{part} pdfa-{part}-delete_attachment-{mode}.pdf": "#304"
        for part in ("1b", "2b", "2u", "3b", "4")
        for mode in ("full", "incremental")
    },
}

#: The Arlington extensions that a PDF/A part adds to the PDF version it is based on, by flavour.
EXTENSIONS = {"3b": "ISO_19005_3"}


def saves(directory: Path) -> dict[Path, str]:
    """Write every save of every edit of every fixture into ``directory``; return all files with their flavour."""
    out = dict(FIXTURES)
    for source, flavour in FIXTURES.items():
        for edit, apply in sorted(EDITS.items()):
            for incremental in (False, True):
                doc = pyropdf.open(source)
                apply(doc)
                mode = "incremental" if incremental else "full"
                path = directory / f"{source.stem}-{edit}-{mode}.pdf"
                path.write_bytes(doc.to_bytes(incremental=incremental))
                out[path] = flavour
    return out


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


def run(jar: Path, files: dict[Path, str]) -> list[tuple[str, Path]]:
    """Validate ``files`` under their flavours; print each verdict and return the unexpected ones."""
    failures = []
    for flavour, paths in by_flavour(files).items():
        verdicts = validate(jar, flavour, paths)
        for path in paths:
            accepted = verdicts[path.name]
            key = f"{flavour} {path.name}"
            note = f" ({REJECTED[key]})" if key in REJECTED else ""
            sys.stdout.write(f"{'PASS' if accepted else 'FAIL'} {key}{note}\n")
            if accepted == (key in REJECTED):
                failures.append((flavour, path))
    for flavour, path in failures:
        sys.stdout.write(f"\nunexpected verdict for {path.name} under {flavour}:\n{details(jar, flavour, path)}\n")
    return failures


def main() -> int:
    """Validate the corpus and its saves, and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jar", type=Path, required=True, help="veraPDF's greenfield-apps jar")
    parser.add_argument("--arlington", type=Path, help="veraPDF's greenfield-apps-arlington jar")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        files = saves(Path(directory))
        failures = run(args.jar.resolve(), files)
        if args.arlington:
            failures += run(
                args.arlington.resolve(), {path: arlington(path, flavour) for path, flavour in files.items()}
            )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
