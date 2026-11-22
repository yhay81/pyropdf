# /// script
# requires-python = ">=3.11"
# dependencies = ["pefile==2024.8.26"]
# ///
"""Compare two Windows wheel images after accounting for the linker's build identifiers."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from importlib import import_module
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile


def link_fields(wheel: Path) -> dict[str, object]:
    """Read the PE timestamps and CodeView GUID and hash the rest of the image."""
    pefile = import_module("pefile")
    with ZipFile(wheel) as archive:
        image = bytearray(archive.read("pyropdf/_core.pyd"))
    pe = pefile.PE(data=bytes(image), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DEBUG"]])
    entries = pe.DIRECTORY_ENTRY_DEBUG
    times = [pe.FILE_HEADER.get_field_absolute_offset("TimeDateStamp")]
    times.extend(entry.struct.get_field_absolute_offset("TimeDateStamp") for entry in entries)
    (codeview,) = (
        entry.struct for entry in entries if entry.struct.Type == pefile.DEBUG_TYPE["IMAGE_DEBUG_TYPE_CODEVIEW"]
    )
    start = codeview.PointerToRawData
    if image[start : start + 4] != b"RSDS" or any(
        entry.struct.TimeDateStamp != pe.FILE_HEADER.TimeDateStamp for entry in entries
    ):
        msg = f"unexpected linker debug records in {wheel.name}"
        raise ValueError(msg)
    guid = start + 4
    pdb_id = UUID(bytes_le=bytes(image[guid : guid + 16]))
    for offset in times:
        image[offset : offset + 4] = bytes(4)
    image[guid : guid + 16] = bytes(16)
    return {
        "size": len(image),
        "timestamp": pe.FILE_HEADER.TimeDateStamp,
        "timestamp_offsets": times,
        "pdb_guid": str(pdb_id),
        "pdb_guid_offset": guid,
        "remaining_image_sha256": hashlib.sha256(image).hexdigest(),
    }


def main() -> None:
    """Print the decoded fields and check every remaining byte of the two images."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("head", type=Path)
    args = parser.parse_args()
    base, head = link_fields(args.base), link_fields(args.head)
    sys.stdout.write(json.dumps({"base": base, "head": head}, indent=2) + "\n")
    if base["remaining_image_sha256"] != head["remaining_image_sha256"]:
        msg = "the Windows images also differ outside the linker identifiers"
        raise ValueError(msg)


if __name__ == "__main__":
    main()
