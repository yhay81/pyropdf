"""Recompress a native abi3 wheel without changing its installed files or RECORD."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from copy import copy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZipInfo

import zopfli
from wheel_size import Wheel, measure, only_wheel


def member_metadata(member: ZipInfo) -> tuple[object, ...]:
    """Fields that describe the installed member independently of its ZIP encoding."""
    return (
        member.filename,
        member.date_time,
        member.compress_type,
        member.create_system,
        member.external_attr,
        member.internal_attr,
        member.comment,
        member.extra,
    )


def verify_contents(original: ZipFile, candidate: ZipFile) -> None:
    """Use the standard ZIP reader to compare each original entry and its metadata."""
    if original.comment != candidate.comment or original.namelist() != candidate.namelist():
        msg = "recompression changed the wheel's archive comment or member list"
        raise ValueError(msg)
    for old, new in zip(original.infolist(), candidate.infolist(), strict=True):
        if member_metadata(old) != member_metadata(new) or original.read(old) != candidate.read(new):
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
            with zopfli.ZipFile(candidate, "w", iterations=1) as output:
                output.comment = original.comment
                for member in original.infolist():
                    output.writestr(copy(member), original.read(member), compress_type=member.compress_type)
            with ZipFile(candidate) as written:
                verify_contents(original, written)
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
