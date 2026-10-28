"""page.widgets and Field.widgets: form widgets, their geometry, and button states.

pylopdf's tests/test_widgets.py at ebb01d3c, the reading cases ported.
Intended differences:

- the styling cases wait for #195, and the fill half of the real form
  case waits for #193.
- a test checks that widget handles go stale with their page and that
  undo restores them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_acroform import _build_form_pdf

import pyropdf
from pyropdf import AnnotationFlags, FieldType, Rect, Widget

ASSETS = Path(__file__).parent / "assets" / "real_world"


def _form() -> pyropdf.Document:
    return pyropdf.open(_build_form_pdf())


def test_widgets_list_the_form_widgets_of_a_page() -> None:
    doc = _form()
    page = doc.pages[0]

    widgets = page.widgets

    assert len(widgets) == 8
    assert all(isinstance(widget, Widget) for widget in widgets)
    names = [None if widget.field is None else widget.field.name for widget in widgets]
    assert names == ["customer", "agree", "person.first", "level", "plan", "plan", "notes", "code"]
    customer = widgets[0]
    assert customer.rect == Rect(50, 72, 250, 92)
    assert customer.page == page
    assert customer.xref == 9
    assert customer.obj["T"].text == "customer"
    assert customer.flags == AnnotationFlags.PRINT
    # Annotations and links leave widgets out.
    assert len(page.annotations) == 0
    radio = widgets[4]
    assert radio.button_states == ("Pro", "Off")
    assert radio.on_state == "Pro"
    assert customer.button_states == ()
    assert customer.on_state is None
    assert repr(customer) == "Widget(rect=Rect(x0=50.0, y0=72.0, x1=250.0, y1=92.0))"


def test_fields_list_their_widgets() -> None:
    doc = _form()

    plan = doc.form["plan"]

    assert [widget.rect for widget in plan.widgets] == [Rect(300, 112, 320, 132), Rect(350, 112, 370, 132)]
    assert plan.widgets[0] == doc.pages[0].widgets[4]
    assert doc.form["customer"].widgets == [doc.pages[0].widgets[0]]
    assert doc.pages[0].widgets[5].field == plan


def test_widget_handles_go_stale_with_their_page() -> None:
    doc = _form()
    doc.pages.new()
    widget = doc.pages[0].widgets[0]

    del doc.pages[0]

    with pytest.raises(pyropdf.StalePageError):
        _ = widget.rect
    assert repr(widget) == "<stale Widget>"
    doc.history.undo()
    assert widget.rect == Rect(50, 72, 250, 92)


def test_a_real_form_reads() -> None:
    doc = pyropdf.open(ASSETS / "f1040.pdf")
    widgets = doc.pages[0].widgets
    text = next(widget for widget in widgets if widget.field is not None and widget.field.type is FieldType.TEXT)
    checkbox = next(widget for widget in widgets if widget.button_states)

    assert len(widgets) > 100
    assert checkbox.on_state is not None
    field = text.field
    assert field is not None
    assert text in field.widgets
    assert doc.form[field.name] == field
