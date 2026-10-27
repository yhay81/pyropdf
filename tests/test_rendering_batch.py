"""doc.render: encoded images of many pages of one version.

Ported from the ``doc.render`` assertions of pylopdf's
tests/test_rendering_api.py, tests/test_pylopdf.py, and tests/test_pixmap.py
at 036be111. Intended differences:

- pyropdf has no ``build_pdf`` or ``pages.select``, so documents come from
  the corpus and edits from ``pages.move``.
- An out-of-range entry raises ``IndexError`` with the message of
  ``doc.pages[index]``, and other threads may commit during a batch, which
  renders the version current when it starts.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from test_content_model import page_with, stream_object
from test_pixmap import png_header

import pyropdf
from pyropdf import Matrix

if TYPE_CHECKING:
    from collections.abc import Iterator

REAL_WORLD = Path(__file__).parent / "assets" / "real_world"
CORPUS = sorted(REAL_WORLD.glob("*.pdf"))

PNG_RGB = 2
PNG_RGBA = 6


def _open(name: str = "usrguide.pdf") -> pyropdf.Document:
    """Open a corpus document."""
    return pyropdf.open((REAL_WORLD / name).read_bytes())


def _jpeg_size(data: bytes) -> tuple[int, int, int]:
    """Return a baseline JPEG's width, height, and components from its SOF0 marker."""
    offset = data.index(b"\xff\xc0")
    height, width = int.from_bytes(data[offset + 5 : offset + 7]), int.from_bytes(data[offset + 7 : offset + 9])
    return width, height, data[offset + 9]


def test_render_follows_the_pixmap_options() -> None:
    doc = _open()

    rgb, rgba = doc.render([0], dpi=36) + doc.render([0], dpi=36, alpha=True)

    assert png_header(rgb)[2] == PNG_RGB
    assert png_header(rgba)[2] == PNG_RGBA
    assert rgb == doc.pages[0].pixmap(dpi=36).to_bytes()
    assert rgba == doc.pages[0].pixmap(dpi=36, alpha=True).to_bytes()
    gray = doc.pages[1].pixmap(matrix=(0, 0.5, -0.5, 0), colorspace="gray", background=(255, 0, 0))
    assert doc.render([1], matrix=(0, 0.5, -0.5, 0), colorspace="gray", background=(255, 0, 0)) == [gray.to_bytes()]


def test_annotations_false_leaves_annotation_appearances_out() -> None:
    page = page_with(
        "",
        annots="6 0 R",
        objects={
            6: "<< /Type /Annot /Subtype /Highlight /Rect [20 20 180 80] /AP << /N 7 0 R >> >>",
            7: stream_object("1 0 0 rg 0 0 160 60 re f", "/Type /XObject /Subtype /Form /BBox [0 0 160 60]"),
        },
    )
    doc = page.document

    assert doc.render(annotations=False) == [page.pixmap(annotations=False).to_bytes()]
    assert doc.render() == [page.pixmap().to_bytes()]
    assert doc.render() != doc.render(annotations=False)
    with pytest.raises(TypeError, match="annotations must be a bool"):
        doc.render(annotations=None)  # ty: ignore[invalid-argument-type]


def test_render_matches_page_pixmaps_and_keeps_the_order_of_entries() -> None:
    doc = _open()
    pages = [2, 0, 2, 1, -1]

    expected = [doc.pages[page].pixmap(dpi=36, alpha=True).to_bytes() for page in pages]

    assert doc.render(pages, dpi=36, alpha=True, workers=2) == expected
    assert doc.render(iter(pages), dpi=36, alpha=True, workers=64) == expected


def test_render_defaults_to_every_page() -> None:
    doc = _open("patent-us223898.pdf")

    assert doc.render(alpha=True, workers=1) == [page.pixmap(alpha=True).to_bytes() for page in doc.pages]


def test_render_takes_dpi_background_and_empty_input() -> None:
    doc = _open()

    expected = doc.pages[1].pixmap(dpi=144, background=(255, 255, 255), alpha=True).to_bytes()
    assert doc.render([1], dpi=144, background=(255, 255, 255), alpha=True, workers=2) == [expected]
    assert doc.render([], alpha=True, workers=2) == []
    with pytest.raises(ValueError, match="dpi or matrix, not both"):
        doc.render(matrix=Matrix(4, 4), dpi=36)


@pytest.mark.parametrize("path", CORPUS, ids=[path.name for path in CORPUS])
def test_render_over_the_corpus_matches_each_page(path: Path) -> None:
    doc = pyropdf.open(path.read_bytes())

    assert doc.render(dpi=36) == [page.pixmap(dpi=36).to_bytes() for page in doc.pages]


@pytest.mark.parametrize("workers", [0, -1, 65])
def test_render_refuses_a_worker_count_out_of_range(workers: int) -> None:
    with pytest.raises(ValueError, match="workers"):
        _open().render(alpha=True, workers=workers)


@pytest.mark.parametrize("workers", [True, 1.5])
def test_render_refuses_workers_that_are_not_integers(workers: object) -> None:
    with pytest.raises(TypeError, match="workers"):
        _open().render(alpha=True, workers=workers)  # ty: ignore[invalid-argument-type]


def test_render_checks_every_entry_before_rendering() -> None:
    doc = _open("patent-us223898.pdf")

    with pytest.raises(IndexError, match="page index 4 is out of range for 4 pages"):
        doc.render([0, 4, 1], alpha=True, workers=2)
    with pytest.raises(IndexError, match="out of range"):
        doc.render([-5])
    with pytest.raises(TypeError, match="page indexes must be integers"):
        doc.render([0, "1"])  # ty: ignore[invalid-argument-type]


def test_render_follows_structural_edits() -> None:
    doc = _open("patent-us223898.pdf")
    last = doc.pages[3].pixmap(alpha=True).to_bytes()

    doc.pages.move(3, 0)

    assert doc.render([0, 1], alpha=True, workers=2) == [last, doc.pages[1].pixmap(alpha=True).to_bytes()]


def test_render_bounds_the_entries_before_reading_them_all() -> None:
    doc = _open("pdfium-type3.pdf")
    consumed = 0

    def pages() -> Iterator[int]:
        nonlocal consumed
        for _ in range(5000):
            consumed += 1
            yield 0

    with pytest.raises(pyropdf.LimitError, match="4096 entries") as caught:
        doc.render(pages(), alpha=True, workers=2)
    assert caught.value.code == "page_entry_count"
    assert consumed == 4097
    assert len(doc.render([0] * 4096, matrix=Matrix(0.05, 0.05), workers=8)) == 4096


def test_render_bounds_the_encoded_bytes_of_all_pages() -> None:
    doc = _open()
    expected = [doc.pages[0].pixmap(alpha=True).to_bytes(), doc.pages[1].pixmap(alpha=True).to_bytes()]
    total = sum(map(len, expected))

    with pytest.raises(pyropdf.LimitError, match=f"exceeds the {total - 1}-byte encoded-output limit") as caught:
        doc.render([0, 1], alpha=True, workers=2, max_size=total - 1)
    assert caught.value.code == "render_output_size"
    assert doc.render([0, 1], alpha=True, workers=2, max_size=total) == expected
    assert doc.render([0, 1], alpha=True, workers=2, max_size=None) == expected


@pytest.mark.parametrize(
    ("max_size", "error"), [(True, TypeError), (1.5, TypeError), ("1024", TypeError), (0, ValueError), (-1, ValueError)]
)
def test_render_refuses_a_bad_size_limit(max_size: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="max_size"):
        _open().render(alpha=True, max_size=max_size)  # ty: ignore[invalid-argument-type]


def test_jpeg_output_refuses_alpha(tmp_path: Path) -> None:
    page = _open().pages[0]
    pixmap = page.pixmap(alpha=True)

    with pytest.raises(ValueError, match="JPEG has no alpha channel"):
        pixmap.to_bytes("jpeg")
    with pytest.raises(ValueError, match="JPEG has no alpha channel"):
        pixmap.save(tmp_path / "page.jpg")
    with pytest.raises(ValueError, match="JPEG has no alpha channel"):
        page.document.render(alpha=True, format="jpeg")
    with pytest.raises(ValueError, match="format must be 'png' or 'jpeg'"):
        page.document.render(format="webp")  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("quality", [0, 101, True, 9.5, "high"])
def test_quality_must_be_an_integer_from_1_through_100(quality: object) -> None:
    page = _open().pages[0]

    with pytest.raises((TypeError, ValueError), match="quality must be an integer from 1 through 100"):
        page.pixmap().to_bytes("jpeg", quality=quality)  # ty: ignore[invalid-argument-type]
    with pytest.raises((TypeError, ValueError), match="quality must be an integer from 1 through 100"):
        page.document.render(format="jpeg", quality=quality)  # ty: ignore[invalid-argument-type]


def test_jpeg_output_keeps_size_and_components(tmp_path: Path) -> None:
    page = page_with("BT /F1 24 Tf 10 40 Td (Rendered) Tj ET")

    assert _jpeg_size(page.pixmap(dpi=36).to_bytes("jpeg")) == (100, 50, 3)
    assert _jpeg_size(page.pixmap(dpi=36, colorspace="gray").to_bytes("jpeg")) == (100, 50, 1)
    low, high = (page.pixmap().to_bytes("jpeg", quality=quality) for quality in (10, 100))
    assert len(low) < len(high)
    batch = page.document.render(dpi=36, format="jpeg", quality=10, colorspace="gray")
    assert batch == [page.pixmap(dpi=36, colorspace="gray").to_bytes("jpeg", quality=10)]
    page.pixmap().save(tmp_path / "page.JPG", quality=10)
    assert (tmp_path / "page.JPG").read_bytes() == low


def test_a_commit_from_another_thread_leaves_a_batch_on_its_version() -> None:
    doc = _open("patent-us223898.pdf")
    first, second = (page.pixmap(dpi=36).to_bytes() for page in doc.pages[:2])
    done = threading.Event()

    def swap() -> None:
        while not done.is_set():
            doc.pages.move(0, 1)

    writer = threading.Thread(target=swap)
    writer.start()
    try:
        for _ in range(10):
            assert doc.render([0, 1, 0, 1], dpi=36, workers=4) in ([first, second] * 2, [second, first] * 2)
    finally:
        done.set()
        writer.join()


def test_render_refuses_a_closed_document() -> None:
    doc = _open()
    doc.close()

    with pytest.raises(pyropdf.DocumentClosedError):
        doc.render()
