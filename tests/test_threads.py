"""Several threads reading one document while another commits (DESIGN.md section 4.6).

A read clones the current version and works on it without locks, and a
commit publishes a complete version, so a read sees one committed version
whole, never a mix of two. These tests run on every CPython; on a
free-threaded build the reads run in parallel. Parts ported from pylopdf's
tests/test_free_threaded.py at 036be111cafe77755db4bb2be17bc70a8f2f9d82;
the speed of parallel rendering is measured under evidence/threads/
instead of asserted, since CI runners share their cores.
"""

from __future__ import annotations

import sys
import sysconfig
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from test_pages import build_raw_pdf

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable

IS_FREE_THREADED = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
ASSETS = Path(__file__).parent / "assets" / "real_world"
READERS = 4
COMMITS = 60

#: The two states that each commit switches between: a page size and the
#: content that fills its left half, which only that size shows whole.
STATES = (
    ([0, 0, 40, 20], b"0 g 0 0 20 20 re f"),
    ([0, 0, 20, 40], b"0 g 0 0 10 40 re f"),
)


def _document() -> pyropdf.Document:
    content = STATES[0][1]
    return pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 40 20] /Contents 4 0 R >>",
                4: f"<< /Length {len(content)} >>\nstream\n{content.decode()}\nendstream",
            }
        )
    )


def _commit(doc: pyropdf.Document, state: int) -> None:
    """Switch the page to ``state`` in one transaction: two edits, one version."""
    box, content = STATES[state]
    with doc.transaction():
        doc.pages[0].obj["MediaBox"] = box
        doc.objects[4].write(content)


def _while_committing(doc: pyropdf.Document, read: Callable[[], None]) -> int:
    """Run ``read`` in reader threads until a writer has committed every state; return the reads."""
    done = threading.Event()
    counts = [0] * READERS

    def reader(slot: int) -> None:
        while not done.is_set():
            read()
            counts[slot] += 1

    def writer() -> None:
        try:
            for commit in range(COMMITS):
                _commit(doc, (commit + 1) % 2)
        finally:
            done.set()

    with ThreadPoolExecutor(max_workers=READERS + 1) as executor:
        futures = [executor.submit(reader, slot) for slot in range(READERS)]
        futures.append(executor.submit(writer))
        for future in futures:
            future.result()
    return sum(counts)


@pytest.mark.skipif(not IS_FREE_THREADED, reason="requires a free-threaded CPython build")
def test_import_keeps_the_gil_disabled() -> None:
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    assert is_gil_enabled is not None
    assert not is_gil_enabled()


def test_renders_during_commits_see_complete_versions() -> None:
    doc = _document()
    expected = set()
    for state in (0, 1):
        _commit(doc, state)
        pixmap = doc.pages[0].pixmap()
        expected.add((pixmap.width, pixmap.height, bytes(pixmap)))
    page = doc.pages[0]

    def read() -> None:
        pixmap = page.pixmap()
        assert (pixmap.width, pixmap.height, bytes(pixmap)) in expected

    assert _while_committing(doc, read) > 0


def test_saves_during_commits_write_complete_versions() -> None:
    doc = _document()

    def read() -> None:
        with pyropdf.open(doc.to_bytes()) as saved:
            box = list(saved.pages[0].obj["MediaBox"])
            assert (box, saved.objects[4].read()) in [(list(b), c) for b, c in STATES]

    assert _while_committing(doc, read) > 0


def test_object_reads_during_commits_see_committed_values() -> None:
    # Each call reads one version; iterating a handle reads each element
    # from the version current at that step, as iterating a dict that
    # another thread edits would.
    doc = _document()
    page = doc.pages[0]
    sizes = [(box[2], box[3]) for box, _ in STATES]
    contents = [content for _, content in STATES]

    def read() -> None:
        rect = page.rect
        assert (rect.x1, rect.y1) in sizes
        assert doc.objects[4].read() in contents
        assert len(doc.pages) == 1

    assert _while_committing(doc, read) > 0


def test_a_transaction_belongs_to_the_thread_that_opened_it() -> None:
    doc = _document()
    opened = threading.Event()
    release = threading.Event()
    edited = threading.Event()

    def owner() -> None:
        with doc.transaction("owner"):
            _commit(doc, 1)
            assert doc.objects[4].read() == STATES[1][1]
            opened.set()
            assert release.wait(timeout=10)

    def editor() -> None:
        doc.pages[0].rotation = 90
        edited.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        owning = executor.submit(owner)
        assert opened.wait(timeout=10)
        # Another thread reads the committed version, not the open writes.
        assert doc.objects[4].read() == STATES[0][1]
        assert doc.pages[0].rect.x1 == STATES[0][0][2]
        # Its edit waits for the transaction to end, then commits on its own.
        editing = executor.submit(editor)
        assert not edited.wait(timeout=0.2)
        release.set()
        owning.result()
        editing.result()

    assert doc.objects[4].read() == STATES[1][1]
    assert doc.pages[0].rotation == 90
    assert doc.history.steps[-2:] == ("owner", "")


def test_pages_render_alike_in_parallel_and_in_turn() -> None:
    doc = pyropdf.open((ASSETS / "bill-hr815.pdf").read_bytes())
    pages = list(doc.pages)[:24]
    serial = [bytes(page.pixmap(dpi=24)) for page in pages]

    with ThreadPoolExecutor(max_workers=READERS) as executor:
        parallel = list(executor.map(lambda page: bytes(page.pixmap(dpi=24)), pages))

    assert parallel == serial


def test_distinct_documents_render_alike_in_parallel() -> None:
    paths = sorted(ASSETS.glob("*.pdf"))

    def first_page(path: Path) -> bytes:
        with pyropdf.open(path.read_bytes()) as doc:
            return bytes(doc.pages[0].pixmap(dpi=24))

    serial = [first_page(path) for path in paths]
    with ThreadPoolExecutor(max_workers=READERS) as executor:
        assert list(executor.map(first_page, paths)) == serial
