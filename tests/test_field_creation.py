"""Creating and removing form fields with page.widgets.add, del page.widgets[i], and del doc.form[name].

Ported from pylopdf's ``tests/test_field_creation.py`` at
ebb01d3c4c742046e8ce7fd72957cd15a2933321. Intended differences:

- ``Widget.font`` waits for #212, so the case that creates a field in
  Courier reads the font back from the widget's ``/DA``.
- pyropdf's ``to_bytes`` has no ``prune``.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, cast

import pytest
from test_acroform import _build_form_pdf

import pyropdf
from pyropdf import (
    Border,
    CheckBox,
    Color,
    ComboBox,
    FieldChoice,
    FieldFlags,
    FieldType,
    Font,
    ListBox,
    PushButton,
    RadioButton,
    Rect,
    SignatureField,
    TextField,
)


def _page(rotation: int = 0) -> pyropdf.Page:
    page = pyropdf.Document().pages.new((300, 300))
    page.rotation = rotation
    return page


def test_every_kind_of_field_is_created_with_its_widget() -> None:
    page = _page()
    doc = page.document

    text = page.widgets.add(TextField("name", (20, 20, 200, 40), value="Ada", label="Your name"))
    page.widgets.add(CheckBox("agree", (20, 60, 35, 75), checked=True))
    page.widgets.add(RadioButton("plan", (20, 100, 35, 115), on_state="Basic", selected=True))
    page.widgets.add(RadioButton("plan", (60, 100, 75, 115), on_state="Pro"))
    page.widgets.add(ComboBox("level", (20, 140, 200, 160), choices=["low", FieldChoice(value="hi", label="High")]))
    page.widgets.add(ListBox("items", (20, 170, 200, 220), choices=["a", "b"], value="b"))
    button = page.widgets.add(PushButton("go", (220, 20, 280, 40), caption="Go"))
    page.widgets.add(SignatureField("sig", (20, 240, 200, 280)))

    assert text.rect == Rect(20, 20, 200, 40)
    assert text.field is not None
    assert text.field.label == "Your name"
    kinds = {name: field.type for name, field in doc.form.items()}
    assert kinds == {
        "agree": FieldType.CHECKBOX,
        "go": FieldType.PUSHBUTTON,
        "items": FieldType.LISTBOX,
        "level": FieldType.COMBOBOX,
        "name": FieldType.TEXT,
        "plan": FieldType.RADIO,
        "sig": FieldType.SIGNATURE,
    }
    values = {name: field.value for name, field in doc.form.items()}
    assert values == {
        "agree": "Yes",
        "go": None,
        "items": "b",
        "level": None,
        "name": "Ada",
        "plan": "Basic",
        "sig": None,
    }
    assert len(doc.form["plan"].widgets) == 2
    assert doc.form["level"].choices == (FieldChoice(value="low", label="low"), FieldChoice(value="hi", label="High"))
    assert button.caption == "Go"
    assert "Ada" in page.text()
    assert "Go" in page.text()
    assert doc.form.need_appearances is False
    reopened = pyropdf.open(doc.to_bytes())
    assert {name: field.value for name, field in reopened.form.items()} == values


def test_created_fields_are_filled_by_pyropdf() -> None:
    page = _page(90)
    doc = page.document
    page.widgets.add(TextField("name", (20, 20, 200, 40), font=Font.standard("Courier"), font_size=10))
    page.widgets.add(CheckBox("agree", (20, 60, 35, 75)))
    page.widgets.add(RadioButton("plan", (20, 100, 35, 115), on_state="Basic"))
    page.widgets.add(RadioButton("plan", (60, 100, 75, 115), on_state="Pro"))

    doc.form["name"].value = "Grace"
    doc.form["agree"].value = True
    doc.form["plan"].value = "Pro"

    assert (doc.form["name"].value, doc.form["agree"].value, doc.form["plan"].value) == ("Grace", "Yes", "Pro")
    assert [widget.obj["AS"] for widget in doc.form["plan"].widgets] == ["Off", "Pro"]
    assert "Grace" in page.text()
    name = page.widgets[0]
    assert name.obj["MK"]["R"] == 90
    assert bytes(name.obj["DA"]).startswith(b"/Cour 10")


def test_a_second_widget_joins_the_field_and_splits_a_merged_one() -> None:
    page = _page()
    doc = page.document
    first = page.widgets.add(TextField("name", (20, 20, 200, 40), value="Ada"))
    assert first.field is not None
    assert first.field.xref == first.xref

    second = page.widgets.add(TextField("name", (20, 50, 200, 70)))

    field = doc.form["name"]
    assert field.widgets == [first, second]
    assert field.xref not in (first.xref, second.xref)
    assert field.value == "Ada"
    assert page.text().count("Ada") == 2
    with pytest.raises(pyropdf.PdfError, match="already has a text field"):
        page.widgets.add(CheckBox("name", (20, 80, 30, 90)))


def test_field_specifications_validate() -> None:
    page = _page()
    for spec, error in [
        (lambda: TextField("", (0, 0, 10, 10)), None),
        (lambda: TextField("a", (0, 0, 10, 10), max_length=-1), ValueError),
        (lambda: TextField("a", (0, 0, 10, 10), font=cast("Any", "Helvetica")), TypeError),
        (lambda: TextField("a", (0, 0, 10, 10), font_size=-1), ValueError),
        (lambda: TextField("a", (0, 0, 10, 10), align=cast("Any", "justify")), ValueError),
        (lambda: CheckBox("a", (0, 0, 10, 10), on_state="Off"), ValueError),
        (lambda: RadioButton("a", (0, 0, 10, 10), on_state=""), ValueError),
        (lambda: CheckBox("a", (0, 0, 10, 10), checked=cast("Any", 1)), TypeError),
        (lambda: ComboBox("a", (0, 0, 10, 10), choices=cast("Any", "abc")), TypeError),
        (lambda: ListBox("a", (0, 0, 10, 10), choices=["x"] * 4097), ValueError),
        (lambda: TextField(cast("Any", 5), (0, 0, 10, 10)), TypeError),
        (lambda: TextField("a", (0, 0, 10, 10), border=cast("Any", 1)), TypeError),
        (lambda: TextField("a", (0, 0, 10, 10), flags=cast("Any", True)), TypeError),  # noqa: FBT003 - a bool is no FieldFlags.
    ]:
        if error is None:
            spec()
            continue
        with pytest.raises(error):
            spec()
    with pytest.raises(pyropdf.PdfError, match="has a name"):
        page.widgets.add(TextField("", (0, 0, 10, 10)))
    with pytest.raises(pyropdf.PdfError, match="no dots"):
        page.widgets.add(TextField("a.b", (0, 0, 10, 10)))
    with pytest.raises(pyropdf.PdfError, match="bits that the kind sets"):
        page.widgets.add(TextField("a", (0, 0, 10, 10), flags=FieldFlags.RADIO))
    with pytest.raises(TypeError, match="field specification"):
        page.widgets.add(cast("Any", object()))
    with pytest.raises(pyropdf.LimitError):
        TextField("a", (0, 0, 10, 10), value="x" * (1024 * 1024 + 1))
    assert len(page.document.form) == 0
    assert "Annots" not in page.obj


def test_styled_fields_draw_their_style() -> None:
    page = _page()

    widget = page.widgets.add(
        TextField(
            "name",
            (20, 20, 200, 60),
            value="Ada",
            border=Border(2, dashes=(3, 1)),
            border_color=Color(0, 0, 1),
            background_color=Color(1, 1, 0),
            text_color=Color(1, 0, 0),
            align="center",
        )
    )

    assert widget.border == Border(2, dashes=(3, 1))
    assert widget.background_color == Color(1, 1, 0)
    assert widget.text_color == Color(1, 0, 0)
    assert widget.obj["Q"] == 1
    pixmap = page.pixmap(dpi=72)
    offset = 50 * pixmap.stride + 190 * pixmap.channels
    assert tuple(bytes(pixmap)[offset : offset + 3]) == (255, 255, 0)


def test_a_field_can_omit_its_border_color() -> None:
    page = _page()
    widget = page.widgets.add(TextField("name", (0, 0, 100, 20), border_color=None))
    assert widget.border_color is None


def test_added_buttons_synchronize_the_whole_field() -> None:
    page = _page()
    page.widgets.add(RadioButton("plan", (0, 0, 20, 20), on_state="Basic", selected=True))
    page.widgets.add(RadioButton("plan", (30, 0, 50, 20), on_state="Pro", selected=True))
    page.widgets.add(CheckBox("agree", (0, 30, 20, 50), checked=True))
    page.widgets.add(CheckBox("agree", (30, 30, 50, 50)))

    for doc in (page.document, pyropdf.open(page.document.to_bytes())):
        assert doc.form["plan"].value == "Pro"
        assert [widget.obj["AS"] for widget in doc.form["plan"].widgets] == ["Off", "Pro"]
        assert [widget.obj["AS"] for widget in doc.form["agree"].widgets] == ["Yes", "Yes"]


def test_a_new_field_cannot_duplicate_a_nonterminal_name() -> None:
    doc = pyropdf.open(_build_form_pdf())
    before = list(doc.form)

    with pytest.raises(pyropdf.PdfError, match="non-terminal"):
        doc.pages[0].widgets.add(TextField("person", (0, 0, 100, 20)))

    assert list(doc.form) == before


def test_adding_a_widget_is_one_edit() -> None:
    page = _page()
    doc = page.document
    page.widgets.add(TextField("name", (20, 20, 200, 40), value="Ada"))
    page.widgets.add(TextField("name", (20, 50, 200, 70)))

    doc.history.undo()

    assert [widget.xref for widget in doc.form["name"].widgets] == [page.widgets[0].xref]
    assert doc.form["name"].value == "Ada"
    doc.history.undo()
    assert len(doc.form) == 0
    assert len(page.widgets) == 0
    assert "AcroForm" not in doc.catalog


def test_a_refused_widget_leaves_no_partial_writes() -> None:
    """A widget that fails to draw after the form and its objects were written changes nothing."""
    page = _page()
    doc = page.document
    before = (doc.to_bytes(), len(doc.objects))
    with pytest.raises(pyropdf.PdfError, match="MaxLen"):
        page.widgets.add(TextField("name", (20, 20, 200, 40), value="too long", max_length=2, flags=FieldFlags.COMB))
    assert (doc.to_bytes(), len(doc.objects)) == before
    assert "AcroForm" not in doc.catalog
    assert len(page.widgets) == 0


def test_adding_a_widget_observes_cancellation() -> None:
    token = pyropdf.CancellationToken()
    doc = pyropdf.open(_page().document.to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token))
    page = doc.pages[0]
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as caught:
        page.widgets.add(TextField("name", (20, 20, 200, 40)))
    assert caught.value.code == "cancelled"


def _generation_pdf() -> bytes:
    """Build a form whose merged text field is object 5 with generation 3."""
    objects = {
        (1, 0): "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        (2, 0): "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (3, 0): "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Annots [5 3 R] >>",
        (4, 0): "<< /Fields [5 3 R] /DA (/Helv 0 Tf 0 g) >>",
        (5, 3): "<< /FT /Tx /T (name) /V (Ada) /Type /Annot /Subtype /Widget /Rect [20 20 200 40] /P 3 0 R >>",
    }
    output = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for (number, generation), value in objects.items():
        offsets[number] = (len(output), generation)
        output.extend(f"{number} {generation} obj\n{value}\nendobj\n".encode())
    xref = len(output)
    output.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for number in range(1, 6):
        offset, generation = offsets[number]
        output.extend(f"{offset:010} {generation:05} n \n".encode())
    output.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def test_joining_a_merged_field_keeps_its_generation() -> None:
    doc = pyropdf.open(_generation_pdf())
    page = doc.pages[0]
    page.widgets.add(TextField("name", (20, 50, 200, 70)))
    field = doc.form["name"]
    assert field.value == "Ada"
    assert field.widgets[0].xref == 5
    reopened = pyropdf.open(doc.to_bytes())
    assert len(reopened.form["name"].widgets) == 2
    assert b"5 3 R" in doc.to_bytes()


def test_widgets_and_fields_are_removed() -> None:
    page = _page()
    doc = page.document
    page.widgets.add(TextField("name", (20, 20, 200, 40), value="Ada"))
    page.widgets.add(RadioButton("plan", (20, 100, 35, 115), on_state="Basic"))
    page.widgets.add(RadioButton("plan", (60, 100, 75, 115), on_state="Pro"))

    del page.widgets[1]
    assert len(doc.form["plan"].widgets) == 1
    del page.widgets[1]
    assert "plan" not in doc.form
    del doc.form["name"]
    assert len(doc.form) == 0
    assert len(page.widgets) == 0
    assert "Ada" not in page.text()
    with pytest.raises(KeyError):
        del doc.form["name"]


def test_a_second_widget_is_removed_from_its_field() -> None:
    page = _page()
    doc = page.document
    first = page.widgets.add(TextField("name", (20, 20, 200, 40), value="Ada"))
    page.widgets.add(TextField("name", (20, 50, 200, 70)))

    del page.widgets[1]

    assert doc.form["name"].widgets == [first]
    assert page.text().count("Ada") == 1


def test_removing_from_an_existing_form_keeps_the_others() -> None:
    doc = pyropdf.open(_build_form_pdf())
    before = set(doc.form)

    del doc.form["person.first"]
    del doc.pages[0].widgets[0]

    assert set(doc.form) == before - {"person.first", "customer"}
    assert "person" not in {name.split(".")[0] for name in doc.form}
    reopened = pyropdf.open(doc.to_bytes())
    assert set(reopened.form) == set(doc.form)


@pytest.mark.parametrize("widget_only", [False, True])
def test_failed_removal_restores_widgets_on_earlier_pages(*, widget_only: bool) -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    doc.pages.new()
    page = doc.pages[0]
    widget = page.widgets.add(TextField("name", (0, 0, 100, 20), value="Ada"))
    # The later page fails admission after the earlier page was visited.
    doc.pages[1].obj["Annots"] = [None] * 4097

    if widget_only:
        with pytest.raises(pyropdf.PdfError, match="too many annotations"):
            del page.widgets[0]
    else:
        with pytest.raises(pyropdf.PdfError, match="too many annotations"):
            del doc.form["name"]

    doc.pages[1].obj["Annots"] = []
    assert doc.form["name"].widgets == [widget]
    assert doc.form["name"].value == "Ada"
    assert "Ada" in page.text()


@pytest.mark.parametrize("indirect", [False, True])
def test_calculation_order_follows_nested_field_splits_and_removals(*, indirect: bool) -> None:
    doc = pyropdf.open(_build_form_pdf())
    form = doc.catalog["AcroForm"]
    order = [doc.form["person.first"].obj, doc.form["customer"].obj]
    form["CO"] = doc.objects[doc.objects.add(order)] if indirect else order
    doc.pages[0].widgets.add(TextField("person.first", (0, 0, 100, 20)))

    assert form["CO"][0].xref == doc.form["person.first"].xref
    del doc.form["person.first"]
    assert [entry.xref for entry in form["CO"]] == [doc.form["customer"].xref]
    assert len(pyropdf.open(doc.to_bytes()).catalog["AcroForm"]["CO"]) == 1


@pytest.mark.parametrize("selection", [slice(None), slice(None, None, -1), slice(0, 3, 2)])
def test_slices_remove_direct_widgets(selection: slice) -> None:
    page = _page()
    page.obj["Annots"] = [
        {"Subtype": pyropdf.Name("Widget"), "Rect": [index * 30, 0, index * 30 + 20, 20]} for index in range(3)
    ]
    expected = list(range(3))
    del expected[selection]

    del page.widgets[selection]

    assert [widget.rect.x0 for widget in page.widgets] == [index * 30 for index in expected]


def test_a_cyclic_parent_chain_is_refused_without_hanging() -> None:
    script = """
import pyropdf
page = pyropdf.Document().pages.new()
page.widgets.add(pyropdf.TextField('name', (0, 0, 100, 20)))
field = page.document.form['name']
field.obj['Parent'] = field.obj
try:
    del page.document.form['name']
except pyropdf.PdfError as error:
    assert 'cycle' in str(error), str(error)
else:
    raise AssertionError('a cyclic parent chain was accepted')
assert len(page.widgets) == 1
assert page.document.form['name'].xref == field.xref
"""
    result = subprocess.run(  # noqa: S603 - fixed script under the active interpreter.
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_removing_a_widget_preserves_its_object_generation() -> None:
    raw = _build_form_pdf().replace(b"[9 0 R", b"[9 2 R").replace(b"\n9 0 obj", b"\n9 2 obj")
    head, tail = raw.rsplit(b"\nxref\n", 1)
    lines = tail.splitlines(keepends=True)
    lines[10] = lines[10].replace(b"00000 n", b"00002 n")
    doc = pyropdf.open(head + b"\nxref\n" + b"".join(lines))
    assert doc.pages[0].widgets[0].obj.generation == 2

    del doc.pages[0].widgets[0]

    assert "customer" not in doc.form
    assert len(doc.pages[0].widgets) == 7


def test_removing_widgets_is_one_edit() -> None:
    page = _page()
    doc = page.document
    page.widgets.add(TextField("name", (20, 20, 200, 40), value="Ada"))
    page.widgets.add(TextField("other", (20, 50, 200, 70)))

    del page.widgets[:]

    assert len(doc.form) == 0
    doc.history.undo()
    assert list(doc.form) == ["name", "other"]
    assert "Ada" in page.text()


def test_removing_a_field_with_a_nonzero_generation() -> None:
    doc = pyropdf.open(_generation_pdf())
    del doc.form["name"]
    assert len(doc.form) == 0
    assert len(doc.pages[0].widgets) == 0
    assert b"5 3 R" not in doc.to_bytes()

    doc = pyropdf.open(_generation_pdf())
    del doc.pages[0].widgets[0]
    assert len(doc.form) == 0
    assert b"5 3 R" not in doc.to_bytes()


def test_removal_observes_cancellation() -> None:
    token = pyropdf.CancellationToken()
    doc = pyropdf.open(_generation_pdf(), limits=pyropdf.DocumentLimits(cancellation=token))
    page = doc.pages[0]
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as caught:
        del page.widgets[0]
    assert caught.value.code == "cancelled"
