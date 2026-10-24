"""Compare reading every page with rotating and then reading every page.

Usage: python edit_read_loop.py BILL

BILL is pylopdf's tests/assets/real_world/bill-hr815.pdf (110 pages). The
script makes documents of 110, 440, and 1,760 pages from it, writes each
with to_bytes(), and opens it again before measuring.
"""

import sys
import time
from pathlib import Path

import pylopdf

SOURCE = Path(sys.argv[1])


def build(copies: int) -> bytes:
    doc = pylopdf.open(SOURCE)
    for _ in range(copies - 1):
        doc.pages.extend(pylopdf.open(SOURCE).pages)
    return doc.to_bytes()


for copies in (1, 4, 16):
    data = build(copies)

    doc = pylopdf.open(data)
    start = time.perf_counter()
    for page in doc.pages:
        page.text()
    read_all = time.perf_counter() - start

    doc = pylopdf.open(data)
    start = time.perf_counter()
    for index in range(len(doc.pages)):
        page = doc.pages[index]
        page.rotation = 90
        page.text()
    edit_read_all = time.perf_counter() - start

    print(
        f"{len(doc.pages):5d} pages | read every page {read_all:6.2f} s | "
        f"rotate and read every page {edit_read_all:6.2f} s "
        f"({edit_read_all / read_all:4.1f}x)"
    )
