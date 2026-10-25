"""doc.pages and Page handles, which keep naming their pages through edits.

The cases of pylopdf's tests/test_page_ops.py and tests/test_page_structure.py
at 884af0db for len, indexing, iteration, new, del, and move, ported. Pages
are told apart by their widths, since text extraction comes later. Intended
differences from pylopdf:

- A page taken before an edit of other pages keeps naming its page, with its
  new index; pylopdf made every earlier page stale.
- ``page.rect`` is a ``(x0, y0, x1, y1)`` tuple until a geometry type exists.
- Flattening repairs a page whose ``/Parent`` names a cycle instead of
  refusing the edit, since the walk of ``/Kids``, not ``/Parent``, decides
  the pages.
"""

from __future__ import annotations

import time
from operator import delitem
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import Document, Page, StaleObjectError, StalePageError

if TYPE_CHECKING:
    from collections.abc import Callable


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


def numbered(count: int) -> bytes:
    """Return ``count`` pages in one flat tree; page ``i`` is ``100 + i`` points wide."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(count))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {count} >>",
    }
    for i in range(count):
        objects[3 + i] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {100 + i} 500] >>"
    return build_raw_pdf(objects)


def doc_of(count: int = 6) -> Document:
    return pyropdf.open(numbered(count))


def numbers(doc: Document) -> list[int | str]:
    """Return each page's number, ``int(width) - 100``, or ``"new"`` for a 50-point page."""
    return ["new" if page.rect[2] == 50 else int(page.rect[2]) - 100 for page in doc.pages]


def test_pages_is_a_sequence_of_page_handles() -> None:
    doc = doc_of(4)
    pages = doc.pages
    assert len(pages) == 4
    assert pages[0] == pages[0]
    assert pages[-1] == pages[3]
    assert pages[-1].index == 3
    assert pages[0].document is doc
    assert [page.index for page in pages] == [0, 1, 2, 3]
    assert [page.index for page in pages[1:3]] == [1, 2]
    assert [page.index for page in pages[::-2]] == [3, 1]
    assert [page.index for page in reversed(pages)] == [3, 2, 1, 0]
    assert pages[2] in pages
    assert pages.index(pages[2]) == 2
    assert Document().pages[:] == []
    assert hash(pages[1]) == hash(pages[1])
    assert len({pages[1], pages[1], pages[2]}) == 2
    assert repr(pages).startswith("<Pages of <pyropdf.Document")
    assert pages[1].xref == 4
    assert isinstance(pages[0], Page)


@pytest.mark.parametrize("index", [4, -5])
def test_page_indexes_out_of_range_raise_index_error(index: int) -> None:
    with pytest.raises(IndexError, match="out of range for 4 pages"):
        _ = doc_of(4).pages[index]


@pytest.mark.parametrize("index", [1.0, "1", True, None])
def test_page_indexes_must_be_integers(index: object) -> None:
    with pytest.raises(TypeError, match="integers"):
        _ = doc_of(2).pages[index]  # ty: ignore[invalid-argument-type]


def test_a_page_keeps_naming_its_page_through_other_edits() -> None:
    doc = doc_of(4)
    page = doc.pages[2]
    first = doc.pages[0]
    doc.pages.new((50, 50), index=0)
    assert page.index == 3
    del doc.pages[1]
    assert page.index == 2
    doc.pages.move(2, 0)
    assert page.index == 0
    assert page == doc.pages[0]
    assert page.rect == (0.0, 0.0, 102.0, 500.0)
    # Deleting the page itself makes it stale, and undo is #34's.
    with pytest.raises(StalePageError, match=r"doc\.pages\[i\]"):
        _ = first.index
    assert issubclass(StalePageError, StaleObjectError)
    assert first not in doc.pages
    assert numbers(doc) == [2, "new", 1, 3]


def test_changing_the_pages_during_iteration_raises() -> None:
    doc = doc_of(3)
    pages = iter(doc.pages)
    assert next(pages).index == 0
    del doc.pages[2]
    with pytest.raises(RuntimeError, match="changed during iteration"):
        next(pages)


@pytest.mark.parametrize(
    ("edit", "remaining"),
    [
        (lambda pages: delitem(pages, 1), [0, 2, 3, 4, 5]),
        (lambda pages: delitem(pages, -1), [0, 1, 2, 3, 4]),
        (lambda pages: delitem(pages, slice(1, 4)), [0, 4, 5]),
        (lambda pages: delitem(pages, slice(4, None)), [0, 1, 2, 3]),
        (lambda pages: delitem(pages, slice(-3, -1)), [0, 1, 2, 5]),
        (lambda pages: delitem(pages, slice(None, None, 2)), [1, 3, 5]),
        (lambda pages: delitem(pages, slice(5, None, -2)), [0, 2, 4]),
        (lambda pages: delitem(pages, slice(3, 1)), [0, 1, 2, 3, 4, 5]),
    ],
)
def test_del_takes_an_index_or_a_slice(edit: Callable[[pyropdf.Pages], None], remaining: list[int]) -> None:
    doc = doc_of()
    edit(doc.pages)
    assert numbers(doc) == remaining


def test_deleting_no_pages_changes_nothing() -> None:
    doc = doc_of(2)
    before = doc.to_bytes()
    del doc.pages[1:1]
    assert doc.to_bytes() == before


@pytest.mark.parametrize(("index", "error"), [(6, IndexError), (-7, IndexError), (1.0, TypeError)])
def test_del_refuses_bad_indexes_before_changing(index: object, error: type[Exception]) -> None:
    doc = doc_of()
    before = doc.to_bytes()
    with pytest.raises(error):
        del doc.pages[index]  # ty: ignore[invalid-argument-type]
    assert doc.to_bytes() == before


@pytest.mark.parametrize(
    ("index", "expected"),
    [(None, [0, 1, 2, "new"]), (0, ["new", 0, 1, 2]), (-1, [0, 1, "new", 2]), (9, [0, 1, 2, "new"])],
)
def test_new_inserts_a_blank_page_where_list_insert_would(index: int | None, expected: list[object]) -> None:
    doc = doc_of(3)
    page = doc.pages.new((50, 300), index=index)
    assert page == doc.pages[page.index]
    assert page.rect == (0.0, 0.0, 50.0, 300.0)
    assert numbers(doc) == expected
    assert numbers(pyropdf.open(doc.to_bytes())) == expected


def test_new_defaults_to_a4_and_validates_the_size() -> None:
    doc = Document()
    assert doc.pages.new().rect == (0.0, 0.0, 595.0, 842.0)
    with pytest.raises(ValueError, match="size"):
        doc.pages.new((0, 842))
    with pytest.raises(ValueError, match="real-number range"):
        doc.pages.new((1e39, 842))
    with pytest.raises(TypeError, match="integers"):
        doc.pages.new(index=0.5)  # ty: ignore[invalid-argument-type]
    assert len(doc.pages) == 1


def test_new_page_preserves_indirect_root_kids() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids 4 0 R /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
                4: "[3 0 R]",
            }
        )
    )
    doc.pages.new((200, 300))
    assert len(doc.pages) == 2
    assert doc.pages[0].rect[2] == pytest.approx(100)
    assert doc.pages[1].rect == (0, 0, 200, 300)
    assert len(pyropdf.open(doc.to_bytes()).pages) == 2
    del doc.pages[1]
    assert len(doc.pages) == 1
    assert doc.pages[0].rect[2] == pytest.approx(100)
    assert len(pyropdf.open(doc.to_bytes()).pages) == 1


def test_new_page_uses_requested_geometry_under_cropped_rotated_parent() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 612 792] /CropBox [0 0 80 100] /Rotate 90 >>",
                3: "<< /Type /Page /Parent 2 0 R >>",
            }
        )
    )
    assert (doc.pages[0].rotation, doc.pages[0].rect) == (90, (0, 0, 100, 80))
    page = doc.pages.new((612, 792))
    assert page.rotation == 0
    assert page.rect == (0, 0, 612, 792)
    assert (doc.pages[0].rotation, doc.pages[0].rect) == (90, (0, 0, 100, 80))
    reopened = pyropdf.open(doc.to_bytes())
    assert (reopened.pages[1].rotation, reopened.pages[1].rect) == (0, (0, 0, 612, 792))


@pytest.mark.parametrize("operation", ["delete_pages", "move"])
def test_page_edits_inherit_past_attributes_that_name_missing_objects(operation: str) -> None:
    attributes = " ".join(f"/{key} 99 0 R" for key in ("MediaBox", "CropBox", "Rotate", "Resources"))
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 /MediaBox [0 0 612 792] >>",
                3: "<< /Type /Pages /Parent 2 0 R /Kids [4 0 R] /Count 1 /CropBox [0 0 300 400] /Rotate 90 "
                "/Resources << /Font << /F1 5 0 R >> >> >>",
                4: f"<< /Type /Page /Parent 3 0 R {attributes} >>",
                5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                6: "<< /Type /Page /Parent 2 0 R >>",
            }
        )
    )
    geometry = (90, (0.0, 0.0, 400.0, 300.0))
    page = doc.pages[0]
    assert (page.rotation, page.rect) == geometry
    if operation == "delete_pages":
        del doc.pages[1:2]
    else:
        doc.pages.move(0, 1)
    for current in (doc, pyropdf.open(doc.to_bytes())):
        moved = current.pages[page.index]
        assert (moved.rotation, moved.rect) == geometry


def test_a_page_whose_parent_is_a_cycle_is_repaired_by_an_edit() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
                4: "<< /Type /Page /Parent 5 0 R /MediaBox [0 0 100 100] >>",
                5: "<< /Type /Pages /Parent 5 0 R >>",
            }
        )
    )
    existing = doc.pages[0]
    doc.pages.new()
    assert existing.index == 0
    assert len(pyropdf.open(doc.to_bytes()).pages) == 3


@pytest.mark.parametrize(
    ("index", "to", "expected"),
    [(0, 2, [1, 2, 0, 3]), (3, 0, [3, 0, 1, 2]), (-1, 1, [0, 3, 1, 2]), (1, -1, [0, 2, 3, 1]), (2, 2, [0, 1, 2, 3])],
)
def test_move_puts_the_page_at_the_destination_index(index: int, to: int, expected: list[int]) -> None:
    doc = doc_of(4)
    page = doc.pages[index]
    doc.pages.move(index, to)
    assert numbers(doc) == expected
    assert doc.pages[to] == page


def test_move_refuses_indexes_out_of_range() -> None:
    doc = doc_of(2)
    with pytest.raises(IndexError):
        doc.pages.move(0, 2)
    with pytest.raises(TypeError):
        doc.pages.move(0, 0.5)  # ty: ignore[invalid-argument-type]
    assert numbers(doc) == [0, 1]


def test_rotation_is_assignable_in_multiples_of_90() -> None:
    doc = doc_of(2)
    page = doc.pages[1]
    page.rotation = -90
    assert page.rotation == 270
    assert page.rect == (0.0, 0.0, 500.0, 101.0)
    assert pyropdf.open(doc.to_bytes()).pages[1].rotation == 270
    with pytest.raises(ValueError, match="multiple of 90"):
        page.rotation = 45
    with pytest.raises(TypeError):
        page.rotation = 90.0  # ty: ignore[invalid-assignment]
    del doc.pages[1]
    with pytest.raises(StalePageError):
        page.rotation = 0
    with pytest.raises(StalePageError):
        _ = page.rect


def test_closed_documents_refuse_pages() -> None:
    doc = doc_of(2)
    page = doc.pages[0]
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        _ = page.index
    with pytest.raises(pyropdf.DocumentClosedError):
        _ = doc.pages


def _index_time(count: int) -> float:
    """Return the median time of ``page.index`` over the middle page of ``count`` pages."""
    doc = doc_of(count)
    page = doc.pages[count // 2]
    _ = page.index
    times = []
    for _ in range(9):
        started = time.perf_counter()
        for _ in range(200):
            _ = page.index
        times.append((time.perf_counter() - started) / 200)
    return sorted(times)[len(times) // 2]


def test_page_index_takes_constant_time() -> None:
    small = _index_time(110)
    large = _index_time(1_760)
    # Sixteen times the pages; a cost that grew with them would show it.
    assert large < small * 3, (small, large)
