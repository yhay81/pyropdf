"""Reading the interactive form: ``doc.form`` and its ``Field`` handles.

pylopdf's tests/test_acroform.py at ebb01d3c, the reading cases ported.
Intended differences:

- setting a check box or radio button raises ``NotImplementedError`` until
  #203, so the button fill cases and the fill halves of mixed button cases
  wait for it; where pylopdf filled a button to show which widgets belong
  to it, these tests read ``Field.widgets`` instead. The cases of embedded
  form fonts wait for #194: a value outside WinAnsiEncoding is left for the
  viewer to draw. ``/NeedAppearances`` stays true while a button widget has
  no appearance of its state.
- pyropdf has no renumbering save and no field removal on page deletion,
  so a handle goes stale when its object leaves ``/Fields``, and undo
  restores it.
- a refused read leaves the document as it was by construction, so the
  ``to_bytes`` comparisons around refused reads are gone.
- a test checks that reading every field costs one walk of the tree.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import TYPE_CHECKING, TypeAlias

import pytest

import pyropdf
from pyropdf import _core

if TYPE_CHECKING:
    from pathlib import Path


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number; absent numbers below the highest are free entries."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 65535 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def form_entries(doc: pyropdf.Document) -> list[dict[str, str | None]]:
    """Return the name, type, and value of every field, in name order."""
    return [{"name": name, "type": str(field.type), "value": field.value} for name, field in doc.form.items()]


def _widget_xrefs(field: pyropdf.Field) -> list[int]:
    return [widget.xref for widget in field.widgets]


def _form_stream(content: str) -> str:
    """Build an uncompressed Form XObject stream."""
    return (
        f"<< /Type /XObject /Subtype /Form /BBox [0 0 20 20] /Length {len(content.encode('ascii'))} >>"
        f"\nstream\n{content}\nendstream"
    )


def _build_form_pdf(*, text_widget_extra: str = "") -> bytes:
    """Build text, choice, checkbox, radio, and nested AcroForm fields."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 8 0 R >>",
            2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 612 792] >>",
            3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            4: "<< /Type /Page /Parent 2 0 R /Annots [9 0 R 10 0 R 14 0 R 15 0 R 17 0 R 18 0 R 23 0 R 25 0 R] >>",
            8: (
                "<< /Fields [9 0 R 10 0 R 13 0 R 15 0 R 16 0 R 23 0 R 24 0 R]"
                " /DA (/Helv 0 Tf 0 g) /DR << /Font << /Helv 3 0 R >> >> >>"
            ),
            # Text field with an initial value.
            9: "<< /FT /Tx /T (customer) /V (initial) /Type /Annot /Subtype /Widget"
            f" /Rect [50 700 250 720] /P 4 0 R /F 4 {text_widget_extra} >>",
            # Checkbox with Yes and Off appearances.
            10: "<< /FT /Btn /T (agree) /V /Off /AS /Off /Type /Annot /Subtype /Widget"
            " /Rect [50 660 70 680] /P 4 0 R /F 4 /AP << /N << /Yes 11 0 R /Off 12 0 R >> >> >>",
            11: _form_stream(""),
            12: _form_stream(""),
            # Nested person.first field inheriting FT from its parent.
            13: "<< /T (person) /FT /Tx /Kids [14 0 R] >>",
            14: "<< /T (first) /Parent 13 0 R /Type /Annot /Subtype /Widget /Rect [50 620 250 640] /P 4 0 R /F 4 >>",
            # Right-aligned combo box.
            15: (
                "<< /FT /Ch /Ff 131072 /Q 2 /T (level) /V (basic) /Opt [(basic) (pro)]"
                " /Type /Annot /Subtype /Widget /Rect [300 700 500 720] /P 4 0 R /F 4 >>"
            ),
            # Radio group with one export state per child widget.
            16: "<< /FT /Btn /Ff 32768 /T (plan) /V /Off /Kids [17 0 R 18 0 R] >>",
            17: (
                "<< /Parent 16 0 R /AS /Off /Type /Annot /Subtype /Widget"
                " /Rect [300 660 320 680] /P 4 0 R /F 4"
                " /AP << /N << /Pro 19 0 R /Off 20 0 R >> >> >>"
            ),
            18: (
                "<< /Parent 16 0 R /AS /Off /Type /Annot /Subtype /Widget"
                " /Rect [350 660 370 680] /P 4 0 R /F 4"
                " /AP << /N << /Basic 21 0 R /Off 22 0 R >> >> >>"
            ),
            19: _form_stream(""),
            20: _form_stream(""),
            21: _form_stream(""),
            22: _form_stream(""),
            23: "<< /FT /Tx /Ff 4096 /T (notes) /Type /Annot /Subtype /Widget /Rect [50 540 250 600] /P 4 0 R /F 4 >>",
            # Six-position centered comb field with inherited field attributes.
            24: "<< /FT /Tx /Ff 16777216 /MaxLen 6 /Q 1 /T (code) /Kids [25 0 R] >>",
            25: (
                "<< /Parent 24 0 R /Type /Annot /Subtype /Widget /Rect [300 540 500 565] /P 4 0 R /F 4"
                " /BS << /W 0 >> >>"
            ),
        }
    )


def _build_button_state_pdf(states: str, *, indirect_kids: bool = False) -> bytes:
    """Build one checkbox with a caller-provided normal-state dictionary."""
    kids = "8 0 R" if indirect_kids else "[6 0 R]"
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [6 0 R] >>",
        4: "<< /Fields [5 0 R] >>",
        5: f"<< /FT /Btn /T (button) /V /Off /Kids {kids} >>",
        6: (
            "<< /Parent 5 0 R /Type /Annot /Subtype /Widget /Rect [10 10 30 30]"
            f" /AS /Off /AP << /N << {states} >> >> >>"
        ),
        7: _form_stream(""),
    }
    if indirect_kids:
        objects[8] = "[6 0 R]"
    return build_raw_pdf(objects)


def _single(field: str) -> bytes:
    """Build a document whose form holds one top-level field with the `field` entries."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: "<< /Fields [5 0 R] >>",
            5: f"<< {field} >>",
        }
    )


def _reloaded(source: pyropdf.Document) -> tuple[pyropdf.Document, pyropdf.Document]:
    return source, pyropdf.open(source.to_bytes())


# Object 6 is an indirect null. Number 7 is a free cross-reference entry below
# the highest object, so its reference stays missing through loading, saving,
# and reloading. ISO 32000-2 §7.3.7 reads a null value as an omitted key, and
# §7.3.10 reads a reference to a missing object as null.
NULL_EQUIVALENTS = {"null": "null", "indirect-null": "6 0 R", "missing": "7 0 R"}
null_equivalents = pytest.mark.parametrize("value", list(NULL_EQUIVALENTS.values()), ids=list(NULL_EQUIVALENTS))


def test_form_lists_all_fields() -> None:
    doc = pyropdf.open(_build_form_pdf())
    fields = {f["name"]: f for f in form_entries(doc)}
    assert set(fields) == {"customer", "agree", "person.first", "level", "plan", "notes", "code"}
    assert fields["customer"]["type"] == "text"
    assert fields["customer"]["value"] == "initial"
    assert fields["agree"]["type"] == "checkbox"
    assert fields["agree"]["value"] == "Off"
    assert fields["person.first"]["type"] == "text"  # FT is inherited from the parent.
    assert fields["person.first"]["value"] is None
    assert fields["level"] == {"name": "level", "type": "combobox", "value": "basic"}
    assert fields["plan"]["type"] == "radio"
    assert fields["notes"]["type"] == "text"
    assert fields["code"]["type"] == "text"


def test_form_is_a_mapping_of_field_handles() -> None:
    doc = pyropdf.open(_build_form_pdf())
    form = doc.form

    assert list(form) == ["agree", "code", "customer", "level", "notes", "person.first", "plan"]
    assert len(form) == 7
    assert "customer" in form
    assert "person" not in form
    field = form["person.first"]
    assert (field.name, field.xref, field.type) == ("person.first", 14, pyropdf.FieldType.TEXT)
    assert field.obj["T"] == b"first"
    assert field == form["person.first"]
    assert hash(field) == hash(form["person.first"])
    assert field != form["customer"]
    assert repr(field) == "<Field 'person.first' 14>"
    with pytest.raises(KeyError):
        form[1]  # ty: ignore[invalid-argument-type]


def test_field_flags_are_inherited_and_drop_undefined_bits() -> None:
    doc = pyropdf.open(_build_form_pdf())
    form = doc.form

    assert form["customer"].flags == pyropdf.FieldFlags(0)
    assert form["level"].flags == pyropdf.FieldFlags.COMBO
    assert form["plan"].flags == pyropdf.FieldFlags.RADIO
    assert form["notes"].flags == pyropdf.FieldFlags.MULTILINE
    assert form["code"].flags == pyropdf.FieldFlags.COMB  # inherited from the parent field
    assert form["plan"].type is pyropdf.FieldType.RADIO

    field = pyropdf.open(_single("/FT /Btn /Ff -1 /T (push)")).form["push"]
    assert field.type is pyropdf.FieldType.PUSHBUTTON
    assert field.flags == sum(flag.value for flag in pyropdf.FieldFlags)


def test_field_choices_read_options_and_pairs() -> None:
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: "<< /Fields [5 0 R 7 0 R] >>",
            5: "<< /FT /Ch /T (size) /Opt 6 0 R /V (m) >>",
            6: "[(s) [(m) (Medium)] /skipped [(l)] (\\376\\377\\000X\\000L)]",
            7: "<< /FT /Tx /T (name) >>",
        }
    )
    form = pyropdf.open(pdf).form

    assert form["size"].type is pyropdf.FieldType.LISTBOX
    assert form["size"].choices == (
        pyropdf.FieldChoice(value="s", label="s"),
        pyropdf.FieldChoice(value="m", label="Medium"),
        pyropdf.FieldChoice(value="XL", label="XL"),
    )
    assert form["name"].choices == ()


def test_field_handles_go_stale() -> None:
    doc = pyropdf.open(_build_form_pdf())
    field = doc.form["customer"]
    del doc.catalog["AcroForm"]["Fields"][0]

    assert "customer" not in doc.form
    with pytest.raises(pyropdf.StaleObjectError, match="no longer a field"):
        _ = field.value
    assert repr(field) == "<Field 9>"

    doc.history.undo()
    assert field.value == "initial"
    assert repr(field) == "<Field 'customer' 9>"


def test_form_errors() -> None:
    doc = pyropdf.open(_build_form_pdf())
    with pytest.raises(KeyError, match="nosuch"):
        doc.form["nosuch"]
    with pytest.raises(KeyError):
        doc.form[""]
    with pytest.raises(TypeError, match="str or bool"):
        doc.form["customer"].value = 1  # ty: ignore[invalid-assignment]


def _opaque_pixels(doc: pyropdf.Document, clip: tuple[float, float, float, float]) -> int:
    pix = doc.pages[0].pixmap(clip=clip, alpha=True)
    return sum(alpha != 0 for alpha in bytes(pix)[3::4])


def _values(doc: pyropdf.Document) -> dict[str, str | None]:
    return {name: field.value for name, field in doc.form.items()}


def test_fill_text_field_roundtrip_without_unicode_font() -> None:
    doc = pyropdf.open(_build_form_pdf())
    assert _opaque_pixels(doc, (45, 67, 255, 97)) == 0
    doc.form["customer"].value = "山田 太郎"
    doc.form["person.first"].value = "Taro"
    data = doc.to_bytes()
    assert b"/NeedAppearances true" in data
    reopened = pyropdf.open(data)
    fields = _values(reopened)
    assert fields["customer"] == "山田 太郎"
    assert fields["person.first"] == "Taro"
    # Without an embedded font the CJK value is left for the viewer, but
    # WinAnsi text has an appearance that survives a save and reopen.
    assert _opaque_pixels(reopened, (45, 67, 255, 97)) == 0
    assert _opaque_pixels(reopened, (45, 147, 255, 177)) > 0


def test_fill_choice_field_generates_appearance() -> None:
    doc = pyropdf.open(_build_form_pdf())
    doc.form["level"].value = "pro"
    assert _values(doc)["level"] == "pro"
    assert _opaque_pixels(doc, (295, 67, 505, 97)) > 0


def test_fill_multiline_text_field_wraps_and_renders() -> None:
    doc = pyropdf.open(_build_form_pdf())
    value = "A deliberately long first line that wraps inside the field.\nA second line."
    doc.form["notes"].value = value
    assert _values(doc)["notes"] == value
    assert _opaque_pixels(doc, (45, 187, 255, 257)) > 0


def test_fill_multiline_text_field_bounds_layout_lines_atomically() -> None:
    doc = pyropdf.open(_build_form_pdf())
    before = doc.to_bytes()

    with pytest.raises(pyropdf.LimitError) as caught:
        doc.form["notes"].value = "\n" * 4096

    assert caught.value.code == "text_line_count"
    assert doc.to_bytes() == before


def test_fill_comb_field_centers_characters_and_enforces_maxlen() -> None:
    doc = pyropdf.open(_build_form_pdf())
    doc.form["code"].value = "A12"
    assert _values(doc)["code"] == "A12"

    # Center alignment places three characters in slots 2 to 4 of six cells.
    cell_width = 200 / 6
    counts = [
        _opaque_pixels(doc, (300 + index * cell_width, 227, 300 + (index + 1) * cell_width, 252)) for index in range(6)
    ]
    assert counts[0] == 0
    assert all(count > 0 for count in counts[1:4])
    assert counts[4:] == [0, 0]

    with pytest.raises(pyropdf.PdfError, match="exceeding MaxLen 6"):
        doc.form["code"].value = "1234567"
    assert _values(doc)["code"] == "A12"


@pytest.mark.parametrize(
    ("original", "replacement", "message"),
    [
        (b"/MaxLen 6", b"/MaxLen 0", "positive MaxLen"),
        (b"/Ff 16777216", b"/Ff 16781312", "cannot also be multiline"),
    ],
)
def test_malformed_comb_field_is_rejected_atomically(original: bytes, replacement: bytes, message: str) -> None:
    doc = pyropdf.open(_build_form_pdf().replace(original, replacement))
    before = doc.to_bytes()
    with pytest.raises(pyropdf.PdfError, match=message):
        doc.form["code"].value = "12"
    assert _values(doc)["code"] is None
    assert doc.to_bytes() == before


def test_buttons_are_not_set_yet() -> None:
    doc = pyropdf.open(_build_form_pdf())
    before = doc.to_bytes()
    for name, value in (("agree", True), ("plan", "Pro")):
        with pytest.raises(NotImplementedError):
            doc.form[name].value = value
    assert doc.to_bytes() == before


def test_widget_rotation_background_and_alignment_render() -> None:
    extra = "/Q 2 /MK << /R 90 /BG [1 1 0] /BC [1 0 0] >> /BS << /W 2 >>"
    doc = pyropdf.open(_build_form_pdf(text_widget_extra=extra))
    doc.form["customer"].value = "right"
    assert re.search(rb"/Matrix\s*\[0 1 -1 0 200 0\]", doc.to_bytes())
    assert _opaque_pixels(doc, (45, 67, 255, 97)) > 0


def test_field_value_rejects_excessive_text_atomically() -> None:
    doc = pyropdf.open(_build_form_pdf())
    field = doc.form["customer"]
    before = doc.to_bytes()
    with pytest.raises(pyropdf.LimitError) as caught:
        field.value = "x" * (1024 * 1024 + 1)
    assert caught.value.code == "form_field_input_size"
    assert doc.to_bytes() == before


def test_fill_rejects_oversized_default_appearance_atomically() -> None:
    appearance = "/Helv 12 Tf " * 400
    doc = pyropdf.open(_build_form_pdf(text_widget_extra=f"/DA ({appearance})"))
    field = doc.form["customer"]
    before = doc.to_bytes()
    with pytest.raises(pyropdf.LimitError) as caught:
        field.value = "changed"
    assert caught.value.code == "form_default_appearance_size"
    assert field.value == "initial"
    assert doc.to_bytes() == before


def test_filling_is_undone_as_one_edit() -> None:
    doc = pyropdf.open(_build_form_pdf())
    before = doc.to_bytes()
    doc.form["customer"].value = "changed"
    doc.history.undo()
    assert doc.to_bytes() == before


INHERITABLE_FIELD_KEYS = ("FT", "Ff", "MaxLen", "Q", "V")


def _build_inherited_comb_pdf(child: str, extra: dict[int, str] | None = None) -> bytes:
    """Build a comb field whose child field `code.inner` adds `child` entries.

    The child inherits the text type, comb flag, MaxLen 6, right alignment,
    and value of its parent `code`. Filling the sibling `other` completes the
    child's missing appearance from the value that it inherits.
    """
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 250 100] /Annots [8 0 R 9 0 R] >>",
            4: "<< /Fields [5 0 R 9 0 R] >>",
            5: "<< /FT /Tx /Ff 16777216 /MaxLen 6 /Q 2 /T (code) /V (P42) /Kids [8 0 R] >>",
            6: "null",
            8: (
                f"<< /T (inner) /Parent 5 0 R {child} /Type /Annot /Subtype /Widget"
                " /Rect [3 30 247 60] /P 3 0 R /F 4 /BS << /W 0 >> >>"
            ),
            9: "<< /FT /Tx /T (other) /Type /Annot /Subtype /Widget /Rect [3 70 247 95] /P 3 0 R /F 4 >>",
            **(extra or {}),
        }
    )


def _inner_comb_cells(doc: pyropdf.Document, count: int = 6) -> list[bool]:
    """Report which comb cells of `code.inner`, inside its 240-point content box, hold ink."""
    width = 240 / count
    return [_opaque_pixels(doc, (5 + index * width, 40, 5 + (index + 1) * width, 70)) > 0 for index in range(count)]


@null_equivalents
@pytest.mark.parametrize("target", ["code.inner", "other"])
def test_comb_appearance_inherits_past_null_child_entries(value: str, target: str) -> None:
    """Comb layout and alignment use the parent's `/MaxLen` and `/Q` past null child entries."""
    entries = " ".join(f"/{key} {value}" for key in INHERITABLE_FIELD_KEYS)
    source = pyropdf.open(_build_inherited_comb_pdf(entries))

    for doc in _reloaded(source):
        doc.form[target].value = "A12"
        with pytest.raises(pyropdf.PdfError, match="exceeding MaxLen 6"):
            doc.form["code.inner"].value = "1234567"
        for current in _reloaded(doc):
            # Right alignment places three characters in the last three of six cells.
            assert _inner_comb_cells(current) == [False, False, False, True, True, True]


@null_equivalents
@pytest.mark.parametrize("target", ["code.inner", "other"])
def test_text_appearance_inherits_past_null_child_entries(value: str, target: str) -> None:
    """A single-line child draws right-aligned text past null-equivalent `/Q` and `/V`."""
    source = pyropdf.open(_build_inherited_comb_pdf(f"/Ff 0 /Q {value} /V {value}"))

    for doc in _reloaded(source):
        doc.form[target].value = "A12"
        for current in _reloaded(doc):
            assert _opaque_pixels(current, (5, 40, 125, 70)) == 0
            assert _opaque_pixels(current, (125, 40, 245, 70)) > 0


@pytest.mark.parametrize(
    ("entries", "extra"),
    [
        ("/MaxLen 4 /Q 0 /V (own)", {}),
        ("/MaxLen 10 0 R /Q 11 0 R /V 12 0 R", {10: "4", 11: "0", 12: "(own)"}),
    ],
    ids=["direct", "indirect"],
)
def test_present_child_field_entries_override_parent_values(entries: str, extra: dict[int, str]) -> None:
    """Only null-equivalent entries inherit; a present child value still wins."""
    doc = pyropdf.open(_build_inherited_comb_pdf(entries, extra))
    assert form_entries(doc)[0] == {"name": "code.inner", "type": "text", "value": "own"}

    doc.form["other"].value = "x"

    for current in _reloaded(doc):
        # Left alignment places three characters in the first three of four cells.
        assert _inner_comb_cells(current, 4) == [True, True, True, False]


def _build_default_quadding_pdf(
    acroform: str,
    *,
    parent: str = "",
    child: str = "",
    extra: dict[int, str] | None = None,
    direct: bool = False,
) -> bytes:
    """Build comb, text, and choice fields without `/Q` beyond the given entries.

    The interactive form dictionary adds `acroform` entries and is a direct
    catalog value when `direct` is true. The comb field `code` adds `parent`
    entries, and its child `code.inner` adds `child` entries and inherits the
    comb flag, MaxLen 6, and value P42. The text field `other` and the combo
    box `level` hold xyz. No widget has an appearance or a border, so filling
    one field draws it and completes the appearances of the other two.
    """
    form = f"<< /Fields [5 0 R 9 0 R 10 0 R] {acroform} >>"
    objects = {
        1: f"<< /Type /Catalog /Pages 2 0 R /AcroForm {form if direct else '4 0 R'} >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 250 100] /Annots [8 0 R 9 0 R 10 0 R] >>",
        5: f"<< /FT /Tx /Ff 16777216 /MaxLen 6 /T (code) /V (P42) {parent} /Kids [8 0 R] >>",
        6: "null",
        8: (
            f"<< /T (inner) /Parent 5 0 R {child} /Type /Annot /Subtype /Widget"
            " /Rect [3 30 247 60] /P 3 0 R /F 4 /BS << /W 0 >> >>"
        ),
        9: (
            "<< /FT /Tx /T (other) /V (xyz) /Type /Annot /Subtype /Widget"
            " /Rect [3 70 247 95] /P 3 0 R /F 4 /BS << /W 0 >> >>"
        ),
        10: (
            "<< /FT /Ch /Ff 131072 /T (level) /V (xyz) /Opt [(xyz) (A12)] /Type /Annot /Subtype /Widget"
            " /Rect [3 3 247 27] /P 3 0 R /F 4 /BS << /W 0 >> >>"
        ),
        **(extra or {}),
    }
    if not direct:
        objects[4] = form
    return build_raw_pdf(objects)


def _quadding_ink(doc: pyropdf.Document) -> dict[str, tuple[bool, ...]]:
    """Report the inked comb cells of `code.inner` and the inked halves of `other` and `level`."""
    return {
        "code.inner": tuple(_inner_comb_cells(doc)),
        "other": tuple(_opaque_pixels(doc, (left, 5, left + 120, 30)) > 0 for left in (5, 125)),
        "level": tuple(_opaque_pixels(doc, (left, 73, left + 120, 97)) > 0 for left in (5, 125)),
    }


LEFT_ALIGNED = {"code.inner": (True, True, True, False, False, False), "other": (True, False), "level": (True, False)}
RIGHT_ALIGNED = {"code.inner": (False, False, False, True, True, True), "other": (False, True), "level": (False, True)}


@pytest.mark.parametrize("direct", [False, True], ids=["indirect", "direct"])
@pytest.mark.parametrize("target", ["code.inner", "other", "level"])
def test_appearances_default_to_acroform_quadding(target: str, *, direct: bool) -> None:
    """Fields without `/Q` on their `/Parent` chain use the AcroForm's `/Q` (ISO 32000-2 Table 225)."""
    source = pyropdf.open(_build_default_quadding_pdf("/Q 2", direct=direct))

    for doc in _reloaded(source):
        doc.form[target].value = "A12"
        for current in _reloaded(doc):
            assert _quadding_ink(current) == RIGHT_ALIGNED


@null_equivalents
@pytest.mark.parametrize("target", ["code.inner", "other"])
def test_null_field_quadding_reaches_acroform_default(value: str, target: str) -> None:
    """Null-equivalent `/Q` entries on the `/Parent` chain fall through to the AcroForm's `/Q`."""
    source = pyropdf.open(_build_default_quadding_pdf("/Q 2", parent=f"/Q {value}", child=f"/Q {value}"))

    for doc in _reloaded(source):
        doc.form[target].value = "A12"
        for current in _reloaded(doc):
            assert _quadding_ink(current) == RIGHT_ALIGNED


@pytest.mark.parametrize(
    "value",
    [*NULL_EQUIVALENTS.values(), "3", "/Right"],
    ids=[*NULL_EQUIVALENTS, "out-of-range", "non-integer"],
)
@pytest.mark.parametrize("target", ["code.inner", "other"])
def test_null_or_unusable_acroform_quadding_aligns_left(value: str, target: str) -> None:
    """A null-equivalent AcroForm `/Q` reads as absent, and an unusable one aligns left."""
    source = pyropdf.open(_build_default_quadding_pdf(f"/Q {value}"))

    for doc in _reloaded(source):
        doc.form[target].value = "A12"
        for current in _reloaded(doc):
            assert _quadding_ink(current) == LEFT_ALIGNED


@pytest.mark.parametrize(
    ("parent", "child", "extra"),
    [
        ("", "/Q 0", {}),
        ("", "/Q 11 0 R", {11: "0"}),
        ("/Q 0", "", {}),
        ("", "/Q 3", {}),
    ],
    ids=["field", "indirect", "parent", "out-of-range"],
)
@pytest.mark.parametrize("target", ["code.inner", "other"])
def test_field_quadding_overrides_acroform_default(parent: str, child: str, extra: dict[int, str], target: str) -> None:
    """A `/Q` on the `/Parent` chain, including `/Q 0`, wins over the AcroForm's `/Q`.

    An out-of-range field value still stops inheritance and aligns left.
    """
    source = pyropdf.open(_build_default_quadding_pdf("/Q 2", parent=parent, child=child, extra=extra))

    for doc in _reloaded(source):
        doc.form[target].value = "A12"
        for current in _reloaded(doc):
            assert _quadding_ink(current) == {**RIGHT_ALIGNED, "code.inner": LEFT_ALIGNED["code.inner"]}


def test_no_form_returns_empty() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    assert form_entries(doc) == []


def test_form_field_duplicate_reference_is_visited_once() -> None:
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: "<< /Fields [5 0 R 5 0 R] >>",
            5: "<< /FT /Tx /T (field) /V (value) >>",
        }
    )
    doc = pyropdf.open(pdf)
    assert form_entries(doc) == [{"name": "field", "type": "text", "value": "value"}]


def test_form_field_tree_rejects_excessive_edges() -> None:
    fields = "5 0 R " * 8193
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: f"<< /Fields [{fields}] >>",
            5: "<< /FT /Tx /T (field) /V (value) >>",
        }
    )
    doc = pyropdf.open(pdf)
    with pytest.raises(pyropdf.PdfError, match="8192-edge safety limit"):
        form_entries(doc)
    with pytest.raises(pyropdf.PdfError, match="8192-edge safety limit"):
        doc.form["field"]


def test_form_field_tree_rejects_excessive_nodes_without_partial_output() -> None:
    field_ids = range(5, 4101)
    fields = " ".join(f"{object_id} 0 R" for object_id in field_ids)
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        4: f"<< /Fields [{fields}] >>",
    }
    for object_id in range(5, 4100):
        objects[object_id] = f"<< /FT /Tx /T (field{object_id}) >>"
    objects[4100] = "<< /FT /Tx /T (parent) /Kids [4101 0 R] >>"
    objects[4101] = "<< /T (child) >>"

    doc = pyropdf.open(build_raw_pdf(objects))
    with pytest.raises(pyropdf.PdfError, match="4096-node safety limit"):
        form_entries(doc)


def test_form_field_tree_rejects_excessive_depth() -> None:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        4: "<< /Fields [5 0 R] >>",
    }
    for object_id in range(5, 69):
        objects[object_id] = f"<< /FT /Tx /T (n{object_id}) /Kids [{object_id + 1} 0 R] >>"
    objects[69] = "<< /FT /Tx /T (leaf) >>"

    doc = pyropdf.open(build_raw_pdf(objects))
    with pytest.raises(pyropdf.PdfError, match="64-level safety limit"):
        form_entries(doc)


def test_form_field_tree_rejects_excessive_name_text() -> None:
    doc = pyropdf.open(_single(f"/FT /Tx /T ({'x' * (1024 * 1024 + 1)})"))
    with pytest.raises(pyropdf.PdfError, match=r"field-name.*1048576-byte safety limit"):
        form_entries(doc)


def test_form_field_tree_rejects_excessive_value_items() -> None:
    doc = pyropdf.open(_single(f"/FT /Ch /T (choice) /V [{'(x) ' * 4097}]"))
    with pytest.raises(pyropdf.PdfError, match="4096-item safety limit"):
        form_entries(doc)


def test_form_field_tree_rejects_excessive_value_text() -> None:
    doc = pyropdf.open(_single(f"/FT /Tx /T (field) /V ({'x' * (1024 * 1024 + 1)})"))
    with pytest.raises(pyropdf.PdfError, match=r"field-value.*1048576-byte safety limit"):
        form_entries(doc)


def test_form_field_tree_bounds_inherited_returned_values() -> None:
    value = "x" * 1024
    child_ids = range(6, 1031)
    kids = " ".join(f"{object_id} 0 R" for object_id in child_ids)
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        4: "<< /Fields [5 0 R] >>",
        5: f"<< /FT /Tx /T (root) /V ({value}) /Kids [{kids}] >>",
    }
    for object_id in child_ids:
        objects[object_id] = f"<< /T (child{object_id}) >>"

    doc = pyropdf.open(build_raw_pdf(objects))
    with pytest.raises(pyropdf.PdfError, match=r"returned field-value.*1048576-byte safety limit"):
        form_entries(doc)


def test_form_field_tree_rejects_excessive_widgets() -> None:
    widgets = "6 0 R " * 4097
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: "<< /Fields [5 0 R] >>",
            5: f"<< /FT /Btn /T (button) /Kids [{widgets}] >>",
            6: "<< /Parent 5 0 R /Type /Annot /Subtype /Widget /Rect [10 10 30 30] >>",
        }
    )
    doc = pyropdf.open(pdf)
    with pytest.raises(pyropdf.PdfError, match="4096-widget safety limit"):
        form_entries(doc)
    with pytest.raises(pyropdf.PdfError, match="4096-widget safety limit"):
        doc.form["button"]


def test_form_button_states_do_not_bound_reading_the_value() -> None:
    states = " ".join(f"/S{index} 7 0 R" for index in range(8193))
    doc = pyropdf.open(_build_button_state_pdf(states))
    assert form_entries(doc) == [{"name": "button", "type": "checkbox", "value": "Off"}]


def test_form_button_supports_indirect_kids_array() -> None:
    doc = pyropdf.open(_build_button_state_pdf("/Off 7 0 R /Yes 7 0 R", indirect_kids=True))
    assert form_entries(doc) == [{"name": "button", "type": "checkbox", "value": "Off"}]
    assert _widget_xrefs(doc.form["button"]) == [6]
    assert doc.form["button"].widgets[0].button_states == ("Off", "Yes")


def _build_widget_kids_pdf(field: str, kids: list[str], extra: dict[int, str] | None = None) -> bytes:
    """Build top-level field 5 whose widget kids 8, 9, and so on add the `kids` entries.

    Kid `index` covers an 80-by-30 rectangle at x = 10 + 100 * index on a
    300-by-100 page. Object 6 is null and number 7 stays free, as
    `NULL_EQUIVALENTS` expects.
    """
    numbers = range(8, 8 + len(kids))
    refs = " ".join(f"{number} 0 R" for number in numbers)
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Annots [{refs}] >>",
        4: "<< /Fields [5 0 R] >>",
        5: f"<< {field} /Kids [{refs}] >>",
        6: "null",
        **(extra or {}),
    }
    for index, (number, entries) in enumerate(zip(numbers, kids, strict=True)):
        left = 10 + 100 * index
        objects[number] = (
            f"<< /Parent 5 0 R {entries} /Type /Annot /Subtype /Widget"
            f" /Rect [{left} 10 {left + 80} 40] /P 3 0 R /F 4 >>"
        )
    return build_raw_pdf(objects)


SOLID_BUTTON_STATES = {20: _form_stream("0 1 0 rg 0 0 20 20 re f"), 21: _form_stream("1 0 0 rg 0 0 20 20 re f")}


@null_equivalents
def test_null_named_kids_are_widgets_of_their_text_field(value: str) -> None:
    """Kids whose `/T` is null-equivalent are widgets of their parent field, as kids without `/T` are."""
    source = pyropdf.open(_build_widget_kids_pdf("/FT /Tx /T (name) /V (old)", [f"/T {value}"] * 3))

    for doc in _reloaded(source):
        assert form_entries(doc) == [{"name": "name", "type": "text", "value": "old"}]
        assert _widget_xrefs(doc.form["name"]) == [8, 9, 10]


@null_equivalents
def test_null_named_kids_are_widgets_of_their_checkbox(value: str) -> None:
    states = "/AS /Off /AP << /N << /Yes 20 0 R /Off 21 0 R >> >>"
    pdf = _build_widget_kids_pdf("/FT /Btn /T (agree) /V /Off", [f"/T {value} {states}"] * 3, SOLID_BUTTON_STATES)

    for doc in _reloaded(pyropdf.open(pdf)):
        assert form_entries(doc) == [{"name": "agree", "type": "checkbox", "value": "Off"}]
        assert [widget.on_state for widget in doc.form["agree"].widgets] == ["Yes"] * 3


@null_equivalents
def test_null_named_kids_are_buttons_of_their_radio_group(value: str) -> None:
    kids = [f"/T {value} /AS /Off /AP << /N << /{state} 20 0 R /Off 21 0 R >> >>" for state in ("A", "B", "C")]
    pdf = _build_widget_kids_pdf("/FT /Btn /Ff 32768 /T (plan) /V /Off", kids, SOLID_BUTTON_STATES)

    for doc in _reloaded(pyropdf.open(pdf)):
        assert form_entries(doc) == [{"name": "plan", "type": "radio", "value": "Off"}]
        assert [widget.on_state for widget in doc.form["plan"].widgets] == ["A", "B", "C"]


@pytest.mark.parametrize(
    ("name", "extra"),
    [("5", {}), ("/name", {}), ("20 0 R", {20: "20 0 R"})],
    ids=["number", "name-object", "reference-cycle"],
)
def test_unusable_kid_names_still_make_child_fields(name: str, extra: dict[int, str]) -> None:
    """Only null-equivalent `/T` values are omitted; any other value makes the kid a child field.

    Such a child field has its parent's full name and reads its own value.
    """
    pdf = _build_widget_kids_pdf("/FT /Tx /T (name) /V (parent)", [f"/T {name} /V (kid)"], extra)

    for doc in _reloaded(pyropdf.open(pdf)):
        assert form_entries(doc) == [{"name": "name", "type": "text", "value": "kid"}]
        assert doc.form["name"].xref == 8


def _build_widget_budget_pdf(kids: str, widget_entries: str = "") -> bytes:
    """Build checkbox 5 with the `kids` entries and widget 8; object 6 is null and number 7 stays free."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [8 0 R] >>",
            4: "<< /Fields [5 0 R] >>",
            5: f"<< /FT /Btn /T (button) /V /Off /Kids [{kids}] >>",
            6: "null",
            8: (
                f"<< /Parent 5 0 R {widget_entries} /Type /Annot /Subtype /Widget /Rect [10 10 30 30]"
                " /AS /Off /AP << /N << /Yes 9 0 R /Off 9 0 R >> >> >>"
            ),
            9: _form_stream("0 0 20 20 re f"),
        }
    )


@null_equivalents
def test_null_kid_entries_cost_only_their_edge(value: str) -> None:
    """Null-equivalent `/Kids` entries are skipped alike instead of counting as widgets.

    Every entry still counts toward the 8,192-edge limit, and the `/Fields`
    entry takes one edge.
    """
    widgets = "8 0 R " * 4096
    source = pyropdf.open(_build_widget_budget_pdf(widgets + f"{value} " * 4095))
    for doc in _reloaded(source):
        assert form_entries(doc) == [{"name": "button", "type": "checkbox", "value": "Off"}]
        assert _widget_xrefs(doc.form["button"]) == [8]

    over_limit = pyropdf.open(_build_widget_budget_pdf(widgets + f"{value} " * 4096))
    for doc in _reloaded(over_limit):
        with pytest.raises(pyropdf.PdfError, match="8192-edge safety limit"):
            form_entries(doc)


@null_equivalents
def test_null_named_kids_count_toward_the_widget_limit(value: str) -> None:
    """Kids whose `/T` is null-equivalent count toward the 4,096-widget limit like other widgets."""
    source = pyropdf.open(_build_widget_budget_pdf("8 0 R " * 4097, f"/T {value}"))

    for doc in _reloaded(source):
        with pytest.raises(pyropdf.PdfError, match="4096-widget safety limit"):
            form_entries(doc)


@pytest.mark.parametrize("kind", list(NULL_EQUIVALENTS))
def test_null_field_entries_cost_only_their_edge(kind: str) -> None:
    """Null-equivalent `/Fields` entries are skipped alike instead of counting as tree nodes."""
    numbers = range(10, 4106)
    entries = "null " * len(numbers) if kind == "null" else " ".join(f"{number} 0 R" for number in numbers)
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        4: f"<< /Fields [5 0 R {entries}] >>",
        5: "<< /FT /Tx /T (field) /V (value) >>",
        # An unreferenced object keeps the missing numbers below the highest one.
        5000: "<< >>",
    }
    if kind == "indirect-null":
        objects.update(dict.fromkeys(numbers, "null"))

    for doc in _reloaded(pyropdf.open(build_raw_pdf(objects))):
        assert form_entries(doc) == [{"name": "field", "type": "text", "value": "value"}]


# An intermediate field is nameless when its `/T` is absent or reads as null.
NAMELESS = {"absent": "", **{kind: f"/T {value}" for kind, value in NULL_EQUIVALENTS.items()}}
nameless_kinds = pytest.mark.parametrize("nameless", list(NAMELESS.values()), ids=list(NAMELESS))

# A string is a widget kid with those entries, and a list is a nameless
# intermediate field whose kids follow the list.
FoldedKids: TypeAlias = "list[str | FoldedKids]"


def _build_folded_pdf(field: str, kids: FoldedKids, nameless: str = "", extra: dict[int, str] | None = None) -> bytes:
    """Build top-level field 5 whose `/Kids` follow `kids`, adding `nameless` to every intermediate field.

    Widget `index`, counted in document order, covers an 80-by-30 rectangle at
    x = 10 + 100 * index. Object 6 is null and number 7 stays free, as
    `NULL_EQUIVALENTS` expects; the kids are numbered from 8 in document order.
    """
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        4: "<< /Fields [5 0 R] >>",
        6: "null",
        **(extra or {}),
    }
    next_number = 8
    widgets: list[str] = []

    def add(parent: int, kid: str | FoldedKids) -> str:
        nonlocal next_number
        number = next_number
        next_number += 1
        assert number not in objects
        if isinstance(kid, str):
            left = 10 + 100 * len(widgets)
            widgets.append(f"{number} 0 R")
            objects[number] = (
                f"<< /Parent {parent} 0 R {kid} /Type /Annot /Subtype /Widget"
                f" /Rect [{left} 10 {left + 80} 40] /P 3 0 R /F 4 >>"
            )
        else:
            grandkids = " ".join(add(number, grandkid) for grandkid in kid)
            objects[number] = f"<< /Parent {parent} 0 R {nameless} /Kids [{grandkids}] >>"
        return f"{number} 0 R"

    refs = " ".join(add(5, kid) for kid in kids)
    objects[3] = (
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {20 + 100 * len(widgets)} 100] /Annots [{' '.join(widgets)}] >>"
    )
    objects[5] = f"<< {field} /Kids [{refs}] >>"
    return build_raw_pdf(objects)


def _page_widgets(doc: pyropdf.Document) -> list[int]:
    return [widget.xref for widget in doc.pages[0].widgets]


@nameless_kinds
def test_nameless_intermediate_widgets_belong_to_the_field_above(nameless: str) -> None:
    """Widgets below a nameless intermediate field are widgets of the field above it."""
    source = pyropdf.open(_build_folded_pdf("/FT /Tx /T (name) /V (old)", [["", "", ""]], nameless))

    for doc in _reloaded(source):
        assert form_entries(doc) == [{"name": "name", "type": "text", "value": "old"}]
        field = doc.form["name"]
        assert _widget_xrefs(field) == _page_widgets(doc) == [9, 10, 11]
        assert all(widget.field == field for widget in doc.pages[0].widgets)


@nameless_kinds
def test_widget_kids_and_nested_intermediates_fold_into_one_field(nameless: str) -> None:
    """Direct widget kids, sibling intermediates, and a nested intermediate all show one field's value."""
    kids: FoldedKids = ["", ["", ""], [[""], ""]]
    source = pyropdf.open(_build_folded_pdf("/FT /Tx /T (name)", kids, nameless))

    for doc in _reloaded(source):
        assert form_entries(doc) == [{"name": "name", "type": "text", "value": None}]
        assert len(_page_widgets(doc)) == 5
        assert _widget_xrefs(doc.form["name"]) == _page_widgets(doc)


@nameless_kinds
def test_nameless_intermediate_widgets_are_buttons_of_their_checkbox(nameless: str) -> None:
    states = "/AS /Off /AP << /N << /Yes 20 0 R /Off 21 0 R >> >>"
    pdf = _build_folded_pdf("/FT /Btn /T (agree) /V /Off", [states, [states, states]], nameless, SOLID_BUTTON_STATES)

    for doc in _reloaded(pyropdf.open(pdf)):
        assert form_entries(doc) == [{"name": "agree", "type": "checkbox", "value": "Off"}]
        assert [widget.on_state for widget in doc.form["agree"].widgets] == ["Yes"] * 3


@nameless_kinds
def test_nameless_intermediate_widgets_are_buttons_of_their_radio_group(nameless: str) -> None:
    first, second, third = (f"/AS /Off /AP << /N << /{state} 20 0 R /Off 21 0 R >> >>" for state in "ABC")
    pdf = _build_folded_pdf(
        "/FT /Btn /Ff 32768 /T (plan) /V /Off", [first, [second, third]], nameless, SOLID_BUTTON_STATES
    )

    for doc in _reloaded(pyropdf.open(pdf)):
        assert form_entries(doc) == [{"name": "plan", "type": "radio", "value": "Off"}]
        assert [widget.on_state for widget in doc.form["plan"].widgets] == ["A", "B", "C"]


@nameless_kinds
def test_named_fields_below_nameless_intermediates_are_child_fields(nameless: str) -> None:
    """A named field below an intermediate is a child field of the field above, with no name component between.

    It inherits through the intermediate, and the field above is no longer a
    leaf.
    """
    pdf = _build_folded_pdf("/FT /Tx /T (name) /V (parent)", [["/T (inner)"]], f"{nameless} /V (folded)")

    for doc in _reloaded(pyropdf.open(pdf)):
        assert form_entries(doc) == [{"name": "name.inner", "type": "text", "value": "folded"}]
        assert "name" not in doc.form
        assert _widget_xrefs(doc.form["name.inner"]) == [9]


def _build_cycle_pdf(field_kids: str, intermediates: dict[int, str]) -> bytes:
    """Build text field 5 with the given kids and intermediates; 9 and 10 are widgets."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Annots [9 0 R 10 0 R] >>",
            4: "<< /Fields [5 0 R] >>",
            5: f"<< /FT /Tx /T (name) /Kids {field_kids} >>",
            9: "<< /Type /Annot /Subtype /Widget /Rect [10 10 90 40] /P 3 0 R /F 4 >>",
            10: "<< /Type /Annot /Subtype /Widget /Rect [110 10 190 40] /P 3 0 R /F 4 >>",
            **intermediates,
        }
    )


@pytest.mark.parametrize(
    "intermediates",
    [
        {8: "<< /Parent 5 0 R /Kids [8 0 R 9 0 R 10 0 R] >>"},
        {8: "<< /Parent 5 0 R /Kids [11 0 R 9 0 R] >>", 11: "<< /Parent 8 0 R /Kids [8 0 R 10 0 R] >>"},
    ],
    ids=["itself", "ancestor"],
)
def test_cycles_through_nameless_intermediates_end(intermediates: dict[int, str]) -> None:
    """An intermediate that is its own kid or a kid of its own kid is expanded once."""
    for doc in _reloaded(pyropdf.open(_build_cycle_pdf("[8 0 R]", intermediates))):
        assert form_entries(doc) == [{"name": "name", "type": "text", "value": None}]
        assert _widget_xrefs(doc.form["name"]) == [9, 10]


@pytest.mark.parametrize("field_kids", ["[5 0 R 9 0 R 10 0 R]", "[8 0 R]"], ids=["direct", "through-intermediate"])
def test_fields_below_their_own_intermediates_are_their_own_child_fields(field_kids: str) -> None:
    """A field below its own intermediate is its own child field, as a field in its own `/Kids` is.

    The field is therefore not a leaf in either case, and the walk still ends.
    """
    pdf = _build_cycle_pdf(field_kids, {8: "<< /Parent 5 0 R /Kids [5 0 R 9 0 R 10 0 R] >>"})

    for doc in _reloaded(pyropdf.open(pdf)):
        assert form_entries(doc) == []
        assert "name" not in doc.form
        assert [widget.field for widget in doc.pages[0].widgets] == [None, None]


@pytest.mark.parametrize(
    "kids",
    ["null", "6 0 R", "7 0 R", "5", "[]"],
    ids=["null", "indirect-null", "missing", "not-an-array", "empty"],
)
def test_nameless_kids_without_kids_to_fold_stay_widgets(kids: str) -> None:
    """A nameless kid is an intermediate field only when its `/Kids` resolves to a non-empty array.

    Any other `/Kids` names no kids to fold, so the kid stays a widget.
    """
    for doc in _reloaded(pyropdf.open(_build_folded_pdf("/FT /Tx /T (name)", [f"/Kids {kids}", ""]))):
        assert _widget_xrefs(doc.form["name"]) == [8, 9]


def test_nameless_widget_with_kids_is_a_widget_and_an_intermediate() -> None:
    """A nameless kid that has kids and looks like a widget is a widget and an intermediate field at once."""
    widget = "/Type /Annot /Subtype /Widget /P 3 0 R /F 4"
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Annots [5 0 R 6 0 R 7 0 R] >>",
            4: "<< /Fields [8 0 R] >>",
            5: f"<< /Parent 8 0 R /Kids [6 0 R 7 0 R] {widget} /Rect [10 10 90 40] >>",
            6: f"<< /Parent 5 0 R {widget} /Rect [110 10 190 40] >>",
            7: f"<< /Parent 5 0 R {widget} /Rect [210 10 290 40] >>",
            8: "<< /FT /Tx /T (name) /Kids [5 0 R] >>",
        }
    )

    for doc in _reloaded(pyropdf.open(pdf)):
        assert _widget_xrefs(doc.form["name"]) == [5, 6, 7]
        doc.form["name"].value = "new"
        for current in _reloaded(doc):
            ink = _kid_ink(current)
            assert ink[0] > 0
            assert ink == [ink[0]] * 3
            assert b"/NeedAppearances false" in current.to_bytes()


def _kid_ink(doc: pyropdf.Document, count: int = 3) -> list[int]:
    """Count the opaque pixels inside each of the first `count` kid widgets."""
    return [_opaque_pixels(doc, (10 + 100 * index, 60, 90 + 100 * index, 90)) for index in range(count)]


@nameless_kinds
def test_need_appearances_waits_for_the_widgets_below_intermediates(nameless: str) -> None:
    """NeedAppearances stays true while a widget below an intermediate lacks an appearance."""
    source = pyropdf.open(_build_folded_pdf("/FT /Tx /T (name)", [["", ""]], nameless))

    for doc in _reloaded(source):
        doc.form["name"].value = "山田"
        for current in _reloaded(doc):
            assert form_entries(current) == [{"name": "name", "type": "text", "value": "山田"}]
            assert _kid_ink(current, 2) == [0, 0]
            assert b"/NeedAppearances true" in current.to_bytes()
        doc.form["name"].value = "new"
        for current in _reloaded(doc):
            assert min(_kid_ink(current, 2)) > 0
            assert b"/NeedAppearances false" in current.to_bytes()


def _assert_checkbox_limit(at_limit: bytes, over_limit: bytes, message: str) -> None:
    """Read checkbox `button` at a limit, and check that one more entry is refused, live and reloaded."""
    for doc in _reloaded(pyropdf.open(at_limit)):
        assert form_entries(doc) == [{"name": "button", "type": "checkbox", "value": "Off"}]
        assert _widget_xrefs(doc.form["button"]) == [9]

    for doc in _reloaded(pyropdf.open(over_limit)):
        with pytest.raises(pyropdf.PdfError, match=message):
            form_entries(doc)


def _build_folded_budget_pdf(kids: str, nameless: str = "") -> bytes:
    """Build checkbox 5 whose only kid is intermediate 8 with the `kids` entries and `nameless`; 9 is a widget."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [9 0 R] >>",
            4: "<< /Fields [5 0 R] >>",
            5: "<< /FT /Btn /T (button) /V /Off /Kids [8 0 R] >>",
            6: "null",
            7: _form_stream("0 0 20 20 re f"),
            8: f"<< /Parent 5 0 R {nameless} /Kids [{kids}] >>",
            9: (
                "<< /Parent 8 0 R /Type /Annot /Subtype /Widget /Rect [10 10 30 30]"
                " /AS /Off /AP << /N << /Yes 7 0 R /Off 7 0 R >> >> >>"
            ),
        }
    )


def test_nameless_intermediate_kids_count_toward_the_edge_limit() -> None:
    """Each `/Kids` entry of an intermediate takes one of the 8,192 edges, after the `/Fields` and field entries."""
    _assert_checkbox_limit(
        _build_folded_budget_pdf("9 0 R " + "null " * 8189),
        _build_folded_budget_pdf("9 0 R " + "null " * 8190),
        "8192-edge safety limit",
    )


@pytest.mark.parametrize(
    ("nameless", "widgets"),
    [("", 4096), ("/Type /Annot /Subtype /Widget /Rect [40 10 60 30]", 4095)],
    ids=["intermediate", "widget-intermediate"],
)
def test_nameless_intermediate_widgets_count_toward_the_widget_limit(nameless: str, widgets: int) -> None:
    """Widgets below an intermediate count toward the 4,096-widget limit, as does an intermediate that is a widget."""
    at_limit = _build_folded_budget_pdf("9 0 R " * widgets, nameless)
    over_limit = _build_folded_budget_pdf("9 0 R " * (widgets + 1), nameless)
    if nameless:
        # The intermediate is a widget of the field too, though no page lists it.
        for doc in _reloaded(pyropdf.open(at_limit)):
            assert _widget_xrefs(doc.form["button"]) == [9]
        for doc in _reloaded(pyropdf.open(over_limit)):
            with pytest.raises(pyropdf.PdfError, match="4096-widget safety limit"):
                form_entries(doc)
    else:
        _assert_checkbox_limit(at_limit, over_limit, "4096-widget safety limit")


def test_nameless_intermediates_count_toward_the_node_limit() -> None:
    """Each intermediate is a node, so the field and 4,095 intermediates reach the 4,096-node limit."""

    def build(count: int) -> bytes:
        numbers = range(10, 10 + count)
        refs = " ".join(f"{number} 0 R" for number in numbers)
        objects = {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [9 0 R] >>",
            4: "<< /Fields [5 0 R] >>",
            5: f"<< /FT /Btn /T (button) /V /Off /Kids [{refs}] >>",
            9: (
                "<< /Parent 10 0 R /Type /Annot /Subtype /Widget /Rect [10 10 30 30]"
                " /AS /Off /AP << /N << /Yes 8 0 R /Off 8 0 R >> >> >>"
            ),
            8: _form_stream("0 0 20 20 re f"),
            # Every intermediate holds one kid; only the first holds the widget.
            **{number: f"<< /Parent 5 0 R /Kids [{'9 0 R' if number == 10 else 'null'}] >>" for number in numbers},
        }
        return build_raw_pdf(objects)

    _assert_checkbox_limit(build(4095), build(4096), "4096-node safety limit")


def test_nameless_intermediates_count_toward_the_depth_limit() -> None:
    """Each intermediate is a level, so 63 nested intermediates below a top-level field reach the 64-level limit."""

    def build(depth: int) -> bytes:
        kids: FoldedKids = [""]
        for _ in range(depth):
            kids = [kids]
        return _build_folded_pdf("/FT /Tx /T (name)", kids)

    for doc in _reloaded(pyropdf.open(build(63))):
        assert _widget_xrefs(doc.form["name"]) == _page_widgets(doc)
        assert len(_page_widgets(doc)) == 1

    for doc in _reloaded(pyropdf.open(build(64))):
        with pytest.raises(pyropdf.PdfError, match="64-level safety limit"):
            form_entries(doc)


@pytest.mark.parametrize(
    ("field", "nameless", "limit", "message"),
    [
        ("/FT /Tx /T ({text})", "", 512 * 1024, r"materialized field-name.*1048576-byte safety limit"),
        ("/FT /Tx /T (name)", "/V ({text})", 1024 * 1024, r"encoded field-value.*1048576-byte safety limit"),
    ],
    ids=["materialized-name", "value"],
)
def test_nameless_intermediates_count_toward_the_text_limits(
    field: str, nameless: str, limit: int, message: str
) -> None:
    """An intermediate materializes the full name of the field above it and reads its own value within the limits."""

    def build(size: int) -> bytes:
        text = "x" * size
        return _build_folded_pdf(field.format(text=text), [[""]], nameless.format(text=text))

    for doc in _reloaded(pyropdf.open(build(limit))):
        assert len(form_entries(doc)) == 1

    for doc in _reloaded(pyropdf.open(build(limit + 1))):
        with pytest.raises(pyropdf.PdfError, match=message):
            form_entries(doc)


# 64 KiB of text that the kids of field 5 inherit. A copy per pushed kid or per
# returned leaf took 256-512 MiB for these inputs.
LONG_TEXT = "x" * (64 * 1024)
LONG_NAME_FIELD = [{"name": f"{LONG_TEXT}.c", "type": "text", "value": None}]
TYPE_LEAVES = range(6, 4101)
# Entries and `/Kids` of field 5, its kids from object 6, and the listed fields.
# A kid repeated 8,190 times takes all but one of the edges after `/Fields`.
SHARED_TEXT_CASES = {
    "child-field": (f"/FT /Tx /T ({LONG_TEXT})", "6 0 R " * 8190, {6: "<< /T (c) >>"}, LONG_NAME_FIELD),
    "intermediate": (
        f"/FT /Tx /T ({LONG_TEXT})",
        "6 0 R " * 8190,
        {6: "<< /Kids [7 0 R] >>", 7: "<< /T (c) >>"},
        LONG_NAME_FIELD,
    ),
    "type": (
        f"/FT /{LONG_TEXT}",
        "6 0 R " * 8190,
        {6: "<< /T (c) >>"},
        [{"name": "c", "type": "unknown", "value": None}],
    ),
    "type-leaves": (
        f"/FT /{LONG_TEXT}",
        " ".join(f"{number} 0 R" for number in TYPE_LEAVES),
        {number: f"<< /T (k{number}) >>" for number in TYPE_LEAVES},
        [{"name": name, "type": "unknown", "value": None} for name in sorted(f"k{number}" for number in TYPE_LEAVES)],
    ),
}


def _build_shared_text_pdf(field: str, kid_refs: str, kids: dict[int, str]) -> bytes:
    """Build field 5 with the `field` entries and the `kid_refs` kids, which `kids` defines from object 6."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: "<< /Fields [5 0 R] >>",
            5: f"<< {field} /Kids [{kid_refs}] >>",
            **kids,
        }
    )


@pytest.mark.parametrize(("field", "kid_refs", "kids", "expected"), SHARED_TEXT_CASES.values(), ids=SHARED_TEXT_CASES)
def test_kids_list_the_long_name_or_type_they_inherit(
    field: str, kid_refs: str, kids: dict[int, str], expected: list[dict[str, str | None]]
) -> None:
    """A kid repeated on 8,190 edges is listed once, as one reference lists it, and 4,095 leaves share one type."""
    doc = pyropdf.open(_build_shared_text_pdf(field, kid_refs, kids))
    assert form_entries(doc) == expected


# Print by how many bytes listing doc.form once raised the peak RSS.
PEAK_RSS_GROWTH = """\
import resource
import sys

import pyropdf

doc = pyropdf.open(sys.argv[1])
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
len(doc.form)
growth = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
print(growth if sys.platform == "darwin" else growth * 1024)
"""


@pytest.mark.parametrize(
    ("field", "kid_refs", "kids"), [case[:3] for case in SHARED_TEXT_CASES.values()], ids=SHARED_TEXT_CASES
)
def test_kids_share_the_long_name_or_type_they_inherit(
    tmp_path: Path, field: str, kid_refs: str, kids: dict[int, str]
) -> None:
    """Listing the fields in a fresh process raises its peak RSS by less than a quarter of the copies."""
    pytest.importorskip("resource")
    path = tmp_path / "form.pdf"
    path.write_bytes(_build_shared_text_pdf(field, kid_refs, kids))
    result = subprocess.run(  # noqa: S603 - fixed script under the active interpreter.
        [sys.executable, "-c", PEAK_RSS_GROWTH, str(path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) < 64 * 1024 * 1024


# --- the memoized walk ---------------------------------------------------------


def test_reading_every_field_costs_one_walk() -> None:
    doc = pyropdf.open(_build_form_pdf())
    core = doc._core()  # noqa: SLF001 - the memo's counts are internal.
    hits, computations = _core.form_walk_counts(core)
    fields = list(doc.form.values())
    values = [field.value for field in fields]
    assert values[2] == "initial"
    assert _core.form_walk_counts(core)[1] == computations + 1
    assert _core.form_walk_counts(core)[0] > hits

    # An edit of a field invalidates the walk; reading again walks once more.
    doc.form["customer"].obj["V"] = pyropdf.String(b"edited")
    assert [field.value for field in fields][2] == "edited"
    assert _core.form_walk_counts(core)[1] == computations + 2


def test_form_refuses_an_overlong_reference_chain_without_truncating_fields() -> None:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R] >> >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        4: "<< /FT /Tx /T 5 0 R >>",
    }
    objects.update({number: f"{number + 1} 0 R" for number in range(5, 39)})
    objects[39] = "(name)"
    doc = pyropdf.open(build_raw_pdf(objects))
    with pytest.raises(pyropdf.MalformedPdfError):
        list(doc.form)


def test_cached_form_lookup_observes_cancellation() -> None:
    token = pyropdf.CancellationToken()
    data = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R] >> >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: "<< /FT /Tx /T (name) >>",
        }
    )
    doc = pyropdf.open(data, limits=pyropdf.DocumentLimits(cancellation=token))
    assert doc.form["name"].name == "name"
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as caught:
        _ = doc.form["name"]
    assert caught.value.code == "cancelled"


def test_read_only_transaction_reuses_the_existing_form_walk() -> None:
    from pyropdf import _core  # noqa: PLC0415 - the memo counters are internal.

    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [4 0 R] >>",
                4: "<< /FT /Tx /T (name) /Type /Annot /Subtype /Widget /Rect [1 1 5 5] >>",
            }
        )
    )
    assert list(doc.form) == ["name"]
    core = doc._core()  # noqa: SLF001
    computations = _core.form_walk_counts(core)[1]
    with doc.transaction("Read without modifying"):
        assert list(doc.form) == ["name"]
        assert len(doc.pages[0].widgets) == 1
        assert _core.form_walk_counts(core)[1] == computations


def test_choice_options_inherit_through_a_parent_alias() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
                5: "<< /FT /Ch /T (choice) /Parent 6 0 R >>",
                6: "7 0 R",
                7: "<< /Opt [(first)] >>",
            }
        )
    )
    assert [(choice.value, choice.label) for choice in doc.form["choice"].choices] == [("first", "first")]


@pytest.mark.parametrize("last_exists", [False, True])
def test_choice_parent_limit_only_counts_existing_nodes(*, last_exists: bool) -> None:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        5: "<< /FT /Ch /T (choice) /Parent 6 0 R >>",
    }
    objects.update({number: f"<< /Parent {number + 1} 0 R >>" for number in range(6, 69)})
    if last_exists:
        objects[69] = "<< /Opt [(too deep)] >>"
    doc = pyropdf.open(build_raw_pdf(objects))
    if last_exists:
        with pytest.raises(pyropdf.PdfError, match="64-level safety limit"):
            _ = doc.form["choice"].choices
    else:
        assert doc.form["choice"].choices == ()


def test_form_depth_limit_ignores_a_visited_intermediate_cycle() -> None:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        5: "<< /FT /Tx /T (name) /Kids [6 0 R] >>",
    }
    objects.update({number: f"<< /Kids [{number + 1} 0 R] >>" for number in range(6, 68)})
    objects[68] = "<< /Kids [68 0 R] >>"
    doc = pyropdf.open(build_raw_pdf(objects))
    assert list(doc.form) == ["name"]


def test_fill_can_commit_before_a_later_whole_form_value_limit() -> None:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [6 0 R] >>",
        4: "<< /Fields [5 0 R 6 0 R] /DA (/Helv 10 Tf 0 g) >>",
        5: "<< /FT /Tx /T (large) /V 9 0 R >>",
        6: "<< /FT /Tx /T (small) /V () /Subtype /Widget /Rect [0 0 50 20] /AP << /N 10 0 R >> >>",
        9: "(" + "a" * (1024 * 1024 - 1) + ")",
        10: "<< /Length 3 >>\nstream\nq Q\nendstream",
    }
    doc = pyropdf.open(build_raw_pdf(objects))
    field = doc.form["small"]
    obj = field.obj
    field.value = "ab"
    assert obj["V"] == b"ab"
    with pytest.raises(pyropdf.PdfError, match="field-value"):
        list(doc.form)
    doc.history.undo()
    field.value = "c"
    assert obj["V"] == b"c"


def test_fill_rechecks_shared_appearances_after_external_edits_and_rollback() -> None:
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [5 0 R 6 0 R] >>",
        4: "<< /Fields [5 0 R 6 0 R] /DA (/Helv 10 Tf 0 g) >>",
        5: "<< /FT /Tx /T (a) /V (old) /Subtype /Widget /Rect [0 0 50 20] /AP << /N 7 0 R >> >>",
        6: "<< /FT /Tx /T (b) /V <FEFF4E2D> /Subtype /Widget /Rect [0 20 50 40] /AP << /N 7 0 R >> >>",
        7: "<< /Length 3 >>\nstream\nq Q\nendstream",
    }
    doc = pyropdf.open(build_raw_pdf(objects))
    field = doc.form["a"]
    field.value = "first"
    assert not doc.form.need_appearances
    doc.objects[7].write(b"", filter=None)
    field.value = "second"
    assert doc.form.need_appearances
    doc.history.undo()
    doc.history.undo()
    field.value = "third"
    assert not doc.form.need_appearances

    def roll_back() -> None:
        with doc.transaction("rollback"):
            doc.objects[7].write(b"", filter=None)
            field.value = "temporary"
            assert doc.form.need_appearances
            message = "rollback"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        roll_back()
    assert not doc.form.need_appearances
    field.value = "after rollback"
    assert not doc.form.need_appearances
    assert not pyropdf.open(doc.to_bytes()).form.need_appearances


def test_cached_fill_observes_cancellation() -> None:
    token = pyropdf.CancellationToken()
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [5 0 R] >>",
        4: "<< /Fields [5 0 R] /DA (/Helv 10 Tf 0 g) >>",
        5: "<< /FT /Tx /T (field) /V (old) /Subtype /Widget /Rect [0 0 50 20] /AP << /N 6 0 R >> >>",
        6: "<< /Length 3 >>\nstream\nq Q\nendstream",
    }
    doc = pyropdf.open(build_raw_pdf(objects), limits=pyropdf.DocumentLimits(cancellation=token))
    field = doc.form["field"]
    field.value = "warm"
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as caught:
        field.value = "cancelled"
    assert caught.value.code == "cancelled"
