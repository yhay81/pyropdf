"""Transactions group edits into one step, which ``doc.history`` undoes and redoes.

The cases of pylopdf's tests/test_transactions.py at 884af0db, ported with
page edits in place of metadata and text, which come later. Intended
differences, as DESIGN.md section 4.1 and #12 decided:

- An edit outside a transaction is a step of its own, named ``""``; pylopdf
  cleared the history instead.
- Handles stay valid across undo while their object is in the restored
  version, and a page deleted by a failed transaction was never deleted.
- ``max_size = 0`` keeps no step instead of being refused.
- Locked encrypted documents and ``is_dirty`` wait for #39 and later work.
"""

from __future__ import annotations

import resource
import sys

import pytest
from test_pages import numbered

import pyropdf
from pyropdf import Document


def widths(doc: Document) -> list[int]:
    return [int(page.rect[2]) for page in doc.pages]


def two_steps() -> Document:
    doc = pyropdf.open(numbered(2))
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
    with doc.transaction("delete"):
        del doc.pages[1]
    return doc


def test_a_transaction_records_one_step() -> None:
    doc = two_steps()
    assert doc.history.steps == ("rotate", "delete")
    assert doc.history.position == 2
    assert doc.history.can_undo
    assert not doc.history.can_redo
    assert repr(doc.history) == "<History 2/2>"


def test_undo_and_redo_walk_the_steps() -> None:
    doc = two_steps()
    doc.history.undo()
    assert len(doc.pages) == 2
    assert doc.pages[0].rotation == 90
    doc.history.undo()
    assert doc.pages[0].rotation == 0
    assert doc.history.position == 0
    assert not doc.history.can_undo
    assert doc.history.can_redo
    doc.history.redo()
    assert doc.pages[0].rotation == 90
    assert len(doc.pages) == 2
    doc.history.redo()
    assert len(doc.pages) == 1
    assert doc.history.position == 2


def test_undo_and_redo_restore_versions_exactly() -> None:
    doc = pyropdf.open(numbered(3))
    saved = [doc.to_bytes()]
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 180
    saved.append(doc.to_bytes())
    with doc.transaction("new"):
        doc.pages.new((50, 50), index=1)
    saved.append(doc.to_bytes())
    doc.history.undo()
    assert doc.to_bytes() == saved[1]
    doc.history.undo()
    assert doc.to_bytes() == saved[0]
    doc.history.redo()
    assert doc.to_bytes() == saved[1]
    doc.history.redo()
    assert doc.to_bytes() == saved[2]


def test_an_undone_state_saves_and_reopens() -> None:
    doc = two_steps()
    doc.history.undo()
    with pyropdf.open(doc.to_bytes()) as reopened:
        assert len(reopened.pages) == 2
        assert reopened.pages[0].rotation == 90


def test_undoing_to_the_original_saves_incrementally_as_read() -> None:
    original = numbered(1)
    doc = pyropdf.open(original)
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
    doc.history.undo()
    assert doc.to_bytes(incremental=True) == original


def test_a_failing_transaction_leaves_the_document_as_it_was() -> None:
    doc = pyropdf.open(numbered(2))
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
    page = doc.pages[1]
    before = doc.to_bytes()

    def broken() -> None:
        with doc.transaction("broken"):
            doc.pages[0].rotation = 180
            del doc.pages[1]
            assert len(doc.pages) == 1
            msg = "stop"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="stop"):
        broken()
    assert doc.to_bytes() == before
    assert doc.history.steps == ("rotate",)
    # The page was never deleted.
    assert page.index == 1


def test_a_nested_transaction_restores_only_its_own_edits() -> None:
    doc = pyropdf.open(numbered(2))

    def inner() -> None:
        with doc.transaction("inner"):
            del doc.pages[1]
            msg = "inner"
            raise ValueError(msg)

    with doc.transaction("outer"):
        doc.pages[0].rotation = 90
        with pytest.raises(ValueError, match="inner"):
            inner()
        assert len(doc.pages) == 2
        with doc.transaction("joined"):
            doc.pages[1].rotation = 270
    assert widths(doc) == [500, 500]
    assert doc.history.steps == ("outer",)
    doc.history.undo()
    assert [page.rotation for page in doc.pages] == [0, 0]


def test_a_transaction_without_edits_records_nothing() -> None:
    doc = pyropdf.open(numbered(1))
    with doc.transaction("read"):
        _ = doc.pages[0].rect
    assert doc.history.steps == ()


def test_an_edit_outside_a_transaction_is_a_step_of_its_own() -> None:
    doc = two_steps()
    doc.pages[0].rotation = 180
    assert doc.history.steps == ("rotate", "delete", "")
    doc.history.undo()
    assert doc.pages[0].rotation == 90


def test_a_new_step_after_undo_drops_the_redo_steps() -> None:
    doc = two_steps()
    doc.history.undo()
    with doc.transaction("turn"):
        doc.pages[0].rotation = 270
    assert doc.history.steps == ("rotate", "turn")
    assert not doc.history.can_redo
    with pytest.raises(pyropdf.PdfError, match="nothing to redo"):
        doc.history.redo()
    doc.history.undo()
    doc.history.undo()
    with pytest.raises(pyropdf.PdfError, match="nothing to undo"):
        doc.history.undo()


def test_saving_keeps_the_history() -> None:
    doc = pyropdf.open(numbered(1))
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
    doc.to_bytes()
    assert doc.history.steps == ("rotate",)
    doc.history.undo()
    assert doc.pages[0].rotation == 0


def test_handles_follow_their_objects_across_undo() -> None:
    doc = pyropdf.open(numbered(2))
    first = doc.pages[0]
    with doc.transaction("new"):
        added = doc.pages.new((50, 50))
    doc.history.undo()
    assert first.index == 0
    with pytest.raises(pyropdf.StalePageError):
        _ = added.index
    doc.history.redo()
    assert added.index == 2


def test_the_budget_drops_the_oldest_steps() -> None:
    def four_steps() -> tuple[Document, list[int]]:
        doc = pyropdf.open(numbered(1))
        sizes = []
        for index in range(4):
            with doc.transaction(f"step {index}"):
                doc.pages[0].rotation = 90 * (index + 1) % 360
            sizes.append(doc.history.size)
        return doc, sizes

    _, sizes = four_steps()
    doc, _ = four_steps()
    # What the two newest steps keep.
    doc.history.max_size = sizes[3] - sizes[1]
    assert doc.history.steps == ("step 2", "step 3")
    doc.history.undo()
    doc.history.undo()
    assert doc.pages[0].rotation == 180
    assert not doc.history.can_undo


def test_a_step_larger_than_the_budget_is_not_kept() -> None:
    doc = pyropdf.open(numbered(1))
    doc.history.max_size = 1
    with doc.transaction("big"):
        doc.pages[0].rotation = 90
    assert doc.history.steps == ()
    assert doc.pages[0].rotation == 90
    doc.history.max_size = 0
    assert doc.history.max_size == 0


def test_the_default_budget_is_256_mib_and_none_keeps_every_step() -> None:
    doc = pyropdf.open(numbered(1))
    assert doc.history.max_size == 256 * 1024 * 1024
    doc.history.max_size = None
    for index in range(20):
        with doc.transaction(str(index)):
            doc.pages[0].rotation = 90 * (index % 4 + 1) % 360 or 90
    assert len(doc.history.steps) == 20


@pytest.mark.parametrize("value", [-1, 1.5, True, "1"])
def test_the_budget_is_a_non_negative_integer_or_none(value: object) -> None:
    doc = pyropdf.open(numbered(1))
    with pytest.raises(ValueError, match="max_size"):
        doc.history.max_size = value  # ty: ignore[invalid-assignment]


def test_clear_forgets_every_step() -> None:
    doc = two_steps()
    doc.history.clear()
    assert doc.history.steps == ()
    assert len(doc.pages) == 1
    assert doc.history.max_size == 256 * 1024 * 1024


def test_the_name_must_be_text() -> None:
    doc = pyropdf.open(numbered(1))
    with pytest.raises(TypeError, match="name must be a str"):
        doc.transaction(1)  # ty: ignore[invalid-argument-type]


def test_a_closed_document_has_no_history() -> None:
    doc = two_steps()
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        _ = doc.history
    with pytest.raises(pyropdf.DocumentClosedError):
        doc.transaction()


def test_reading_history_inside_a_transaction_keeps_previous_steps() -> None:
    doc = two_steps()
    with doc.transaction("turn"):
        doc.pages[0].rotation = 180
        assert doc.history.steps == ("rotate", "delete")
        assert doc.history.can_undo
    assert doc.history.steps == ("rotate", "delete", "turn")
    doc.history.undo()
    doc.history.undo()
    assert len(doc.pages) == 2


@pytest.mark.parametrize("operation", ["undo", "redo", "clear"])
def test_history_cannot_replace_the_version_inside_a_transaction(operation: str) -> None:
    doc = two_steps()
    if operation == "redo":
        doc.history.undo()
    steps, position = doc.history.steps, doc.history.position
    with doc.transaction("read"), pytest.raises(pyropdf.PdfError, match="inside a transaction"):
        getattr(doc.history, operation)()
    assert doc.history.steps == steps
    assert doc.history.position == position


@pytest.mark.skipif(sys.platform == "win32", reason="ru_maxrss is POSIX")
def test_a_failing_transaction_copies_nothing_of_a_large_document() -> None:
    # 1,760 pages of 20 KiB content: a copy of the document would raise the
    # peak resident memory by tens of MiB.
    pages = 1_760
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages))
    objects = {1: "<< /Type /Catalog /Pages 2 0 R >>", 2: f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>"}
    content = "0 g " + "1 1 1 1 re f " * 1_600
    for i in range(pages):
        objects[3 + 2 * i] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents {4 + 2 * i} 0 R >>"
        objects[4 + 2 * i] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    from test_pages import build_raw_pdf  # noqa: PLC0415 - the large builder is this test's alone.

    data = build_raw_pdf(objects)
    doc = pyropdf.open(data)
    del data
    _ = len(doc.pages)
    before = doc.to_bytes(incremental=True)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    def fail() -> None:
        with doc.transaction("lost"):
            doc.pages[5].rotation = 90
            raise RuntimeError

    for _ in range(50):
        with pytest.raises(RuntimeError):
            fail()
    grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - peak
    # ru_maxrss is in KiB on Linux and in bytes on macOS.
    grown_bytes = grown if sys.platform == "darwin" else grown * 1024
    assert grown_bytes < 8 * 1024 * 1024, grown_bytes
    assert doc.history.steps == ()
    assert doc.to_bytes(incremental=True) == before
