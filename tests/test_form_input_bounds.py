"""The core's form specifications and edits are bounded before they are copied.

``_core.widget_add``, ``_core.form_field_edit``, ``_core.widget_edit``, and
``_core.form_field_fill`` skip the Python wrappers' checks, so these tests
call them directly. An item that raises when read stands after a refused
field or past a refused length: the refusal must come before the binding
reads it.
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

    def __index__(self) -> int:
        """Sleep past the deadline and return a length."""
        time.sleep(0.2)
        return 10


_FIELD: dict[str, object] = {"kind": "text", "name": "f", "rect": (10.0, 10.0, 100.0, 30.0)}


def _form(limits: pyropdf.DocumentLimits | None = None) -> tuple[pyropdf.Document, Any, tuple[Any, ...], Any]:
    """Return a document with text field ``f``, its id, its page's location, and its widget's position."""
    source = pyropdf.Document()
    source.pages.new((200, 200))
    _core.widget_add(source._core(), *_location(source), _FIELD)  # noqa: SLF001
    doc = pyropdf.open(source.to_bytes(), limits=limits)
    widget = doc.pages[0].widgets[0]
    return doc, doc.form["f"]._id, _location(doc), (widget._index, widget._id)  # noqa: SLF001


def _location(doc: pyropdf.Document) -> tuple[Any, ...]:
    page = doc.pages[0]
    return (page._number, page._generation)  # noqa: SLF001


def _state(doc: pyropdf.Document) -> tuple[bytes, tuple[str, ...]]:
    return doc.to_bytes(), doc.history.steps


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ({**_FIELD, "name": "x" * (MIB + 1), "border_color": (Unreadable(), 0.0, 0.0)}, "form_field_input_size"),
        (
            {**_FIELD, "value": "x" * MIB, "label": "y", "border_color": (Unreadable(), 0.0, 0.0)},
            "form_field_input_size",
        ),
        (
            {**_FIELD, "kind": "combobox", "choices": [("a" * MIB, "b")], "border_color": (Unreadable(), 0.0, 0.0)},
            "form_field_input_size",
        ),
        ({**_FIELD, "border": (1.0, "D", [Unreadable()] * 33)}, "annotation_dash_count"),
    ],
)
def test_add_refuses_oversized_texts_before_reading_later_fields(spec: dict[str, Any], code: str) -> None:
    doc, _, location, _ = _form()
    before = _state(doc)
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.widget_add(doc._core(), *location, spec)  # noqa: SLF001
    assert raised.value.code == code
    assert _state(doc) == before


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        ({**_FIELD, "kind": "k" * 33}, "unknown field kind"),
        ({**_FIELD, "font_name": "f" * 33, "font_size": Unreadable()}, "unknown Standard 14 font"),
        (
            {**_FIELD, "kind": "combobox", "choices": [Unreadable()] * 4097},
            "at most 4096 choices",
        ),
    ],
)
def test_add_refuses_overlong_names_and_lists_before_copying(spec: dict[str, Any], match: str) -> None:
    doc, _, location, _ = _form()
    before = _state(doc)
    with pytest.raises(pyropdf.PdfError, match=match):
        _core.widget_add(doc._core(), *location, spec)  # noqa: SLF001
    assert _state(doc) == before


def test_add_refuses_missing_and_mistyped_fields() -> None:
    doc, _, location, _ = _form()
    with pytest.raises(TypeError, match="missing name"):
        _core.widget_add(doc._core(), *location, {"kind": "text", "rect": (10, 10, 20, 20)})  # noqa: SLF001
    with pytest.raises(TypeError, match="str"):
        _core.widget_add(doc._core(), *location, {**_FIELD, "value": b"x"})  # noqa: SLF001
    with pytest.raises(TypeError, match="pairs"):
        _core.widget_add(doc._core(), *location, {**_FIELD, "kind": "combobox", "choices": [("a",)]})  # noqa: SLF001


def test_field_edit_counts_its_texts_together() -> None:
    doc, field, _, _ = _form()
    before = _state(doc)
    edit: dict[str, object] = {
        "label": "x" * (MIB // 2),
        "choices": [("y" * (MIB // 2), "z")],
        "default_value": "w",
        "clear": [],
    }
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.form_field_edit(doc._core(), field, edit)  # noqa: SLF001
    assert raised.value.code == "form_field_input_size"
    assert _state(doc) == before


def test_field_edit_refuses_overlong_property_names() -> None:
    doc, field, _, _ = _form()
    with pytest.raises(pyropdf.PdfError, match="cannot be removed"):
        _core.form_field_edit(doc._core(), field, {"clear": ["x" * MIB]})  # noqa: SLF001
    with pytest.raises(pyropdf.PdfError, match="at most 3 properties"):
        _core.form_field_edit(doc._core(), field, {"clear": ["label"] * 4})  # noqa: SLF001


def test_fill_refuses_an_oversized_value_before_copying() -> None:
    doc, field, _, _ = _form()
    before = _state(doc)
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.form_field_fill(doc._core(), field, "x" * (MIB + 1))  # noqa: SLF001
    assert raised.value.code == "form_field_input_size"
    assert _state(doc) == before
    _core.form_field_fill(doc._core(), field, "x" * MIB, False)  # noqa: SLF001, FBT003 - the core's flag
    assert doc.form["f"].value == "x" * MIB


def test_widget_edit_refuses_oversized_inputs_before_reading_later_fields() -> None:
    doc, _, location, widget = _form()
    before = _state(doc)
    edit: dict[str, object] = {"caption": "x" * (MIB + 1), "text_color": (Unreadable(), 0.0, 0.0), "clear": []}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.widget_edit(doc._core(), *location, *widget, edit)  # noqa: SLF001
    assert raised.value.code == "annotation_input_size"
    edit = {"border": (1.0, "D", [Unreadable()] * 33), "clear": []}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.widget_edit(doc._core(), *location, *widget, edit)  # noqa: SLF001
    assert raised.value.code == "annotation_dash_count"
    with pytest.raises(pyropdf.PdfError, match="cannot be removed"):
        _core.widget_edit(doc._core(), *location, *widget, {"clear": ["x" * MIB]})  # noqa: SLF001
    assert _state(doc) == before


def test_add_observes_cancellation_while_reading() -> None:
    token = pyropdf.CancellationToken()
    doc, _, location, _ = _form(pyropdf.DocumentLimits(cancellation=token))
    spec: dict[str, object] = {**_FIELD, "name": "g", "rect": (Cancels(token), 10.0, 100.0, 30.0), "value": "v"}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.widget_add(doc._core(), *location, spec)  # noqa: SLF001
    assert raised.value.code == "cancelled"


def test_edits_observe_their_deadline_without_changes() -> None:
    doc, field, location, widget = _form(pyropdf.DocumentLimits(max_duration=0.05))
    before = _state(doc)
    spec: dict[str, object] = {**_FIELD, "name": "g", "rect": (Slow(), 10.0, 100.0, 30.0), "value": "v"}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.widget_add(doc._core(), *location, spec)  # noqa: SLF001
    assert raised.value.code == "deadline"
    edit: dict[str, object] = {"rect": (Slow(), 10.0, 100.0, 30.0), "caption": "c", "clear": []}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.widget_edit(doc._core(), *location, *widget, edit)  # noqa: SLF001
    assert raised.value.code == "deadline"
    edit = {"max_length": Slow(), "default_value": "d", "clear": []}
    with pytest.raises(pyropdf.LimitError) as raised:
        _core.form_field_edit(doc._core(), field, edit)  # noqa: SLF001
    assert raised.value.code == "deadline"
    assert _state(doc) == before
