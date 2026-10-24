"""Measure reading every field's value through handles, by field count.

Usage: python field_read_cost.py

The script creates forms of 100, 400, and 1,600 text fields, 50 to a page,
writes each with to_bytes(), and opens it again before measuring. It also
times one walk of the form through the core, which every handle access
repeats.
"""

import time

import pylopdf

PER_PAGE = 50

for count in (100, 400, 1600):
    doc = pylopdf.Document()
    for first in range(0, count, PER_PAGE):
        page = doc.pages.new()
        for i in range(first, min(first + PER_PAGE, count)):
            row = i - first
            x = 36 + (row % 5) * 100
            y = 36 + (row // 5) * 70
            page.widgets.add(pylopdf.TextField(f"f{i}", (x, y, x + 90, y + 20)))
    doc = pylopdf.open(doc.to_bytes())
    fields = list(doc.form.values())

    start = time.perf_counter()
    values = [field.value for field in fields]
    elapsed = time.perf_counter() - start

    start = time.perf_counter()
    doc._doc.form_fields()
    walk = time.perf_counter() - start

    print(
        f"{len(values):5d} fields | read every value {elapsed * 1e3:9.1f} ms | "
        f"one form walk {walk * 1e3:6.2f} ms"
    )
