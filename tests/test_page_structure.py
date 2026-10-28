"""doc.pages.select: keep pages in a new order and remove the others.

The select case of pylopdf's tests/test_page_structure.py at ebb01d3c,
ported. Its len, indexing, iteration, new, del, and move cases are in
tests/test_pages.py. Not ported here:

- repeating a page in a selection, and ``insert``, which copy pages
  (yhay81/pyropdf#175) or import them (yhay81/pyropdf#176);
- ``labeled``, since pyropdf reads no page labels yet.
"""

from __future__ import annotations

import pytest

import pyropdf
from pyropdf import Document


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def doc_of(count: int) -> Document:
    """Return ``count`` pages; page ``i`` is ``100 + i`` points wide."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(count))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {count} >>",
    }
    for i in range(count):
        objects[3 + i] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {100 + i} 500] >>"
    return pyropdf.open(build_raw_pdf(objects))


def numbers(doc: Document) -> list[int]:
    return [int(page.rect[2]) - 100 for page in doc.pages]


def test_select_keeps_pages_in_order() -> None:
    doc = doc_of(4)
    last = doc.pages[3]

    doc.pages.select([3, 0, -2])

    assert numbers(doc) == [3, 0, 2]
    assert last.index == 0
    assert doc.history.can_undo
    doc.history.undo()
    assert numbers(doc) == [0, 1, 2, 3]


def test_select_frees_the_pages_that_leave() -> None:
    doc = doc_of(3)
    removed = doc.pages[1].xref

    doc.pages.select([0, 2])

    assert removed not in doc.objects
    assert f"{removed} 0 obj".encode() not in doc.to_bytes()


def test_select_refuses_before_changing() -> None:
    doc = doc_of(3)
    before = doc.to_bytes()
    with pytest.raises(TypeError):
        doc.pages.select(indexes=[0])  # ty: ignore[positional-only-parameter-as-kwarg]
    with pytest.raises(IndexError):
        doc.pages.select([3])
    with pytest.raises(TypeError):
        doc.pages.select([1.0])  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="at most once"):
        doc.pages.select([0, 0])
    assert doc.to_bytes() == before
    assert numbers(doc) == [0, 1, 2]


def test_selecting_every_page_in_order_keeps_the_pages() -> None:
    doc = doc_of(2)

    doc.pages.select([0, 1])

    assert numbers(doc) == [0, 1]
