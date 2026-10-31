"""Canvas aborts only its own live transaction scope."""

import pyropdf
from pyropdf import _core


def test_aborting_ended_canvas_preserves_parent_transaction() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((100, 100))
    core = doc._core()  # noqa: SLF001
    _core.begin_transaction(core, "parent")
    operation = _core.begin_canvas(core)
    page.rotation = 90
    _core.end_transaction(core, True)  # noqa: FBT003
    _core.abort_canvas(core, operation)
    page.rotation = 180
    _core.end_transaction(core, True)  # noqa: FBT003
    assert page.rotation == 180
    assert doc.history.steps[-1] == "parent"


def test_abort_discards_canvas_and_nested_regular_transactions() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((100, 100))
    core = doc._core()  # noqa: SLF001
    _core.begin_transaction(core, "parent")
    page.rotation = 90
    operation = _core.begin_canvas(core)
    page.rotation = 180
    _core.begin_transaction(core, "inner")
    page.rotation = 270
    _core.abort_canvas(core, operation)
    assert page.rotation == 90
    _core.end_transaction(core, True)  # noqa: FBT003
    assert page.rotation == 90
    assert doc.history.steps[-1] == "parent"


def test_abort_ignores_wrong_or_cancelled_operation() -> None:
    original = pyropdf.Document()
    original.pages.new((100, 100))
    token = pyropdf.CancellationToken()
    doc = pyropdf.open(original.to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token))
    page = doc.pages[0]
    core = doc._core()  # noqa: SLF001
    outer = _core.begin_canvas(core)
    page.rotation = 90
    inner = _core.begin_canvas(core)
    page.rotation = 180
    _core.abort_canvas(core, outer)
    assert page.rotation == 180
    token.cancel()
    _core.abort_canvas(core, inner)
    _core.abort_canvas(core, outer)
    _core.abort_canvas(core, outer)
    # A cancelled token should not have prevented rollback or lock release.
    doc.close()
