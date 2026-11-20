"""Budgets, retained revisions and pending output through public composite operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

import pyropdf


def _target(hops: int) -> pyropdf.EmbeddedDestination:
    target = pyropdf.EmbeddedTarget(embedded_file="leaf.pdf")
    for _ in range(hops - 1):
        target = pyropdf.EmbeddedTarget(relationship="parent", next_target=target)
    return pyropdf.EmbeddedDestination(name="summary", target=target)


def _source(*, hops: int = 1, filled: bool = False) -> bytes:
    with pyropdf.Document() as doc:
        page = doc.pages.new((160, 120))
        for index, name in enumerate(("first", "last")):
            top = 25 * index
            page.widgets.add(pyropdf.TextField(name, (0, top, 80, top + 20), value=name if filled else None))
        root = doc.structure.add("Document")
        for role in ("H1", "P", "P"):
            root.children.add(role)
        doc.open_action = _target(hops)
        return doc.to_bytes()


def _state(doc: pyropdf.Document) -> tuple[object, ...]:
    return doc.revisions, doc.history.steps, doc.history.position, doc.to_bytes(incremental=True)


def test_structure_count_returns_a_complete_walk_at_the_boundary() -> None:
    with pyropdf.open(_source()) as doc:
        before = _state(doc)
        with pytest.raises(pyropdf.LimitError) as error:
            doc.structure.walk(max_elements=3)
        assert error.value.code == "object_count"
        assert _state(doc) == before
        assert [node.role for node in doc.structure.walk(max_elements=4)] == ["Document", "H1", "P", "P"]


@pytest.mark.parametrize("kind", ["fdf", "xfdf"])
def test_form_import_checks_source_bytes_before_any_edit_and_preserves_redo(kind: str) -> None:
    with pyropdf.open(_source(filled=True)) as source, pyropdf.open(_source()) as doc:
        doc.form["last"].value = "redo me"
        doc.history.undo()
        before = _state(doc)
        if kind == "fdf":
            data = source.form.to_fdf()
            with pytest.raises(pyropdf.LimitError) as error:
                doc.form.import_fdf(data, limits=pyropdf.DocumentLimits(max_file_size=len(data) - 1))
            assert error.value.code == "file_size"
            assert _state(doc) == before
            changed = doc.form.import_fdf(data, limits=pyropdf.DocumentLimits(max_file_size=len(data)))
        else:
            data = source.form.to_xfdf().encode()
            with pytest.raises(pyropdf.LimitError) as error:
                doc.form.import_xfdf(data, max_size=len(data) - 1)
            assert error.value.code == "form_field_input_size"
            assert _state(doc) == before
            changed = doc.form.import_xfdf(data, max_size=len(data))
        assert changed == ["first", "last"]
        assert [doc.form[name].value for name in changed] == changed
        assert doc.history.steps == (f"import {kind}",)
        doc.history.undo()
        assert doc.form["first"].value is None
        assert doc.form["last"].value is None


def test_cancelled_composite_read_leaves_history_available() -> None:
    token = pyropdf.CancellationToken()
    with pyropdf.open(_source(hops=3), limits=pyropdf.DocumentLimits(cancellation=token)) as doc:
        assert doc.open_action == _target(3)
        assert len(list(doc.structure.walk())) == 4
        before = doc.history.steps, doc.history.position
        token.cancel()
        with pytest.raises(pyropdf.LimitError) as action_error:
            _ = doc.open_action
        assert action_error.value.code == "cancelled"
        with pytest.raises(pyropdf.LimitError) as walk_error:
            doc.structure.walk()
        assert walk_error.value.code == "cancelled"
        assert (doc.history.steps, doc.history.position) == before


def test_reading_embedded_target_hops_shares_the_document_memory_budget() -> None:
    expected = _target(16)
    with pyropdf.Document() as seed:
        seed.pages.new((80, 80))
        seed.open_action = expected
        data = seed.to_bytes()
    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_memory_size=8_000)) as small:
        before = _state(small)
        with pytest.raises(pyropdf.LimitError) as error:
            _ = small.open_action
        assert error.value.code == "memory_size"
        assert _state(small) == before
        assert small.pages[0].rect.width == 80
    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_memory_size=32_000)) as enough:
        assert enough.open_action == expected
        assert enough.open_action == expected
        assert enough.history.steps == ()


def test_nested_imports_share_the_canvas_budget_and_discard_pending_output() -> None:
    with pyropdf.open(_source(filled=True)) as source:
        fdf = source.form.to_fdf(fields=["last"])
        xfdf = source.form.to_xfdf(fields=["first"])
    with pyropdf.open(_source(), limits=pyropdf.DocumentLimits(max_memory_size=200_000)) as doc:
        page = doc.pages[0]
        before = _state(doc)
        completed: list[int] = []
        with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:  # noqa: PT012 - exercise the whole block.
            canvas.rect((1, 75, 20, 90), fill=pyropdf.Color(1, 0, 0))
            for index in range(8):
                if index % 2:
                    doc.form.import_xfdf(xfdf)
                else:
                    doc.form.import_fdf(fdf)
                completed.append(index)
        assert error.value.code == "memory_size"
        assert 0 < len(completed) < 8
        assert _state(doc) == before
        assert doc.form["first"].value is None
        assert doc.form["last"].value is None
        assert page.obj.get("Contents") is None
        with page.draw() as canvas:
            canvas.rect((1, 75, 20, 90), fill=pyropdf.Color(1, 0, 0))
            assert doc.form.import_xfdf(pyropdf.FdfData(fields={"last": "done"})) == ["last"]
        assert doc.form["last"].value == "done"
        assert page.obj.get("Contents") is not None
        assert doc.history.steps == ("draw",)
        doc.history.undo()
        assert doc.form["last"].value is None
        assert page.obj.get("Contents") is None


def test_action_read_nested_in_canvas_uses_its_remaining_budget() -> None:
    expected = _target(16)
    with pyropdf.Document() as seed:
        seed.pages.new((80, 80))
        seed.open_action = expected
        data = seed.to_bytes()
    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_memory_size=32_000)) as doc:
        page = doc.pages[0]
        before = _state(doc)
        results = []
        with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:  # noqa: PT012
            canvas.rect((2, 3, 10, 12), fill=pyropdf.Color(0, 1, 0))
            results.extend(doc.open_action for _ in range(16))
        assert error.value.code == "memory_size"
        assert 0 < len(results) < 16
        assert all(result == expected for result in results)
        assert _state(doc) == before
        assert doc.open_action == expected


def test_composite_read_observes_committed_data_while_another_thread_edits() -> None:
    expected = _target(2)
    with pyropdf.open(_source(hops=2)) as doc, ThreadPoolExecutor(max_workers=1) as pool:
        with doc.transaction("edit action"):
            doc.open_action = _target(3)
            assert doc.open_action == _target(3)
            # The pool outlives the transaction so a timeout releases the writer.
            assert pool.submit(lambda: doc.open_action).result(timeout=10) == expected
        assert pool.submit(lambda: doc.open_action).result(timeout=10) == _target(3)
        assert doc.history.steps == ("edit action",)
