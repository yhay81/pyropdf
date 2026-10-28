# ruff: noqa: INP001 - a script, not a package.
"""Measure repeated text fills as the number of form fields grows.

Run: uv run python evidence/form-fill-cost/field_scale.py
All widgets initially share a valid normal appearance. The selected field
is warmed once before seven timed edits; timings include appearance settling
and the update of the form's NeedAppearances flag.
"""

from __future__ import annotations

import platform
import statistics
import time

import pyropdf


def document(fields: int) -> bytes:
    """Build one page with `fields` merged text-field widgets."""
    references = " ".join(f"{number} 0 R" for number in range(6, fields + 6))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [{references}] >>",
        4: f"<< /Fields [{references}] /DA (/Helv 10 Tf 0 g) /NeedAppearances false >>",
        5: "<< /Type /XObject /Subtype /Form /BBox [0 0 50 20] /Length 0 >>\nstream\n\nendstream",
    }
    for index in range(fields):
        objects[index + 6] = (
            f"<< /FT /Tx /T (field{index}) /V (old) /Type /Annot /Subtype /Widget "
            "/Rect [0 0 50 20] /P 3 0 R /AP << /N 5 0 R >> >>"
        )
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
    """Print the median time for seven repeated edits at each field count."""
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print("\n| Fields | Median fill |\n| ---: | ---: |")  # noqa: T201
    for count in (1, 10, 100, 1000):
        doc = pyropdf.open(document(count))
        field = doc.form["field0"]
        field.value = "warm"
        timings = []
        for number in range(7):
            start = time.perf_counter()
            field.value = f"value {number}"
            timings.append(time.perf_counter() - start)
        print(f"| {count:,} | {statistics.median(timings) * 1000:.3f} ms |")  # noqa: T201


if __name__ == "__main__":
    main()
