"""PDF/X-4 and PDF/VT-1 print exchange through the public store, profile, and part APIs."""

from __future__ import annotations

import io

import pikepdf
import pytest
from print_exchange_fixtures import CONTENTS, ICC, break_job, edit_job, new_job, recipient

import pyropdf
from pyropdf import Name


def _print_defects(doc: pyropdf.Document) -> list[tuple[str, str]]:
    return [(item.clause, item.path) for item in doc.check().defects if item.kind == "profile"]


def _inspect_job(doc: pyropdf.Document, level: str, *, edited: bool = False) -> None:
    assert (doc.profiles.pdfx, doc.profiles.pdfvt) == ("PDF/X-4", "PDF/VT-1" if level == "vt1" else None)
    (intent,) = doc.profiles.output_intents
    assert (intent.subtype, intent.identifier, intent.registry) == ("GTS_PDFX", "FOGRA39", "http://www.color.org")
    assert intent.condition == ("FOGRA39L coated offset printing" if edited else None)
    assert intent.profile is not None
    profile = doc.objects[intent.profile]
    assert profile.read() == ICC.read_bytes()
    assert profile["N"] == 4
    assert profile.read()[12:20] == b"prtrCMYK"
    assert doc.metadata["title"] == ("Revised press records" if edited else "Two press records")
    assert tuple(page.obj["Contents"].read() for page in doc.pages) == CONTENTS
    assert all(tuple(page.trim_box) == (4, 4, 96, 96) for page in doc.pages)
    assert _print_defects(doc) == []
    if level == "vt1":
        assert doc.parts.record_level == 1
        assert doc.parts.node_names == ("Job", "Record")
        root = doc.parts.root
        assert root is not None
        assert [part.dpm for part in root.children] == [recipient("1"), recipient("replacement" if edited else "2")]
        for page, part in zip(doc.pages, root.children, strict=True):
            assert part.start_page == part.end_page == page
            assert part.obj == page.obj["DPart"]


@pytest.mark.spec("pdfx", "create")
@pytest.mark.spec("pdfvt", "create")
@pytest.mark.spec("output-intents", "create")
@pytest.mark.parametrize("level", ["x4", "vt1"])
def test_write_self_contained_press_job(level: str) -> None:
    with new_job(level) as doc:
        data = doc.to_bytes()
    assert data.startswith(b"%PDF-1.6")
    with pyropdf.open(data) as reopened, pikepdf.Pdf.open(io.BytesIO(data)) as reader:
        _inspect_job(reopened, level)
        assert reader.check_pdf_syntax() == []
        assert reader.Root.OutputIntents[0].DestOutputProfile.read_bytes() == ICC.read_bytes()
        assert tuple(page.Contents.read_bytes() for page in reader.pages) == CONTENTS
        if level == "vt1":
            (leaves,) = reader.Root.DPartRoot.DPartRootNode.DParts
            assert [leaf.Start.objgen for leaf in leaves] == [page.objgen for page in reader.pages]


@pytest.mark.spec("pdfx", "edit")
@pytest.mark.spec("pdfx", "preserve")
@pytest.mark.spec("pdfvt", "edit")
@pytest.mark.spec("pdfvt", "preserve")
@pytest.mark.spec("output-intents", "edit")
@pytest.mark.parametrize("level", ["x4", "vt1"])
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_edit_job_ticket_preserves_press_marks_and_first_record(level: str, *, incremental: bool) -> None:
    with new_job(level) as doc:
        original = doc.to_bytes()
    with pyropdf.open(original) as opened:
        pixels = tuple(page.pixmap().to_bytes() for page in opened.pages)
        edit_job(opened, level)
        encoded = opened.to_bytes(incremental=incremental)
    if incremental:
        assert encoded.startswith(original)
    with pyropdf.open(encoded) as reopened, pikepdf.Pdf.open(io.BytesIO(encoded)) as reader:
        _inspect_job(reopened, level, edited=True)
        assert reader.check_pdf_syntax() == []
        assert tuple(page.pixmap().to_bytes() for page in reopened.pages) == pixels
        assert len(set(pixels)) == 2
        assert reader.docinfo.Title == "Revised press records"


@pytest.mark.spec("pdfx", "read")
@pytest.mark.spec("pdfvt", "read")
@pytest.mark.parametrize("element", [False, True], ids=["attributes", "elements"])
def test_expanded_print_identification_names_and_foreign_namespace(*, element: bool) -> None:
    with new_job("vt1") as doc:
        assert doc.xmp is not None
        packet = doc.xmp.replace("pdfxid:", "press:").replace("xmlns:pdfxid=", "xmlns:press=")
        packet = packet.replace("pdfvtid:", "records:").replace("xmlns:pdfvtid=", "xmlns:records=")
        if element:
            packet = packet.replace('press:GTS_PDFXVersion="PDF/X-4"', "").replace(
                "<dc:format>", "<press:GTS_PDFXVersion>PDF/X-4</press:GTS_PDFXVersion><dc:format>"
            )
            packet = packet.replace('records:GTS_PDFVTVersion="PDF/VT-1"', "").replace(
                'records:GTS_PDFVTModDate="2026-01-12T00:00:00Z"/>',
                'records:GTS_PDFVTModDate="2026-01-12T00:00:00Z"><records:GTS_PDFVTVersion>'
                "PDF/VT-1</records:GTS_PDFVTVersion></rdf:Description>",
            )
        doc.xmp = packet
        assert (doc.profiles.pdfx, doc.profiles.pdfvt) == ("PDF/X-4", "PDF/VT-1")
        doc.xmp = packet.replace("http://www.npes.org/", "https://unrelated.example/")
        assert (doc.profiles.pdfx, doc.profiles.pdfvt) == (None, None)


@pytest.mark.spec("pdfx", "interpret")
@pytest.mark.spec("pdfvt", "interpret")
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("no-intent", ("PDF/X-4/OutputIntents", "OutputIntents")),
        ("duplicate-intent", ("PDF/X-4/OutputIntents", "OutputIntents")),
        ("no-icc", ("PDF/X-4/OutputIntents", "OutputIntents/0/DestOutputProfile")),
        ("external-profile", ("PDF/X-4/OutputIntents", "OutputIntents/0/DestOutputProfileRef")),
        ("no-root", ("PDF/VT-1/DPartRoot", "DPartRoot")),
        ("flat-children", ("PDF/VT-1/DParts", "DParts")),
        ("no-leaf", ("PDF/VT-1/DPart", "DPart")),
        ("wrong-leaf", ("PDF/VT-1/DPart", "DPart")),
        ("wrong-x", ("PDF/VT-1/Metadata", "Metadata")),
    ],
)
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_report_identifies_the_broken_print_rule(case: str, expected: tuple[str, str], *, incremental: bool) -> None:
    with new_job("vt1") as base:
        original = base.to_bytes()
    with pyropdf.open(original) as doc:
        break_job(doc, case)
        assert _print_defects(doc) == [expected]
        data = doc.to_bytes(incremental=incremental)
    with pyropdf.open(data) as reopened:
        assert _print_defects(reopened) == [expected]
        assert tuple(page.obj["Contents"].read() for page in reopened.pages) == CONTENTS


@pytest.mark.spec("output-intents", "create")
@pytest.mark.spec("output-intents", "edit")
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_indirect_array_keeps_other_workflows_and_flags_nontext_identifier(*, incremental: bool) -> None:
    with new_job("x4") as base:
        original = base.to_bytes()
    with pyropdf.open(original) as doc:
        press = doc.catalog["OutputIntents"][0]
        doc.catalog["OutputIntents"] = doc.objects[doc.objects.add([{"S": Name("Custom")}, press])]
        assert [intent.subtype for intent in doc.profiles.output_intents] == ["Custom", "GTS_PDFX"]
        assert not _print_defects(doc)
        doc.catalog["OutputIntents"][1]["OutputConditionIdentifier"] = Name("FOGRA39")
        data = doc.to_bytes(incremental=incremental)
    with pyropdf.open(data) as reopened:
        assert [intent.subtype for intent in reopened.profiles.output_intents] == ["Custom", "GTS_PDFX"]
        assert _print_defects(reopened) == [("PDF/X-4/OutputIntents", "OutputIntents/1/OutputConditionIdentifier")]
        assert tuple(page.obj["Contents"].read() for page in reopened.pages) == CONTENTS


@pytest.mark.spec("pdfvt", "interpret")
@pytest.mark.parametrize("case", ["absent", "wrong-type", "cycle"])
def test_malformed_page_tree_still_produces_a_structural_report(case: str) -> None:
    with new_job("vt1") as doc:
        if case == "absent":
            del doc.catalog["Pages"]
        elif case == "wrong-type":
            doc.catalog["Pages"] = 42
        else:
            pages = doc.catalog["Pages"]
            pages["Kids"] = [pages]
        defects = doc.check().defects
        assert any(item.kind == "invariant" for item in defects)
        assert not any(item.kind == "profile" and item.path == "DPart" for item in defects)
