"""Generate single-page, tagged PDF/UA fixtures independently of pyropdf.

The UA-1 file describes a drawing. The PDF 2.0 files show a rising water
level and use the PDF 2.0 structure namespace. One also claims PDF/A-4.

    uv run python tests/assets/pdfua/generate.py
"""

from __future__ import annotations

import runpy
from pathlib import Path

HERE = Path(__file__).resolve().parent

PDFA = runpy.run_path(str(HERE.parent / "pdfa" / "generate.py"))
UA2 = "<pdfuaid:part>2</pdfuaid:part><pdfuaid:rev>2024</pdfuaid:rev>"
A4 = "<pdfaid:part>4</pdfaid:part><pdfaid:rev>2020</pdfaid:rev>"

#: Each file, its PDF header version, the XMP pdfuaid properties, and veraPDF's flavour.
FLAVOURS: dict[str, tuple[str, str, str]] = {
    "pdfua-2.pdf": ("2.0", UA2, "ua2"),
    "pdfua-2-pdfa-4.pdf": ("2.0", UA2 + A4, "ua2"),
    "pdfua-1.pdf": ("1.7", "<pdfuaid:part>1</pdfuaid:part>", "ua1"),
}


def xmp(claim: str) -> bytes:
    """Return the XMP packet that gives the title and claims a part through its pdfuaid properties."""
    title = "Water level across three readings" if UA2 in claim else "pyropdf PDF/UA fixture"
    namespace = ' xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"' if A4 in claim else ""
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:pdfuaid="http://www.aiim.org/pdfua/ns/id/"'
        f' xmlns:dc="http://purl.org/dc/elements/1.1/"{namespace}>'
        f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{title}</rdf:li></rdf:Alt></dc:title>'
        f"{claim}</rdf:Description></rdf:RDF></x:xmpmeta>\n"
        '<?xpacket end="w"?>'
    ).encode()


def stream(dictionary: bytes, data: bytes) -> bytes:
    """Return a stream object's body with its /Length."""
    return b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def write(version: str, claim: str) -> bytes:
    """Return the bytes of one fixture."""
    content = b"/Figure << /MCID 0 >> BDC 0 0.4 0.8 rg 100 100 400 600 re f 1 0 0 RG 4 w 72 72 m 540 720 l S EMC"
    objects = [
        (
            b"<< /Type /Catalog /Pages 2 0 R /Metadata 5 0 R /MarkInfo << /Marked true >> /Lang (en-US)"
            b" /ViewerPreferences << /DisplayDocTitle true >> /StructTreeRoot 6 0 R >>"
        ),
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> /Contents 4 0 R /StructParents 0 >>"),
        stream(b"", content),
        stream(b"/Type /Metadata /Subtype /XML", xmp(claim)),
        b"<< /Type /StructTreeRoot /K [7 0 R] /ParentTree 9 0 R /ParentTreeNextKey 1 >>",
        b"<< /Type /StructElem /S /Document /P 6 0 R /K [8 0 R] >>",
        b"<< /Type /StructElem /S /Figure /P 7 0 R /Pg 3 0 R /K 0 /Alt (A blue rectangle crossed by a red line) >>",
        b"<< /Nums [0 [8 0 R]] >>",
    ]
    if version == "2.0":
        # The three bars convey an increasing water level without relying on hue.
        content = (
            b"/Figure << /MCID 0 >> BDC 0 0.4 0.8 rg 72 150 100 120 re 252 150 100 240 re 432 150 100 360 re f EMC"
        )
        objects[3] = stream(b"", content)
        objects[5] = objects[5][:-2] + b"/Namespaces [10 0 R] >>"
        objects[6] = objects[6][:-2] + b"/NS 10 0 R >>"
        objects[7] = (
            b"<< /Type /StructElem /S /Figure /NS 10 0 R /P 7 0 R /Pg 3 0 R /K 0"
            b" /Alt (Water level rose at each of three readings; the final level was three times the first.) >>"
        )
        objects.append(b"<< /Type /Namespace /NS (http://iso.org/pdf2/ssn) >>")
    if A4 in claim:
        objects[0] = objects[0][:-2] + (
            b"/OutputIntents [<< /Type /OutputIntent /S /GTS_PDFA1 /OutputConditionIdentifier (pyropdf RGB)"
            b" /Info (pyropdf RGB) /DestOutputProfile 11 0 R >>] >>"
        )
        objects.append(stream(b"/N 3", PDFA["icc"]()))
    return PDFA["serialize"](version, objects)


def main() -> None:
    """Write the fixtures."""
    for name, (version, claim, _) in FLAVOURS.items():
        (HERE / name).write_bytes(write(version, claim))


if __name__ == "__main__":
    main()
