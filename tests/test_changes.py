"""The change report: what the edits since opening invalidate before a save (#157, #158)."""

from __future__ import annotations

from pathlib import Path

import pytest

import pyropdf
from pyropdf import Name

ASSETS = Path(__file__).parent / "assets" / "real_world"


def _pdf(objects: dict[int, str], signed_before: int | None = None) -> bytes:
    """Return a PDF of ``objects``; ``"BYTERANGE"`` becomes a range that ends where object ``signed_before`` begins."""
    out = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        body = objects[number]
        if signed_before is not None:
            end = offsets.get(signed_before, 0)
            body = body.replace("BYTERANGE", f"[0 {end} {end} 0]")
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _signed() -> pyropdf.Document:
    """Open a document whose signature signs objects 1 to 3, and not 4 to 6."""
    return pyropdf.open(
        _pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] /SigFlags 3 >> >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
                4: "<< /Note (unsigned) >>",
                5: "<< /FT /Sig /T (Approval) /V 6 0 R >>",
                6: "<< /Type /Sig /ByteRange BYTERANGE /Contents <00> >>",
            },
            signed_before=4,
        )
    )


def _xmp(properties: str) -> str:
    data = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f"<rdf:Description {properties}/></rdf:RDF></x:xmpmeta>"
    )
    return f"<< /Type /Metadata /Subtype /XML /Length {len(data)} >>\nstream\n{data}\nendstream"


def _claiming(properties: str) -> pyropdf.Document:
    return pyropdf.open(
        _pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /Metadata 4 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
                4: _xmp(properties),
            }
        )
    )


def test_an_unedited_document_reports_nothing_for_an_incremental_save() -> None:
    report = _signed().changes(incremental=True)
    assert report == pyropdf.ChangeReport(signatures=(), claims=())
    assert not report
    assert not pyropdf.Document().changes(incremental=False)


@pytest.mark.spec("digital-signatures", "interpret")
def test_editing_an_object_inside_a_signed_range_reports_the_signature_field() -> None:
    doc = _signed()
    doc.objects[3]["Rotate"] = 90
    (signature,) = doc.changes(incremental=True).signatures
    assert signature.field == "Approval"
    assert signature.xref == 5
    assert signature.objects == (3,)
    assert "signs" in signature.reason


def test_edits_that_touch_no_signed_object_report_no_signature() -> None:
    doc = _signed()
    doc.objects[4]["Note"] = pyropdf.String(b"changed")
    doc.objects.add({"Added": True})
    assert doc.changes(incremental=True).signatures == ()


def test_a_save_that_is_not_incremental_invalidates_every_signature() -> None:
    doc = _signed()
    (signature,) = doc.changes(incremental=False).signatures
    assert signature.field == "Approval"
    assert signature.objects == ()
    assert "not incremental" in signature.reason


def test_undoing_the_edit_withdraws_the_signature() -> None:
    doc = _signed()
    doc.objects[3]["Rotate"] = 90
    assert doc.changes(incremental=True).signatures
    doc.history.undo()
    assert not doc.changes(incremental=True)


def test_an_open_transaction_reports_what_it_wrote() -> None:
    doc = _signed()
    with doc.transaction():
        doc.objects[2]["Count"] = 1
        assert doc.changes(incremental=True).signatures[0].objects == (2,)


@pytest.mark.parametrize(
    ("name", "field"), [("bill-hr815.pdf", "USGPOSignature"), ("f1040.pdf", "Perms/UR3")], ids=["docmdp", "ur3"]
)
@pytest.mark.spec("digital-signatures", "read")
def test_corpus_signatures_are_reported(name: str, field: str) -> None:
    doc = pyropdf.open(ASSETS / name)
    assert not doc.changes(incremental=True)
    assert [signature.field for signature in doc.changes(incremental=False).signatures] == [field]
    doc.catalog["Edited"] = True
    (signature,) = doc.changes(incremental=True).signatures
    assert signature.field == field
    assert signature.objects == (doc.trailer["Root"].xref,)


@pytest.mark.parametrize(
    "transparency",
    [
        {"Type": Name("ExtGState"), "ca": 0.5},
        {"Type": Name("ExtGState"), "BM": Name("Multiply")},
        {"Type": Name("ExtGState"), "SMask": {"S": Name("Luminosity")}},
    ],
    ids=["alpha", "blend", "soft-mask"],
)
@pytest.mark.spec("pdfa", "interpret")
def test_transparency_breaks_a_pdfa_1_claim(transparency: dict[str, object]) -> None:
    doc = _claiming('xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/" pdfaid:part="1" pdfaid:conformance="B"')
    state = doc.objects.add(transparency)
    page = doc.objects[3]
    page["Resources"] = {"ExtGState": {"G0": doc.objects[state]}}
    (claim,) = doc.changes(incremental=True).claims
    assert claim.claim == "PDF/A-1B"
    assert claim.objects == (state,)
    assert "PDF/A-1" in claim.reason


def test_edits_without_transparency_keep_a_pdfa_1_claim() -> None:
    doc = _claiming('pdfaid:part="1" pdfaid:conformance="B"')
    doc.objects[3]["Rotate"] = 90
    doc.objects.add({"Type": Name("ExtGState"), "ca": 1, "BM": Name("Normal"), "SMask": Name("None")})
    assert doc.changes(incremental=True).claims == ()


def test_a_later_pdfa_part_allows_transparency() -> None:
    doc = _claiming('pdfaid:part="2" pdfaid:conformance="B"')
    doc.objects.add({"Type": Name("ExtGState"), "ca": 0.5})
    assert doc.changes(incremental=True).claims == ()


def _pdfa_file(part: str) -> bytes:
    """Return the start of a PDF whose metadata claims PDF/A part ``part``."""
    return f'%PDF-1.7\n<rdf:Description pdfaid:part="{part}" pdfaid:conformance="B"/>'.encode()


@pytest.mark.spec("pdfa", "interpret")
def test_an_embedded_file_breaks_a_pdfa_1_claim() -> None:
    doc = _claiming('pdfaid:part="1" pdfaid:conformance="B"')
    doc.attachments.add("a.pdf", _pdfa_file("1"))
    (claim,) = doc.changes(incremental=True).claims
    assert claim.claim == "PDF/A-1B"
    assert doc.catalog["Names"]["EmbeddedFiles"]["Names"][1].xref in claim.objects
    assert "embedded file" in claim.reason


@pytest.mark.spec("pdfa", "interpret")
@pytest.mark.parametrize(
    ("properties", "claimed"), [('pdfaid:part="2" pdfaid:conformance="U"', "PDF/A-2U"), ('pdfaid:part="4"', "PDF/A-4")]
)
def test_an_embedded_file_that_is_not_pdfa_breaks_a_pdfa_2_or_4_claim(properties: str, claimed: str) -> None:
    doc = _claiming(properties)
    doc.attachments.add("a.txt", b"data")
    doc.attachments.add("b.pdf", _pdfa_file("3"))
    (claim,) = doc.changes(incremental=True).claims
    assert claim.claim == claimed
    files = doc.catalog["Names"]["EmbeddedFiles"]["Names"]
    assert claim.objects == (files[1]["EF"]["F"].xref, files[3]["EF"]["F"].xref)
    assert "embedded file that is not PDF/A-1" in claim.reason


@pytest.mark.spec("pdfa", "interpret")
@pytest.mark.parametrize(
    ("properties", "data"),
    [
        ('pdfaid:part="2" pdfaid:conformance="B"', _pdfa_file("1")),
        ('pdfaid:part="2" pdfaid:conformance="B"', _pdfa_file("2")),
        ('pdfaid:part="4"', _pdfa_file("4")),
        ('pdfaid:part="4" pdfaid:conformance="F"', b"data"),
        ('pdfaid:part="3" pdfaid:conformance="B"', b"data"),
    ],
    ids=["2-with-1", "2-with-2", "4-with-4", "4f", "3"],
)
def test_embedded_files_that_a_pdfa_part_allows_keep_its_claim(properties: str, data: bytes) -> None:
    doc = _claiming(properties)
    doc.attachments.add("a.bin", data)
    assert doc.changes(incremental=True).claims == ()


def test_metadata_that_starts_with_a_property_name_claims_nothing() -> None:
    doc = _claiming("")
    doc.objects[4].write(b"pdfaid:part")
    doc.objects.add({"ca": 0.5})
    assert doc.changes(incremental=True).claims == ()


@pytest.mark.spec("pdfua", "interpret")
def test_an_untagged_annotation_breaks_a_pdfua_claim() -> None:
    doc = _claiming('pdfuaid:part="1"')
    note = doc.objects.add({"Type": Name("Annot"), "Subtype": Name("Text"), "Rect": [0, 0, 10, 10]})
    doc.objects.add({"Type": Name("Annot"), "Subtype": Name("Text"), "Rect": [0, 0, 10, 10], "StructParent": 0})
    (claim,) = doc.changes(incremental=True).claims
    assert claim.claim == "PDF/UA-1"
    assert claim.objects == (note,)


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("direct", [False, True], ids=["property", "catalog"])
def test_removing_the_language_needed_by_alternate_text_breaks_pdfua(*, incremental: bool, direct: bool) -> None:
    doc = pyropdf.open(ASSETS.parent / "pdfua" / "pdfua-1.pdf")
    doc.catalog["Unrelated"] = True
    assert doc.changes(incremental=incremental).claims == ()
    if direct:
        del doc.catalog["Lang"]
    else:
        del doc.language
    (claim,) = doc.changes(incremental=incremental).claims
    assert claim.claim == "PDF/UA-1"
    assert claim.objects == (doc.catalog.xref,)
    assert "language" in claim.reason

    doc.history.undo()
    assert doc.changes(incremental=incremental).claims == ()
    doc.history.redo()
    doc.language = "fr-CA"
    assert doc.changes(incremental=incremental).claims == ()


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("on_parent", [False, True], ids=["figure", "ancestor"])
def test_structure_language_can_supply_alternate_text_language(*, incremental: bool, on_parent: bool) -> None:
    doc = pyropdf.open(ASSETS.parent / "pdfua" / "pdfua-1.pdf")
    root = doc.catalog["StructTreeRoot"]["K"][0]
    element = root if on_parent else root["K"][0]
    element["Lang"] = pyropdf.String(b"en-US")
    # Give the XMP title an explicit language as well (ISO 14289-1 §7.2).
    assert doc.xmp is not None
    doc.xmp = doc.xmp.replace('xml:lang="x-default"', 'xml:lang="en-US"')
    del doc.language
    assert doc.changes(incremental=incremental).claims == ()


def test_edit_to_a_catalog_that_already_lacked_language_is_not_a_language_loss() -> None:
    doc = pyropdf.open(ASSETS.parent / "pdfua" / "pdfua-1.pdf")
    del doc.language
    reopened = pyropdf.open(doc.to_bytes())
    reopened.catalog["Unrelated"] = True
    assert reopened.changes(incremental=True).claims == ()


def test_claims_are_read_from_elements_and_exact_names() -> None:
    doc = _claiming('notpdfaid:part="1"><pdfaid:part> 1 </pdfaid:part><pdfaid:conformance>a</pdfaid:conformance')
    doc.objects.add({"ca": 0.5})
    assert [claim.claim for claim in doc.changes(incremental=True).claims] == ["PDF/A-1A"]


def test_incremental_must_be_a_bool() -> None:
    doc = pyropdf.Document()
    with pytest.raises(TypeError, match="incremental"):
        doc.changes(incremental=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        doc.changes()  # ty: ignore[missing-argument]


def test_a_closed_document_refuses_a_report() -> None:
    doc = pyropdf.Document()
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        doc.changes(incremental=True)


# The independent machine rule requires a nonempty label in the widget's structure
# parent or a Contents entry. A bare Artifact association supplies neither.
WIDGET_LABELS = {"none": False, "empty": False, "label": True, "legacy": True, "namespace": True, "other": False}


def tagged_signature_widget(label: str) -> bytes:
    """Attach the invisible signature widget to an Artifact through a real OBJR and ParentTree entry."""
    with pyropdf.open(ASSETS.parent / "profile-sources/ua2-pass.pdf") as doc:
        widget = doc.objects[doc.prepare_signature("Review")]
        widget["Contents"] = "Approval by the reviewer"
        root = doc.catalog["StructTreeRoot"]
        parent = root["K"]
        page = doc.pages[0].obj
        key = root["ParentTreeNextKey"]
        widget["StructParent"] = key
        objr = {"Type": Name("OBJR"), "Pg": page, "Obj": widget}
        artifact = doc.objects[
            doc.objects.add(
                {
                    "Type": Name("StructElem"),
                    "S": Name("Artifact"),
                    "P": parent,
                    "NS": parent["NS"],
                    "Pg": page,
                    "K": objr,
                }
            )
        ]
        parent["K"] = [parent["K"], artifact]
        root["ParentTree"]["Nums"].extend([key, artifact])
        root["ParentTreeNextKey"] = key + 1
        if label != "none":
            child = doc.objects[
                doc.objects.add(
                    {"Type": Name("StructElem"), "S": Name("Lbl"), "P": artifact, "NS": parent["NS"], "Pg": page}
                )
            ]
            artifact["K"] = [objr, child]
            if label != "empty":
                child["K"] = 1
                root["ParentTree"]["Nums"][1].append(child)
                stream = doc.objects.add_stream(b"/Lbl <</MCID 1>> BDC BT /TT0 10 Tf 72 650 Td (test) Tj ET EMC")
                page["Contents"] = [page["Contents"], doc.objects[stream]]
            if label == "other":
                child["S"] = Name("Span")
            if label in {"legacy", "namespace"}:
                child["S"] = Name("ReviewLabel")
                if label == "legacy":
                    del child["NS"]
                    root["RoleMap"] = {"ReviewLabel": Name("Lbl")}
                else:
                    ns = doc.objects[
                        doc.objects.add(
                            {
                                "Type": Name("Namespace"),
                                "NS": "https://example.org/review-tags",
                                "RoleMapNS": {"ReviewLabel": [Name("Lbl"), parent["NS"]]},
                            }
                        )
                    ]
                    root["Namespaces"].append(ns)
                    child["NS"] = ns
        return doc.to_bytes()


@pytest.mark.parametrize(("label", "has_label"), WIDGET_LABELS.items())
def test_an_artifact_parent_supplies_a_widget_label_only_through_its_labeled_content(
    label: str, *, has_label: bool
) -> None:
    with pyropdf.open(tagged_signature_widget(label)) as doc:
        widget = doc.form["Review"].obj
        del widget["Contents"]
        for incremental in (False, True):
            losses = doc.changes(incremental=incremental).claims
            if has_label:
                assert losses == ()
            else:
                (loss,) = losses
                assert loss.claim == "PDF/UA-2"
                assert loss.objects == (widget.xref,)
                assert "description" in loss.reason


def test_changing_the_label_role_names_the_changed_structure_element() -> None:
    with pyropdf.open(tagged_signature_widget("label")) as original:
        del original.form["Review"].obj["Contents"]
        baseline = original.to_bytes()
    with pyropdf.open(baseline) as doc:
        label = doc.catalog["StructTreeRoot"]["K"]["K"][1]["K"][1]
        label["S"] = Name("Span")
        for incremental in (False, True):
            (loss,) = doc.changes(incremental=incremental).claims
            assert loss.objects == (label.xref,)
            assert "description" in loss.reason


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("tabs", [False, True])
@pytest.mark.parametrize("described", [False, True])
def test_invisible_signature_widget_reports_the_description_and_page_tab_order(
    *, incremental: bool, tabs: bool, described: bool
) -> None:
    with pyropdf.open(ASSETS.parent / "profile-sources/ua2-pass.pdf") as doc:
        page = doc.pages[0].obj
        field = doc.prepare_signature("Review")
        if not tabs:
            del page["Tabs"]
        if described:
            doc.objects[field]["Contents"] = "Approval by the reviewer"
        losses = doc.changes(incremental=incremental).claims
        assert all(loss.claim == "PDF/UA-2" for loss in losses)
        expected = {}
        if not tabs:
            expected["the page containing a widget lacks a valid tab order"] = (page.xref,)
        if not described:
            expected["the invisible widget lacks a description in Contents"] = (field,)
        assert {loss.reason: loss.objects for loss in losses} == expected

        page["Tabs"] = Name("W")
        doc.objects[field]["Contents"] = "Reviewed for publication"
        assert doc.changes(incremental=incremental).claims == ()


@pytest.mark.parametrize("incremental", [False, True])
def test_unchanged_widget_defects_do_not_reappear_on_unrelated_edits(*, incremental: bool) -> None:
    with pyropdf.open(ASSETS.parent / "profile-sources/ua2-pass.pdf") as doc:
        doc.prepare_signature("Review")
        del doc.pages[0].obj["Tabs"]
        saved = doc.to_bytes()
    with pyropdf.open(saved) as reopened:
        reopened.pages[0].rotation = 90
        assert reopened.changes(incremental=incremental).claims == ()


def test_adding_a_described_widget_does_not_reintroduce_an_existing_page_tab_defect() -> None:
    with pyropdf.open(ASSETS.parent / "profile-sources/ua2-pass.pdf") as source:
        source.pages[0].annotations.add(pyropdf.Square((20, 20, 40, 40)))
        del source.pages[0].obj["Tabs"]
        saved = source.to_bytes()
    with pyropdf.open(saved) as doc:
        field = doc.prepare_signature("Review")
        doc.objects[field]["Contents"] = "Approval by the reviewer"
        assert doc.changes(incremental=True).claims == ()


def test_resizing_an_already_undescribed_widget_does_not_report_a_new_description_loss() -> None:
    with pyropdf.open(ASSETS.parent / "profile-sources/ua2-pass.pdf") as source:
        field = source.prepare_signature("Review")
        source.objects[field]["Rect"] = [14, 17, 48, 31]
        saved = source.to_bytes()
    with pyropdf.open(saved) as doc:
        doc.objects[field]["Rect"] = [14, 17, 14, 17]
        for incremental in (False, True):
            assert doc.changes(incremental=incremental).claims == ()


def test_invisible_widget_reports_the_changed_indirect_description_or_annotation_array() -> None:
    with pyropdf.open(ASSETS.parent / "profile-sources/ua2-pass.pdf") as doc:
        page = doc.pages[0].obj
        annotations = doc.objects.add([])
        page["Annots"] = doc.objects[annotations]
        del page["Tabs"]
        empty = doc.to_bytes()
    with pyropdf.open(empty) as doc:
        field = doc.prepare_signature("Review")
        description = doc.objects.add([])
        doc.objects[field]["Contents"] = doc.objects[description]
        doc.objects[description] = "Approval by the reviewer"
        (tab_loss,) = doc.changes(incremental=True).claims
        assert "tab order" in tab_loss.reason
        assert tab_loss.objects == (annotations,)
        doc.pages[0].obj["Tabs"] = Name("A")
        baseline = doc.to_bytes(incremental=True)
    with pyropdf.open(baseline) as doc:
        doc.objects[description] = None
        (description_loss,) = doc.changes(incremental=True).claims
        assert "description" in description_loss.reason
        assert description_loss.objects == (description,)
        doc.history.undo()
        assert doc.changes(incremental=True).claims == ()


@pytest.mark.parametrize("flavour", ["ua1", "ua2"])
def test_visible_widgets_and_pdfua1_keep_the_untagged_annotation_warning(flavour: str) -> None:
    with pyropdf.open(ASSETS.parent / f"profile-sources/{flavour}-pass.pdf") as doc:
        field = doc.prepare_signature("Review")
        widget = doc.objects[field]
        widget["Contents"] = "Approval by the reviewer"
        widget["Rect"] = [4, 8, 4, 8] if flavour == "ua1" else [4, 8, 4, 20]
        losses = doc.changes(incremental=True).claims
        assert any("not in the structure tree" in loss.reason and loss.objects == (field,) for loss in losses)


@pytest.mark.parametrize("incremental", [False, True])
def test_preexisting_untagged_annotation_is_not_reported_again(*, incremental: bool) -> None:
    with pyropdf.open(ASSETS.parent / "pdfua/pdfua-2.pdf") as source:
        item = source.pages[0].annotations.add(pyropdf.Square((10, 10, 20, 20)))
        number = item.xref
        data = source.to_bytes()
    with pyropdf.open(data) as doc:
        doc.objects[number]["Contents"] = pyropdf.String(b"Edited")
        assert doc.changes(incremental=incremental).claims == ()


@pytest.mark.parametrize("incremental", [False, True])
def test_xmp_edit_can_remove_just_one_of_two_profile_identifications(*, incremental: bool) -> None:
    with pyropdf.open(ASSETS.parent / "pdfua/pdfua-2-pdfa-4.pdf") as doc:
        metadata = doc.catalog["Metadata"]
        metadata.write(metadata.read().replace(b"<pdfaid:part>4</pdfaid:part>", b""))
        (loss,) = doc.changes(incremental=incremental).claims
        assert (loss.claim, loss.objects) == ("PDF/A-4", (metadata.xref,))
        assert "XMP" in loss.reason


@pytest.mark.parametrize("incremental", [False, True])
def test_a_removed_intent_does_not_break_a_device_independent_drawing(*, incremental: bool) -> None:
    with pyropdf.open(ASSETS.parent / "pdfa/pdfa-2b.pdf") as source:
        source.pages[0].obj["Contents"].write(b"")
        data = source.to_bytes()
    with pyropdf.open(data) as doc:
        del doc.catalog["OutputIntents"]
        assert doc.changes(incremental=incremental).claims == ()


def test_profile_dependencies_share_the_document_operation_budget() -> None:
    with pyropdf.open(ASSETS.parent / "pdfua/pdfua-2-pdfa-4.pdf") as source:
        # A large tagged page stream is only decoded to determine whether the
        # edited output intent was required. Opening and editing stay cheap.
        stream = source.pages[0].obj["Contents"]
        stream.write(stream.read() + b" " * 100_000)
        data = source.to_bytes()
    with pyropdf.open(data, limits=pyropdf.DocumentLimits(max_page_content_size=5000)) as doc:
        del doc.catalog["OutputIntents"]
        with pytest.raises(pyropdf.LimitError) as refusal:
            doc.changes(incremental=True)
        assert refusal.value.code == "page_content_size"
        assert "OutputIntents" not in doc.catalog
        doc.history.undo()
        assert doc.changes(incremental=True).claims == ()
