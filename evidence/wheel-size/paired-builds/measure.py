"""Fingerprint installed native-wheel contents after validating every RECORD entry."""

# ruff: noqa: INP001 -- standalone measurement script
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import sys
from pathlib import Path
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recompression import record_entries


def _digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


def _sbom_bytes(data: bytes, checkout: str | None) -> bytes:
    sbom = json.loads(data)
    del sbom["serialNumber"], sbom["metadata"]["timestamp"]
    if checkout:
        main = sbom["metadata"]["component"]
        root, separator, package = main["bom-ref"].rpartition(f"/{checkout}/crates/")
        if not root.startswith("path+file:///") or not package.startswith("pyropdf-python#"):
            msg = f"missing local {checkout} crate reference in {main['bom-ref']}"
            raise ValueError(msg)
        prefix = root + separator

        def local_ref(ref: str) -> str:
            return root + "/{checkout}/crates/" + ref[len(prefix) :] if ref.startswith(prefix) else ref

        for component in [main, *main.get("components", []), *sbom["components"]]:
            if "bom-ref" in component:
                component["bom-ref"] = local_ref(component["bom-ref"])
        for dependency in sbom["dependencies"]:
            dependency["ref"] = local_ref(dependency["ref"])
            if "dependsOn" in dependency:
                dependency["dependsOn"] = [local_ref(ref) for ref in dependency["dependsOn"]]
    return json.dumps(sbom, sort_keys=True, separators=(",", ":")).encode()


def inspect_wheel(path: Path, checkout: str | None = None) -> dict[str, str | int]:
    """Compare validated installed files, optionally accounting for the hosted checkout location."""
    with ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            msg = f"duplicate member in {path.name}"
            raise ValueError(msg)
        record_entries(archive)
        (record,) = (name for name in names if name.endswith(".dist-info/RECORD"))
        (native,) = (name for name in names if name.startswith("pyropdf/_core.") and name.endswith((".so", ".pyd")))
        rows = list(csv.reader(io.StringIO(archive.read(record).decode(), newline="")))

        # Preserve the other entries and their order after checking the original RECORD.
        for row in rows:
            name = row[0]
            if ".dist-info/sboms/" in name and name.endswith(".cyclonedx.json"):
                normalized = _sbom_bytes(archive.read(name), checkout)
                row[1:] = ["sha256=" + _digest(normalized), str(len(normalized))]
        manifest = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
        return {
            "name": path.name,
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "normalized_sha256": hashlib.sha256(manifest).hexdigest(),
            "native_sha256": hashlib.sha256(archive.read(native)).hexdigest(),
            "native_compressed_size": archive.getinfo(native).compress_size,
            "members": len(names),
        }


def main() -> None:
    """Print the fingerprints as a JSON array."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", type=Path, nargs="+")
    parser.add_argument("--checkout", choices=("base", "head"), help="account for local crate IDs in the hosted SBOM")
    args = parser.parse_args()
    sys.stdout.write(json.dumps([inspect_wheel(path, args.checkout) for path in args.wheels], indent=2) + "\n")


if __name__ == "__main__":
    main()
