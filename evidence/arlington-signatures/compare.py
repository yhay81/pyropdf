"""Compare PDF 2.0 signature rules with and without the ETSI extension model."""

# ruff: noqa: INP001, S101 - stand-alone executable evidence with contract assertions.

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pyropdf

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from test_signing import DER, DOCUMENT, _pem_signer, make_pki

RESPONSE = bytes.fromhex("30030a0106")
OCSP_RULE = "VRI-OCSP-Array-9"
CADES_RULE = "Signature-SubFilter-Name-ETSI.CAdES.detached-27"


def examples(directory: Path) -> None:
    """Keep one control without a VRI, then add the same OCSP response to the signed revision."""
    pki = make_pki()
    trust = [pki.root.public_bytes(DER)]
    with pyropdf.open(DOCUMENT) as doc:
        signed = doc.sign("Example", _pem_signer(pki))

    with pyropdf.open(signed) as doc:
        (signature,) = doc.signatures
        assert signature.verify(trust)
        assert signature.xref is not None
        sig_obj = doc.objects[signature.xref]
        assert sig_obj["SubFilter"] == "ETSI.CAdES.detached"
        key = pyropdf.vri_key(bytes(sig_obj["Contents"]))
        store = pyropdf.SecurityStore(ocsps=(RESPONSE,), vri={key: pyropdf.ValidationInfo(ocsps=(RESPONSE,))})
        assert "Extensions" not in doc.catalog
        for case in ("signature", "nonempty", "empty"):
            if case == "nonempty":
                doc.security_store = store
            if case == "empty":
                doc.catalog["DSS"]["VRI"][key]["OCSP"] = []
            for mode in ("full", "incremental"):
                incremental = mode == "incremental"
                data = doc.to_bytes(incremental=incremental)
                with pyropdf.open(data) as check:
                    assert bool(check.signatures[0].verify(trust)) is incremental
                    if case != "signature":
                        dss = check.catalog["DSS"]
                        entries = dss["VRI"][key]["OCSP"]
                        assert len(entries) == (case == "nonempty")
                        if entries:
                            assert entries[0].xref == dss["OCSPs"][0].xref
                            assert entries[0].read() == RESPONSE
                (directory / f"{case}-{mode}.pdf").write_bytes(data)


def compare(jar: Path, directory: Path, *, extension: bool) -> None:
    """Require exact verbose rule IDs on every input, including the empty-array control."""
    paths = sorted(directory.glob("*.pdf"))
    command = ["java", "-cp", str(jar), "org.verapdf.apps.GreenfieldCliWrapper", "--flavour", "arlington2.0"]
    if extension:
        command.extend(["--extensions", "ETSI_PAdES"])
    result = subprocess.run(  # noqa: S603 - named local validator on generated fixtures
        [*command, "--format", "text", "-v", *map(str, paths)], text=True, capture_output=True, check=False
    )
    reports: dict[str, set[str]] = {}
    current: set[str] | None = None
    for line in result.stdout.splitlines():
        if row := re.fullmatch(r"(PASS|DEVIATION|FAIL) (.+?\.pdf) arlington2\.0.*", line):
            current = set()
            reports[Path(row[2]).name] = current
            if row[1] != "PASS":
                current.add(row[1])
        elif current is not None and (rule := re.fullmatch(r"\s+(?:FAIL|DEVIATION) (\S+)", line)):
            current.add(rule[1])
    assert set(reports) == {path.name for path in paths}, (result.stdout, result.stderr)
    for path in paths:
        actual = reports[path.name]
        rules = actual - {"FAIL", "DEVIATION"}
        expected = set() if extension else {CADES_RULE}
        if path.name.startswith("empty-") or (not extension and path.name.startswith("nonempty-")):
            expected.add(OCSP_RULE)
        summary = ", ".join(sorted(actual)) or "PASS"
        model = "ETSI_PAdES" if extension else "base"
        sys.stdout.write(f"{jar.name} {model} {path.name} {summary}\n")
        assert rules == expected, (path.name, actual, expected)
        assert bool(actual) == bool(expected), (path.name, actual, expected)


def main() -> None:
    """Run both models of each supplied jar against the same bytes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jar", nargs="+", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory() as temp:
        directory = Path(temp)
        examples(directory)
        for jar in args.jar:
            compare(jar.resolve(), directory, extension=False)
            compare(jar.resolve(), directory, extension=True)


if __name__ == "__main__":
    main()
