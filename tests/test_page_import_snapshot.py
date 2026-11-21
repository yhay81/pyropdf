"""A media page and its catalog declarations must come from the same immutable revision."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_multimedia import media_pdf

import pyropdf as pdf
from pyropdf import _core


def _capture(target: pdf.Document, source: pdf.Document) -> pdf.Document:
    result = _core.import_pages_with_source(target._core(), source._core(), [(3, 0)], 0)  # noqa: SLF001
    assert result is not None
    (_major, _minor, first, _has_prc), core = result
    assert first > 0
    return source._import_source(core)  # noqa: SLF001


def _mark(source: pdf.Document, value: int) -> None:
    with source.transaction("edit page and catalog"):
        source.catalog["Marker"] = value
        source.pages[0].obj["Marker"] = value


def test_catalog_stays_with_imported_graph_across_concurrent_commit_undo_and_redo() -> None:
    with pdf.open(media_pdf()) as source, pdf.Document() as target, ThreadPoolExecutor(1) as writer:
        _mark(source, 1)
        history, original = source.history.steps, source.to_bytes(incremental=True)
        with _capture(target, source) as captured:
            assert (source.history.steps, source.to_bytes(incremental=True)) == (history, original)
            writer.submit(_mark, source, 2).result(timeout=5)
            for action, live in ((None, 2), (source.history.undo, 1), (source.history.redo, 2)):
                if action is not None:
                    writer.submit(action).result(timeout=5)
                assert source.catalog["Marker"] == source.pages[0].obj["Marker"] == live
                assert captured.catalog["Marker"] == target.pages[0].obj["Marker"] == 1


def test_capture_an_uncommitted_source_then_discard_its_edits() -> None:
    with pdf.open(media_pdf()) as source, pdf.Document() as target:
        original_steps = source.history.steps
        original = source.to_bytes(incremental=True)
        with pytest.raises(ValueError, match="discard draft"), source.transaction():  # noqa: PT012 - verify the draft before rolling it back.
            _mark(source, 17)
            captured = _capture(target, source)
            _mark(source, 21)
            assert captured.catalog["Marker"] == 17
            msg = "discard draft"
            raise ValueError(msg)
        with captured:
            assert captured.catalog["Marker"] == target.pages[0].obj["Marker"] == 17
        assert "Marker" not in source.catalog
        assert (source.history.steps, source.to_bytes(incremental=True)) == (original_steps, original)


def test_media_capture_is_optional_and_copy_rolls_back_with_its_receiver() -> None:
    with pdf.open(media_pdf()) as source, pdf.Document() as target:
        msg = "invalid declaration"
        with pytest.raises(ValueError, match=msg), target.transaction(), _capture(target, source):
            raise ValueError(msg)
        assert len(target.pages) == 0
        assert not target.history.can_undo
        source.pages.new()
        blank = _core.page_objects(source._core())[-1]  # noqa: SLF001
        assert _core.import_pages_with_source(target._core(), source._core(), [blank], 0) is None  # noqa: SLF001
        assert len(target.pages) == 1


def test_imported_catalog_observes_the_same_meter_and_the_source_limit() -> None:
    limits = pdf.DocumentLimits(max_memory_size=8_000_000)
    with pdf.open(media_pdf(), limits=limits) as source, pdf.Document() as target:

        def import_and_read(operation: _core._OperationBudget) -> None:
            with _capture(target, source) as view:
                before = operation.used("memory_size")

                def read(source_budget: _core._OperationBudget) -> None:
                    assert source_budget.limit("memory_size") == limits.max_memory_size
                    assert source_budget.used("memory_size") == before
                    source_budget.charge("memory_size", 137)

                _core.read_operation(view._core(), read)  # noqa: SLF001
                assert operation.used("memory_size") == before + 137

        _core.read_operation(target._core(), import_and_read)  # noqa: SLF001
