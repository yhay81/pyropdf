# ruff: noqa: INP001 - standalone evidence script.
"""Independently verify that two encodings of a release wheel install the same files."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import struct
import sys
from pathlib import Path
from zipfile import ZipFile, ZipInfo


def record_entries(archive: ZipFile) -> int:
    """Verify every RECORD entry against the actual uncompressed wheel members."""
    (record,) = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
    checked = set()
    for name, digest, size in csv.reader(io.StringIO(archive.read(record).decode("utf-8"))):
        if name in checked:
            msg = f"repeated RECORD entry for {name}"
            raise ValueError(msg)
        if name == record and digest == size == "":
            checked.add(name)
            continue
        data = archive.read(name)
        algorithm, separator, expected = digest.partition("=")
        calculated = hashlib.new(algorithm, data)
        if not separator or calculated.digest_size < hashlib.sha256().digest_size:
            msg = f"unsupported RECORD digest {algorithm} for {name}"
            raise ValueError(msg)
        actual = base64.urlsafe_b64encode(calculated.digest()).rstrip(b"=").decode("ascii")
        if expected != actual or size != str(len(data)):
            msg = f"incorrect RECORD entry for {name}"
            raise ValueError(msg)
        checked.add(name)
    members = {member.filename for member in archive.infolist() if not member.is_dir()}
    if checked != members - {record + ".jws", record + ".p7s"}:
        msg = "RECORD and wheel membership differ"
        raise ValueError(msg)
    return len(checked)


def describe(path: Path) -> dict[str, int | str]:
    """Report the archive hash and its compressed native contribution."""
    with path.open("rb") as stream, ZipFile(path) as archive:
        (extension,) = [
            member
            for member in archive.infolist()
            if member.filename.startswith("pyropdf/_core.") and member.filename.endswith((".pyd", ".so"))
        ]
        return {
            "bytes": path.stat().st_size,
            "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
            "extension_bytes": extension.compress_size,
            "record_entries": record_entries(archive),
        }


def local_metadata(path: Path, member: ZipInfo) -> bytes:
    """Read the local ZIP header and its original name and extra fields."""
    with path.open("rb") as archive:
        archive.seek(member.header_offset)
        fixed = archive.read(30)
        if not fixed.startswith(b"PK\x03\x04"):
            msg = f"missing local header for {member.filename}"
            raise ValueError(msg)
        name_size, extra_size = struct.unpack_from("<HH", fixed, 26)
        return fixed[:18] + fixed[22:] + archive.read(name_size + extra_size)


def main() -> None:
    """Compare member order, methods, attributes and payloads with the standard ZIP reader."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("delivered", type=Path)
    parser.add_argument("--source", required=True, help="URL of the original release job or action artifact")
    args = parser.parse_args()
    with ZipFile(args.original) as old, ZipFile(args.delivered) as new:
        if old.namelist() != new.namelist() or old.comment != new.comment:
            msg = "archive members or comment differ"
            raise ValueError(msg)
        for left, right in zip(old.infolist(), new.infolist(), strict=True):
            attributes = (
                "orig_filename",
                "date_time",
                "compress_type",
                "create_system",
                "create_version",
                "extract_version",
                "reserved",
                "flag_bits",
                "volume",
                "external_attr",
                "internal_attr",
                "extra",
                "comment",
            )
            if (
                any(getattr(left, field) != getattr(right, field) for field in attributes)
                or local_metadata(args.original, left) != local_metadata(args.delivered, right)
                or old.read(left) != new.read(right)
            ):
                msg = f"different member {left.filename}"
                raise ValueError(msg)
    result = {
        "source": args.source,
        "wheel": args.original.name,
        "original": describe(args.original),
        "delivered": describe(args.delivered),
        "saved_bytes": args.original.stat().st_size - args.delivered.stat().st_size,
    }
    sys.stdout.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
