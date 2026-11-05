"""A canvas operation shares one budget, and reads its source from one version.

These are the private foundations of #505: ``begin_canvas`` opens the
operation, ``canvas_source_page`` reads a source page with a snapshot of its
version, and ``copy_page_resources`` copies the page's resources from that
snapshot.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import _core

if TYPE_CHECKING:
    from collections.abc import Callable

# ruff: noqa: SLF001, FBT003 - the private core is the interface under test.


def _source(rotation: int = 0) -> tuple[pyropdf.Document, pyropdf.Page]:
    document = pyropdf.Document()
    page = document.pages.new((240, 160))
    page.rotation = rotation
    page.obj["CropBox"] = [10, 15, 210, 130]
    content = b"q 0.5 g 30 25 50 40 re f Q\n"
    page.obj["Contents"] = document.objects[document.objects.add_stream(content)]
    return document, page


def _read(source: pyropdf.Document, page: pyropdf.Page, operation: _core._OperationBudget) -> tuple:
    result = _core.canvas_source_page(source._core(), page.xref, page._generation, operation)
    assert result is not None
    return result


# The operation's budget.


def test_calls_of_one_operation_charge_one_budget() -> None:
    document = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=1_000_000))
    core = document._core()
    page = document.pages.new()
    operation = _core.begin_canvas(core)
    try:
        assert operation.limit("memory_size") == 1_000_000
        operation.charge("memory_size", 999_000)
        assert operation.used("memory_size") == 999_000
        # An edit inside the canvas charges the operation, so it is refused
        # with the operation's typed limit error, and writes nothing.
        with pytest.raises(pyropdf.LimitError) as error:
            page.obj["Marker"] = list(range(2_000))
        assert error.value.code == "memory_size"
        operation.release("memory_size", 999_000)
        page.obj["Marker"] = 1
    finally:
        _core.end_transaction(core, True)
    assert document.pages[0].obj["Marker"] == 1


def test_independent_operations_do_not_share_counters() -> None:
    first, second = pyropdf.Document(), pyropdf.Document()
    one = _core.begin_canvas(first._core())
    two = _core.begin_canvas(second._core())
    try:
        one.charge("memory_size", 1_000)
        assert two.used("memory_size") == 0
    finally:
        _core.abort_canvas(first._core(), one)
        _core.abort_canvas(second._core(), two)
    again = _core.begin_canvas(first._core())
    try:
        assert again.used("memory_size") == 0
    finally:
        _core.abort_canvas(first._core(), again)


def test_a_nested_canvas_charges_the_outer_operation() -> None:
    document = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=1000))
    core = document._core()
    outer = _core.begin_canvas(core)
    try:
        outer.charge("memory_size", 60)
        inner = _core.begin_canvas(core)
        # The nested savepoint itself also belongs to this operation.
        used = inner.used("memory_size")
        assert used > 60
        remaining = 1000 - used
        with pytest.raises(pyropdf.LimitError) as error:
            inner.charge("memory_size", remaining + 1)
        assert error.value.code == "memory_size"
        inner.charge("memory_size", remaining)
        # Only the innermost canvas is active.
        with pytest.raises(ValueError, match="not active"):
            outer.used("memory_size")
        _core.end_transaction(core, True)
        assert outer.used("memory_size") == 1000
        with pytest.raises(ValueError, match="not active"):
            inner.poll()
    finally:
        _core.abort_canvas(core, outer)


def test_an_unknown_code_is_refused() -> None:
    document = pyropdf.Document()
    operation = _core.begin_canvas(document._core())
    try:
        with pytest.raises(ValueError, match="unknown limit code"):
            operation.charge("no_such_code", 1)
    finally:
        _core.abort_canvas(document._core(), operation)


def test_the_operation_belongs_to_its_thread_and_canvas() -> None:
    source, page = _source()
    target = pyropdf.Document()
    target_core, source_core, generation = target._core(), source._core(), page._generation
    operation = _core.begin_canvas(target_core)
    calls: list[Callable[[], object]] = [
        lambda: operation.charge("memory_size", 1),
        lambda: operation.release("memory_size", 1),
        lambda: operation.check("memory_size", 1),
        lambda: operation.used("memory_size"),
        lambda: operation.limit("memory_size"),
        operation.poll,
        lambda: _core.canvas_source_page(source_core, page.xref, generation, operation),
    ]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            for call in calls:
                with pytest.raises(ValueError, match="not active"):
                    pool.submit(call).result(timeout=30)
    finally:
        _core.end_transaction(target_core, False)
    for call in calls:
        with pytest.raises(ValueError, match="not active"):
            call()


def test_a_cancelled_operation_commits_no_step() -> None:
    token = pyropdf.CancellationToken()
    target = pyropdf.Document(limits=pyropdf.DocumentLimits(cancellation=token))
    page = target.pages.new()
    core = target._core()
    before = _core.history_state(core)
    operation = _core.begin_canvas(core)
    page.obj["Marker"] = 1
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as error:
        _core.end_transaction(core, True)
    assert error.value.code == "cancelled"
    assert _core.history_state(core) == before
    # The canvas already ended: aborting it again changes nothing.
    _core.abort_canvas(core, operation)


def test_a_cancelled_canvas_cannot_begin() -> None:
    token = pyropdf.CancellationToken()
    token.cancel()
    target = pyropdf.Document(limits=pyropdf.DocumentLimits(cancellation=token))
    with pytest.raises(pyropdf.LimitError) as error:
        _core.begin_canvas(target._core())
    assert error.value.code == "cancelled"


def test_an_expired_operation_commits_nothing() -> None:
    target = pyropdf.Document(limits=pyropdf.DocumentLimits(max_duration=0.05))
    core = target._core()
    page = target.pages.new()
    before = _core.history_state(core)
    operation = _core.begin_canvas(core)
    try:
        page.obj["Marker"] = 1

        time.sleep(0.1)
        with pytest.raises(pyropdf.LimitError) as error:
            operation.poll()
        assert error.value.code == "deadline"
        with pytest.raises(pyropdf.LimitError):
            _core.end_transaction(core, True)
    finally:
        _core.abort_canvas(core, operation)
    assert _core.history_state(core) == before
    assert "Marker" not in page.obj


def test_a_refused_nested_canvas_keeps_the_outer_edit() -> None:
    target = pyropdf.Document()
    page = target.pages.new()
    core = target._core()
    with target.transaction("outer"):
        page.obj["OuterMarker"] = 1
        operation = _core.begin_canvas(core)
        try:
            page.obj["InnerMarker"] = 2
            _core.begin_transaction(core, "nested")
            page.obj["NestedMarker"] = 3
            limit = operation.limit("content_element_count")
            assert limit is not None
            operation.charge("content_element_count", limit)
            with pytest.raises(pyropdf.LimitError):
                operation.charge("content_element_count", 1)
        finally:
            _core.abort_canvas(core, operation)
        assert page.obj["OuterMarker"] == 1
        assert "InnerMarker" not in page.obj
        assert "NestedMarker" not in page.obj
        page.obj["After"] = 4
    assert page.obj["OuterMarker"] == 1
    assert page.obj["After"] == 4
    assert "InnerMarker" not in page.obj
    assert _core.history_state(core)[0][-1] == "outer"


def test_a_failed_nested_transaction_in_a_canvas_rolls_back_alone() -> None:
    target = pyropdf.Document()
    page = target.pages.new()
    core = target._core()
    steps = len(_core.history_state(core)[0])
    operation = _core.begin_canvas(core)
    page.obj["Kept"] = 1
    _core.begin_transaction(core, "nested")
    page.obj["Dropped"] = 2
    _core.end_transaction(core, False)
    operation.poll()
    _core.end_transaction(core, True)
    assert page.obj["Kept"] == 1
    assert "Dropped" not in page.obj
    names = _core.history_state(core)[0]
    assert len(names) == steps + 1
    assert names[-1] == "draw"


def test_existing_canvas_drawing_is_unchanged() -> None:
    document = pyropdf.Document()
    page = document.pages.new((100, 100))
    with page.draw() as canvas:
        canvas.rect((10, 10, 50, 50), fill=pyropdf.Color(1, 0, 0))
    assert b"1 0 0 rg" in page.operations.to_bytes()


# The source snapshot.


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_source_read_reports_geometry_and_content(rotation: int) -> None:
    source, page = _source(rotation)
    target = pyropdf.Document()
    operation = _core.begin_canvas(target._core())
    try:
        actual_rotation, box, width, height, content, snapshot = _read(source, page, operation)
        assert (actual_rotation, box) == _core.page_space(source._core(), page.xref, page._generation)
        assert (actual_rotation, width, height) == _core.page_geometry(source._core(), page.xref, page._generation)
        assert content == page.operations.to_bytes()
        assert operation.used("memory_size") > 0
        assert snapshot.held
    finally:
        _core.end_transaction(target._core(), False)


@pytest.mark.parametrize(
    "content",
    [
        b"  % comment\nq\t1 0 0 1 3 4 cm Q  % tail\n",
        b"q 1 0 0 1 3 4 cm Q (unterminated",
        b"BI /W 1 /H 1 /BPC 8 /CS /G ID \x7f EI Q\n",
        b"",
    ],
)
def test_a_source_read_keeps_the_original_operation_bytes(content: bytes) -> None:
    source, page = _source()
    page.obj["Contents"] = source.objects[source.objects.add_stream(content)]
    expected = page.operations.to_bytes()
    target = pyropdf.Document()
    operation = _core.begin_canvas(target._core())
    try:
        assert _read(source, page, operation)[4] == expected
    finally:
        _core.end_transaction(target._core(), False)


def test_a_source_read_observes_the_source_limits() -> None:
    source, _page = _source()
    limited = pyropdf.open(source.to_bytes(), limits=pyropdf.DocumentLimits(max_page_content_size=8))
    target = pyropdf.Document()
    operation = _core.begin_canvas(target._core())
    try:
        with pytest.raises(pyropdf.LimitError) as error:
            _read(limited, limited.pages[0], operation)
        assert error.value.code == "page_content_size"
    finally:
        _core.end_transaction(target._core(), False)


def test_a_source_read_observes_the_source_cancellation() -> None:
    source, _page = _source()
    token = pyropdf.CancellationToken()
    limited = pyropdf.open(source.to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token))
    target = pyropdf.Document()
    operation = _core.begin_canvas(target._core())
    try:
        token.cancel()
        with pytest.raises(pyropdf.LimitError) as error:
            _read(limited, limited.pages[0], operation)
        assert error.value.code == "cancelled"
        # The source's token stops the source read, not the operation.
        operation.poll()
    finally:
        _core.end_transaction(target._core(), False)


def test_a_snapshot_reads_one_version_while_the_source_changes() -> None:
    source, page = _source()
    page.obj["Resources"] = {"Marker": 1}
    target = pyropdf.Document()
    target_core, source_core, generation = target._core(), source._core(), page._generation
    operation = _core.begin_canvas(target_core)
    try:
        rotation, box, _, _, old_content, snapshot = _read(source, page, operation)

        def edit_source() -> None:
            page.rotation = 90
            page.obj["Resources"]["Marker"] = 2
            page.obj["Contents"] = source.objects[source.objects.add_stream(b"q Q\n")]

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(edit_source).result(timeout=30)
        assert (rotation, box) != _core.page_space(source_core, page.xref, generation)
        assert old_content != page.operations.to_bytes()
        number = _core.copy_page_resources(
            target_core, source_core, page.xref, generation, operation=operation, snapshot=snapshot
        )
        assert number is not None
        assert target.objects[number]["Marker"] == 1
    finally:
        _core.end_transaction(target_core, True)


def test_a_snapshot_refuses_the_wrong_owner_and_a_second_use() -> None:
    source, page = _source()
    target = pyropdf.Document()
    target_core, source_core, generation = target._core(), source._core(), page._generation
    operation = _core.begin_canvas(target_core)
    try:
        snapshot = _read(source, page, operation)[-1]
        with pytest.raises(ValueError, match="go together"):
            _core.copy_page_resources(target_core, source_core, page.xref, generation, operation=operation)
        with pytest.raises(ValueError, match="go together"):
            _core.copy_page_resources(target_core, source_core, page.xref, generation, snapshot=snapshot)
        another = source.pages.new()
        with pytest.raises(ValueError, match="snapshot"):
            _core.copy_page_resources(
                target_core, source_core, another.xref, another._generation, operation=operation, snapshot=snapshot
            )
        other_source = pyropdf.Document()
        with pytest.raises(ValueError, match="snapshot"):
            _core.copy_page_resources(
                target_core, other_source._core(), page.xref, generation, operation=operation, snapshot=snapshot
            )
        other_target = pyropdf.Document()
        with pytest.raises(ValueError, match="not active"):
            _core.copy_page_resources(
                other_target._core(), source_core, page.xref, generation, operation=operation, snapshot=snapshot
            )
        assert snapshot.held
        _core.copy_page_resources(
            target_core, source_core, page.xref, generation, operation=operation, snapshot=snapshot
        )
        assert not snapshot.held
        with pytest.raises(ValueError, match="already used"):
            _core.copy_page_resources(
                target_core, source_core, page.xref, generation, operation=operation, snapshot=snapshot
            )
        again = _read(source, page, operation)[-1]
        again.release()
        assert not again.held
    finally:
        _core.end_transaction(target_core, False)


def test_a_snapshot_of_another_operation_is_refused() -> None:
    source, page = _source()
    target = pyropdf.Document()
    core = target._core()
    first = _core.begin_canvas(core)
    snapshot = _read(source, page, first)[-1]
    _core.end_transaction(core, False)
    second = _core.begin_canvas(core)
    try:
        with pytest.raises(ValueError, match="snapshot"):
            _core.copy_page_resources(
                core, source._core(), page.xref, page._generation, operation=second, snapshot=snapshot
            )
    finally:
        _core.abort_canvas(core, second)


# Same-document copies.


def _same_document_copy(document: pyropdf.Document, page: pyropdf.Page, change: Callable[[], object]) -> int:
    """Copy the page's resources in a canvas, and run ``change`` after the copy and before the commit."""
    core, generation = document._core(), page._generation
    operation = _core.begin_canvas(core)
    try:
        snapshot = _read(document, page, operation)[-1]
        number = _core.copy_page_resources(core, core, page.xref, generation, operation=operation, snapshot=snapshot)
        assert number is not None
        change()
    except BaseException:
        _core.abort_canvas(core, operation)
        raise
    _core.end_transaction(core, True)
    return number


def test_a_same_document_copy_is_independent_of_later_source_edits() -> None:
    document, page = _source()
    font = document.objects.add({"Marker": 1})
    page.obj["Resources"] = {"Font": {"F1": document.objects[font]}}

    def change() -> None:
        document.objects[font]["Marker"] = 2

    number = _same_document_copy(document, page, change)
    copied = document.objects[number]["Font"]["F1"]
    assert copied.xref != font
    assert copied["Marker"] == 1
    assert document.objects[font]["Marker"] == 2


def test_a_same_document_copy_keeps_the_snapshot_version() -> None:
    document, page = _source()
    font = document.objects.add({"Marker": 1})
    page.obj["Resources"] = {"Font": {"F1": document.objects[font]}}
    core, generation = document._core(), page._generation
    operation = _core.begin_canvas(core)
    try:
        snapshot = _read(document, page, operation)[-1]
        document.objects[font]["Marker"] = 2
        number = _core.copy_page_resources(core, core, page.xref, generation, operation=operation, snapshot=snapshot)
        assert number is not None
    finally:
        _core.end_transaction(core, True)
    assert document.objects[number]["Font"]["F1"]["Marker"] == 1


def test_a_same_document_copy_keeps_cycles_and_sharing() -> None:
    document, page = _source()
    first = document.objects.add({"Name": "first"})
    second = document.objects.add({"Name": "second", "Back": document.objects[first]})
    document.objects[first]["Next"] = document.objects[second]
    page.obj["Resources"] = {
        "Font": {"F1": document.objects[first], "F2": document.objects[first]},
        "ExtGState": {"G1": document.objects[second]},
    }
    number = _same_document_copy(document, page, lambda: None)
    resources = document.objects[number]
    one, two = resources["Font"]["F1"], resources["Font"]["F2"]
    assert one.xref == two.xref != first
    assert one["Next"].xref == resources["ExtGState"]["G1"].xref != second
    assert one["Next"]["Back"].xref == one.xref


def test_a_same_document_copy_copies_streams() -> None:
    document, page = _source()
    form = document.objects.add_stream(b"0 0 1 1 re f", {"Type": "XObject", "Subtype": "Form", "BBox": [0, 0, 1, 1]})
    page.obj["Resources"] = {"XObject": {"X1": document.objects[form]}}

    def change() -> None:
        document.objects[form].write(b"changed")

    number = _same_document_copy(document, page, change)
    copied = document.objects[number]["XObject"]["X1"]
    assert copied.xref != form
    assert copied.read() == b"0 0 1 1 re f"
    assert document.objects[form].read() == b"changed"


def test_a_same_document_copy_in_a_tagged_document_keeps_the_structure() -> None:
    document, page = _source()
    form = document.objects.add_stream(
        b"q Q", {"Type": "XObject", "Subtype": "Form", "BBox": [0, 0, 1, 1], "StructParents": 0}
    )
    element = document.objects.add({"Type": "StructElem", "S": "Figure"})
    tree = document.objects.add({"Type": "StructTreeRoot", "K": [document.objects[element]]})
    document.catalog["StructTreeRoot"] = document.objects[tree]
    document.catalog["MarkInfo"] = {"Marked": True}
    page.obj["Resources"] = {"XObject": {"X1": document.objects[form]}}
    number = _same_document_copy(document, page, lambda: None)
    copied = document.objects[number]["XObject"]["X1"]
    assert copied.xref != form
    assert copied["StructParents"] == 0
    assert document.catalog["StructTreeRoot"].xref == tree
    assert document.objects[tree]["K"][0].xref == element


def test_a_refused_same_document_copy_writes_nothing() -> None:
    document = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=10_000_000))
    page = document.pages.new()
    fonts = {f"F{i}": document.objects[document.objects.add({"Marker": i})] for i in range(50)}
    page.obj["Resources"] = {"Font": fonts}
    core, generation = document._core(), page._generation
    before = _core.history_state(core)
    count = len(document.objects)
    operation = _core.begin_canvas(core)
    try:
        snapshot = _read(document, page, operation)[-1]
        # The operation has used nearly all of its memory: the copy runs out.
        operation.charge("memory_size", 10_000_000 - operation.used("memory_size") - 64)
        with pytest.raises(pyropdf.LimitError) as error:
            _core.copy_page_resources(core, core, page.xref, generation, operation=operation, snapshot=snapshot)
        assert error.value.code == "memory_size"
    finally:
        _core.abort_canvas(core, operation)
    assert _core.history_state(core) == before
    assert len(document.objects) == count
