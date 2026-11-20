# ruff: noqa: INP001, T201 - probes run inside each engine's isolated environment.
"""Exercise the two PDF behavior changes documented in PyMuPDF 2.1 with shared input bytes."""

from __future__ import annotations

import json
import math
import sys

PREFIX = "付/(A)\\"
SCALES = [(1, 1), (0.5, 1), (2, 1), (1, 0.5), (1, 2)]


def example(sx: float, sy: float) -> bytes:
    """One labeled page containing a Helvetica H under a non-uniform transform."""
    # The literal string exercises PDF delimiters. A second page uses a UTF-16 label.
    stream = f"BT /F1 20 Tf {sx} 0 0 {sy} 30 40 Tm (H) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /PageLabels << /Nums [0 << /P (a/\\(b\\)\\\\) >> 1 << /P <"
        + ("\ufeff" + PREFIX).encode("utf-16-be").hex().encode()
        + b"> >>] >> >>",
        b"<< /Type /Pages /Count 2 /Kids [3 0 R 6 0 R] >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
    ]
    data = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(data)
    data.extend(b"xref\n0 7\n0000000000 65535 f \n")
    for offset in offsets:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(data)


def probe(engine: str) -> dict:
    """Read label prefixes and the height of a glyph under each X/Y scale."""
    heights = []
    labels = []
    for sx, sy in SCALES:
        source = example(sx, sy)
        if engine == "pymupdf":
            import pymupdf  # noqa: PLC0415

            with pymupdf.open(stream=source, filetype="pdf") as doc:
                labels = [page.get_label() for page in doc]
                bbox = doc[0].get_texttrace()[0]["chars"][0][3]
        else:
            import pyropdf  # noqa: PLC0415

            with pyropdf.open(source) as doc:
                labels = [page.label for page in doc.pages]
                bbox = doc.pages[0].chars()[0].bbox
        heights.append(bbox[3] - bbox[1])
    return {
        "page_label_prefixes": {"values": labels, "passed": labels == ["a/(b)\\", PREFIX]},
        "glyph_height_under_scale": {
            "scales": SCALES,
            "heights": heights,
            "passed": all(
                math.isclose(h, heights[0] * sy, rel_tol=1e-5) for h, (_, sy) in zip(heights, SCALES, strict=True)
            ),
        },
    }


if __name__ == "__main__":
    print(json.dumps(probe(sys.argv[1])))
