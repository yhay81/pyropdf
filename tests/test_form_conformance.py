"""Appearance ``/Resources`` and PDF 2.0 form-font conformance (#798).

Covers required ``/Resources`` dictionaries on empty and button widget
appearances, Standard 14 ``Type1`` ``/FirstChar``/``/LastChar``/``/Widths``/
``/FontDescriptor`` emission in PDF 2.0 (including unused ``/AcroForm /DR``
resources), version-sensitive ``/NeedAppearances`` omission in PDF 2.0, and
preservation of existing resources, complete appearances, and unrelated pages.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest
from test_preservation import snapshot

import pyropdf
from pyropdf import Array, CheckBox, Dictionary, Font, Name, RadioButton, Stream, TextField

ROOT = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).resolve().parent / "assets"
PDFA = ASSETS / "pdfa"
REAL_WORLD = ASSETS / "real_world"


@cache
def japanese() -> Font:
    return Font.from_file(ROOT / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata" / "NotoSansJP-subset.otf")


def _defects(data: bytes) -> set[tuple[int | None, str, str, str]]:
    with pyropdf.open(data) as doc:
        return {(d.xref, d.path, d.clause, d.kind) for d in doc.check().defects}


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_empty_and_button_widget_appearances_emit_required_resources(*, incremental: bool) -> None:
    seed = pyropdf.Document()
    seed.pages.new((200, 200))
    doc = pyropdf.open(seed.to_bytes())
    page = doc.pages[0]
    txt = page.widgets.add(TextField("empty_text", (10, 10, 100, 30)))
    chk = page.widgets.add(CheckBox("agree", (10, 40, 26, 56), checked=True))
    rad1 = page.widgets.add(RadioButton("choice", (10, 70, 26, 86), on_state="a", selected=True))
    rad2 = page.widgets.add(RadioButton("choice", (40, 70, 56, 86), on_state="b"))
    assert rad1.xref != rad2.xref

    saved = doc.to_bytes(incremental=incremental)
    assert [d for d in pyropdf.open(saved).check().defects if "Resources" in d.path or "XObject" in d.clause] == []

    with pyropdf.open(saved) as reopened:
        ap_text = reopened.pages[0].widgets[0].obj["AP"]["N"]
        assert isinstance(ap_text, Stream)
        assert isinstance(ap_text.get("Resources"), Dictionary)

        ap_chk = reopened.pages[0].widgets[1].obj["AP"]["N"]
        assert isinstance(ap_chk, Dictionary)
        for state in ("Off", "Yes"):
            stream = ap_chk[state]
            assert isinstance(stream, Stream)
            assert isinstance(stream.get("Resources"), Dictionary)

        for index in (2, 3):
            ap_rad = reopened.pages[0].widgets[index].obj["AP"]["N"]
            assert isinstance(ap_rad, Dictionary)
            for stream in ap_rad.values():
                assert isinstance(stream, Stream)
                assert isinstance(stream.get("Resources"), Dictionary)
    assert txt.xref > 0
    assert chk.xref > 0


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize(
    "source_name",
    [
        "f1040.pdf",
        "pdfium-links-highlights-annots.pdf",
        "pdf20-simple.pdf",
    ],
)
def test_real_world_form_cases_add_no_structural_defects(source_name: str, *, incremental: bool) -> None:
    source = (REAL_WORLD / source_name).read_bytes()
    baseline_defects = _defects(source)

    # 1. Bare TextField creation (Standard 14 Type1 in /DR, empty appearance + missing widget completion).
    with pyropdf.open(source) as doc:
        doc.pages[0].widgets.add(TextField("regression", (10, 50, 100, 70)))
        bare_saved = doc.to_bytes(incremental=incremental)
    assert _defects(bare_saved) - baseline_defects == set()

    # 2. #794 add_field sequence: add TextField (leaving /Helv in /AcroForm /DR), then draw with embedded face.
    with pyropdf.open(source) as doc:
        doc.pages[0].widgets.add(TextField("pyropdf_preservation", (10, 50, 100, 70), label="pyropdf-preservation"))
        doc.form.font = japanese()
        doc.form["pyropdf_preservation"].value = "AVA"
        added_saved = doc.to_bytes(incremental=incremental)
    assert _defects(added_saved) - baseline_defects == set()

    # 3. #794 fill_field, delete_widget, delete_field, and flatten_widgets on the prepared baseline.
    with pyropdf.open(source) as prep:
        prep.pages[0].widgets.add(TextField("pyropdf_preservation", (10, 50, 100, 70), label="pyropdf-preservation"))
        prep.form.font = japanese()
        prep.form["pyropdf_preservation"].value = "AVA"
        prepared = prep.to_bytes(incremental=True)
    prepared_defects = _defects(prepared)
    assert prepared_defects - baseline_defects == set()

    with pyropdf.open(prepared) as doc_fill:
        doc_fill.form.font = japanese()
        doc_fill.form["pyropdf_preservation"].value = "AVA office"
        assert _defects(doc_fill.to_bytes(incremental=incremental)) - prepared_defects == set()

    with pyropdf.open(prepared) as doc_del_w:
        target_w = doc_del_w.form["pyropdf_preservation"].widgets[0]
        idx = next(i for i, w in enumerate(doc_del_w.pages[0].widgets) if w == target_w)
        del doc_del_w.pages[0].widgets[idx]
        assert _defects(doc_del_w.to_bytes(incremental=incremental)) - prepared_defects == set()

    with pyropdf.open(prepared) as doc_del_f:
        del doc_del_f.form["pyropdf_preservation"]
        assert _defects(doc_del_f.to_bytes(incremental=incremental)) - prepared_defects == set()

    with pyropdf.open(prepared) as doc_flat:
        doc_flat.pages[0].flatten(annotations=False)
        assert _defects(doc_flat.to_bytes(incremental=incremental)) - prepared_defects == set()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("source", [REAL_WORLD / "pdf20-simple.pdf", PDFA / "pdfa-4.pdf"], ids=lambda p: p.stem)
def test_pdf20_and_pdfa4_form_type1_entries_and_need_appearances_omission(source: Path, *, incremental: bool) -> None:
    raw = source.read_bytes()
    baseline_defects = _defects(raw)

    with pyropdf.open(raw) as doc:
        doc.pages[0].widgets.add(TextField("latin_field", (10, 20, 120, 40)))
        doc.form["latin_field"].value = "Hello 2.0"
        doc.pages[0].widgets.add(TextField("cjk_field", (10, 50, 120, 70)))
        doc.form.font = japanese()
        doc.form["cjk_field"].value = "AVA"
        saved = doc.to_bytes(incremental=incremental)

    assert _defects(saved) - baseline_defects == set()
    with pyropdf.open(saved) as reopened:
        form = reopened.catalog["AcroForm"]
        assert isinstance(form, Dictionary)
        assert "NeedAppearances" not in form
        assert reopened.form.need_appearances is False

        helv = form["DR"]["Font"]["Helv"]
        assert isinstance(helv, Dictionary)
        assert helv["FirstChar"] == 32
        assert helv["LastChar"] == 255
        assert isinstance(helv["Widths"], Array)
        assert len(helv["Widths"]) == 224
        descriptor = helv["FontDescriptor"]
        assert isinstance(descriptor, Dictionary)
        assert descriptor.xref is not None
        assert descriptor["FontName"] == Name("Helvetica")
        assert descriptor["Descent"] < 0
        assert descriptor["StemV"] == 80


def test_catalog_version_20_override_triggers_pdf20_type1_entries_and_preserves_siblings() -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 200))
    doc.pages.new((200, 200))
    doc.catalog["Version"] = Name("2.0")
    doc.pages[1].widgets.add(TextField("sibling", (10, 10, 100, 30)))
    doc.form["sibling"].value = "keep"
    baseline = doc.to_bytes()
    assert _defects(baseline) == set()

    with pyropdf.open(baseline) as reopened:
        before = snapshot(reopened)
        sibling_widget = reopened.form["sibling"].widgets[0]
        sibling_ap_xref = sibling_widget.obj["AP"]["N"].xref
        assert sibling_ap_xref is not None
        page1_state = (reopened.pages[1].text(), bytes(reopened.pages[1].pixmap(dpi=72)))
        custom_gs_xref = reopened.objects.add({"Type": Name("ExtGState"), "CA": 1.0})
        reopened.catalog["AcroForm"]["DR"]["ExtGState"] = {"GS0": reopened.objects[custom_gs_xref]}
        baseline2 = reopened.to_bytes(incremental=True)

    with pyropdf.open(baseline2) as doc2:
        before2 = snapshot(doc2)
        touched = {doc2.catalog["AcroForm"].xref, doc2.pages[0].obj.xref}
        doc2.pages[0].widgets.add(TextField("added", (10, 50, 100, 70)))
        doc2.form["added"].value = "new"
        saved2 = doc2.to_bytes(incremental=True)

    assert _defects(saved2) == set()
    with pyropdf.open(saved2) as after_doc:
        after2 = snapshot(after_doc)
        assert [xref for xref, val in before2.items() if xref not in touched and after2.get(xref) != val] == []
        assert after2[sibling_ap_xref] == before[sibling_ap_xref]
        assert after_doc.catalog["AcroForm"]["DR"]["ExtGState"]["GS0"]["CA"] == 1.0
        assert (after_doc.pages[1].text(), bytes(after_doc.pages[1].pixmap(dpi=72))) == page1_state
