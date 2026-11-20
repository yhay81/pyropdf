"""Tagged/Artifact content association and claim reporting when flattening (#800).

Covers ``/Artifact BMC ... EMC`` wrapping for untagged annotations and widgets
flattened onto a tagged PDF/UA document, ``/OBJR`` to ``/MCR`` structure-tree
preservation when flattening tagged annotations/widgets, removal of stale claim
reports after annotations or widgets leave a page, and preservation of visual
appearance, text, existing structure, and unrelated pages.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest
from test_preservation import snapshot

import pyropdf
from pyropdf import AnnotationFlags, Array, Color, Dictionary, Font, Name, Square, Stream, TextField

ROOT = Path(__file__).resolve().parent.parent
PDFUA = Path(__file__).resolve().parent / "assets" / "pdfua" / "pdfua-1.pdf"


@cache
def japanese() -> Font:
    return Font.from_file(ROOT / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata" / "NotoSansJP-subset.otf")


def _defects(data: bytes) -> set[tuple[int | None, str, str, str]]:
    with pyropdf.open(data) as doc:
        return {(d.xref, d.path, d.clause, d.kind) for d in doc.check().defects}


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_flatten_untagged_annotation_on_pdfua_marks_artifact_and_clears_stale_claim(*, incremental: bool) -> None:
    with pyropdf.open(PDFUA) as doc:
        before_text = doc.pages[0].text()
        annot = doc.pages[0].annotations.add(
            Square((10, 10, 30, 30), interior_color=Color(0.25, 0.5, 0.75), contents="regression")
        )
        # Before flattening, the live untagged annotation is reported.
        pre_claims = doc.changes(incremental=incremental).claims
        assert len(pre_claims) == 1
        assert pre_claims[0].claim == "PDF/UA-1"
        assert pre_claims[0].objects == (annot.xref,)

        live_pixels = bytes(doc.pages[0].pixmap(dpi=72))
        doc.pages[0].flatten(widgets=False)

        # Once flattened off the page, the stale annotation report is gone.
        assert doc.changes(incremental=incremental).claims == ()
        saved = doc.to_bytes(incremental=incremental)

    assert _defects(saved) == set()
    with pyropdf.open(saved) as reopened:
        assert not reopened.pages[0].annotations
        assert before_text in reopened.pages[0].text()
        assert bytes(reopened.pages[0].pixmap(dpi=72)) == live_pixels
        flattened = reopened.pages[0].obj["Resources"]["XObject"]["Flattened"]
        assert isinstance(flattened, Stream)
        assert b"/Artifact BMC\n" in flattened.read()
        assert b"\nEMC\n" in flattened.read()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("non-interactive-forms", "preserve")
def test_flatten_untagged_widget_on_pdfua_marks_artifact_preserves_text_and_clears_claim(*, incremental: bool) -> None:
    with pyropdf.open(PDFUA) as doc:
        before_text = doc.pages[0].text()
        widget = doc.pages[0].widgets.add(TextField("pyropdf_field", (10, 50, 100, 70), label="field"))
        doc.form.font = japanese()
        doc.form["pyropdf_field"].value = "AVA"
        assert doc.changes(incremental=incremental).claims[0].objects == (widget.xref,)

        doc.pages[0].flatten(annotations=False)
        assert doc.changes(incremental=incremental).claims == ()
        saved = doc.to_bytes(incremental=incremental)

    assert _defects(saved) == set()
    with pyropdf.open(saved) as reopened:
        assert not reopened.pages[0].widgets
        assert "pyropdf_field" not in reopened.form
        text = reopened.pages[0].text()
        assert before_text in text
        assert "AVA" in text
        flattened = reopened.pages[0].obj["Resources"]["XObject"]["Flattened"]
        assert isinstance(flattened, Stream)
        assert b"/Artifact BMC\n" in flattened.read()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_deleting_annotations_or_widgets_clears_stale_claims(*, incremental: bool) -> None:
    # 1. Direct deletion via del page.annotations[0].
    with pyropdf.open(PDFUA) as doc:
        doc.pages[0].annotations.add(Square((10, 10, 30, 30)))
        assert len(doc.changes(incremental=incremental).claims) == 1
        del doc.pages[0].annotations[0]
        assert doc.changes(incremental=incremental).claims == ()

    # 2. Direct deletion via del doc.form[...] and del page.widgets[0].
    with pyropdf.open(PDFUA) as doc:
        doc.pages[0].widgets.add(TextField("f1", (10, 50, 100, 70)))
        assert len(doc.changes(incremental=incremental).claims) == 1
        del doc.form["f1"]
        assert doc.changes(incremental=incremental).claims == ()

    # 3. Annotation without /P in an opened file that is edited and then removed.
    with pyropdf.open(PDFUA) as seed:
        annot_xref = seed.objects.add({"Type": Name("Annot"), "Subtype": Name("Square"), "Rect": [10, 10, 30, 30]})
        seed.pages[0].obj["Annots"] = [seed.objects[annot_xref]]
        seeded = seed.to_bytes(incremental=True)
    with pyropdf.open(seeded) as doc2:
        doc2.pages[0].annotations[0].contents = "edited"
        assert doc2.changes(incremental=incremental).claims[0].objects == (annot_xref,)
        del doc2.pages[0].annotations[0]
        assert doc2.changes(incremental=incremental).claims == ()


def _prepare_tagged_widget_pdf() -> tuple[bytes, int, int]:
    with pyropdf.open(PDFUA) as setup:
        other = setup.pages.new((200, 200))
        with other.draw() as canvas:
            canvas.rect((20, 20, 40, 40), fill=Color(0.2, 0.4, 0.6))
        widget = setup.pages[0].widgets.add(TextField("tagged_field", (10, 80, 120, 100)))
        setup.form.font = japanese()
        setup.form["tagged_field"].value = "AVA"
        hidden = setup.pages[0].annotations.add(Square((140, 80, 160, 100)))
        hidden.flags = AnnotationFlags.HIDDEN

        struct_root = setup.catalog["StructTreeRoot"]
        assert isinstance(struct_root, Dictionary)
        doc_elem = struct_root["K"]
        if isinstance(doc_elem, Array):
            doc_elem = doc_elem[0]
        assert isinstance(doc_elem, Dictionary)

        form_elem_xref = setup.objects.add(
            {
                "Type": Name("StructElem"),
                "S": Name("Form"),
                "P": doc_elem,
                "Pg": setup.pages[0].obj,
                "K": {"Type": Name("OBJR"), "Pg": setup.pages[0].obj, "Obj": widget.obj},
            }
        )
        form_elem = setup.objects[form_elem_xref]
        hidden_elem_xref = setup.objects.add(
            {
                "Type": Name("StructElem"),
                "S": Name("Annot"),
                "P": doc_elem,
                "Pg": setup.pages[0].obj,
                "K": [{"Type": Name("OBJR"), "Pg": setup.pages[0].obj, "Obj": hidden.obj}],
            }
        )
        hidden_elem = setup.objects[hidden_elem_xref]
        existing_k = doc_elem.get("K")
        kids = list(existing_k) if isinstance(existing_k, Array) else ([existing_k] if existing_k is not None else [])
        kids.extend([form_elem, hidden_elem])
        doc_elem["K"] = kids

        parent_tree = struct_root["ParentTree"]
        assert isinstance(parent_tree, Dictionary)
        nums = list(parent_tree["Nums"])
        next_key = int(struct_root.get("ParentTreeNextKey", 1))
        nums.extend([next_key, form_elem, next_key + 1, hidden_elem])
        parent_tree["Nums"] = nums
        struct_root["ParentTreeNextKey"] = next_key + 2
        widget.obj["StructParent"] = next_key
        hidden.obj["StructParent"] = next_key + 1

        return setup.to_bytes(incremental=True), form_elem_xref, hidden_elem_xref


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("non-interactive-forms", "preserve")
def test_flatten_tagged_widget_converts_objr_to_mcr_and_preserves_other_pages(*, incremental: bool) -> None:
    prepared, form_elem_xref, hidden_elem_xref = _prepare_tagged_widget_pdf()

    with pyropdf.open(prepared) as doc:
        before = snapshot(doc)
        other_xref = doc.pages[1].obj.xref
        assert other_xref is not None
        other_state = (doc.pages[1].text(), bytes(doc.pages[1].pixmap(dpi=72)))
        doc.pages[0].flatten()
        assert doc.changes(incremental=incremental).claims == ()
        saved = doc.to_bytes(incremental=incremental)

    assert _defects(saved) == set()
    with pyropdf.open(saved) as reopened:
        assert "AVA" in reopened.pages[0].text()
        flattened = reopened.pages[0].obj["Resources"]["XObject"]["Flattened"]
        assert isinstance(flattened, Stream)
        assert b"/Form << /MCID 0 >> BDC\n" in flattened.read()
        assert isinstance(flattened.get("StructParents"), int)

        reopened_elem = reopened.objects[form_elem_xref]
        assert isinstance(reopened_elem, Dictionary)
        attr = reopened_elem["A"]
        assert isinstance(attr, Dictionary)
        assert dict(attr) == {"O": Name("PrintField"), "Role": Name("tv")}
        mcr = reopened_elem["K"]
        assert isinstance(mcr, Dictionary)
        assert mcr["Type"] == Name("MCR")
        assert mcr["Stm"].xref == flattened.xref
        assert mcr["MCID"] == 0

        reopened_hidden_elem = reopened.objects[hidden_elem_xref]
        assert isinstance(reopened_hidden_elem, Dictionary)
        assert list(reopened_hidden_elem["K"]) == []

        after = snapshot(reopened)
        assert after[other_xref] == before[other_xref]
        assert (reopened.pages[1].text(), bytes(reopened.pages[1].pixmap(dpi=72))) == other_state
