"""Page batches beyond the 4,096-entry floor, and the page labels that page edits keep.

The cases of pylopdf's tests/test_page_batches.py at ebb01d3c that delete,
select, extract, or render pages are ported. That file has no move case;
moving pages is covered here by the page label cases. Not ported:

- selecting a page once per page of the document, which repeats a page
  (yhay81/pyropdf#175);
- importing pages with ``extend`` and its entry limit (yhay81/pyropdf#176);
- the batches of pylopdf's internal core document, which pyropdf has no
  counterpart of: its Python API is the contract, and the entry limit cases
  below bound the same batches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import pyropdf
from pyropdf import Destination, Document, OutlineEntry

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Two internal 4,096-entry chunks, while staying fast to build and interpret.
LARGE_PAGE_COUNT = 4_200
SCALE = (0.05, 0.0, 0.0, 0.05, 0.0, 0.0)


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


def text_pdf(count: int) -> bytes:
    """Return ``count`` pages; page ``i`` shows the text ``Page i``."""
    kids = " ".join(f"{4 + 2 * index} 0 R" for index in range(count))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {count} >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index in range(count):
        content = f"BT /F1 12 Tf 72 700 Td (Page {index}) Tj ET"
        objects[4 + 2 * index] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * index} 0 R >>"
        )
        objects[5 + 2 * index] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    return build_raw_pdf(objects)


@pytest.fixture(scope="module")
def large_pdf() -> bytes:
    return text_pdf(LARGE_PAGE_COUNT)


def _zeros(limit: int, consumed: list[int]) -> Iterator[int]:
    for _ in range(limit):
        consumed[0] += 1
        yield 0


def test_text_extracts_every_page_across_chunks(large_pdf: bytes) -> None:
    doc = pyropdf.open(large_pdf)
    expected = "".join(f"Page {index}\n" for index in range(LARGE_PAGE_COUNT))

    assert doc.text() == expected
    assert doc.text(range(LARGE_PAGE_COUNT)) == expected
    assert doc.text([4199, 4096, 4095, 0]) == "Page 4199\nPage 4096\nPage 4095\nPage 0\n"


def test_markdown_converts_every_page(large_pdf: bytes) -> None:
    doc = pyropdf.open(large_pdf)

    assert doc.markdown() == "\n\n".join(f"Page {index}" for index in range(LARGE_PAGE_COUNT))
    assert doc.markdown([4199, 0]) == "Page 4199\n\nPage 0"


def test_render_renders_every_page_across_chunks(large_pdf: bytes) -> None:
    doc = pyropdf.open(large_pdf)

    rendered = doc.render(matrix=SCALE, alpha=True)

    assert len(rendered) == LARGE_PAGE_COUNT
    for index in (0, 4095, 4096, LARGE_PAGE_COUNT - 1):
        assert rendered[index] == doc.pages[index].pixmap(matrix=SCALE, alpha=True).to_bytes()


def test_select_reorders_every_page(large_pdf: bytes) -> None:
    doc = pyropdf.open(large_pdf)
    doc.pages.select(range(LARGE_PAGE_COUNT - 1, -1, -1))

    assert len(doc.pages) == LARGE_PAGE_COUNT
    assert doc.pages[0].text() == f"Page {LARGE_PAGE_COUNT - 1}\n"
    assert doc.pages[LARGE_PAGE_COUNT - 1].text() == "Page 0\n"


def test_large_manual_toc_round_trips_through_page_edits(large_pdf: bytes) -> None:
    doc = pyropdf.open(large_pdf)
    outline: list[tuple[int, str, int]] = []
    for index in range(LARGE_PAGE_COUNT):
        outline.append((1, f"Chapter {index}", index))
        outline.append((2, f"Section {index}.1", index))
    doc.outline = [OutlineEntry(level, title, Destination(page)) for level, title, page in outline]

    def tuples(document: Document) -> list[tuple[int, str, int | None]]:
        return [(entry.level, entry.title, entry.page) for entry in document.outline.entries()]

    assert len(outline) > 4_096
    assert tuples(doc) == outline
    assert tuples(pyropdf.open(doc.to_bytes())) == outline

    half = LARGE_PAGE_COUNT // 2
    del doc.pages[:half]
    # Items that led to a deleted page stay without a destination.
    retained = [(level, title, None if page < half else page - half) for level, title, page in outline]
    assert tuples(doc) == retained


def test_delete_pages_accepts_more_than_the_floor(large_pdf: bytes) -> None:
    doc = pyropdf.open(large_pdf)
    del doc.pages[0:4150]

    assert len(doc.pages) == LARGE_PAGE_COUNT - 4_150
    assert doc.pages[0].text() == "Page 4150\n"


@pytest.mark.parametrize(
    "operation",
    [
        lambda doc, pages: doc.text(pages),
        lambda doc, pages: doc.markdown(pages),
        lambda doc, pages: doc.render(pages, matrix=SCALE, alpha=True),
        lambda doc, pages: doc.pages.select(pages),
    ],
    ids=["text", "markdown", "render", "select"],
)
def test_explicit_entries_stop_after_the_page_count(
    large_pdf: bytes,
    operation: Callable[[Document, Iterator[int]], object],
) -> None:
    doc = pyropdf.open(large_pdf)
    page = doc.pages[LARGE_PAGE_COUNT - 1]
    consumed = [0]

    with pytest.raises(pyropdf.LimitError, match=f"more than {LARGE_PAGE_COUNT} entries") as caught:
        operation(doc, _zeros(LARGE_PAGE_COUNT + 800, consumed))

    assert caught.value.code == "page_entry_count"
    assert consumed[0] == LARGE_PAGE_COUNT + 1
    assert len(doc.pages) == LARGE_PAGE_COUNT
    assert page.text() == f"Page {LARGE_PAGE_COUNT - 1}\n"


# Page labels: i, ii, 1, 2, 3, A-1, A-2.
LABELS = "<< /Nums [0 << /S /r >> 2 << /S /D >> 5 << /P (A-) /S /D >>] >>"
ROMAN = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"))
ROMAN += ((50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))


def labelled_pdf(labels: str, count: int = 7, extra: dict[int, str] | None = None) -> Document:
    """Return ``count`` pages, page ``i`` ``100 + i`` points wide, with ``labels`` as the catalog's page labels."""
    kids = " ".join(f"{3 + index} 0 R" for index in range(count))
    objects = {
        1: f"<< /Type /Catalog /Pages 2 0 R /PageLabels {labels} >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {count} >>",
    }
    for index in range(count):
        objects[3 + index] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {100 + index} 500] >>"
    objects.update(extra or {})
    return pyropdf.open(build_raw_pdf(objects))


def numbers(doc: Document) -> list[int]:
    return [int(page.rect[2]) - 100 for page in doc.pages]


def _ranges(tree: Any) -> list[tuple[int, Any]]:  # noqa: ANN401 - a PDF value is any of several types.
    nums = list(tree.get("Nums", []))
    ranges = list(zip(nums[::2], nums[1::2], strict=True))
    for kid in tree.get("Kids", []):
        ranges += _ranges(kid)
    return sorted(ranges, key=lambda pair: pair[0])


def _number(style: str, value: int) -> str:
    if style == "D":
        return str(value)
    if style in {"r", "R"}:
        digits = ""
        for amount, digit in ROMAN:
            count, value = divmod(value, amount)
            digits += digit * count
        return digits if style == "r" else digits.upper()
    letter = chr(ord("a") + (value - 1) % 26) * ((value - 1) // 26 + 1)
    return letter if style == "a" else letter.upper()


def labels(doc: Document) -> list[str]:
    """Return the label of each page, as ISO 32000-2 section 12.4.2 numbers it."""
    ranges = _ranges(doc.catalog["PageLabels"])
    out = []
    for index in range(len(doc.pages)):
        start, label = next((start, label) for start, label in reversed(ranges) if start <= index)
        prefix = label["P"].text if "P" in label else ""
        value = label.get("St", 1) + index - start
        out.append(prefix + (_number(str(label["S"]), value) if "S" in label else ""))
    return out


def test_deleting_pages_keeps_the_labels_of_the_pages_that_stay() -> None:
    doc = labelled_pdf(LABELS)
    assert labels(doc) == ["i", "ii", "1", "2", "3", "A-1", "A-2"]

    del doc.pages[1]
    del doc.pages[2]

    assert numbers(doc) == [0, 2, 4, 5, 6]
    assert labels(doc) == ["i", "1", "3", "A-1", "A-2"]
    assert labels(pyropdf.open(doc.to_bytes())) == ["i", "1", "3", "A-1", "A-2"]
    doc.history.undo()
    doc.history.undo()
    assert labels(doc) == ["i", "ii", "1", "2", "3", "A-1", "A-2"]


def test_selecting_pages_keeps_their_labels() -> None:
    doc = labelled_pdf(LABELS)

    doc.pages.select([6, 0, 4, 2, 3])

    assert labels(doc) == ["A-2", "i", "3", "1", "2"]
    assert [start for start, _ in _ranges(doc.catalog["PageLabels"])] == [0, 1, 2, 3]


def test_moving_a_page_keeps_the_labels() -> None:
    doc = labelled_pdf(LABELS)

    doc.pages.move(0, 6)

    assert labels(doc) == ["ii", "1", "2", "3", "A-1", "A-2", "i"]


def test_a_label_tree_of_several_nodes_becomes_one_node() -> None:
    doc = labelled_pdf(
        "10 0 R",
        extra={
            10: "<< /Kids [11 0 R 12 0 R] >>",
            11: "<< /Limits [0 2] /Nums [0 << /S /r >> 2 << /S /D >>] >>",
            12: "<< /Limits [5 5] /Nums [5 13 0 R] >>",
            13: "<< /P (A-) /S /D >>",
        },
    )

    del doc.pages[0]

    assert labels(doc) == ["ii", "1", "2", "3", "A-1", "A-2"]
    assert "Kids" not in doc.objects[10]
    assert {11, 12, 13}.isdisjoint(doc.objects)


@pytest.mark.parametrize(
    ("labels_before", "deleted"),
    [(LABELS, 6), ("<< /Nums [2 << /S /D >>] >>", 0)],
    ids=["unchanged", "no_first_range"],
)
def test_labels_stay_when_no_page_changes_its_label(labels_before: str, deleted: int) -> None:
    doc = labelled_pdf(labels_before)
    before = _ranges(doc.catalog["PageLabels"])

    del doc.pages[deleted]

    assert _ranges(doc.catalog["PageLabels"]) == before
