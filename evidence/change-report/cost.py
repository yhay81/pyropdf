# ruff: noqa: INP001, T201, S101 - a script that checks its documents and prints a Markdown table.
"""Measure `doc.changes()` against the number of edits and the size of the document.

Each document has one signature field that signs every object but the
signature's own, a PDF/A-1B claim, and `objects` filler dictionaries. The
edits set a key of the first `edits` fillers, each in a transaction of its
own. The median of 21 calls of `doc.changes(incremental=True)` is printed.
Run with `uv run python evidence/change-report/cost.py`.
"""

from __future__ import annotations

import statistics
import sys
import time

import pyropdf

XMP = '<x:xmpmeta><rdf:RDF><rdf:Description pdfaid:part="1" pdfaid:conformance="B"/></rdf:RDF></x:xmpmeta>'


def document(objects: int) -> bytes:
    """Return a signed document with `objects` filler objects, numbered from 10."""
    bodies = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> /Metadata 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        4: f"<< /Type /Metadata /Subtype /XML /Length {len(XMP)} >>\nstream\n{XMP}\nendstream",
        5: "<< /FT /Sig /T (Approval) /V 6 0 R >>",
    }
    for number in range(10, 10 + objects):
        bodies[number] = f"<< /Filler {number} >>"
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(bodies):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{bodies[number]}\nendobj\n".encode()
    # The signature, last: it signs every byte before it.
    end = len(out)
    offsets[6] = end
    out += f"6 0 obj\n<< /Type /Sig /ByteRange [0 {end} {end} 0] >>\nendobj\n".encode()
    size = 10 + objects
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def measure(objects: int, edits: int) -> float:
    """Return the median seconds of a report of `edits` edits of a document of `objects` fillers."""
    doc = pyropdf.open(document(objects))
    for number in range(10, 10 + edits):
        doc.objects[number]["Edited"] = True
    report = doc.changes(incremental=True)
    assert len(report.signatures[0].objects) == min(edits, 1024)
    times = []
    for _ in range(21):
        start = time.perf_counter()
        doc.changes(incremental=True)
        times.append(time.perf_counter() - start)
    return statistics.median(times)


def main() -> int:
    """Print both tables."""
    print("Edits grow, document fixed at 100,000 objects:\n")
    print("| Edits | Median ms |")
    print("| ---: | ---: |")
    for edits in (1, 10, 100, 1000):
        print(f"| {edits:,} | {measure(100_000, edits) * 1000:.3f} |")
    print("\nDocument grows, edits fixed at 10:\n")
    print("| Objects | Median ms |")
    print("| ---: | ---: |")
    for objects in (1_000, 10_000, 100_000):
        print(f"| {objects:,} | {measure(objects, 10) * 1000:.3f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
