"""doc.pages.select, insert, and extend: keep, repeat, and copy pages.

The select and copy cases of pylopdf's tests/test_page_structure.py at
ebb01d3c, ported. Its len, indexing, iteration, new, del, and move cases
are in tests/test_pages.py. Intended differences:

- the copy's independence is shown with its rotation and annotations,
  since pyropdf has no ``page.draw`` yet;
- pages of another document raise ``NotImplementedError`` until importing
  lands (yhay81/pyropdf#176), and a stale page is one that was deleted,
  since a page handle keeps naming its page through other edits.

Not ported here:

- importing pages of another document (yhay81/pyropdf#176);
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
    with pytest.raises(TypeError):
        doc.pages.select([0, None])  # ty: ignore[invalid-argument-type]
    assert doc.to_bytes() == before
    assert numbers(doc) == [0, 1, 2]


def test_selecting_every_page_in_order_keeps_the_pages() -> None:
    doc = doc_of(2)

    doc.pages.select([0, 1])

    assert numbers(doc) == [0, 1]


def test_select_keeps_and_repeats_pages_in_order() -> None:
    doc = doc_of(3)
    first = doc.pages[0]

    doc.pages.select([2, 0, 0, -1])

    assert numbers(doc) == [2, 0, 0, 2]
    assert first.index == 1
    assert len({page.xref for page in doc.pages}) == 4
    assert numbers(pyropdf.open(doc.to_bytes())) == [2, 0, 0, 2]
    doc.history.undo()
    assert numbers(doc) == [0, 1, 2]


def test_insert_copies_pages_of_the_same_document() -> None:
    doc = doc_of(3)

    doc.pages.extend(doc.pages[0])
    doc.pages.insert(0, doc.pages[2])
    doc.pages.insert(-1, [doc.pages[1], doc.pages[1]])

    assert numbers(doc) == [2, 0, 1, 2, 0, 0, 0]
    assert numbers(pyropdf.open(doc.to_bytes())) == [2, 0, 1, 2, 0, 0, 0]


@pytest.mark.parametrize(("index", "expected"), [(-99, [1, 0, 1]), (99, [0, 1, 1]), (-1, [0, 1, 1])])
def test_insert_clamps_the_index_as_list_insert_does(index: int, expected: list[int]) -> None:
    doc = doc_of(2)

    doc.pages.insert(index, doc.pages[1])

    assert numbers(doc) == expected


def test_a_copy_is_independent_of_the_original() -> None:
    doc = doc_of(1)
    xref = doc.pages[0].xref
    doc.pages.extend(doc.pages[0])

    doc.pages[1].rotation = 90
    doc.pages[1].annotations.add(pyropdf.Square((10, 10, 50, 50)))

    assert doc.pages[0].xref == xref
    assert doc.pages[1].xref != xref
    assert doc.pages[0].rotation == 0
    assert len(doc.pages[0].annotations) == 0
    reopened = pyropdf.open(doc.to_bytes())
    assert (reopened.pages[0].rotation, reopened.pages[1].rotation) == (0, 90)
    assert [len(page.annotations) for page in reopened.pages] == [0, 1]


def test_insert_refuses_bad_sources_before_changing() -> None:
    doc = doc_of(2)
    other = doc_of(2)
    before = doc.to_bytes()

    with pytest.raises(NotImplementedError, match="another document"):
        doc.pages.extend(other.pages[0])
    with pytest.raises(NotImplementedError, match="another document"):
        doc.pages.extend([doc.pages[0], other.pages[0]])
    with pytest.raises(TypeError, match="Page"):
        doc.pages.extend([0, 1])  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="Page"):
        doc.pages.extend(other)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="insertion index"):
        doc.pages.insert(0.5, doc.pages)  # ty: ignore[invalid-argument-type]
    assert doc.to_bytes() == before
    stale = doc.pages[1]
    del doc.pages[1]
    before = doc.to_bytes()
    with pytest.raises(pyropdf.StalePageError):
        doc.pages.extend(stale)
    with pytest.raises(pyropdf.StalePageError):
        doc.pages.extend([doc.pages[0], stale])

    assert doc.to_bytes() == before
    doc.pages.extend([])
    assert doc.to_bytes() == before
    assert numbers(doc) == [0]


def test_insert_limits_its_entries() -> None:
    doc = doc_of(1)

    with pytest.raises(pyropdf.LimitError) as raised:
        doc.pages.extend(doc.pages[0] for _ in range(4097))

    assert raised.value.code == "page_entry_count"
    assert len(doc.pages) == 1
