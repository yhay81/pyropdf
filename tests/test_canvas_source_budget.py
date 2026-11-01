"""Source-page reads share a canvas's budget and preserve page content."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

import pyropdf
from pyropdf import _core


def _source(rotation: int = 0) -> tuple[pyropdf.Document, pyropdf.Page]:
    document = pyropdf.Document()
    page = document.pages.new((240, 160))
    page.rotation = rotation
    page.obj["CropBox"] = [10, 15, 210, 130]
    content = b"q 0.5 g 30 25 50 40 re f Q\n"
    page.obj["Contents"] = document.objects[document.objects.add_stream(content)]
    return document, page


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_source_page_helper_preserves_geometry_and_content(rotation: int) -> None:
    source, page = _source(rotation)
    target = pyropdf.Document()
    target.pages.new((200, 200))
    operation = _core.begin_canvas(target._core())  # noqa: SLF001
    try:
        result = _core.canvas_source_page(source._core(), page.xref, page._generation, operation)  # noqa: SLF001
        assert result is not None
        actual_rotation, box, width, height, content, _snapshot = result
        expected_space = _core.page_space(source._core(), page.xref, page._generation)  # noqa: SLF001
        expected_geometry = _core.page_geometry(source._core(), page.xref, page._generation)  # noqa: SLF001
        assert expected_space is not None
        assert expected_geometry is not None
        assert (actual_rotation, box) == expected_space
        assert (actual_rotation, width, height) == expected_geometry
        assert content == page.operations.to_bytes()
        assert operation.used("memory_size") > 0
    finally:
        _core.end_transaction(target._core(), False)  # noqa: FBT003, SLF001


@pytest.mark.parametrize(
    "content",
    [
        b"  % comment\nq\t1 0 0 1 3 4 cm Q  % tail\n",
        b"q 1 0 0 1 3 4 cm Q (unterminated",
        b"BI /W 1 /H 1 /BPC 8 /CS /G ID \x7f EI Q\n",
        b"",
    ],
)
def test_source_page_helper_preserves_original_operation_bytes(content: bytes) -> None:
    source, page = _source()
    page.obj["Contents"] = source.objects[source.objects.add_stream(content)]
    expected = page.operations.to_bytes()
    target = pyropdf.Document()
    target.pages.new((200, 200))
    operation = _core.begin_canvas(target._core())  # noqa: SLF001
    try:
        result = _core.canvas_source_page(source._core(), page.xref, page._generation, operation)  # noqa: SLF001
        assert result is not None
        assert result[4] == expected
    finally:
        _core.end_transaction(target._core(), False)  # noqa: FBT003, SLF001


def test_source_page_helper_refuses_source_document_limit() -> None:
    source, _page = _source()
    limited = pyropdf.open(source.to_bytes(), limits=pyropdf.DocumentLimits(max_page_content_size=8))
    source_page = limited.pages[0]
    target = pyropdf.Document()
    target.pages.new((200, 200))
    operation = _core.begin_canvas(target._core())  # noqa: SLF001
    try:
        with pytest.raises(pyropdf.LimitError) as error:
            _core.canvas_source_page(limited._core(), source_page.xref, source_page._generation, operation)  # noqa: SLF001
        assert error.value.code == "page_content_size"
    finally:
        _core.end_transaction(target._core(), False)  # noqa: FBT003, SLF001


def test_source_page_helper_observes_source_cancellation() -> None:
    source, _page = _source()
    token = pyropdf.CancellationToken()
    limited = pyropdf.open(source.to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token))
    source_page = limited.pages[0]
    target = pyropdf.Document()
    target.pages.new((200, 200))
    operation = _core.begin_canvas(target._core())  # noqa: SLF001
    try:
        token.cancel()
        with pytest.raises(pyropdf.LimitError) as error:
            _core.canvas_source_page(limited._core(), source_page.xref, source_page._generation, operation)  # noqa: SLF001
        assert error.value.code == "cancelled"
    finally:
        _core.end_transaction(target._core(), False)  # noqa: FBT003, SLF001


def test_snapshot_uses_one_source_version_and_rejects_wrong_owners() -> None:
    source, page = _source()
    page.obj["Resources"] = {"Marker": 1}
    target = pyropdf.Document()
    target_core, source_core, generation = target._core(), source._core(), page._generation  # noqa: SLF001
    operation = _core.begin_canvas(target_core)
    try:
        result = _core.canvas_source_page(source_core, page.xref, generation, operation)
        assert result is not None
        rotation, box, _, _, old_content, snapshot = result
        with pytest.raises(ValueError, match="must be used together"):
            _core.copy_page_resources(target_core, source_core, page.xref, generation, operation=operation)
        with pytest.raises(ValueError, match="must be used together"):
            _core.copy_page_resources(target_core, source_core, page.xref, generation, snapshot=snapshot)

        def edit_source() -> None:
            page.rotation = 90
            page.obj["Resources"]["Marker"] = 2
            page.obj["Contents"] = source.objects[source.objects.add_stream(b"q Q\n")]

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(edit_source).result(timeout=30)

        assert rotation == 0
        new_space = _core.page_space(source_core, page.xref, generation)
        assert new_space is not None
        assert (rotation, box) != new_space
        assert old_content != page.operations.to_bytes()
        number = _core.copy_page_resources(
            target_core, source_core, page.xref, generation, operation=operation, snapshot=snapshot
        )
        assert number is not None
        assert target.objects[number]["Marker"] == 1
        another = source.pages.new()
        another_generation = another._generation  # noqa: SLF001
        with pytest.raises(ValueError, match="snapshot"):
            _core.copy_page_resources(
                target_core, source_core, another.xref, another_generation, operation=operation, snapshot=snapshot
            )
        other_target = pyropdf.Document()
        other_core = other_target._core()  # noqa: SLF001
        with pytest.raises(ValueError, match="operation"):
            _core.copy_page_resources(
                other_core, source_core, page.xref, generation, operation=operation, snapshot=snapshot
            )
    finally:
        _core.end_transaction(target_core, False)  # noqa: FBT003


def test_same_document_snapshot_refuses_changed_indirect_resource() -> None:
    document, page = _source()
    font_ref = document.objects.add({"Marker": 1})
    page.obj["Resources"] = {"Font": {"F1": document.objects[font_ref]}}
    core, generation = document._core(), page._generation  # noqa: SLF001
    operation = _core.begin_canvas(core)
    try:
        result = _core.canvas_source_page(core, page.xref, generation, operation)
        assert result is not None
        snapshot = result[-1]
        document.objects[font_ref]["Marker"] = 2
        with pytest.raises(ValueError, match="resource graph copy"):
            _core.copy_page_resources(core, core, page.xref, generation, operation=operation, snapshot=snapshot)
    finally:
        _core.end_transaction(core, False)  # noqa: FBT003


def test_same_document_snapshot_refuses_until_graph_copy_is_available() -> None:
    document, page = _source()
    page.obj["Resources"] = {"Marker": 1}
    core, generation = document._core(), page._generation  # noqa: SLF001
    operation = _core.begin_canvas(core)
    try:
        result = _core.canvas_source_page(core, page.xref, generation, operation)
        assert result is not None
        with pytest.raises(ValueError, match="resource graph copy"):
            _core.copy_page_resources(core, core, page.xref, generation, operation=operation, snapshot=result[-1])
    finally:
        _core.end_transaction(core, False)  # noqa: FBT003


def test_source_read_requires_active_operation_on_owning_thread() -> None:
    source, page = _source()
    target = pyropdf.Document()
    target_core, source_core, generation = target._core(), source._core(), page._generation  # noqa: SLF001
    operation = _core.begin_canvas(target_core)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(_core.canvas_source_page, source_core, page.xref, generation, operation)
            with pytest.raises(ValueError, match="not active"):
                result.result(timeout=30)
            for method, args in [
                (operation.charge, ("memory_size", 1)),
                (operation.check, ("memory_size", 1)),
                (operation.used, ("memory_size",)),
                (operation.poll, ()),
            ]:
                with pytest.raises(ValueError, match="not active"):
                    pool.submit(method, *args).result(timeout=30)
    finally:
        _core.end_transaction(target_core, False)  # noqa: FBT003
    with pytest.raises(ValueError, match="not active"):
        _core.canvas_source_page(source_core, page.xref, generation, operation)
    for method, args in [
        (operation.charge, ("memory_size", 1)),
        (operation.check, ("memory_size", 1)),
        (operation.used, ("memory_size",)),
        (operation.poll, ()),
    ]:
        with pytest.raises(ValueError, match="not active"):
            method(*args)


def test_same_document_snapshot_refuses_after_nested_rollback() -> None:
    document, page = _source()
    core, generation = document._core(), page._generation  # noqa: SLF001
    operation = _core.begin_canvas(core)
    try:
        result = _core.canvas_source_page(core, page.xref, generation, operation)
        assert result is not None
        _core.begin_transaction(core, "nested")
        page.obj["UnrelatedMarker"] = 2
        _core.end_transaction(core, False)  # noqa: FBT003
        with pytest.raises(ValueError, match="resource graph copy"):
            _core.copy_page_resources(core, core, page.xref, generation, operation=operation, snapshot=result[-1])
        refreshed = _core.canvas_source_page(core, page.xref, generation, operation)
        assert refreshed is not None
        with pytest.raises(ValueError, match="resource graph copy"):
            _core.copy_page_resources(core, core, page.xref, generation, operation=operation, snapshot=refreshed[-1])
    finally:
        _core.end_transaction(core, False)  # noqa: FBT003


def test_nested_operation_refusal_rolls_back_without_losing_outer_edit() -> None:
    target = pyropdf.Document()
    page = target.pages.new()
    with target.transaction("outer"):
        page.obj["OuterMarker"] = 1
        operation = _core.begin_canvas(target._core())  # noqa: SLF001
        try:
            page.obj["InnerMarker"] = 2
            operation.charge("content_element_count", 262_144)
            with pytest.raises(pyropdf.LimitError):
                operation.charge("content_element_count", 1)
        finally:
            _core.abort_canvas(target._core(), operation)  # noqa: SLF001
        assert page.obj["OuterMarker"] == 1
        assert "InnerMarker" not in page.obj
    assert page.obj["OuterMarker"] == 1
    assert "InnerMarker" not in page.obj


def test_call_override_cannot_relax_an_active_operation_limit() -> None:
    document, page = _source()
    core = document._core()  # noqa: SLF001
    operation = _core.begin_canvas(core)
    try:
        operation.charge("content_element_count", 262_143)
        with pytest.raises(pyropdf.LimitError) as error:
            page.content(max_elements=None)
        assert error.value.code == "content_element_count"
        assert operation.used("content_element_count") <= 262_144
    finally:
        _core.abort_canvas(core, operation)


def test_cancelled_operation_does_not_commit_a_history_step() -> None:
    token = pyropdf.CancellationToken()
    target = pyropdf.Document(limits=pyropdf.DocumentLimits(cancellation=token))
    page = target.pages.new()
    before = _core.history_state(target._core())  # noqa: SLF001
    operation = _core.begin_canvas(target._core())  # noqa: SLF001
    page.obj["Marker"] = 1
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as error:
        _core.end_transaction(target._core(), True)  # noqa: FBT003, SLF001
    assert error.value.code == "cancelled"
    assert _core.history_state(target._core()) == before  # noqa: SLF001
    _core.abort_canvas(target._core(), operation)  # noqa: SLF001
