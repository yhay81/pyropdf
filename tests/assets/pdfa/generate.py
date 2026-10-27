"""Write one PDF/A file per flavour that the corpus covers, with no library.

Each file is one page of vector graphics in DeviceRGB, with an RGB output
intent whose ICC profile this script builds (a matrix and gamma 2.2 display
profile with sRGB's primaries adapted to D50), XMP metadata that claims the
flavour, and a trailer /ID. With no text and no fonts, each file meets its
flavour's rules, which veraPDF checks in CI (tools/verapdf.py). The output is
the same on every run:

    uv run python tests/assets/pdfa/generate.py
"""

from __future__ import annotations

import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Each file, its PDF header version, the XMP pdfaid properties, and veraPDF's flavour.
FLAVOURS: dict[str, tuple[str, str, str]] = {
    "pdfa-1b.pdf": ("1.4", "<pdfaid:part>1</pdfaid:part><pdfaid:conformance>B</pdfaid:conformance>", "1b"),
    "pdfa-2b.pdf": ("1.7", "<pdfaid:part>2</pdfaid:part><pdfaid:conformance>B</pdfaid:conformance>", "2b"),
    "pdfa-2u.pdf": ("1.7", "<pdfaid:part>2</pdfaid:part><pdfaid:conformance>U</pdfaid:conformance>", "2u"),
    "pdfa-3b.pdf": ("1.7", "<pdfaid:part>3</pdfaid:part><pdfaid:conformance>B</pdfaid:conformance>", "3b"),
    "pdfa-4.pdf": ("2.0", "<pdfaid:part>4</pdfaid:part><pdfaid:rev>2020</pdfaid:rev>", "4"),
}


def s15(value: float) -> bytes:
    """Return ``value`` as an ICC s15Fixed16Number."""
    return struct.pack(">i", round(value * 65536))


def xyz(x: float, y: float, z: float) -> bytes:
    """Return an ICC XYZType tag."""
    return b"XYZ \0\0\0\0" + s15(x) + s15(y) + s15(z)


def icc() -> bytes:
    """Return an ICC v2 RGB display profile: sRGB's primaries at D50 and a gamma of 2.2."""
    description = b"pyropdf RGB"
    gamma = b"curv\0\0\0\0" + struct.pack(">IH", 1, 0x0233) + b"\0\0"
    tags = [
        (
            b"desc",
            b"desc\0\0\0\0" + struct.pack(">I", len(description) + 1) + description + b"\0" + bytes(4 + 4 + 2 + 1 + 67),
        ),
        (b"cprt", b"text\0\0\0\0No copyright, use freely\0"),
        (b"wtpt", xyz(0.9642, 1.0, 0.8249)),
        (b"rXYZ", xyz(0.4361, 0.2225, 0.0139)),
        (b"gXYZ", xyz(0.3851, 0.7169, 0.0971)),
        (b"bXYZ", xyz(0.1431, 0.0606, 0.7141)),
        (b"rTRC", gamma),
        (b"gTRC", gamma),
        (b"bTRC", gamma),
    ]
    offset = 128 + 4 + 12 * len(tags)
    table = struct.pack(">I", len(tags))
    data = b""
    for signature, body in tags:
        while (offset + len(data)) % 4:
            data += b"\0"
        table += signature + struct.pack(">II", offset + len(data), len(body))
        data += body
    size = offset + len(data)
    header = (
        struct.pack(">I", size)
        + b"\0\0\0\0"
        + bytes([2, 0x10, 0, 0])
        + b"mntrRGB XYZ "
        + struct.pack(">6H", 2026, 1, 1, 0, 0, 0)
        + b"acsp"
        + bytes(4 + 4 + 4 + 4 + 8)
        + struct.pack(">I", 0)
        + s15(0.9642)
        + s15(1.0)
        + s15(0.8249)
        + bytes(4 + 16 + 28)
    )
    assert len(header) == 128
    return header + table + data


def xmp(claim: str) -> bytes:
    """Return the XMP packet that claims a flavour through its pdfaid properties."""
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">'
        f"{claim}</rdf:Description></rdf:RDF></x:xmpmeta>\n"
        '<?xpacket end="w"?>'
    ).encode()


def stream(dictionary: bytes, data: bytes) -> bytes:
    """Return a stream object's body with its /Length."""
    return b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def write(version: str, claim: str) -> bytes:
    """Return the bytes of one fixture."""
    profile = icc()
    content = b"0 0.4 0.8 rg 100 100 400 600 re f 1 0 0 RG 4 w 72 72 m 540 720 l S"
    objects = [
        (
            b"<< /Type /Catalog /Pages 2 0 R /Metadata 5 0 R /OutputIntents [<<"
            b" /Type /OutputIntent /S /GTS_PDFA1 /OutputConditionIdentifier (pyropdf RGB) /Info (pyropdf RGB)"
            b" /DestOutputProfile 6 0 R >>] >>"
        ),
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> /Contents 4 0 R >>",
        stream(b"", content),
        stream(b"/Type /Metadata /Subtype /XML", xmp(claim)),
        stream(b"/N 3", profile),
    ]
    out = bytearray(b"%PDF-" + version.encode() + b"\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    size = len(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    file_id = bytes(range(16)).hex().encode()
    out += b"trailer\n<< /Size %d /Root 1 0 R /ID [<%s> <%s>] >>\nstartxref\n%d\n%%%%EOF\n" % (
        size,
        file_id,
        file_id,
        start,
    )
    return bytes(out)


def main() -> None:
    """Write the fixtures."""
    for name, (version, claim, _) in FLAVOURS.items():
        (HERE / name).write_bytes(write(version, claim))


if __name__ == "__main__":
    main()
