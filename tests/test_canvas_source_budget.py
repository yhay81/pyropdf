"""Source-page reads share a canvas's budget and preserve page content."""

from __future__ import annotations

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
