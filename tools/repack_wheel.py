"""Recompress a native abi3 wheel without changing its installed files or RECORD."""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
from copy import copy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZipInfo

import zopfli
from wheel_size import Wheel, measure, only_wheel

LOCAL_HEADER_SIZE = 30
CENTRAL_HEADER_SIZE = 46
END_SIZE = 22


def archive_metadata(path: Path, members: list[ZipInfo]) -> list[bytes] | None:
    """Read the physical headers, ignoring only compressed lengths and relocated offsets.

    Native release wheels have ordinary ZIP32 entries in directory order. A
    prefix, data descriptor, ZIP64 record or other layout is kept as supplied.
    Local extra fields can differ from the central copy exposed by ZipInfo.
    """
    headers = []
    with path.open("rb") as raw:
        for member in members:
            offset = raw.tell()
            local = raw.read(LOCAL_HEADER_SIZE)
            if (
                offset != member.header_offset
                or len(local) != LOCAL_HEADER_SIZE
                or not local.startswith(b"PK\x03\x04")
                or struct.unpack_from("<I", local, 18)[0] != member.compress_size
                or member.flag_bits & 8
            ):
                return None
            lengths = struct.unpack_from("<HH", local, 26)
            headers.append(local[:18] + local[22:] + raw.read(sum(lengths)))
            raw.seek(member.compress_size, 1)

        directory = raw.tell()
        for member in members:
            central = raw.read(CENTRAL_HEADER_SIZE)
            if (
                len(central) != CENTRAL_HEADER_SIZE
                or not central.startswith(b"PK\x01\x02")
                or struct.unpack_from("<I", central, 42)[0] != member.header_offset
            ):
                return None
            lengths = struct.unpack_from("<HHH", central, 28)
            headers.append(central[:20] + central[24:42] + raw.read(sum(lengths)))

        end = raw.read()
        if (
            len(end) < END_SIZE
            or not end.startswith(b"PK\x05\x06")
            or struct.unpack_from("<I", end, 16)[0] != directory
            or len(end) != END_SIZE + struct.unpack_from("<H", end, 20)[0]
        ):
            return None
        headers.append(end[:16] + end[20:])
    return headers


def verify_contents(original: ZipFile, candidate: ZipFile) -> None:
    """Reject a compressor that loses, reorders or changes an installed member."""
    if original.namelist() != candidate.namelist():
        msg = "recompression changed the wheel's member list"
        raise ValueError(msg)
    for old, new in zip(original.infolist(), candidate.infolist(), strict=True):
        if original.read(old) != candidate.read(new):
            msg = f"recompression changed wheel member {old.filename}"
            raise ValueError(msg)


def repack(path: Path) -> tuple[Wheel, Wheel]:
    """Install the smaller verified archive atomically and report actual artifact hashes."""
    before = measure(path)
    if before.tag.split("-")[1] != "abi3":
        msg = f"expected a native abi3 wheel, found {before.tag}"
        raise ValueError(msg)
    with TemporaryDirectory(dir=path.parent) as temporary:
        candidate = Path(temporary) / path.name
        with ZipFile(path) as original:
            headers = archive_metadata(path, original.infolist())
            if headers is None:
                if (bad := original.testzip()) is not None:
                    msg = f"incorrect CRC for wheel member {bad}"
                    raise ValueError(msg)
                return before, before
            with zopfli.ZipFile(candidate, "w", iterations=1) as output:
                output.comment = original.comment
                for member in original.infolist():
                    output.writestr(copy(member), original.read(member), compress_type=member.compress_type)
            with ZipFile(candidate) as written:
                verify_contents(original, written)
                if archive_metadata(candidate, written.infolist()) != headers:
                    return before, before
        after = measure(candidate)
        if after.size_bytes >= before.size_bytes:
            return before, before
        shutil.copymode(path, candidate)
        candidate.replace(path)
        return before, after


def main() -> None:
    """Report the original and delivered wheel sizes for release and comparison jobs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    before, after = repack(only_wheel(parser.parse_args().directory))
    sys.stdout.write(json.dumps({"original": asdict(before), "delivered": asdict(after)}, indent=2) + "\n")


if __name__ == "__main__":
    main()
