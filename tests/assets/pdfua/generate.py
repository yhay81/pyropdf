"""Write the PDF/UA-1 fixture of the corpus, with no library.

The file is one page whose vector graphics are tagged as a Figure with
alternate text in a Document element, through a marked-content sequence, a
parent tree, and the page's /StructParents. The catalog marks the document
as tagged and gives its language, and its XMP gives the title that the
viewer preferences ask viewers to display, and claims PDF/UA-1. With no text
and no fonts, the file meets PDF/UA-1's rules, which veraPDF checks in CI
(tools/verapdf.py). The output is the same on every run:

    uv run python tests/assets/pdfua/generate.py
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Each file, its PDF header version, the XMP pdfuaid properties, and veraPDF's flavour.
FLAVOURS: dict[str, tuple[str, str, str]] = {
    "pdfua-1.pdf": ("1.7", "<pdfuaid:part>1</pdfuaid:part>", "ua1"),
}


def xmp(claim: str) -> bytes:
    """Return the XMP packet that gives the title and claims a part through its pdfuaid properties."""
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:pdfuaid="http://www.aiim.org/pdfua/ns/id/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">pyropdf PDF/UA fixture</rdf:li></rdf:Alt></dc:title>'
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
