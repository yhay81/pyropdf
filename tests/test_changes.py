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
    assert report == pyropdf.ChangeReport((), ())
    assert not report
    assert not pyropdf.Document().changes(incremental=False)


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


def test_an_untagged_annotation_breaks_a_pdfua_claim() -> None:
    doc = _claiming('pdfuaid:part="1"')
    note = doc.objects.add({"Type": Name("Annot"), "Subtype": Name("Text"), "Rect": [0, 0, 10, 10]})
    doc.objects.add({"Type": Name("Annot"), "Subtype": Name("Text"), "Rect": [0, 0, 10, 10], "StructParent": 0})
    (claim,) = doc.changes(incremental=True).claims
    assert claim.claim == "PDF/UA-1"
    assert claim.objects == (note,)


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
