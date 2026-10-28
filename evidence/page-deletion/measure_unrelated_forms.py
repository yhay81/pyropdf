# ruff: noqa: INP001 - a script, not a package.
"""Measure repeated page deletion as unrelated form fields grow.

Run: uv run python evidence/page-deletion/measure_unrelated_forms.py
The first of two pages has no annotations. Every field is widgetless, so no
field refers to either page. The first deletion warms the reference index;
seven subsequent deletions each follow an undo.
"""

from __future__ import annotations

import platform
import statistics
import time

import pyropdf


def document(fields: int) -> bytes:
    """Build a two-page PDF with `fields` unrelated form fields."""
    references = " ".join(f"{number} 0 R" for number in range(6, fields + 6))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 5 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        4: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        5: f"<< /Fields [{references}] >>",
    }
    for index in range(fields):
        objects[index + 6] = f"<< /FT /Tx /T (f{index}) /V (value) >>"
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


def main() -> None:
    """Print the median of seven warm deletions for each form size."""
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print("\n| Unrelated fields | Median deletion |\n| ---: | ---: |")  # noqa: T201
    for count in (0, 10, 100, 1000):
        doc = pyropdf.open(document(count))
        del doc.pages[0]
        doc.history.undo()
        times = []
        for _ in range(7):
            start = time.perf_counter()
            del doc.pages[0]
            times.append(time.perf_counter() - start)
            doc.history.undo()
        print(f"| {count:,} | {statistics.median(times) * 1000:.3f} ms |")  # noqa: T201


if __name__ == "__main__":
    main()
