# ruff: noqa: SLF001 - exercise the binding's operation budget directly.
"""Binding-owned collections refuse before growing beyond the budget (#677)."""

from __future__ import annotations

from functools import partial

import pytest
from corpus import ASSETS
from test_changes import _pdf

import pyropdf
from pyropdf import _core


@pytest.mark.parametrize("layout", [False, True])
def test_cached_character_columns_charge_the_operation(*, layout: bool) -> None:
    document = pyropdf.open(
        ASSETS / "bill-hr815.pdf",
        limits=pyropdf.DocumentLimits(max_memory_size=100_000_000),
    )
    page = document.pages[0]
    operation = _core.begin_canvas(document._core())
    try:
        expected = len(page.chars())
        assert expected > 1000
        operation.charge("memory_size", 100_000_000 - operation.used("memory_size") - 5000)
        read = partial(page.layout, chars=True) if layout else page.chars
        with pytest.raises(pyropdf.LimitError) as error:
            read()
        assert error.value.code == "memory_size"
    finally:
        _core.abort_canvas(document._core(), operation)
    assert len(page.chars()) == expected


def test_type3_lookup_result_charges_memory_even_when_no_fonts_are_held() -> None:
    document = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=1024))
    with pytest.raises(pyropdf.LimitError) as error:
        _core.type3_codes(document._core(), [], 1000, ["missing"] * 1000)
    assert error.value.code == "memory_size"
    assert _core.type3_codes(document._core(), [], 1000, ["missing"]) == [None]


@pytest.mark.parametrize("canvas", [False, True])
def test_nested_savepoints_observe_memory_and_leave_the_outer_transaction_usable(*, canvas: bool) -> None:
    document = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=1))
    core = document._core()
    begin = partial(_core.begin_canvas, core) if canvas else partial(_core.begin_transaction, core, "inner")
    with document.transaction("outer"):
        with pytest.raises(pyropdf.LimitError) as error:
            begin()
        assert error.value.code == "memory_size"
    with document.transaction("next"):
        pass


def test_fresh_transaction_budgets_do_not_allow_an_unbounded_nesting_stack() -> None:
    document = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=1024))
    core = document._core()
    _core.begin_transaction(core, "outer")
    depth = 0
    refusal = None
    try:
        for _ in range(1000):
            try:
                _core.begin_transaction(core, "nested")
            except pyropdf.LimitError as error:
                refusal = error
                break
            depth += 1
        else:
            pytest.fail("nested transactions bypassed the memory limit")
        assert refusal is not None
        assert refusal.code == "memory_size"
    finally:
        for _ in range(depth + 1):
            _core.end_transaction(core, False)  # noqa: FBT003 - the core function is positional-only.
    with document.transaction("next"):
        pass


def test_python_array_conversion_is_charged_before_serializing_it() -> None:
    document = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=1_000_000))
    before = len(document.objects)
    operation = _core.begin_canvas(document._core())
    try:
        # The serialized nulls fit; their Value slots need much more memory.
        operation.charge("memory_size", 1_000_000 - operation.used("memory_size") - 20_000)
        with pytest.raises(pyropdf.LimitError) as error:
            document.objects.add([None] * 1000)
        assert error.value.code == "memory_size"
    finally:
        _core.abort_canvas(document._core(), operation)
    assert len(document.objects) == before
    assert document.objects[document.objects.add([None])][0] is None


def test_change_report_charges_its_pending_signature_fields() -> None:
    raw = _pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [" + "4 0 R " * 2000 + "] >> >>",
            2: "<< /Type /Pages /Kids [] /Count 0 >>",
            3: "<< /Type /Sig /ByteRange [0 1 1 0] /Contents <00> >>",
            4: "<< /FT /Sig /T (Approval) /V 3 0 R >>",
        }
    )
    document = pyropdf.open(raw, limits=pyropdf.DocumentLimits(max_memory_size=10_000_000))
    operation = _core.begin_canvas(document._core())
    try:
        assert len(document.changes(incremental=False).signatures) == 1
        operation.charge("memory_size", 10_000_000 - operation.used("memory_size") - 5000)
        with pytest.raises(pyropdf.LimitError) as error:
            document.changes(incremental=False)
        assert error.value.code == "memory_size"
    finally:
        _core.abort_canvas(document._core(), operation)
    assert len(document.changes(incremental=False).signatures) == 1
