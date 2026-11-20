"""Tests for article threads, beads, and thread actions (ISO 32000-2 §12.4.3, §12.6.4.7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest
from test_preservation import snapshot

import pyropdf
from pyropdf import (
    Bead,
    Beads,
    BeadSpecification,
    Destination,
    LimitError,
    MalformedPdfError,
    Name,
    NavigationStep,
    OutlineEntry,
    Rect,
    StaleObjectError,
    StalePageError,
    Thread,
    ThreadAction,
    Threads,
)

if TYPE_CHECKING:
    from pathlib import Path


def _pdf_with_version(version: str, page_count: int = 3) -> bytes:
    """Build a minimal valid PDF of ``version`` with ``page_count`` pages."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(page_count))
    parts = [
        f"%PDF-{version}\n".encode("ascii"),
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {page_count} >>\nendobj\n".encode("ascii"),
    ]
    parts.extend(
        f"{3 + i} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << >> >>\nendobj\n".encode(
            "ascii"
        )
        for i in range(page_count)
    )
    body = b"".join(parts)
    offsets = []
    pos = 0
    total_objs = 2 + page_count
    for obj_num in range(1, total_objs + 1):
        marker = f"{obj_num} 0 obj".encode("ascii")
        idx = body.index(marker, pos)
        offsets.append(idx)
        pos = idx + 1
    xref_offset = len(body)
    xref = [f"xref\n0 {total_objs + 1}\n0000000000 65535 f \n".encode("ascii")]
    xref.extend(f"{off:010d} 00000 n \n".encode("ascii") for off in offsets)
    trailer = f"trailer\n<< /Size {total_objs + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    return body + b"".join(xref) + trailer


def _assert_valid(doc: pyropdf.Document) -> None:
    """Assert zero structural defects in memory (aside from unsaved trailer /Size) and after saving."""
    assert [d for d in doc.check().defects if d.clause != "FileTrailer/Size"] == []
    with pyropdf.open(doc.to_bytes()) as saved:
        assert not saved.check()


@pytest.mark.spec("articles", "read", "interpret", "create", "edit", "preserve")
def test_threads_creation_info_undo_and_versions(tmp_path: Path) -> None:
    path = tmp_path / "articles.pdf"
    path.write_bytes(_pdf_with_version("1.4", page_count=3))
    created_dt = datetime(2026, 5, 1, 9, 30, 0, tzinfo=UTC)
    modified_dt = datetime(2026, 5, 2, 14, 15, 0, tzinfo=timezone(timedelta(hours=9)))

    with pyropdf.open(path) as doc:
        assert (isinstance(doc.threads, Threads), len(doc.threads), repr(doc.threads)) == (True, 0, "Threads([])")
        t1 = doc.threads.add(
            [BeadSpecification(0, (20, 30, 140, 180)), (-1, (20, 30, 140, 180))],
            title="Lead Story",
            author="Ada Lovelace",
            subject="Computing",
            keywords="analytical, engine",
            created=created_dt,
            modified=modified_dt,
        )
        assert (isinstance(t1, Thread), t1.document is doc, doc.threads[0] == t1, hash(doc.threads[0]) == hash(t1)) == (
            True,
            True,
            True,
            True,
        )
        assert doc.catalog["Threads"].is_indirect
        assert (t1.title, t1.author, t1.subject, t1.keywords, t1.created, t1.modified) == (
            "Lead Story",
            "Ada Lovelace",
            "Computing",
            "analytical, engine",
            "D:20260501093000Z",
            "D:20260502141500+09'00'",
        )
        b0, b1 = t1.beads[0], t1.beads[1]
        assert (isinstance(t1.beads, Beads), isinstance(b0, Bead), b0.thread == t1, b1.thread == t1) == (
            True,
            True,
            True,
            True,
        )
        assert (b0.page, b1.page, b0.rect) == (doc.pages[0], doc.pages[2], Rect(20, 30, 140, 180))
        assert ("T" in b0.obj, "T" not in b1.obj, b0.obj["N"] == b1.obj, b0.obj["V"] == b1.obj) == (
            True,
            True,
            True,
            True,
        )
        _assert_valid(doc)

        doc.history.undo()
        with pytest.raises(StaleObjectError):
            _ = t1.title
        with pytest.raises(StaleObjectError):
            _ = b0.rect
        assert ("stale Thread" in repr(t1), "stale Bead" in repr(b0)) == (True, True)
        doc.history.redo()
        assert (t1.title, b0.rect) == ("Lead Story", Rect(20, 30, 140, 180))
        doc.save(path, incremental=True)

    for ver in ("1.1", "1.4", "1.7", "2.0"):
        v_path = tmp_path / f"v_{ver}.pdf"
        v_path.write_bytes(_pdf_with_version(ver, page_count=1))
        with pyropdf.open(v_path) as v_doc:
            v_doc.threads.add([(0, (12, 12, 80, 80))], title=f"PDF {ver}")
            v_doc.save(v_path)
        with pyropdf.open(v_path) as checked:
            assert not checked.check()


@pytest.mark.spec("articles", "read", "interpret", "create", "edit", "preserve")
def test_bead_ring_reordering_deletion_info_cleanup_and_save_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ring.pdf"
    path.write_bytes(_pdf_with_version("1.4", page_count=3))

    with pyropdf.open(path) as doc:
        t1 = doc.threads.add([(0, (20, 30, 140, 180)), (2, (20, 30, 140, 180))], title="Lead Story")
        b0, b1 = t1.beads[0], t1.beads[1]
        threads_arr_xref = doc.catalog["Threads"].xref

        b_pre = t1.beads.add(doc.pages[1], (30, 40, 120, 90), index=0)
        assert (list(t1.beads), t1.obj["F"] == b_pre.obj, b_pre.obj["T"] == t1.obj, "T" not in b0.obj) == (
            [b_pre, b0, b1],
            True,
            True,
            True,
        )
        t1.beads.move(0, 2)
        assert (list(t1.beads), t1.obj["F"] == b0.obj, b0.obj["T"] == t1.obj, "T" not in b_pre.obj) == (
            [b0, b1, b_pre],
            True,
            True,
            True,
        )

        b0_xref = b0.xref
        del t1.beads[0]
        assert (list(t1.beads), b0_xref in doc.objects, "B" in doc.pages[0].obj, b1.obj["T"] == t1.obj) == (
            [b1, b_pre],
            False,
            False,
            True,
        )
        with pytest.raises(ValueError, match="at least one bead"):
            del t1.beads[:]

        t0 = doc.threads.add([(0, (10, 10, 90, 60))], index=0, title="Editorial")
        assert doc.catalog["Threads"].xref == threads_arr_xref
        doc.threads.move(0, 1)
        assert list(doc.threads) == [t1, t0]

        indirect_i = doc.objects.add({"Title": "Custom Indirect"})
        t0.obj["I"] = doc.objects[indirect_i]
        t0.author = "Babbage"
        del t0.title
        assert (t0.title, t0.author) == (None, "Babbage")
        del t0.author
        assert ("I" in t0.obj, indirect_i in doc.objects) == (False, False)
        _assert_valid(doc)
        doc.save(path, incremental=True)

    full_path = tmp_path / "ring_full.pdf"
    with pyropdf.open(path) as reopened:
        assert not reopened.check()
        t0_xref = reopened.threads[1].xref
        del reopened.threads[1]
        assert (len(reopened.threads), t0_xref in reopened.objects) == (1, False)
        reopened.save(full_path)

    with pyropdf.open(full_path) as final:
        assert not final.check()
        del final.threads
        assert (len(final.threads), "Threads" in final.catalog) == (0, False)
        del final.threads
        assert not final.check()


@pytest.mark.spec("articles", "edit", "preserve")
@pytest.mark.parametrize("indirect", [False, True])
def test_bead_move_preserves_page_reading_order_and_action_targets(*, indirect: bool) -> None:
    with pyropdf.Document() as original:
        page = original.pages.new((200, 200))
        original.threads.add([(page, (10, 10, 30, 30)), (page, (40, 40, 60, 60))])
        page.links.add((10, 100, 30, 120), ThreadAction(0))
        page.links.add((40, 100, 60, 120), ThreadAction(0, 0))
        if indirect:
            array_xref = original.objects.add(list(page.obj["B"]))
            page.obj["B"] = original.objects[array_xref]
        base = original.to_bytes()

    with pyropdf.open(base) as doc:
        page, article = doc.pages[0], doc.threads[0]
        first, second = article.beads
        before = snapshot(doc)
        article.beads.move(0, 1)
        assert list(page.obj["B"]) == [second.obj, first.obj] == [bead.obj for bead in article.beads]
        assert page.links[0].target == ThreadAction(0)
        assert page.links[1].target == ThreadAction(0, 1)
        array = page.obj["B"]
        assert array.is_indirect == indirect
        if indirect:
            assert array.xref == array_xref
        after = snapshot(doc)
        assert before.keys() == after.keys()
        assert {xref for xref in before if before[xref] != after[xref]} == {
            article.xref,
            first.xref,
            second.xref,
            array.xref if indirect else page.xref,
        }
        doc.history.undo()
        assert snapshot(doc) == before
        doc.history.redo()
        assert snapshot(doc) == after

        for saved in (doc.to_bytes(), doc.to_bytes(incremental=True), doc.to_bytes(linearize=True)):
            with pyropdf.open(saved) as reopened:
                page, article = reopened.pages[0], reopened.threads[0]
                second, first = article.beads
                assert list(page.obj["B"]) == [second.obj, first.obj]
                assert [second.rect, first.rect] == [Rect(40, 40, 60, 60), Rect(10, 10, 30, 30)]
                assert page.obj["B"].is_indirect == indirect
                assert page.links[0].obj["A"]["D"] == article.obj
                assert page.links[1].obj["A"]["B"] == first.obj


@pytest.mark.spec("articles", "edit", "preserve")
def test_bead_edits_keep_other_threads_in_reading_order_on_shared_pages() -> None:
    with pyropdf.Document() as doc:
        left = doc.pages.new((200, 200))
        right = doc.pages.new((200, 200))
        rect = (10, 10, 30, 30)
        article = doc.threads.add([(left, rect), (right, rect), (left, rect), (right, rect)])
        a, b, c, d = article.beads
        other = doc.threads.add([(left, rect), (right, rect), (left, rect)])
        x, y, z = other.beads
        left_array = doc.objects.add([x.obj, a.obj, z.obj, c.obj])
        right_array = doc.objects.add([b.obj, y.obj, d.obj])
        left.obj["B"] = doc.objects[left_array]
        right.obj["B"] = doc.objects[right_array]

        article.beads.move(0, 2)
        assert list(article.beads) == [b, c, a, d]
        assert list(left.obj["B"]) == [x.obj, c.obj, z.obj, a.obj]
        assert list(right.obj["B"]) == [b.obj, y.obj, d.obj]

        inserted = article.beads.add(left, rect, index=1)
        assert list(left.obj["B"]) == [x.obj, inserted.obj, z.obj, c.obj, a.obj]
        before = snapshot(doc)
        b.page = left
        assert list(left.obj["B"]) == [x.obj, b.obj, z.obj, inserted.obj, c.obj, a.obj]
        assert list(right.obj["B"]) == [y.obj, d.obj]
        assert (left.obj["B"].xref, right.obj["B"].xref) == (left_array, right_array)
        after = snapshot(doc)
        assert {xref for xref in before if before[xref] != after[xref]} == {b.xref, left_array, right_array}
        doc.history.undo()
        assert snapshot(doc) == before
        doc.history.redo()
        assert snapshot(doc) == after


@pytest.mark.spec("articles", "read", "interpret", "create", "edit", "preserve")
def test_bead_coordinates_across_rotations_crops_user_unit_and_page_moves() -> None:
    for rotation in (0, 90, 180, 270):
        with pyropdf.Document() as doc:
            p_plain = doc.pages.new((200, 300))
            p_trans = doc.pages.new((200, 300))
            p_trans.crop_box = (20, 30, 180, 250)
            p_trans.rotation = rotation
            p_trans.obj["UserUnit"] = 2.0

            wanted = Rect(15, 25, 95, 75)
            bead = doc.threads.add([BeadSpecification(p_trans, wanted)]).beads[0]
            assert bead.rect == pytest.approx(wanted)

            edited = Rect(10, 12, 60, 48)
            bead.rect = edited
            assert bead.rect == pytest.approx(edited)
            doc.history.undo()
            assert bead.rect == pytest.approx(wanted)
            doc.history.redo()
            assert bead.rect == pytest.approx(edited)

            bead.page = p_plain
            assert (bead.page == p_plain, "B" not in p_trans.obj, len(p_plain.obj["B"])) == (True, True, 1)
            assert bead.rect == pytest.approx(edited)
            bead.page = 1
            assert (bead.page == p_trans, "B" not in p_plain.obj, len(p_trans.obj["B"])) == (True, True, 1)
            assert bead.rect == pytest.approx(edited)
            _assert_valid(doc)

    with pyropdf.Document() as doc:
        p = doc.pages.new((200, 200))
        t = doc.threads.add([(p, (10, 10, 50, 50)), (p, (60, 60, 100, 100))])
        b0, b1 = t.beads[0], t.beads[1]
        b1.obj["V"] = b1.obj
        assert b1.thread == t
        del b0.obj["T"]
        assert b1.thread == t


@pytest.mark.spec("articles", "edit", "preserve")
def test_page_delete_select_and_insert_maintain_thread_invariants() -> None:
    with pyropdf.Document() as doc:
        for _ in range(4):
            doc.pages.new((200, 200))
        t_span = doc.threads.add(
            [(0, (10, 10, 80, 80)), (1, (10, 10, 80, 80)), (2, (10, 10, 80, 80))],
            title="Spanning",
        )
        t_single = doc.threads.add([(1, (20, 20, 90, 90))], title="Page 1 Only")
        b_span_p0, b_span_p1 = t_span.beads[0], t_span.beads[1]
        b_single = t_single.beads[0]
        p1_xref = doc.pages[1].xref

        link_dead_thread = doc.pages[3].links.add((10, 10, 60, 30), ThreadAction(1))
        link_dead_bead = doc.pages[3].links.add((10, 40, 60, 60), ThreadAction(0, 1))
        _assert_valid(doc)

        del doc.pages[1]
        _assert_valid(doc)
        assert (len(doc.threads), len(t_span.beads), p1_xref in doc.objects) == (1, 2, False)
        assert (t_single.xref in doc.objects, b_single.xref in doc.objects, b_span_p1.xref in doc.objects) == (
            False,
            False,
            False,
        )
        assert (link_dead_thread.target, link_dead_bead.target) == (None, ThreadAction(0, None))

        doc.history.undo()
        assert (len(doc.pages), len(doc.threads), len(t_span.beads)) == (4, 2, 3)
        assert (link_dead_thread.target, link_dead_bead.target) == (ThreadAction(1), ThreadAction(0, 1))
        _assert_valid(doc)

        del doc.pages[0]
        assert (b_span_p0.xref in doc.objects, t_span.beads[0] == b_span_p1, b_span_p1.obj["T"] == t_span.obj) == (
            False,
            True,
            True,
        )
        doc.pages.insert(0, doc.pages[0])
        assert "B" not in doc.pages[0].obj
        doc.pages.select([1, 1])
        assert ("B" in doc.pages[0].obj, "B" not in doc.pages[1].obj) == (True, True)
        _assert_valid(doc)


@pytest.mark.spec("thread-actions", "read", "interpret", "create", "edit", "preserve")
def test_thread_actions_across_links_outline_and_navigation_steps(tmp_path: Path) -> None:
    path = tmp_path / "thread_actions.pdf"
    path.write_bytes(_pdf_with_version("1.7", page_count=2))

    by_index = ThreadAction(0, 1)
    by_thread_only = ThreadAction(1)
    by_title = ThreadAction("First Article", 0)
    remote_title = ThreadAction("External Story", 2, file="issue2.pdf")
    remote_idx = ThreadAction(0, file="issue2.pdf")

    with pyropdf.open(path) as doc:
        doc.threads.add([(0, (10, 10, 90, 90)), (1, (10, 10, 90, 90))], title="First Article")
        doc.threads.add([(1, (100, 10, 180, 90))], title="Second Article")

        link = doc.pages[0].links.add((10, 200, 100, 220), by_index)
        assert (link.target, "ThreadAction" in repr(link), link.obj["A"]["D"] == doc.threads[0].obj) == (
            by_index,
            True,
            True,
        )
        doc.history.undo()
        assert len(doc.pages[0].links) == 0
        doc.history.redo()
        link = doc.pages[0].links[0]

        doc.threads.move(0, 1)
        assert link.target == ThreadAction(1, 1)
        doc.threads.move(1, 0)
        link.target = remote_title

        entries = [
            OutlineEntry(1, "Articles", by_thread_only, is_open=True),
            OutlineEntry(2, "By Title", by_title),
            OutlineEntry(2, "Remote", remote_idx),
        ]
        doc.outline = entries
        assert doc.outline.entries() == entries
        doc.outline[0].destination = by_index
        assert (doc.outline[0].destination, doc.outline[0].page) == (by_index, None)

        doc.pages[0].navigation_steps = [
            NavigationStep(next_action=by_index, previous_action=by_title, duration=1.5),
            NavigationStep(next_action=remote_title),
        ]
        _assert_valid(doc)
        doc.save(path, incremental=True)

    full_path = tmp_path / "thread_actions_full.pdf"
    with pyropdf.open(path) as reopened:
        assert not reopened.check()
        assert (reopened.pages[0].links[0].target, reopened.outline[0].destination) == (remote_title, by_index)
        assert reopened.pages[0].navigation_steps[0].next_action == by_index

        l_int = reopened.pages[1].links.add((10, 10, 60, 30), Destination(0))
        l_int.obj["A"] = {"Type": Name("Action"), "S": Name("Thread"), "D": 0, "B": reopened.threads[0].beads[1].obj}
        del l_int.obj["Dest"]

        l_filespec = reopened.pages[1].links.add((10, 40, 60, 60), Destination(0))
        l_filespec.obj["A"] = {
            "Type": Name("Action"),
            "S": Name("Thread"),
            "F": {"Type": Name("Filespec"), "F": "archive.pdf", "UF": "archive.pdf"},
            "D": "Feature",
            "B": 3,
        }
        del l_filespec.obj["Dest"]
        _assert_valid(reopened)
        reopened.save(full_path)

    with pyropdf.open(full_path) as final:
        assert not final.check()
        assert final.pages[1].links[0].target == ThreadAction(0, 1)
        assert final.pages[1].links[1].target == ThreadAction("Feature", 3, file="archive.pdf")


def test_bead_and_action_input_validation() -> None:
    bad_true: Any = True
    bad_false: Any = False
    for bad_page in (bad_true, "0"):
        with pytest.raises(TypeError):
            BeadSpecification(bad_page, (0, 0, 10, 10))  # ty: ignore[invalid-argument-type]
    for bad_rect in ("not a rect", (bad_true, 0, 10, 10)):
        with pytest.raises(TypeError):
            BeadSpecification(0, bad_rect)  # ty: ignore[invalid-argument-type]
    for invalid_rect in ((0, 0, 10), (10, 10, 10, 20), (0, 0, float("inf"), 20)):
        with pytest.raises(ValueError, match="rect"):
            BeadSpecification(0, invalid_rect)

    with pytest.raises(TypeError):
        ThreadAction(bad_false)
    with pytest.raises(TypeError):
        ThreadAction(0, bad_true)
    with pytest.raises(TypeError):
        ThreadAction(0, file=123)  # ty: ignore[invalid-argument-type]
    for bad_thread in (-1, ""):
        with pytest.raises(ValueError, match="thread"):
            ThreadAction(bad_thread)
    with pytest.raises(ValueError, match="non-negative"):
        ThreadAction(0, -1)
    with pytest.raises(ValueError, match="empty"):
        ThreadAction(0, file="")


def test_document_thread_errors_and_safety_limits() -> None:
    with pyropdf.Document() as doc, pyropdf.Document() as other:
        p0 = doc.pages.new((100, 100))
        other_page = other.pages.new((100, 100))
        p_dead = doc.pages.new((100, 100))
        del doc.pages[1]

        with pytest.raises(ValueError, match="at least one bead"):
            doc.threads.add([])
        with pytest.raises(TypeError):
            doc.threads.add("not-an-iterable")  # ty: ignore[invalid-argument-type]
        with pytest.raises(ValueError, match="another document"):
            doc.threads.add([(other_page, (0, 0, 10, 10))])
        with pytest.raises(IndexError):
            doc.threads.add([(5, (0, 0, 10, 10))])
        with pytest.raises(StalePageError):
            doc.threads.add([(p_dead, (0, 0, 10, 10))])

        t = doc.threads.add([(p0, (10, 10, 50, 50))])
        with pytest.raises(TypeError):
            t.title = 123  # ty: ignore[invalid-assignment]
        with pytest.raises(ValueError, match="empty"):
            t.title = ""
        with pytest.raises(LimitError) as text_exc:
            t.title = "x" * (1024 * 1024 + 1)
        assert text_exc.value.code == "text_input_size"

        for bad_action in (ThreadAction(5), ThreadAction(0, 3)):
            with pytest.raises(ValueError, match="out of range"):
                p0.links.add((0, 0, 20, 20), bad_action)
        with pytest.raises(ValueError, match="out of range"):
            doc.outline = [OutlineEntry(1, "Bad", ThreadAction(5))]
        assert (len(p0.links), len(doc.outline)) == (0, 0)

        t.beads[0].obj["R"] = [1, 2]
        with pytest.raises(MalformedPdfError):
            _ = t.beads[0].rect

    with pyropdf.Document() as doc:
        page = doc.pages.new((100, 100))
        with pytest.raises(LimitError) as ring_exc:
            doc.threads.add([(page, (0, 0, 10, 10)) for _ in range(4097)])
        assert ring_exc.value.code == "page_entry_count"

        t = doc.threads.add([(page, (0, 0, 10, 10))])
        with doc.transaction():
            xrefs = [doc.objects.add({"Type": Name("Bead"), "P": page.obj, "R": [0, 0, 10, 10]}) for _ in range(4097)]
            for idx in range(4097):
                doc.objects[xrefs[idx]] = {
                    "Type": Name("Bead"),
                    "P": page.obj,
                    "R": [0, 0, 10, 10],
                    "N": doc.objects[xrefs[(idx + 1) % 4097]],
                    "V": doc.objects[xrefs[(idx - 1) % 4097]],
                }
            t.obj["F"] = doc.objects[xrefs[0]]
        with pytest.raises(LimitError) as read_exc:
            _ = len(t.beads)
        assert read_exc.value.code == "page_entry_count"
