"""Rebuild the small virtual gray and RGB printing conditions used in the structural tests."""

from __future__ import annotations

import argparse
import itertools
import runpy
import struct
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def gray_profile() -> bytes:
    """Use a D50 media white point and gamma 2.2 for the ICC v2 monochrome output model."""
    template = runpy.run_path(str(HERE.parent / "pdfa" / "generate.py"))["icc"]()
    count = int.from_bytes(template[128:132], "big")
    tags = {}
    for index in range(count):
        signature, offset, size = struct.unpack_from(">4sII", template, 132 + index * 12)
        tags[signature] = template[offset : offset + size]
    # These are the four required tags of the monochrome output profile.
    description = b"pyropdf monochrome output\0"
    entries = (
        (b"desc", b"desc" + bytes(4) + struct.pack(">I", len(description)) + description + bytes(78)),
        (b"cprt", tags[b"cprt"]),
        (b"wtpt", tags[b"wtpt"]),
        (b"kTRC", tags[b"rTRC"]),
    )
    raw = bytearray(template[:128]) + struct.pack(">I", len(entries)) + bytes(12 * len(entries))
    raw[12:20] = b"prtrGRAY"
    for index, (signature, body) in enumerate(entries):
        struct.pack_into(">4sII", raw, 132 + 12 * index, signature, len(raw), len(body))
        raw.extend(body)
        raw.extend(bytes(-len(raw) % 4))
    struct.pack_into(">I", raw, 0, len(raw))
    return bytes(raw)


def rgb_measurements() -> str:
    """Model an RGB printing condition on a 5×5×5 grid using D50-adapted primaries."""
    lines = []
    for number, device in enumerate(itertools.product(range(5), repeat=3), 1):
        r, g, b = ((component / 4) ** 2.2 for component in device)
        xyz = (
            0.4361 * r + 0.3851 * g + 0.1431 * b,
            0.2225 * r + 0.7169 * g + 0.0606 * b,
            0.0139 * r + 0.0971 * g + 0.7142 * b,
        )
        values = [*(component * 25 for component in device), *(100 * value for value in xyz)]
        lines.append(f"{number} " + " ".join(f"{value:.6f}" for value in values))
    return (
        """CTI3
DESCRIPTOR "pyropdf virtual RGB output measurements"
ORIGINATOR "pyropdf"
DEVICE_CLASS "OUTPUT"
COLOR_REP "RGB_XYZ"
NUMBER_OF_FIELDS 7
BEGIN_DATA_FORMAT
SAMPLE_ID RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z
END_DATA_FORMAT
NUMBER_OF_SETS 125
BEGIN_DATA
"""
        + "\n".join(lines)
        + "\nEND_DATA\n"
    )


def main() -> None:
    """Use ArgyllCMS 3.5.0 to fit the bidirectional RGB printer lookup tables."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--colprof", type=Path, required=True, help="Path to ArgyllCMS 3.5.0 colprof")
    args = parser.parse_args()
    (HERE / "virtual-gray.icc").write_bytes(gray_profile())
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "press-rgb.ti3"
        source.write_text(rgb_measurements(), encoding="ascii")
        subprocess.run(  # noqa: S603 - the caller selects the ArgyllCMS binary.
            [args.colprof.resolve(), "-ql", "-bl", "-D", "pyropdf virtual RGB output", "-C", "CC0", source.stem],
            cwd=temporary,
            check=True,
        )
        profile = bytearray(source.with_suffix(".icc").read_bytes())
        profile[24:36] = struct.pack(">6H", 2026, 1, 1, 0, 0, 0)
        (HERE / "virtual-rgb.icc").write_bytes(profile)


if __name__ == "__main__":
    main()
