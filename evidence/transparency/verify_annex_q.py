"""Compare the public Annex Q predicate with veraPDF's PDF/A-4 page rule."""  # noqa: INP001 - evidence script.

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pyropdf

_ROOT = Path(__file__).resolve().parents[2]
_RULE = "details/rule[@specification='ISO 19005-4:2020'][@clause='6.2.9'][@testNumber='2'][@status='failed']"


def fixtures() -> dict[str, tuple[bytes, bool]]:
    """Reuse the exact public paint, object and annotation controls."""
    sys.path.insert(0, str(_ROOT / "tests"))
    from test_page_transparency import _SINGLE, _nested, _vector  # noqa: PLC0415 - shared test corpus.

    cases = {name: (_vector(name), expected) for name, (_, expected) in _SINGLE.items()}
    for name in ("appearance", "annotation-blend", "form", "group", "pattern", "type3", "soft-image"):
        cases[name] = _nested(name), True
    return cases


def validate(jar: Path, directory: Path) -> dict[str, bool]:
    """Read only normally completed PDF/A-4 jobs from the validator's local XML."""
    command = ["java", "-cp", str(jar), "org.verapdf.apps.GreenfieldCliWrapper", "--flavour", "4", "--format", "xml"]
    completed = subprocess.run(  # noqa: S603 - explicit validator and generated fixtures.
        [*command, str(directory)], capture_output=True, check=False, timeout=120
    )
    if completed.returncode not in (0, 1):
        raise SystemExit(completed.stderr.decode(errors="replace"))
    verdicts = {}
    for job in ET.fromstring(completed.stdout).findall("./jobs/job"):  # noqa: S314 - local validator output.
        name = Path(job.findtext("item/name", "")).name
        report = job.find("validationReport[@jobEndStatus='normal'][@profileName='PDF/A-4 validation profile']")
        if report is None or report.find("details") is None or name in verdicts:
            msg = f"Missing, duplicated or incomplete PDF/A-4 validation for {name}"
            raise SystemExit(msg)
        verdicts[name] = report.find(_RULE) is not None
    return verdicts


def main() -> None:
    """Compare original, full and incremental saves with the controlled predicate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, required=True, help="pinned veraPDF 1.28.2 CLI jar")
    args = parser.parse_args()
    expected = {}
    with tempfile.TemporaryDirectory() as scratch:
        temp = Path(scratch)
        for name, (source, truth) in fixtures().items():
            with pyropdf.open(source) as doc:
                versions = {"original": source, "full": doc.to_bytes(), "incremental": doc.to_bytes(incremental=True)}
            for mode, data in versions.items():
                path = temp / f"{name}-{mode}.pdf"
                path.write_bytes(data)
                with pyropdf.open(data) as saved:
                    if saved.pages[0].uses_transparency() != truth:
                        msg = f"Public predicate disagrees with the control for {path.name}"
                        raise SystemExit(msg)
                expected[path.name] = truth
        actual = validate(args.jar.resolve(), temp)
    if set(actual) != set(expected):
        msg = f"Validator file set differs: {sorted(set(actual) ^ set(expected))}"
        raise SystemExit(msg)
    differences = [name for name in sorted(expected) if actual[name] != expected[name]]
    # veraPDF 1.28.2 misses alpha at inline images. The public raster test
    # measures that alpha independently with Poppler on the same source.
    known = [f"inline-image-{mode}.pdf" for mode in ("full", "incremental", "original")]
    if differences != known:
        msg = f"Unexpected validator differences: {differences}"
        raise SystemExit(msg)
    sys.stdout.write(f"{len(expected) - len(known)}/{len(expected)} predicate agreements.\n")
    sys.stdout.write(f"Pinned veraPDF reports opaque for the {len(known)} transparent inline-image saves.\n")


if __name__ == "__main__":
    main()
