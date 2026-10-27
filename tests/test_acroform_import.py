"""Register the form fields of the pages that an import brings.

Ported from pylopdf ebb01d3c's ``tests/test_acroform_import.py``. Intended
differences:

- The checks read the saved document through the object model instead of
  matching lopdf's serialization with regular expressions.
- The tests of the form defaults (``/DR``, ``/DA``, ``/Q``, and
  ``/NeedAppearances``) come with #235, and so do the default-resource
  checks of ``test_inserting_f1040_twice_renames_the_second_form``.
  ``test_complete_import_registers_every_field`` is the field half of
  ``test_complete_import_keeps_every_field_and_form_default``.
- A refused import raises :class:`pyropdf.MalformedPdfError` for a field
  tree past its limits, a :class:`pyropdf.PdfError`.
"""

from __future__ import annotations

import pytest
from test_acroform_page_deletion import (
    F1040,
    TWO_PAGE_FIELDS,
    _deep_form,
    _dictionaries,
    _field,
    _fields,
    _holds_marker,
    _named,
    _two_page_form,
    _wide_form,
    _widgets,
    build_raw_pdf,
    form_entries,
)
from test_annotation_appearances import build_pdf

import pyropdf
from pyropdf import Dictionary

F1040_TEXT = "topmostSubform[0].Page1[0].Address_ReadOrder[0].f1_20[0]"
F1040_CHECKBOX = "topmostSubform[0].Page1[0].Checkbox_ReadOrder[0].c1_8[0]"


@pytest.fixture
def one_page_pdf() -> bytes:
    return build_pdf(["Hello PDF"])


def _acroform(doc: pyropdf.Document) -> Dictionary:
    form = doc.catalog["AcroForm"]
    assert isinstance(form, Dictionary)
    return form


def _pages(doc: pyropdf.Document) -> list[Dictionary]:
    return [value for value in _dictionaries(doc) if value.get("Type") == "Page"]


def test_insert_pdf_registers_f1040_fields_with_values() -> None:
    src = pyropdf.open(F1040.read_bytes())
    src.form[F1040_TEXT].value = "Jane Doe"
    src.form[F1040_CHECKBOX].value = True
    source_bytes = src.to_bytes()

    dst = pyropdf.Document()
    dst.pages.extend(src.pages)

    assert src.to_bytes() == source_bytes
    assert form_entries(dst) == form_entries(src)
    assert len(form_entries(dst)) == 199
    assert _fields(dst)[F1040_TEXT] == ("text", "Jane Doe")
    for page in range(2):
        matrix = pyropdf.Matrix(0.5, 0, 0, 0.5, 0, 0)
        assert bytes(dst.pages[page].pixmap(matrix=matrix, alpha=True)) == bytes(
            src.pages[page].pixmap(matrix=matrix, alpha=True)
        )

    dst.form[F1040_TEXT].value = "John Roe"
    dst.form[F1040_CHECKBOX].value = False
    saved = dst.to_bytes()
    assert b"/XFA" not in saved
    assert b"/SigFlags" not in saved
    reopened = pyropdf.open(saved)
    assert len(form_entries(reopened)) == 199
    assert _fields(reopened)[F1040_TEXT] == ("text", "John Roe")
    assert _fields(reopened)[F1040_CHECKBOX] == ("checkbox", "Off")
    reopened.form[F1040_TEXT].value = "Again"
    assert _fields(reopened)[F1040_TEXT] == ("text", "Again")


def test_insert_pdf_after_existing_pages_registers_f1040_fields(one_page_pdf: bytes) -> None:
    dst = pyropdf.open(one_page_pdf)
    dst.pages.insert(0, pyropdf.open(F1040.read_bytes()).pages)
    assert len(dst.pages) == 3
    assert len(form_entries(dst)) == 199
    assert len(dst.pages[0].widgets) == 128
    assert "Hello PDF" in dst.pages[2].text()


def test_inserting_f1040_twice_renames_the_second_form() -> None:
    src = pyropdf.open(F1040.read_bytes())
    src.form[F1040_TEXT].value = "Jane Doe"
    dst = pyropdf.Document()
    for _ in range(3):
        dst.pages.extend(src.pages)

    fields = _fields(dst)
    assert len(fields) == 3 * 199
    tail = F1040_TEXT.removeprefix("topmostSubform[0]")
    copies = [F1040_TEXT, f"topmostSubform[0]+1{tail}", f"topmostSubform[0]+2{tail}"]
    assert [fields[name] for name in copies] == [("text", "Jane Doe")] * 3
    assert sum(name.startswith("topmostSubform[0]+1.") for name in fields) == 199

    for index, name in enumerate(copies):
        dst.form[name].value = f"copy {index}"
    reopened = pyropdf.open(dst.to_bytes())
    assert [_fields(reopened)[name][1] for name in copies] == ["copy 0", "copy 1", "copy 2"]

    # The partial names are UTF-16BE, and the suffix keeps that encoding.
    (renamed,) = _named(reopened, "T", "topmostSubform[0]+1")
    assert bytes(renamed["T"]) == b"\xfe\xff" + "topmostSubform[0]+1".encode("utf-16-be")


@pytest.mark.parametrize(
    ("page", "prefix", "count"), [(0, "topmostSubform[0].Page1[0].", 128), (1, "topmostSubform[0].Page2[0].", 71)]
)
def test_insert_pdf_range_registers_only_imported_page_fields(page: int, prefix: str, count: int) -> None:
    src = pyropdf.open(F1040.read_bytes())
    expected = [field for field in form_entries(src) if field["name"].startswith(prefix)]
    dst = pyropdf.Document()
    dst.pages.extend(src.pages[page])

    assert form_entries(dst) == expected
    assert len(expected) == count
    reopened = pyropdf.open(dst.to_bytes())
    assert len(_widgets(reopened)) == count
    assert form_entries(reopened) == expected
    reopened.form[expected[0]["name"]].value = "x" if expected[0]["type"] == "text" else "Off"


def test_complete_import_registers_every_field() -> None:
    src = pyropdf.open(_two_page_form())
    dst = pyropdf.Document()
    dst.pages.extend(src.pages[0:2][::-1])

    assert _fields(dst) == TWO_PAGE_FIELDS
    saved = pyropdf.open(dst.to_bytes())
    assert _holds_marker(saved)
    assert b"/XFA" not in dst.to_bytes()
    assert b"/SigFlags" not in dst.to_bytes()
    assert len(_acroform(saved)["CO"]) == 2


def test_partial_import_drops_widgets_and_fields_of_other_pages() -> None:
    src = pyropdf.open(_two_page_form())
    dst = pyropdf.Document()
    dst.pages.extend(src.pages[1:2])

    expected = {name: TWO_PAGE_FIELDS[name] for name in ("choice", "person.last", "person.note")}
    assert _fields(dst) == expected
    saved = pyropdf.open(dst.to_bytes())
    assert not _holds_marker(saved)
    choice = _field(saved, "choice")
    assert list(choice["Opt"]) == [b"second"]
    assert len(choice["Kids"]) == 1
    assert len(_field(saved, "person")["Kids"]) == 2
    assert list(_acroform(saved)["CO"]) == [_field(saved, "last")]

    (page,) = _pages(saved)
    (stale,) = _named(saved, "TU", "stale page")
    assert stale["P"] == page

    assert _fields(saved) == expected
    saved.form["person.last"].value = "Byron"
    saved.form["choice"].value = "Off"
    assert _fields(saved)["person.last"] == ("text", "Byron")


def test_partial_import_of_first_page_keeps_its_radio_widget() -> None:
    dst = pyropdf.Document()
    dst.pages.extend(pyropdf.open(_two_page_form()).pages[0:1])

    expected = {name: TWO_PAGE_FIELDS[name] for name in ("choice", "person.first", "person.note", "total")}
    assert _fields(dst) == expected
    saved = pyropdf.open(dst.to_bytes())
    assert list(_field(saved, "choice")["Opt"]) == [b"first"]
    assert _holds_marker(saved)


@pytest.mark.parametrize(("page", "names"), [(0, ["text"]), (1, ["signature"])])
def test_a_widget_that_no_page_lists_follows_its_page_entry(page: int, names: list[str]) -> None:
    """An invisible signature widget is often missing from `/Annots`."""
    src = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R 6 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [5 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R >>",
                5: "<< /FT /Tx /T (text) /Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P 4 0 R >>",
                6: "<< /FT /Sig /T (signature) /Kids [7 0 R] >>",
                7: "<< /Parent 6 0 R /Type /Annot /Subtype /Widget /Rect [0 0 0 0] /F 132 /P 4 0 R >>",
            }
        )
    )
    dst = pyropdf.Document()
    dst.pages.extend(src.pages[page])
    assert [field["name"] for field in form_entries(dst)] == names


def test_partial_import_without_imported_widgets_adds_no_form(one_page_pdf: bytes) -> None:
    src = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R >>",
                4: "<< /Type /Page /Parent 2 0 R /Annots [5 0 R] >>",
                5: "<< /FT /Tx /T (only) /Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P 4 0 R >>",
            }
        )
    )
    dst = pyropdf.open(one_page_pdf)
    dst.pages.extend(src.pages[0:1])
    assert form_entries(dst) == []
    assert b"/AcroForm" not in dst.to_bytes()


def _receiving_form(fields: str, extra: str = "", objects: dict[int, str] | None = None) -> bytes:
    """Build a one-page form whose catalog references the dictionary in object 4."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 250 100] >>",
            3: "<< /Type /Page /Parent 2 0 R >>",
            4: f"<< /Fields {fields} {extra} >>",
            **(objects or {}),
        }
    )


def _named_fields(names: list[str], acroform: str = "") -> bytes:
    """Build one page whose widget-fields carry the given top-level names."""
    widgets = {
        5 + index: f"<< /FT /Tx /T ({name}) /Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P 3 0 R >>"
        for index, name in enumerate(names)
    }
    references = " ".join(f"{number} 0 R" for number in widgets)
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
            3: f"<< /Type /Page /Parent 2 0 R /Annots [{references}] >>",
            4: f"<< /Fields [{references}] {acroform} >>",
            **widgets,
        }
    )


def test_colliding_top_level_names_take_the_smallest_unused_suffix() -> None:
    dst = pyropdf.open(_named_fields(["a", "a+1"]))
    src = pyropdf.open(_named_fields(["a", "a+2", "b"]))
    dst.pages.extend(src.pages)
    assert [field["name"] for field in form_entries(dst)] == ["a", "a+1", "a+2", "a+3", "b"]
    dst.pages.extend(src.pages)
    assert [field["name"] for field in form_entries(dst)] == [
        "a",
        "a+1",
        "a+2",
        "a+2+1",
        "a+3",
        "a+4",
        "b",
        "b+1",
    ]
    assert len(form_entries(pyropdf.open(dst.to_bytes()))) == 8


@pytest.mark.parametrize("value", ["7", "null", "8 0 R"])
def test_unusable_receiving_acroform_is_replaced(value: str) -> None:
    dst = pyropdf.open(
        build_raw_pdf(
            {
                1: f"<< /Type /Catalog /Pages 2 0 R /AcroForm {value} >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R >>",
                4: "null",
                5: "null",
                6: "null",
                7: "null",
                8: "42",
            }
        )
    )
    dst.pages.extend(pyropdf.open(_named_fields(["a", "b"])).pages)
    assert [field["name"] for field in form_entries(dst)] == ["a", "b"]
    assert [field["name"] for field in form_entries(pyropdf.open(dst.to_bytes()))] == ["a", "b"]


def test_import_appends_to_an_indirect_fields_array() -> None:
    dst = pyropdf.open(_receiving_form("5 0 R", objects={5: "[6 0 R]", 6: "<< /FT /Tx /T (own) >>"}))
    dst.pages.extend(pyropdf.open(_named_fields(["a"])).pages)
    assert [field["name"] for field in form_entries(dst)] == ["a", "own"]
    saved = pyropdf.open(dst.to_bytes())
    assert _acroform(saved)["Fields"].is_indirect
    assert len(saved.objects[5]) == 2


def test_partial_import_walks_a_cyclic_field_tree() -> None:
    """A nameless intermediate field that lists itself is folded once."""
    src = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [7 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R /Annots [8 0 R] >>",
                5: "<< /FT /Tx /T (outer) /Kids [6 0 R] >>",
                6: "<< /Parent 5 0 R /Kids [7 0 R 6 0 R 8 0 R] >>",
                7: "<< /Parent 6 0 R /Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P 3 0 R >>",
                8: "<< /Parent 6 0 R /Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P 4 0 R >>",
            }
        )
    )
    assert [field["name"] for field in form_entries(src)] == ["outer"]
    dst = pyropdf.Document()
    dst.pages.extend(src.pages[0:1])
    assert [field["name"] for field in form_entries(dst)] == ["outer"]
    assert len(_widgets(pyropdf.open(dst.to_bytes()))) == 1


@pytest.mark.parametrize(
    ("source", "message"),
    [(_wide_form(4096), "4096-node"), (_wide_form(4097, widgets=True), "4096-widget"), (_deep_form(65), "64-level")],
    ids=["nodes", "widgets", "levels"],
)
def test_partial_import_refuses_field_trees_beyond_the_read_limits(
    source: bytes, message: str, one_page_pdf: bytes
) -> None:
    src = pyropdf.open(source)
    dst = pyropdf.open(one_page_pdf)
    page = dst.pages[0]
    before = dst.to_bytes()
    with pytest.raises(pyropdf.PdfError, match=message):
        dst.pages.extend(src.pages[0:1])
    assert dst.to_bytes() == before
    assert "Hello PDF" in page.text()


@pytest.mark.parametrize(
    ("source", "name"), [(_wide_form(4095), "root.f0"), (_deep_form(64), None)], ids=["nodes", "levels"]
)
def test_field_trees_within_the_read_limits_import_partially(source: bytes, name: str | None) -> None:
    dst = pyropdf.Document()
    dst.pages.extend(pyropdf.open(source).pages[0:1])
    names = [field["name"] for field in form_entries(dst)]
    assert names == [name or ".".join(f"l{level}" for level in range(64))]


def test_complete_import_keeps_a_field_tree_beyond_the_read_limits() -> None:
    src = pyropdf.open(_wide_form(4096))
    dst = pyropdf.Document()
    dst.pages.extend(src.pages)
    for doc in (src, dst):
        with pytest.raises(pyropdf.PdfError, match="4096-node"):
            form_entries(doc)


def _flat_form(count: int) -> bytes:
    """Build two pages and `count` widgetless top-level fields."""
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields ["
        + " ".join(f"{5 + index} 0 R" for index in range(count))
        + "] >> >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
        3: "<< /Type /Page /Parent 2 0 R >>",
        4: "<< /Type /Page /Parent 2 0 R >>",
    }
    for index in range(count):
        objects[5 + index] = f"<< /FT /Tx /T (f{index}) >>"
    return build_raw_pdf(objects)


def test_partial_import_refuses_more_than_8192_top_level_fields(one_page_pdf: bytes) -> None:
    dst = pyropdf.open(one_page_pdf)
    before = dst.to_bytes()
    with pytest.raises(pyropdf.PdfError, match="8192-edge"):
        dst.pages.extend(pyropdf.open(_flat_form(8193)).pages[0:1])
    assert dst.to_bytes() == before


def test_complete_import_never_refuses_a_large_form() -> None:
    """Copy `/Fields` without walking it, so its size only limits later reads."""
    dst = pyropdf.open(_receiving_form("[" + "5 0 R " * 8192 + "]", objects={5: "<< /FT /Tx /T (own) >>"}))
    dst.pages.extend(pyropdf.open(_named_fields(["a"])).pages)
    dst.pages.extend(pyropdf.open(_flat_form(8193)).pages)
    assert len(dst.pages) == 4
    assert len(_acroform(pyropdf.open(dst.to_bytes()))["Fields"]) == 8192 + 1 + 8193
    with pytest.raises(pyropdf.PdfError, match="8192-edge"):
        form_entries(dst)


def test_annotations_listed_as_fields_are_not_imported() -> None:
    """pdf.js `issue19281.pdf` lists 35,200 link annotations in `/Fields`."""
    links = range(6, 6 + 9000)
    src = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields ["
                + " ".join(f"{number} 0 R" for number in (5, *links))
                + "] >> >>",
                2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
                3: "<< /Type /Page /Parent 2 0 R /Annots [5 0 R 6 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R >>",
                5: "<< /FT /Tx /T (only) /Type /Annot /Subtype /Widget /Rect [0 0 10 10] /P 3 0 R >>",
                **dict.fromkeys(links, "<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] /F 4 >>"),
            }
        )
    )
    for first, last in ((0, 1), (0, 0)):
        dst = pyropdf.Document()
        dst.pages.extend(src.pages[first : last + 1])
        assert [field["name"] for field in form_entries(dst)] == ["only"]
        assert len(_acroform(pyropdf.open(dst.to_bytes()))["Fields"]) == 1


def test_import_refuses_top_level_names_beyond_the_name_limit() -> None:
    dst = pyropdf.open(_named_fields(["a"]))
    before = dst.to_bytes()
    with pytest.raises(pyropdf.PdfError, match="field-name text exceeds"):
        dst.pages.extend(pyropdf.open(_named_fields(["x" * (1024 * 1024 + 1)])).pages)
    assert dst.to_bytes() == before
