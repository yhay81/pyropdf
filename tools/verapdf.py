"""Validate the PDF/A corpus, and every save of every edit of it, with veraPDF.

CI runs this with veraPDF's greenfield-apps jar, which it downloads pinned by
its SHA-256:

    uv run python tools/verapdf.py --jar greenfield-apps-1.28.1.jar

Each fixture of tests/assets/pdfa is validated under its claimed flavour, as
is each edit of the preservation harness's EDITS applied to it and saved in
full and incrementally. A save that veraPDF rejects must be listed in
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

PDFA = ROOT / "tests" / "assets" / "pdfa"
FLAVOURS: dict[str, tuple[str, str, str]] = runpy.run_path(str(PDFA / "generate.py"))["FLAVOURS"]

#: Saves that veraPDF rejects, by file name, with the issue that tracks each.
REJECTED: dict[str, str] = {}


def saves(directory: Path) -> dict[str, list[Path]]:
    """Write every fixture and every save of every edit of it into ``directory``, by flavour."""
    out: dict[str, list[Path]] = {}
    for name, (_, _, flavour) in FLAVOURS.items():
        source = PDFA / name
        paths = [source]
        for edit, apply in sorted(EDITS.items()):
            for incremental in (False, True):
                doc = pyropdf.open(source)
                apply(doc)
                mode = "incremental" if incremental else "full"
                path = directory / f"{source.stem}-{edit}-{mode}.pdf"
                path.write_bytes(doc.to_bytes(incremental=incremental))
                paths.append(path)
        out[flavour] = paths
    return out


def validate(jar: Path, flavour: str, paths: list[Path]) -> dict[str, bool]:
    """Return whether veraPDF accepts each of ``paths`` under ``flavour``, by file name."""
    command = ["java", "-cp", str(jar), "org.verapdf.cli.VeraPdfCli", "--flavour", flavour, "--format", "text"]
    result = subprocess.run([*command, *map(str, paths)], capture_output=True, text=True, check=False)  # noqa: S603 - a fixed command on generated files.
    verdicts: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        verdict, _, rest = line.partition(" ")
        if verdict in {"PASS", "FAIL"}:
            verdicts[Path(rest.split(" ")[0]).name] = verdict == "PASS"
    missing = [path.name for path in paths if path.name not in verdicts]
    if missing:
        sys.stderr.write(f"{result.stdout}\n{result.stderr}\n")
        msg = f"veraPDF reported no verdict for {missing}"
        raise SystemExit(msg)
    return verdicts


def details(jar: Path, flavour: str, path: Path) -> str:
    """Return veraPDF's text report of the rules that ``path`` fails."""
    command = ["java", "-cp", str(jar), "org.verapdf.cli.VeraPdfCli", "--flavour", flavour, "--format", "text"]
    result = subprocess.run([*command, "-v", str(path)], capture_output=True, text=True, check=False)  # noqa: S603 - a fixed command on a generated file.
    return result.stdout


def main() -> int:
    """Validate the corpus and its saves, and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jar", type=Path, required=True, help="veraPDF's greenfield-apps jar")
    jar = parser.parse_args().jar.resolve()
    failures = []
    with tempfile.TemporaryDirectory() as directory:
        for flavour, paths in saves(Path(directory)).items():
            verdicts = validate(jar, flavour, paths)
            for path in paths:
                accepted = verdicts[path.name]
                listed = path.name in REJECTED
                note = f" ({REJECTED[path.name]})" if listed else ""
                sys.stdout.write(f"{'PASS' if accepted else 'FAIL'} {flavour} {path.name}{note}\n")
                if accepted == listed:
                    failures.append((flavour, path))
        for flavour, path in failures:
            sys.stdout.write(f"\nunexpected verdict for {path.name} under {flavour}:\n{details(jar, flavour, path)}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
