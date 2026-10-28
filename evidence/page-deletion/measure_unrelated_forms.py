# ruff: noqa: INP001 - a script, not a package.
"""Measure repeated page deletion as unrelated form fields grow.

Run: uv run python evidence/page-deletion/measure_unrelated_forms.py
The unrelated fields are widgetless, so none refers to either page. In the
unaffected column the deleted first page has no annotations; in the affected
column it holds the one widget of one more field. The first deletion warms
the reference index; seven subsequent deletions each follow an undo.
"""

from __future__ import annotations

import platform
import statistics
import time

import pyropdf


def document(fields: int, *, affected: bool) -> bytes:
    """Build a two-page PDF with `fields` unrelated form fields, and with `affected` a widget on the first page."""
    widget = fields + 6
    numbers = [*range(6, fields + 6), *([widget] if affected else [])]
    references = " ".join(f"{number} 0 R" for number in numbers)
    annots = f"/Annots [{widget} 0 R]" if affected else ""
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 5 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] {annots} >>",
        4: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        5: f"<< /Fields [{references}] >>",
    }
    for index in range(fields):
        objects[index + 6] = f"<< /FT /Tx /T (f{index}) /V (value) >>"
    if affected:
        objects[widget] = "<< /FT /Tx /T (on) /Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P 3 0 R >>"
    output = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for number, value in objects.items():
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n{value}\nendobj\n".encode())
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def median_deletion(data: bytes) -> float:
    """Return the median of seven warm deletions of the first page, in milliseconds."""
    doc = pyropdf.open(data)
    del doc.pages[0]
    doc.history.undo()
    times = []
    for _ in range(7):
        start = time.perf_counter()
        del doc.pages[0]
        times.append(time.perf_counter() - start)
        doc.history.undo()
    return statistics.median(times) * 1000


def main() -> None:
    """Print the median deletion for each form size, without and with a widget on the deleted page."""
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print("\n| Unrelated fields | Unaffected | Affected |\n| ---: | ---: | ---: |")  # noqa: T201
    for count in (0, 10, 100, 1000, 4000):
        unaffected = median_deletion(document(count, affected=False))
        affected = median_deletion(document(count, affected=True))
        print(f"| {count:,} | {unaffected:.3f} ms | {affected:.3f} ms |")  # noqa: T201


if __name__ == "__main__":
    main()
