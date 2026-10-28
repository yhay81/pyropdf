"""Field labels, maximum lengths, default values, flags, choices, reset, and NeedAppearances.

Ported from pylopdf's ``tests/test_field_properties.py`` at
ebb01d3c4c742046e8ce7fd72957cd15a2933321. Intended differences: the core
edit is ``_core.form_field_edit`` on a field's object and generation, where
pylopdf calls ``Document.edit_field`` on its object number.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from test_acroform import _build_form_pdf, build_raw_pdf

import pyropdf
from pyropdf import FieldChoice, FieldFlags, _core


def _form() -> pyropdf.Document:
    return pyropdf.open(_build_form_pdf())


def test_text_properties_are_set_read_and_removed() -> None:
    doc = _form()
    customer = doc.form["customer"]
    assert (customer.label, customer.max_length, customer.default_value) == (None, None, None)

    customer.label = "Customer name"
    customer.max_length = 20
    customer.default_value = "nobody"

    assert (customer.label, customer.max_length, customer.default_value) == ("Customer name", 20, "nobody")
    reopened = pyropdf.open(doc.to_bytes()).form["customer"]
    assert (reopened.label, reopened.max_length, reopened.default_value) == ("Customer name", 20, "nobody")
    del customer.label, customer.max_length, customer.default_value
    assert (customer.label, customer.max_length, customer.default_value) == (None, None, None)
    # An inherited /MaxLen reads through the field's parents.
    assert doc.form["code"].max_length == 6


def test_reset_restores_the_default_or_clears_the_value() -> None:
    doc = _form()
    customer = doc.form["customer"]
    customer.default_value = "nobody"

    customer.reset()

    assert customer.value == "nobody"
    assert "nobody" in doc.pages[0].text()
    del customer.default_value
    customer.reset()
    assert customer.value is None
    assert "nobody" not in doc.pages[0].text()
    agree = doc.form["agree"]
    agree.value = True
    agree.reset()
    assert agree.value == "Off"
    plan = doc.form["plan"]
    plan.value = "Pro"
    plan.default_value = "Basic"
    plan.reset()
    assert plan.value == "Basic"
    assert [widget.obj["AS"] for widget in plan.widgets] == ["Off", "Basic"]


def test_flags_and_choices_are_assignable() -> None:
    doc = _form()
    customer = doc.form["customer"]
    level = doc.form["level"]

    customer.flags = customer.flags | FieldFlags.REQUIRED | FieldFlags.MULTILINE
    level.choices = [FieldChoice(value="a", label="Alpha"), "b"]

    assert FieldFlags.REQUIRED in customer.flags
    assert FieldFlags.MULTILINE in customer.flags
    assert level.choices == (FieldChoice(value="a", label="Alpha"), FieldChoice(value="b", label="b"))
    assert level.obj["Opt"][1] == b"b"
    with pytest.raises(pyropdf.PdfError, match="decide a field's kind"):
        level.flags = FieldFlags(0)
    with pytest.raises(pyropdf.PdfError, match="only a choice field"):
        customer.choices = ["x"]
    with pytest.raises(pyropdf.PdfError, match="only a text field"):
        level.max_length = 3
    with pytest.raises(TypeError, match="FieldChoice or a str"):
        level.choices = cast("Any", [1])
    with pytest.raises(ValueError, match="at most 4096"):
        level.choices = ["x"] * 4097
    with pytest.raises(ValueError, match="max_length"):
        customer.max_length = -1
    with pytest.raises(pyropdf.LimitError):
        customer.label = "x" * (1024 * 1024 + 1)


def test_need_appearances_is_read_and_set() -> None:
    doc = _form()

    assert doc.form.need_appearances is False
    doc.form.need_appearances = True
    assert doc.form.need_appearances is True
    assert doc.catalog["AcroForm"]["NeedAppearances"] is True
    # Filling every field draws them, which clears the flag.
    doc.form["customer"].value = "x"
    assert doc.form.need_appearances is False
    with pytest.raises(TypeError, match="bool"):
        doc.form.need_appearances = cast("Any", 1)
    empty = pyropdf.Document()
    empty.pages.new()
    assert empty.form.need_appearances is False
    with pytest.raises(pyropdf.PdfError, match="no interactive form"):
        empty.form.need_appearances = True


def test_the_core_refuses_bad_field_edits() -> None:
    doc = _form()
    core = doc._core()  # noqa: SLF001 - the core edit is internal.
    customer = (doc.form["customer"].xref, 0)
    with pytest.raises(pyropdf.PdfError, match="cannot be removed"):
        _core.form_field_edit(core, customer, {"clear": ["value"]})
    with pytest.raises(pyropdf.PdfError, match="32-bit"):
        _core.form_field_edit(core, customer, {"clear": [], "flags": -1})
    with pytest.raises(pyropdf.StaleObjectError):
        _core.form_field_edit(core, (1, 0), {"clear": []})
    assert doc.form["customer"].value == "initial"


def test_an_edit_is_one_step_and_a_refused_edit_changes_nothing() -> None:
    doc = _form()
    code = doc.form["code"]
    code.value = "abc"
    with pytest.raises(pyropdf.PdfError, match="exceeding MaxLen 2"):
        code.max_length = 2
    assert (code.max_length, code.value) == (6, "abc")

    code.label = "Code"
    code.max_length = 4
    doc.history.undo()
    assert (code.label, code.max_length) == ("Code", 6)


def _inherited_default_pdf() -> bytes:
    """Build a text field whose /DV and /MaxLen come from its parent."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [6 0 R] >>",
            4: "<< /Fields [5 0 R] /DA (/Helv 0 Tf 0 g) >>",
            5: "<< /FT /Tx /T (parent) /DV (inherited) /MaxLen 12 /Kids [6 0 R] >>",
            6: "<< /T (child) /Parent 5 0 R /V (typed) /Type /Annot /Subtype /Widget /Rect [10 10 190 40] >>",
        }
    )


def test_reset_fills_an_inherited_default_value() -> None:
    doc = pyropdf.open(_inherited_default_pdf())
    child = doc.form["parent.child"]
    assert (child.default_value, child.max_length, child.value) == ("inherited", 12, "typed")
    child.label = "Child"
    child.reset()
    assert (child.label, child.value) == ("Child", "inherited")
    reopened = pyropdf.open(doc.to_bytes()).form["parent.child"]
    assert (reopened.label, reopened.value) == ("Child", "inherited")


def test_edits_in_a_transaction_roll_back_together() -> None:
    doc = _form()
    customer = doc.form["customer"]
    before = doc.to_bytes()

    def edit_and_fail() -> None:
        with doc.transaction("edits"):
            customer.label = "Customer"
            customer.value = "changed"
            customer.reset()
            assert (customer.label, customer.value) == ("Customer", None)
            message = "rollback"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        edit_and_fail()
    assert (customer.label, customer.value) == (None, "initial")
    assert doc.to_bytes() == before


def test_a_field_edit_observes_cancellation() -> None:
    token = pyropdf.CancellationToken()
    doc = pyropdf.open(_build_form_pdf(), limits=pyropdf.DocumentLimits(cancellation=token))
    customer = doc.form["customer"]
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as caught:
        customer.label = "Customer"
    assert caught.value.code == "cancelled"
