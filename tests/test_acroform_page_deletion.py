"""Remove the form widgets and fields of the pages that deletion and selection drop.

Ported from pylopdf ebb01d3c's ``tests/test_acroform_page_deletion.py``,
with its fixtures from ``tests/test_acroform_import.py``. Intended
difference: the checks read the saved document through the object model
instead of matching lopdf's serialization with regular expressions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import pyropdf
from pyropdf import Dictionary, Stream, String

F1040 = Path(__file__).parent / "assets" / "real_world" / "f1040.pdf"
EXCLUDED_APPEARANCE = "EXCLUDED-WIDGET-APPEARANCE-4d1e"


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


def form_entries(doc: pyropdf.Document) -> list[dict[str, Any]]:
    """Return every field of ``doc.form`` as ``{"name", "type", "value"}`` in name order."""
    return [{"name": name, "type": str(field.type), "value": field.value} for name, field in doc.form.items()]


def _fields(doc: pyropdf.Document) -> dict[str, tuple[str, str | None]]:
    return {field["name"]: (field["type"], field["value"]) for field in form_entries(doc)}


def _stream(content: str) -> str:
    return f"<< /Type /XObject /Subtype /Form /BBox [0 0 20 20] /Length {len(content)} >>\nstream\n{content}\nendstream"


def _two_page_form() -> bytes:
    """Build fields whose widgets lie on one or both of two pages.

    `choice` is a radio group with one widget per page and one `/Opt` export
    value per widget. `person` holds `first` on the first page, `last` on the
    second, and the widgetless `note`. `total` is on the first page, and the
    top-level `hidden` has no widget. The second-page widget's `/P` names the
    first page, and the first-page radio appearance holds a marker.
    """
    marker = f"BT /F1 4 Tf ({EXCLUDED_APPEARANCE}) Tj ET"
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 5 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 200] >>",
            3: "<< /Type /Page /Parent 2 0 R /Annots [8 0 R 11 0 R 13 0 R] >>",
            4: "<< /Type /Page /Parent 2 0 R /Annots [9 0 R 12 0 R] >>",
            5: (
                "<< /Fields [6 0 R 10 0 R 13 0 R 15 0 R] /DA (/Helv 0 Tf 0 g) /DR << /Font << /Helv 7 0 R >> >>"
                " /CO [12 0 R 13 0 R] /NeedAppearances true /XFA [(template) 18 0 R] /SigFlags 3 >>"
            ),
            6: "<< /FT /Btn /Ff 49152 /T (choice) /V /second /Opt [(first) (second)] /Kids [8 0 R 9 0 R] >>",
            7: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            8: (
                "<< /Type /Annot /Subtype /Widget /Parent 6 0 R /Rect [10 170 30 190] /P 3 0 R /AS /Off"
                " /AP << /N << /first 16 0 R /Off 17 0 R >> >> >>"
            ),
            9: (
                "<< /Type /Annot /Subtype /Widget /Parent 6 0 R /Rect [10 170 30 190] /P 3 0 R /AS /second"
                " /TU (stale page) /AP << /N << /second 17 0 R /Off 17 0 R >> >> >>"
            ),
            10: "<< /FT /Tx /T (person) /Kids [11 0 R 12 0 R 14 0 R] >>",
            11: (
                "<< /T (first) /V (Ada) /Parent 10 0 R /Type /Annot /Subtype /Widget /Rect [40 140 190 160] /P 3 0 R >>"
            ),
            12: (
                "<< /T (last) /V (Lovelace) /Parent 10 0 R /Type /Annot /Subtype /Widget"
                " /Rect [40 140 190 160] /P 4 0 R >>"
            ),
            13: "<< /FT /Tx /T (total) /V (42) /Type /Annot /Subtype /Widget /Rect [40 100 190 120] /P 3 0 R >>",
            14: "<< /T (note) /V (kept) /Parent 10 0 R >>",
            15: "<< /FT /Tx /T (hidden) /V (secret) >>",
            16: _stream(marker),
            17: _stream(""),
            18: "<< /Length 0 >>\nstream\n\nendstream",
        }
    )


TWO_PAGE_FIELDS = {
    "choice": ("radio", "second"),
    "person.first": ("text", "Ada"),
    "person.last": ("text", "Lovelace"),
    "person.note": ("text", "kept"),
    "total": ("text", "42"),
    "hidden": ("text", "secret"),
}


def _wide_form(kids: int, *, widgets: bool = False, page: int = 0) -> bytes:
    """Build two pages and one field with `kids` child fields or widgets; page `page` lists the first."""
    annots = ["/Annots [6 0 R]", ""]
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
        3: f"<< /Type /Page /Parent 2 0 R {annots[page]} >>",
        4: f"<< /Type /Page /Parent 2 0 R {annots[1 - page]} >>",
        5: "<< /FT /Tx /T (root) /Kids [" + " ".join(f"{6 + index} 0 R" for index in range(kids)) + "] >>",
    }
    for index in range(kids):
        objects[6 + index] = (
            "<< /Parent 5 0 R /Type /Annot /Subtype /Widget /Rect [0 0 10 10] >>"
            if widgets
            else f"<< /T (f{index}) /Parent 5 0 R /Type /Annot /Subtype /Widget /Rect [0 0 10 10] >>"
        )
    return build_raw_pdf(objects)


def _deep_form(levels: int, *, page: int = 0) -> bytes:
    """Build two pages and a chain of `levels` nested fields whose widget page `page` lists."""
    annots = [f"/Annots [{4 + levels} 0 R]", ""]
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
        3: f"<< /Type /Page /Parent 2 0 R {annots[page]} >>",
        4: f"<< /Type /Page /Parent 2 0 R {annots[1 - page]} >>",
    }
    for level in range(levels):
        kids = f"/Kids [{6 + level} 0 R]" if level + 1 < levels else ""
        widget = f"/Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P {3 + page} 0 R" if level + 1 == levels else ""
        objects[5 + level] = f"<< /FT /Tx /T (l{level}) {kids} {widget} >>"
    return build_raw_pdf(objects)


def _dictionaries(doc: pyropdf.Document) -> list[Dictionary]:
    return [value for number in doc.objects if isinstance(value := doc.objects[number], Dictionary)]


def _widgets(doc: pyropdf.Document) -> list[Dictionary]:
    return [value for value in _dictionaries(doc) if value.get("Subtype") == "Widget"]


def _named(doc: pyropdf.Document, key: str, text: str) -> list[Dictionary]:
    return [
        value
        for value in _dictionaries(doc)
        if not isinstance(value, Stream) and isinstance(found := value.get(key), String) and found.text == text
    ]


def _field(doc: pyropdf.Document, partial_name: str) -> Dictionary:
    """Return the one object whose `/T` is `partial_name`."""
    (field,) = _named(doc, "T", partial_name)
    return field


def _holds_marker(doc: pyropdf.Document) -> bool:
    streams = [value for number in doc.objects if isinstance(value := doc.objects[number], Stream)]
    return any(EXCLUDED_APPEARANCE.encode() in stream.read() for stream in streams)


@pytest.mark.parametrize("edit", ["delete", "select"])
def test_deleting_the_second_page_removes_its_widgets_and_fields(edit: str) -> None:
    doc = pyropdf.open(_two_page_form())
    if edit == "delete":
        del doc.pages[1:2]
    else:
        doc.pages.select([0])

    expected = {name: TWO_PAGE_FIELDS[name] for name in ("choice", "person.first", "person.note", "total", "hidden")}
    assert _fields(doc) == expected
    saved = pyropdf.open(doc.to_bytes())
    assert len(saved.pages) == 1
    assert len(_widgets(saved)) == 3
    assert _named(saved, "TU", "stale page") == []
    assert _holds_marker(saved)
    choice = _field(saved, "choice")
    assert list(choice["Opt"]) == [b"first"]
    assert len(choice["Kids"]) == 1
    assert len(_field(saved, "person")["Kids"]) == 2
    assert list(saved.catalog["AcroForm"]["CO"]) == [_field(saved, "total")]

    assert _fields(saved) == expected
    saved.form["person.first"].value = "Grace"
    assert _fields(saved)["person.first"] == ("text", "Grace")


@pytest.mark.parametrize("edit", ["delete", "select"])
def test_deleting_the_first_page_points_remaining_widgets_at_their_page(edit: str) -> None:
    doc = pyropdf.open(_two_page_form())
    if edit == "delete":
        del doc.pages[0:1]
    else:
        doc.pages.select([1])

    expected = {name: TWO_PAGE_FIELDS[name] for name in ("choice", "person.last", "person.note", "hidden")}
    assert _fields(doc) == expected
    saved = pyropdf.open(doc.to_bytes())
    assert not _holds_marker(saved)
    assert len(_widgets(saved)) == 2
    assert list(_field(saved, "choice")["Opt"]) == [b"second"]
    (stale,) = _named(saved, "TU", "stale page")
    assert stale["P"] == saved.pages[0].obj
    assert list(saved.catalog["AcroForm"]["CO"]) == [_field(saved, "last")]
    assert _named(saved, "T", "total") == []

    assert _fields(saved) == expected
    saved.form["choice"].value = "Off"
    assert _fields(saved)["choice"] == ("radio", "Off")


@pytest.mark.parametrize("order", [[1, 0], [0, 1], [0, 0, 1], [1, 1, 0]])
def test_selecting_every_page_keeps_the_form(order: list[int]) -> None:
    doc = pyropdf.open(_two_page_form())
    doc.pages.select(order)
    assert _fields(doc) == TWO_PAGE_FIELDS
    assert _fields(pyropdf.open(doc.to_bytes())) == TWO_PAGE_FIELDS


def test_selecting_no_page_keeps_only_widgetless_fields() -> None:
    doc = pyropdf.open(_two_page_form())
    doc.pages.select([])
    assert _fields(doc) == {"hidden": TWO_PAGE_FIELDS["hidden"]}
    saved = pyropdf.open(doc.to_bytes())
    assert _widgets(saved) == []
    assert list(saved.catalog["AcroForm"]["CO"]) == []


@pytest.mark.parametrize(("deleted", "names"), [(0, ["signature", "unplaced"]), (1, ["text", "unplaced"])])
def test_a_widget_that_no_page_lists_goes_only_with_its_page_entry(deleted: int, names: list[str]) -> None:
    """An invisible signature widget is often missing from `/Annots`; one without `/P` stays."""
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R 6 0 R 8 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [5 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R >>",
                5: "<< /FT /Tx /T (text) /Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P 3 0 R >>",
                6: "<< /FT /Sig /T (signature) /Kids [7 0 R] >>",
                7: "<< /Parent 6 0 R /Type /Annot /Subtype /Widget /Rect [0 0 0 0] /F 132 /P 4 0 R >>",
                8: "<< /FT /Tx /T (unplaced) /Type /Annot /Subtype /Widget /Rect [0 0 0 0] >>",
            }
        )
    )
    del doc.pages[deleted]
    assert [field["name"] for field in form_entries(doc)] == names


def test_f1040_keeps_the_fields_of_its_remaining_page() -> None:
    doc = pyropdf.open(F1040.read_bytes())
    fields = form_entries(doc)
    first_page = [field for field in fields if field["name"].startswith("topmostSubform[0].Page1[0].")]
    assert len(fields) == 199
    assert len(first_page) == 128

    del doc.pages[1:2]
    assert form_entries(doc) == first_page
    reopened = pyropdf.open(doc.to_bytes())
    assert form_entries(reopened) == first_page
    reopened.form[first_page[0]["name"]].value = "x" if first_page[0]["type"] == "text" else "Off"


def test_documents_without_a_form_delete_pages_unchanged() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R >>",
            }
        )
    )
    doc.pages.new()
    del doc.pages[1:2]
    assert form_entries(doc) == []
    assert b"/AcroForm" not in doc.to_bytes()


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (_wide_form(4096, page=1), "4096-node"),
        (_wide_form(4097, widgets=True, page=1), "4096-widget"),
        (_deep_form(65, page=1), "64-level"),
    ],
    ids=["nodes", "widgets", "levels"],
)
@pytest.mark.parametrize("edit", ["delete", "select"])
def test_deletion_refuses_field_trees_beyond_the_read_limits(source: bytes, message: str, edit: str) -> None:
    doc = pyropdf.open(source)
    page = doc.pages[0]
    before = doc.to_bytes()

    def delete() -> None:
        del doc.pages[1]

    action = delete if edit == "delete" else (lambda: doc.pages.select([0]))
    with pytest.raises(pyropdf.PdfError, match=message):
        action()
    assert doc.to_bytes() == before
    assert len(doc.pages) == 2
    assert page.rect.width == 100


def test_indirect_fields_and_kids_arrays_are_filtered_in_place() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields 5 0 R >> >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [7 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R /Annots [8 0 R] >>",
                5: "[6 0 R]",
                6: "<< /FT /Tx /T (group) /Kids 9 0 R >>",
                7: "<< /T (a) /Parent 6 0 R /Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P 3 0 R >>",
                8: "<< /T (b) /Parent 6 0 R /Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P 4 0 R >>",
                9: "[7 0 R 8 0 R]",
            }
        )
    )
    del doc.pages[1:2]
    assert [field["name"] for field in form_entries(doc)] == ["group.a"]
