"""page.widgets and Field.widgets: form widgets, their geometry and styling, and button states.

Ported from pylopdf's tests/test_widgets.py at ebb01d3c. Intended
differences:

- ``Widget.font`` waits for pyropdf's ``Font`` (#153), so the styling case
  sets the font through the core edit's ``font_name``, and the validation
  case leaves out a font that is no Font.
- a test checks that widget handles go stale with their page and that
  undo restores them, and another that a widget edit is one step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from test_acroform import _build_form_pdf

import pyropdf
from pyropdf import AnnotationFlags, Border, BorderStyle, Color, FieldType, Rect, Widget, _core

ASSETS = Path(__file__).parent / "assets" / "real_world"


def _form() -> pyropdf.Document:
    return pyropdf.open(_build_form_pdf())


def _pixel(pixmap: pyropdf.Pixmap, x: float, y: float) -> tuple[int, int, int]:
    offset = int(y) * pixmap.stride + int(x) * pixmap.channels
    red, green, blue = bytes(pixmap)[offset : offset + 3]
    return red, green, blue


def _edit(widget: Widget, edit: dict[str, object]) -> None:
    """Apply a core widget edit, as the setters do."""
    page = widget.page
    core = page.document._core()  # noqa: SLF001 - the core edit is internal.
    _core.widget_edit(core, page._number, page._generation, widget._index, widget._id, {"clear": [], **edit})  # noqa: SLF001


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


def test_a_real_form_reads_and_restyles() -> None:
    doc = pyropdf.open(ASSETS / "f1040.pdf")
    widgets = doc.pages[0].widgets
    text = next(widget for widget in widgets if widget.field is not None and widget.field.type is FieldType.TEXT)
    checkbox = next(widget for widget in widgets if widget.button_states)

    assert len(widgets) > 100
    assert text.font_size > 0
    assert checkbox.on_state is not None
    field = text.field
    assert field is not None
    assert text in field.widgets
    assert doc.form[field.name] == field
    field.value = "Jane"
    text.text_color = Color(0, 0.5, 0)

    assert "Jane" in doc.pages[0].text()
    assert pyropdf.open(doc.to_bytes()).form[field.name].value == "Jane"


def test_text_styling_is_read_and_set_and_the_value_is_drawn_again() -> None:
    doc = _form()
    customer = doc.pages[0].widgets[0]
    # The AcroForm's /DA, which the widget inherits.
    assert (customer.font_size, customer.text_color) == (0.0, Color(0, 0, 0))

    customer.text_color = Color(1, 0, 0)
    _edit(customer, {"font_name": "Courier-Bold"})
    customer.font_size = 14
    customer.background_color = Color(1, 1, 0)
    customer.border_color = Color(0, 0, 1)
    customer.border = Border(2, style=BorderStyle.DASHED, dashes=(3, 1))

    assert customer.text_color == Color(1, 0, 0)
    assert customer.font_size == 14
    assert customer.background_color == Color(1, 1, 0)
    assert customer.border_color == Color(0, 0, 1)
    assert customer.border == Border(2, dashes=(3, 1))
    assert customer.obj["DA"].text == "/CoBo 14 Tf 1 0 0 rg"
    assert doc.form["customer"].value == "initial"
    pixmap = doc.pages[0].pixmap(dpi=72)
    assert _pixel(pixmap, 240, 75) == (255, 255, 0)
    inked = {_pixel(pixmap, x, y) for x in range(52, 120) for y in range(74, 90)}
    assert any(red > 200 and green < 80 and blue < 80 for red, green, blue in inked)
    assert "initial" in doc.pages[0].text()

    reopened = pyropdf.open(doc.to_bytes()).pages[0].widgets[0]
    assert reopened.font_size == 14
    assert reopened.background_color == Color(1, 1, 0)

    del customer.background_color, customer.border_color, customer.border
    assert customer.background_color is None
    assert customer.border_color is None
    assert customer.border is None
    customer.font_size = 0
    assert customer.font_size == 0


def test_moving_hiding_and_restyling_buttons() -> None:
    doc = _form()
    agree = doc.pages[0].widgets[1]

    agree.rect = (60, 100, 90, 130)
    agree.flags = AnnotationFlags.PRINT | AnnotationFlags.HIDDEN
    agree.border_color = Color(1, 0, 0)

    assert agree.rect == Rect(60, 100, 90, 130)
    assert AnnotationFlags.HIDDEN in agree.flags
    assert agree.border_color == Color(1, 0, 0)
    assert agree.button_states == ("Yes", "Off")
    # The redrawn states draw the new border.
    agree.flags = AnnotationFlags.PRINT
    assert _pixel(doc.pages[0].pixmap(dpi=72), 60, 115) == (255, 0, 0)
    doc.form["agree"].value = True
    assert doc.form["agree"].value == "Yes"


def test_widget_edits_validate() -> None:
    doc = _form()
    customer = doc.pages[0].widgets[0]

    with pytest.raises(ValueError, match="font_size"):
        customer.font_size = -1
    with pytest.raises(TypeError, match="Color"):
        customer.text_color = cast("Any", (1, 0, 0))
    with pytest.raises(TypeError, match="Border"):
        customer.border = cast("Any", 1)
    with pytest.raises(pyropdf.LimitError):
        customer.caption = "x" * (1024 * 1024 + 1)
    edits: list[tuple[dict[str, object], str]] = [
        ({"rect": (10.0, 10.0, 5.0, 20.0)}, "not empty"),
        ({"font_name": "Comic"}, "unknown Standard 14"),
        ({"flags": 2048}, "bits 1 to 10"),
        ({"clear": ["rect"]}, "cannot be removed"),
        ({"text_color": (2.0, 0.0, 0.0)}, "components"),
    ]
    for edit, message in edits:
        with pytest.raises(pyropdf.PdfError, match=message):
            _edit(customer, edit)
    assert customer.rect == Rect(50, 72, 250, 92)


def test_pushbutton_captions_are_drawn() -> None:
    doc = pyropdf.open(
        _build_form_pdf(
            text_widget_extra="/FT /Btn /Ff 65536 /MK << /CA (Old) /BG [0.8] >>",
        )
    )
    button = doc.pages[0].widgets[0]

    button.caption = "Submit"

    assert button.caption == "Submit"
    assert "Submit" in doc.pages[0].text()
    del button.caption
    assert button.caption is None


def test_a_widget_edit_is_one_step_and_a_refused_one_changes_nothing() -> None:
    doc = _form()
    customer = doc.pages[0].widgets[0]
    customer.background_color = Color(1, 1, 0)
    doc.form["code"].value = "abcdef"
    code = doc.form["code"].widgets[0]

    # A comb of six cells cannot draw a seventh character.
    doc.form["code"].obj["MaxLen"] = 5
    with pytest.raises(pyropdf.PdfError, match="exceeding MaxLen 5"):
        code.border_color = Color(1, 0, 0)
    assert code.border_color is None
    customer.border_color = Color(0, 0, 1)
    doc.history.undo()
    assert (customer.background_color, customer.border_color) == (Color(1, 1, 0), None)
