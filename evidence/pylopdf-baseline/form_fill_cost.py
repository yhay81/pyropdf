"""Measure one text-field fill as pages without fields join the document.

Usage: python form_fill_cost.py FORM FILLER

FORM is pylopdf's tests/assets/real_world/f1040.pdf and FILLER its
tests/assets/real_world/bill-hr815.pdf. The fill is the best of seven.
"""

import sys
import time
from pathlib import Path

import pylopdf

FORM = Path(sys.argv[1])
FILLER = Path(sys.argv[2])

for copies in (0, 4, 16):
    doc = pylopdf.open(FORM)
    for _ in range(copies):
        doc.pages.extend(pylopdf.open(FILLER).pages)
    doc = pylopdf.open(doc.to_bytes())
    name = next(
        name
        for name, field in doc.form.items()
        if field.type == pylopdf.FieldType.TEXT
    )
    field = doc.form[name]
    times = []
    for round_ in range(7):
        start = time.perf_counter()
        field.value = f"value {round_}"
        times.append(time.perf_counter() - start)
    print(
        f"{len(doc.pages):5d} pages, {len(doc.objects):6d} objects | "
        f"one text-field fill {min(times) * 1e3:7.2f} ms"
    )
