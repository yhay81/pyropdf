# ruff: noqa: INP001, D103 - a script, not a package.
"""Writes the two reproduction files next to this script.

`stray-q.pdf`: the page content is `Q 1 0 0 rg`, a `Q` with no matching `q`
followed by a red fill color. One square annotation has an appearance stream
that fills its box green inside `q ... Q` and then draws the word "Note" with
no color of its own. `balanced.pdf` is the same file without the stray `Q`.
"""

from pathlib import Path

HERE = Path(__file__).parent
PAGE = "Q 1 0 0 rg"
APPEARANCE = "q 0 1 0 rg 0 0 40 20 re f Q BT /F1 14 Tf 2 5 Td (Note) Tj ET"


def stream(content: str, entries: str = "") -> str:
    return f"<< {entries}/Length {len(content)} >>\nstream\n{content}\nendstream"


def build(page_content: str) -> bytes:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
        3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Annots [5 0 R] >>",
        4: stream(page_content),
        5: "<< /Type /Annot /Subtype /Square /Rect [20 40 60 60] /F 4 /AP << /N 6 0 R >> >>",
        7: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        6: stream(
            APPEARANCE, "/Type /XObject /Subtype /Form /BBox [0 0 40 20] /Resources << /Font << /F1 7 0 R >> >> "
        ),
    }
    out = b"%PDF-1.7\n"
    offsets = {}
    for number, body in objects.items():
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for number in objects:
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


(HERE / "stray-q.pdf").write_bytes(build(PAGE))
(HERE / "balanced.pdf").write_bytes(build(PAGE.removeprefix("Q ")))
