"""doc.form.repair_radio_groups: flat radio buttons gathered under one field.

pylopdf's tests/test_radio_groups.py at ebb01d3c, ported. Intended
differences:

- selecting a button after the repair waits for #193, so the test reads
  the repaired group, saves it, and undoes the repair instead.
- the untouched form is built from raw objects, since adding widgets
  waits for #193.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_acroform import build_raw_pdf

import pyropdf
from pyropdf import FieldType

_BUTTON = "/FT /Btn /Ff 49152 /T (plan) /Type /Annot /Subtype /Widget /P 3 0 R"


def _flat() -> pyropdf.Document:
    on = "/AP << /N << /{state} 8 0 R /Off 8 0 R >> >>"
    return pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R 5 0 R 6 0 R 7 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [4 0 R 5 0 R 6 0 R 7 0 R] >>",
                4: f"<< {_BUTTON} /Rect [10 10 25 25] /AS /Off {on.format(state='Basic')} >>",
                5: "<< /FT /Tx /T (name) /Type /Annot /Subtype /Widget /Rect [10 40 90 60] /P 3 0 R >>",
                6: f"<< {_BUTTON} /Rect [30 10 45 25] /V /Pro /AS /Pro {on.format(state='Pro')} >>",
                7: f"<< {_BUTTON} /Rect [50 10 65 25] /V /Pro /AS /Off {on.format(state='Team')} >>",
                8: "<< /Length 0 /Subtype /Form /BBox [0 0 15 15] >>\nstream\n\nendstream",
            }
        )
    )


def test_flat_radio_buttons_become_one_field() -> None:
    doc = _flat()
    assert len(doc.form["plan"].widgets) == 1

    assert doc.form.repair_radio_groups() == 1

    plan = doc.form["plan"]
    assert plan.type is FieldType.RADIO
    assert plan.value == "Pro"
    assert [widget.on_state for widget in plan.widgets] == ["Basic", "Pro", "Team"]
    assert list(doc.form) == ["name", "plan"]
    fields = list(doc.catalog["AcroForm"]["Fields"])
    assert fields[0] == plan.obj
    assert len(fields) == 2
    assert all("T" not in widget.obj and widget.obj["Parent"] == plan.obj for widget in plan.widgets)

    reopened = pyropdf.open(doc.to_bytes())
    assert reopened.form["plan"].value == "Pro"
    assert [widget.on_state for widget in reopened.form["plan"].widgets] == ["Basic", "Pro", "Team"]
    assert reopened.form.repair_radio_groups() == 0

    doc.history.undo()
    assert len(doc.form["plan"].widgets) == 1
    assert len(doc.catalog["AcroForm"]["Fields"]) == 4


def test_repair_leaves_other_forms_alone() -> None:
    doc = pyropdf.Document()
    assert doc.form.repair_radio_groups() == 0
    on = "/AP << /N << /{state} 9 0 R /Off 9 0 R >> >>"
    widget = "/Type /Annot /Subtype /Widget /P 3 0 R"
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R 7 0 R 8 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [5 0 R 6 0 R 7 0 R 8 0 R] >>",
                4: "<< /FT /Btn /Ff 49152 /T (plan) /Kids [5 0 R 6 0 R] >>",
                5: f"<< /Parent 4 0 R {widget} /Rect [10 10 25 25] /AS /Off {on.format(state='A')} >>",
                6: f"<< /Parent 4 0 R {widget} /Rect [30 10 45 25] /AS /Off {on.format(state='B')} >>",
                # A check box, and a radio button alone with its name.
                7: f"<< /FT /Btn /T (agree) {widget} /Rect [50 10 65 25] /AS /Off {on.format(state='Yes')} >>",
                8: f"<< /FT /Btn /Ff 49152 /T (solo) {widget} /Rect [70 10 85 25] /AS /Off {on.format(state='S')} >>",
                9: "<< /Length 0 /Subtype /Form /BBox [0 0 15 15] >>\nstream\n\nendstream",
            }
        )
    )
    before = doc.to_bytes()

    assert doc.form.repair_radio_groups() == 0
    assert doc.to_bytes() == before
    assert list(doc.form) == ["agree", "plan", "solo"]


def test_waiting_radio_repair_uses_the_first_writers_new_groups() -> None:
    doc = _flat()
    doc.objects[6]["T"] = "six"
    doc.objects[7]["T"] = "seven"
    started = Event()

    def repair() -> int:
        started.set()
        return doc.form.repair_radio_groups()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with doc.transaction("Create a group before the waiting repair"):
            future = pool.submit(repair)
            assert started.wait(timeout=10)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            doc.objects[6]["T"] = "plan"
            doc.objects[7]["T"] = "plan"
        assert future.result(timeout=10) == 1
    assert len(doc.form["plan"].widgets) == 3


def _two_radios(*, form: str = "8 0 R", fields: str = "[4 0 R 5 0 R]", title: str = "(plan)", kind: str = "") -> bytes:
    return build_raw_pdf(
        {
            1: f"<< /Type /Catalog /Pages 2 0 R /AcroForm {form} >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
            3: "<< /Type /Page /Parent 2 0 R /Annots [4 0 R 5 0 R] >>",
            4: f"<< {_BUTTON} /Rect [1 1 5 5] >>",
            5: f"<< /FT /Btn /Ff 49152 /T {title} /Type /Annot /Subtype /Widget /P 3 0 R /Rect [6 1 10 5] >>",
            6: "7 0 R",
            7: "[4 0 R 5 0 R]",
            8: f"<< {kind} /Fields {fields} >>",
            9: "8 0 R",
        }
    )


@pytest.mark.parametrize("entry", ["form", "fields"])
def test_radio_repair_preserves_arrays_and_form_dictionaries_behind_aliases(entry: str) -> None:
    data = _two_radios(form="9 0 R") if entry == "form" else _two_radios(fields="6 0 R")
    doc = pyropdf.open(data)
    assert doc.form.repair_radio_groups() == 1
    assert len(doc.catalog["AcroForm"]["Fields"]) == 1
    assert len(doc.form["plan"].widgets) == 2
    if entry == "fields":
        assert len(doc.objects[7]) == 1


def test_radio_repair_groups_names_with_different_pdf_encodings() -> None:
    doc = pyropdf.open(_two_radios(title="<FEFF0070006C0061006E>"))
    assert doc.form.repair_radio_groups() == 1
    assert len(doc.form["plan"].widgets) == 2


@pytest.mark.parametrize("kind", ["Page", "Pages", "Catalog"])
def test_radio_repair_refuses_a_form_alias_of_another_identity(kind: str) -> None:
    doc = pyropdf.open(_two_radios(kind=f"/Type /{kind}"))
    before = doc.to_bytes()
    with doc.transaction("Catch a malformed form"), pytest.raises(pyropdf.MalformedPdfError):
        doc.form.repair_radio_groups()
    assert doc.to_bytes() == before


def test_radio_repair_moves_field_actions_and_keeps_widget_actions() -> None:
    doc = pyropdf.open(_two_radios())
    actions = doc.objects.add(
        {"K": {"S": pyropdf.Name("JavaScript"), "JS": "field"}, "E": {"S": pyropdf.Name("JavaScript"), "JS": "widget"}}
    )
    doc.objects[4]["AA"] = doc.objects[actions]
    assert doc.form.repair_radio_groups() == 1
    field = doc.form["plan"]
    assert set(field.obj["AA"]) == {"K"}
    assert set(field.widgets[0].obj["AA"]) == {"E"}


def _aliased_radio_fields(*, distinct: bool) -> bytes:
    second_name = "(other)" if distinct else "(plan)"
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R 5 0 R] >> >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
            3: "<< /Type /Page /Parent 2 0 R /Annots [4 0 R 5 0 R] >>",
            4: "10 0 R",
            10: f"<< {_BUTTON} /Rect [1 1 5 5] >>",
            5: f"<< /FT /Btn /Ff 49152 /T {second_name} /Type /Annot /Subtype /Widget /P 3 0 R /Rect [6 1 10 5] >>",
        }
    )


def test_form_and_widget_reads_follow_aliases_to_the_same_field() -> None:
    doc = pyropdf.open(_aliased_radio_fields(distinct=True))
    assert list(doc.form) == ["other", "plan"]
    assert doc.form["plan"].xref == 10
    (widget,) = doc.form["plan"].widgets
    assert widget.field is not None
    assert widget.field.xref == 10
    assert widget.xref == 10
    assert doc.pages[0].widgets[0] == widget
    doc.pages[0].obj["Annots"][0] = doc.objects[10]
    assert widget.xref == 10
    field_after = widget.field
    assert field_after is not None
    assert field_after.xref == 10


def test_radio_repair_joins_members_reached_through_aliases() -> None:
    doc = pyropdf.open(_aliased_radio_fields(distinct=False))
    assert doc.form.repair_radio_groups() == 1
    field = doc.form["plan"]
    assert len(field.widgets) == 2
    assert all(widget.field == field for widget in field.widgets)


def test_radio_repair_deduplicates_aliases_of_the_same_widget() -> None:
    doc = pyropdf.open(_aliased_radio_fields(distinct=False))
    doc.catalog["AcroForm"]["Fields"].insert(1, doc.objects[10])
    assert doc.form.repair_radio_groups() == 1
    assert len(doc.catalog["AcroForm"]["Fields"]) == 1
    assert len(doc.form["plan"].widgets) == 2


def test_radio_repair_rolls_back_when_a_later_widget_has_a_conflicting_identity() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R 5 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [4 0 R 5 0 R] >>",
                4: f"<< {_BUTTON} /Rect [1 1 5 5] >>",
                5: "<< /FT /Btn /Ff 49152 /T (plan) /Type /Page /Subtype /Widget /P 3 0 R /Rect [6 1 10 5] >>",
            }
        )
    )
    before = doc.to_bytes()
    with doc.transaction("Catch a failed radio repair"), pytest.raises(pyropdf.MalformedPdfError):
        doc.form.repair_radio_groups()
    assert doc.to_bytes() == before
