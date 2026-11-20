"""Measure the compressed distributable and enforce #258 on native release wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from email.parser import BytesHeaderParser
from pathlib import Path
from zipfile import ZipFile

MIB = 1024 * 1024
MAX_BYTES = 9 * MIB
MAX_GROWTH = MIB // 4
LICENSE_FILES = ("LICENSE", "NOTICE", "tools/arlington/model/LICENSE")
ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Wheel:
    """A distributable and the compressed size of its compiled extension."""

    filename: str
    tag: str
    sha256: str
    size_bytes: int
    size_mib: float
    extension_bytes: int


def measure(path: Path) -> Wheel:
    """Identify a native wheel and measure its on-disk bytes, including ZIP overhead."""
    _, python, abi, platform = path.stem.rsplit("-", 3)
    size = path.stat().st_size
    with path.open("rb") as stream, ZipFile(stream) as archive:
        (extension,) = [
            i
            for i in archive.infolist()
            if i.filename.startswith("pyropdf/_core.") and i.filename.endswith((".so", ".pyd"))
        ]
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return Wheel(path.name, f"{python}-{abi}-{platform}", digest, size, round(size / MIB, 6), extension.compress_size)


def check_notices(path: Path) -> None:
    """Verify the declared license and the shipped notice text against the checkout."""
    with ZipFile(path) as archive:
        (metadata,) = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        headers = BytesHeaderParser().parsebytes(archive.read(metadata))
        if headers["License-Expression"] != "MIT" or not set(LICENSE_FILES) <= set(headers.get_all("License-File", [])):
            msg = f"{path.name}: expected MIT and the project and third-party license files in METADATA"
            raise ValueError(msg)
        for name in LICENSE_FILES:
            contents = archive.read(f"{metadata.removesuffix('METADATA')}licenses/{name}")
            if contents.splitlines() != (ROOT / name).read_bytes().splitlines():
                msg = f"{path.name}: incorrect contents of {name}"
                raise ValueError(msg)


def only_wheel(directory: Path) -> Path:
    """Require each build to produce exactly one pyropdf wheel."""
    wheels = sorted(directory.glob("pyropdf-*.whl"))
    if len(wheels) != 1:
        msg = f"expected one pyropdf wheel in {directory}, found {len(wheels)}"
        raise ValueError(msg)
    return wheels[0]


def check_size(head: Wheel, abi: str, base: Wheel | None, justification: str) -> None:
    """Apply the absolute cap and PR growth allowance only to core abi3 artifacts."""
    if head.tag.split("-")[1] != abi:
        msg = f"expected {abi}, found {head.tag}"
        raise ValueError(msg)
    if base and base.tag != head.tag:
        msg = f"incomparable wheel tags: {base.tag} and {head.tag}"
        raise ValueError(msg)
    if abi != "abi3":
        return
    if head.size_bytes > MAX_BYTES:
        msg = f"{head.filename} exceeds the {MAX_BYTES}-byte (9 MiB) abi3 budget"
        raise ValueError(msg)
    if base and head.size_bytes - base.size_bytes > MAX_GROWTH and not justification:
        msg = 'growth exceeds 0.25 MiB; add "Wheel growth justification: <reason>" to the PR description'
        raise ValueError(msg)


def main() -> None:
    """Print the measured wheels and fail when a release requirement is unmet."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--abi", choices=["abi3", "cp314t", "cp315t"], required=True)
    parser.add_argument("--base", type=Path, help="directory containing the PR base wheel built on the same runner")
    args = parser.parse_args()
    path = only_wheel(args.directory)
    head = measure(path)
    base = measure(only_wheel(args.base)) if args.base else None
    match = re.search(r"^Wheel growth justification:[ \t]*(\S[^\r\n]*)", os.getenv("PR_BODY", ""), re.MULTILINE)
    reason = match[1].strip() if match else ""
    report = {"head": asdict(head)}
    if base:
        report.update(base=asdict(base), growth_bytes=head.size_bytes - base.size_bytes)
        if reason:
            report["justification"] = reason
    sys.stdout.write(json.dumps(report, ensure_ascii=True, indent=2) + "\n")
    check_notices(path)
    check_size(head, args.abi, base, reason)


if __name__ == "__main__":
    main()
