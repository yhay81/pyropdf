# ruff: noqa: INP001 - a script, not a package.
"""Measure repeated text and check box fills as the number of form fields grows.

Run: uv run python evidence/form-fill-cost/field_scale.py
All widgets initially share a nonempty normal appearance, or, for check
boxes, nonempty `Yes` and `Off` states. The selected field
is warmed once before seven timed edits; timings include appearance settling
and the update of the form's NeedAppearances flag. The external-edit column
times a separate field-value edit; the next column times the fill after it.
A check box document alternately checks and clears its first check box.
"""

from __future__ import annotations

import platform
import statistics
import time
from contextlib import nullcontext

import pyropdf


def document(fields: int, kind: str = "text") -> bytes:
    """Build one page with `fields` merged text-field or check box widgets."""
    references = " ".join(f"{number} 0 R" for number in range(6, fields + 6))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [{references}] >>",
        4: f"<< /Fields [{references}] /DA (/Helv 10 Tf 0 g) /NeedAppearances false >>",
        5: "<< /Type /XObject /Subtype /Form /BBox [0 0 50 20] /Length 3 >>\nstream\nq Q\nendstream",
    }
    for index in range(fields):
        if kind == "checkbox":
            objects[index + 6] = (
                f"<< /FT /Btn /T (field{index}) /V /Off /AS /Off /Type /Annot /Subtype /Widget "
                "/Rect [0 0 50 20] /P 3 0 R /AP << /N << /Yes 5 0 R /Off 5 0 R >> >> >>"
            )
        else:
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


def measure(fields: int, mode: str, kind: str = "text") -> tuple[float, float]:
    """Measure warm fills, and the preceding external edits when requested."""
    doc = pyropdf.open(document(fields, kind))
    if mode in {"no-history", "external"}:
        doc.history.max_size = 0
    field = doc.form["field0"]
    other = doc.form["field1" if fields > 1 else "field0"].obj
    timings = []
    external = []
    context = doc.transaction("measure") if mode == "transaction" else nullcontext()
    with context:
        field.value = True if kind == "checkbox" else "warm"
        for number in range(7):
            if mode == "external":
                start = time.perf_counter()
                other["V"] = pyropdf.String(f"external {number}".encode())
                external.append(time.perf_counter() - start)
            start = time.perf_counter()
            field.value = number % 2 == 0 if kind == "checkbox" else f"value {number}"
            timings.append(time.perf_counter() - start)
    return statistics.median(timings) * 1000, statistics.median(external) * 1000 if external else 0.0


def main() -> None:
    """Print median times for seven repeated edits at each field count."""
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    for kind in ("text", "checkbox"):
        print(  # noqa: T201
            f"\n{'Text' if kind == 'text' else 'Check box'} fields:\n"
            "\n| Fields | Ordinary | No history | Transaction | External edit | Fill after edit |\n"
            "| ---: | ---: | ---: | ---: | ---: | ---: |"
        )
        for count in (1, 10, 100, 1000, 4000):
            ordinary, _ = measure(count, "ordinary", kind)
            no_history, _ = measure(count, "no-history", kind)
            transaction, _ = measure(count, "transaction", kind)
            after_edit, external = measure(count, "external", kind)
            print(  # noqa: T201
                f"| {count:,} | {ordinary:.3f} ms | {no_history:.3f} ms | {transaction:.3f} ms | "
                f"{external:.3f} ms | {after_edit:.3f} ms |"
            )


if __name__ == "__main__":
    main()
