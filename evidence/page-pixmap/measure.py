# ruff: noqa: INP001 - a script, not a package.
"""Measure ``page.rotation = ...`` followed by ``page.pixmap()`` at 110, 440, and 1,760 pages (issue #35).

The documents are evidence/bridge-render's: letter pages of 20 lines of
Helvetica text and a filled rectangle. Run from the repository root:
``uv run python evidence/page-pixmap/measure.py``. It prints a Markdown table.
"""

from __future__ import annotations

import platform
import time

import pyropdf

SIZES = (110, 440, 1_760)
EDITS = 41


def document(pages: int) -> bytes:
    """Return a classic file of ``pages`` pages, as evidence/bridge-render writes it."""
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []

    def put(body: str) -> None:
        offsets.append(len(out))
        out.extend(f"{len(offsets)} 0 obj\n{body}\nendobj\n".encode())

    kids = " ".join(f"{4 + 2 * p} 0 R" for p in range(pages))
    put("<< /Type /Catalog /Pages 2 0 R >>")
    put(
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 3 0 R >> >> >>"
    )
    put("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page in range(pages):
        lines = " ".join(f"(Page {page}, line {line}: the quick brown fox) Tj 0 -14 Td" for line in range(20))
        text = f"0.8 g 72 600 468 120 re f 0 g BT /F1 12 Tf 72 720 Td {lines} ET"
        put(f"<< /Type /Page /Parent 2 0 R /Contents {5 + 2 * page} 0 R >>")
        put(f"<< /Length {len(text)} >>\nstream\n{text}\nendstream")
    xref = len(out)
    out.extend(f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode())
    out.extend(b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets))
    out.extend(f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def main() -> None:
    """Print the table."""
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print()  # noqa: T201
    print("| Pages | Rotation | Pixmap | Total |")  # noqa: T201
    print("| ---: | ---: | ---: | ---: |")  # noqa: T201
    for count in SIZES:
        doc = pyropdf.open(document(count))
        doc.pages[0].pixmap()
        rotations, pixmaps = [], []
        for edit in range(EDITS):
            page = doc.pages[edit * 7 % count]
            started = time.perf_counter()
            page.rotation = 90 * (edit % 4)
            rotated = time.perf_counter()
            page.pixmap()
            done = time.perf_counter()
            rotations.append(rotated - started)
            pixmaps.append(done - rotated)
        totals = sorted(r + p for r, p in zip(rotations, pixmaps, strict=True))
        median = EDITS // 2
        print(  # noqa: T201
            f"| {count:,} | {sorted(rotations)[median] * 1e6:.1f} µs | {sorted(pixmaps)[median] * 1e3:.2f} ms "
            f"| {totals[median] * 1e3:.2f} ms |"
        )


if __name__ == "__main__":
    main()
