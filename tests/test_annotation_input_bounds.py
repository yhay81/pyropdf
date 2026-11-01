"""The core's annotation specifications are bounded before they are copied.

``_core.annotation_add`` and ``_core.annotation_edit`` skip the Python
wrappers' checks, so these tests call them directly. An item that raises
when read stands after a refused field or past a refused length: the
refusal must come before the binding reads it.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

import pyropdf
from pyropdf import _core

MIB = 1024 * 1024


class Unreadable:
    """A number that fails the test when the binding reads it."""

    def __float__(self) -> float:
        """Fail the test."""
        msg = "the binding read an input past a refused limit"
        raise AssertionError(msg)


class Cancels:
    """A number that cancels `token` when the binding reads it."""

    def __init__(self, token: pyropdf.CancellationToken) -> None:
        """Cancel `token` once read."""
        self.token = token

    def __float__(self) -> float:
        """Cancel the token and return a coordinate."""
        self.token.cancel()
        return 10.0


class Slow:
    """A number that outlasts an operation's deadline when the binding reads it."""

    def __float__(self) -> float:
        """Sleep past the deadline and return a coordinate."""
        time.sleep(0.2)
        return 10.0


def _page(limits: pyropdf.DocumentLimits | None = None) -> tuple[pyropdf.Document, pyropdf.Page, tuple[Any, ...]]:
    source = pyropdf.Document()
    source.pages.new((200, 200))
    doc = pyropdf.open(source.to_bytes(), limits=limits)
    page = doc.pages[0]
    return doc, page, (doc._core(), page._number, page._generation)  # noqa: SLF001


def _square(page: pyropdf.Page) -> tuple[Any, ...]:
    annotation = page.annotations.add(pyropdf.Square((10, 10, 50, 50)))
    return annotation._location()  # noqa: SLF001


def _state(doc: pyropdf.Document) -> tuple[bytes, tuple[str, ...], int]:
    return doc.to_bytes(), doc.history.steps, doc.history.position


def test_add_refuses_oversized_contents_before_reading_later_fields() -> None:
    doc, _, location = _page()
    before = _state(doc)
    spec: dict[str, object] = {"subtype": "Ink", "points": [[(Unreadable(), 0.0)]], "contents": "x" * (MIB + 1)}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.annotation_add(*location, spec)
    assert raised.value.code == "annotation_input_size"
    assert _state(doc) == before


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ({"subtype": "Highlight", "rects": [Unreadable()] * 4097}, "annotation_quad_count"),
        ({"subtype": "Ink", "points": [Unreadable()] * 65537}, "annotation_point_count"),
        ({"subtype": "Ink", "points": [[Unreadable()] * 65537]}, "annotation_point_count"),
        ({"subtype": "Ink", "points": [[(0.0, 0.0)] * 40000, [Unreadable()] * 30000]}, "annotation_point_count"),
        (
            {"subtype": "Square", "rect": (10, 10, 20, 20), "border": (1.0, "D", [Unreadable()] * 33)},
            "annotation_dash_count",
        ),
        (
            {
                "subtype": "FileAttachment",
                "rect": (10, 10, 20, 20),
                "data": bytearray(16),
                "filename": "f",
                "max_size": 15,
            },
            "embedded_file_size",
        ),
        (
            {
                "subtype": "FileAttachment",
                "rect": (10, 10, 20, 20),
                "data": b"x",
                "filename": "f" * MIB,
                "description": "d",
            },
            "embedded_file_input_size",
        ),
    ],
)
def test_add_refuses_oversized_collections_before_reading_items(spec: dict[str, Any], code: str) -> None:
    doc, _, location = _page()
    before = _state(doc)
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.annotation_add(*location, spec)
    assert raised.value.code == code
    assert _state(doc) == before


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"subtype": "S" * MIB}, "does not add"),
        ({"subtype": "Text", "rect": (10, 10, 20, 20), "icon": "N" * MIB}, "unknown icon"),
        ({"subtype": "Square", "rect": (10, 10, 20, 20), "blend_mode": "M" * MIB}, "unknown blend mode"),
        ({"subtype": "Square", "rect": (10, 10, 20, 20), "created": "D:" + "1" * MIB}, "PDF date string"),
        ({"subtype": "Square", "rect": (10, 10, 20, 20), "border": (1.0, "S" * MIB, [])}, "border style"),
        ({"subtype": "Line", "points": [[(0, 0), (9, 9)]], "line_endings": ("None", "A" * MIB)}, "line ending"),
    ],
)
def test_add_refuses_overlong_names_before_copying(spec: dict[str, Any], message: str) -> None:
    doc, _, location = _page()
    before = _state(doc)
    with pytest.raises(pyropdf.PdfError, match=message):
        _core.annotation_add(*location, spec)
    assert _state(doc) == before


def test_add_refuses_wrong_types_without_copying() -> None:
    _, _, location = _page()
    with pytest.raises(TypeError, match="contents is a str"):
        _core.annotation_add(*location, {"subtype": "Square", "rect": (10, 10, 20, 20), "contents": b"x"})
    with pytest.raises(TypeError, match="points is a list or a tuple"):
        _core.annotation_add(*location, {"subtype": "Ink", "points": "xy"})
    with pytest.raises(TypeError, match="subtype"):
        _core.annotation_add(*location, {"rect": (10, 10, 20, 20)})


def test_add_accepts_tuples_and_the_largest_point_count() -> None:
    _, page, location = _page()
    points = ((tuple((float(i % 100), 1.0) for i in range(65536))),)
    assert _core.annotation_add(*location, {"subtype": "Ink", "points": points}) is not None
    assert len(page.annotations) == 1


def test_edit_counts_its_texts_together_before_copying_the_next() -> None:
    doc, page, _ = _page()
    location = _square(page)
    before = _state(doc)
    edit: dict[str, object] = {
        "contents": "x" * (MIB // 2),
        "author": "y" * (MIB // 2 + 1),
        "points": [[(Unreadable(), 0.0)]],
        "clear": [],
    }
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.annotation_edit(*location, edit)
    assert raised.value.code == "annotation_input_size"
    assert _state(doc) == before


def test_edit_at_the_input_boundary_is_accepted() -> None:
    doc, page, _ = _page()
    location = _square(page)
    # The subtype counts with the texts that an edit writes.
    contents = "x" * (MIB - len("Square"))
    with pytest.raises(pyropdf.LimitError):
        _core.annotation_edit(*location, {"contents": contents + "x", "clear": []})
    assert _core.annotation_edit(*location, {"contents": contents, "clear": []}) is not None
    # Listing the page would count its other texts too, so read the bytes.
    assert f"/Contents ({contents})".encode() in doc.to_bytes()


@pytest.mark.parametrize(
    ("edit", "error", "match"),
    [
        ({"clear": ["contents"] * 11}, pyropdf.PdfError, "at most 10"),
        ({"clear": ["c" * MIB]}, pyropdf.PdfError, "cannot be removed"),
        ({"modified": "D:" + "1" * MIB, "clear": []}, pyropdf.PdfError, "PDF date string"),
        ({"icon": "N" * MIB, "clear": []}, pyropdf.PdfError, "unknown icon"),
        ({"border": (1.0, "D", [Unreadable()] * 33), "clear": []}, pyropdf.LimitError, "dash"),
        ({"clear": None}, TypeError, "clear"),
    ],
)
def test_edit_refuses_oversized_fields_before_copying(edit: dict[str, Any], error: type[Exception], match: str) -> None:
    doc, page, _ = _page()
    location = _square(page)
    before = _state(doc)
    with pytest.raises(error, match=match):
        _core.annotation_edit(*location, edit)
    assert _state(doc) == before


def test_add_observes_cancellation_between_items() -> None:
    token = pyropdf.CancellationToken()
    _, _, location = _page(pyropdf.DocumentLimits(cancellation=token))
    spec: dict[str, object] = {"subtype": "Ink", "points": [[(Cancels(token), 0.0), (Unreadable(), 0.0)]]}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.annotation_add(*location, spec)
    assert raised.value.code == "cancelled"


def test_edit_observes_its_deadline_without_changes() -> None:
    doc, page, _ = _page(pyropdf.DocumentLimits(max_duration=0.05))
    location = _square(page)
    before = _state(doc)
    edit: dict[str, object] = {"points": [[(Slow(), 0.0), (Unreadable(), 0.0)]], "clear": []}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.annotation_edit(*location, edit)
    assert raised.value.code == "deadline"
    assert _state(doc) == before


def test_add_refuses_a_spec_past_the_memory_budget_without_changes() -> None:
    doc, page, location = _page(pyropdf.DocumentLimits(max_memory_size=64 * 1024))
    before = _state(doc)
    spec: dict[str, object] = {"subtype": "Square", "rect": (10, 10, 50, 50), "contents": "x" * (256 * 1024)}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.annotation_add(*location, spec)
    assert raised.value.code == "memory_size"
    assert _state(doc) == before
    assert len(page.annotations) == 0


def test_edit_refuses_a_text_past_the_memory_budget_without_changes() -> None:
    doc, page, _ = _page(pyropdf.DocumentLimits(max_memory_size=64 * 1024))
    location = _square(page)
    before = _state(doc)
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.annotation_edit(*location, {"contents": "x" * (256 * 1024)})
    assert raised.value.code == "memory_size"
    assert _state(doc) == before


def test_a_spec_within_the_memory_budget_is_accepted() -> None:
    _, page, _ = _page(pyropdf.DocumentLimits(max_memory_size=4 * MIB))
    annotation = page.annotations.add(pyropdf.Square((10, 10, 50, 50), contents="x" * 1024))
    assert annotation.contents == "x" * 1024
