"""A two-page, self-contained CMYK print job shared by the public tests and the external validators."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyropdf
from pyropdf import Name

ASSETS = Path(__file__).parent / "assets" / "print-exchange"
ICC = ASSETS / "FOGRA39L-Coated.icc"
CONTENTS = (b"q 1 0 0 0 k 10 10 80 80 re f Q", b"q 0 1 0 0 k 10 10 80 80 re f Q")
VT_IDENTIFICATION = """<rdf:Description rdf:about="" xmlns:pdfvtid="http://www.npes.org/pdfvt/ns/id/"
    pdfvtid:GTS_PDFVTVersion="PDF/VT-1" pdfvtid:GTS_PDFVTModDate="2026-01-12T00:00:00Z"/>"""


def recipient(identifier: str) -> dict[str, object]:
    """Return one recipient identifier in the CIP4 document-part metadata structure."""
    return {"CIP4_Root": {"CIP4_Recipient": {"CIP4_UniqueID": identifier}}}


def new_job(level: str) -> pyropdf.Document:
    """Create PDF/X-4 press pages, with a recipient record for each page in PDF/VT-1."""
    doc = pyropdf.Document(version="1.6")
    creation = datetime(2026, 1, 12, tzinfo=UTC)
    doc.metadata = {
        "title": "Two press records",
        "creator": "pyropdf",
        "producer": "pyropdf",
        "trapped": "False",
        "created": creation,
        "modified": creation,
    }
    doc.trailer["Info"]["GTS_PDFXVersion"] = "PDF/X-4"
    packet = (ASSETS / "job.xmp").read_text(encoding="utf-8")
    doc.xmp = packet.replace("<!-- VARIABLE_RECORDS -->", VT_IDENTIFICATION if level == "vt1" else "")
    icc = doc.objects.add_stream(ICC.read_bytes(), {"N": 4})
    doc.catalog["OutputIntents"] = [
        {
            "Type": Name("OutputIntent"),
            "S": Name("GTS_PDFX"),
            "OutputConditionIdentifier": "FOGRA39",
            "RegistryName": "http://www.color.org",
            "Info": "FOGRA39L Coated",
            "DestOutputProfile": doc.objects[icc],
        }
    ]
    for index, content in enumerate(CONTENTS):
        page = doc.pages.new((100, 100))
        page.trim_box = (4, 4, 96, 96)
        page.obj["Contents"] = doc.objects[doc.objects.add_stream(content)]
        if level == "vt1":
            part = doc.parts.add_part(page, dpm=recipient(str(index + 1)))
            page.obj["DPart"] = part.obj
    if level == "vt1":
        doc.parts.record_level = 1
        doc.parts.node_names = ["Job", "Record"]
    return doc


def edit_job(doc: pyropdf.Document, level: str) -> None:
    """Revise the job ticket, retaining the printing condition, page marks and first recipient."""
    doc.trailer["Info"]["Title"] = "Revised press records"
    assert doc.xmp is not None
    doc.xmp = doc.xmp.replace("Two press records", "Revised press records").replace(
        "bd585830-0675-49aa-b06e-0fb989e4a2c5",
        "74ce42ab-3553-48bd-8e67-8d42ec19aa5f",
    )
    doc.catalog["OutputIntents"][0]["OutputCondition"] = "FOGRA39L coated offset printing"
    if level == "vt1":
        root = doc.parts.root
        assert root is not None
        root.children[1].dpm = recipient("replacement")


def break_job(doc: pyropdf.Document, case: str) -> None:
    """Apply one isolated malformed change to a conforming job."""
    if case == "no-intent":
        del doc.catalog["OutputIntents"]
    elif case == "no-icc":
        del doc.catalog["OutputIntents"][0]["DestOutputProfile"]
    elif case == "no-root":
        del doc.catalog["DPartRoot"]
    elif case == "no-leaf":
        del doc.pages[0].obj["DPart"]
    elif case == "wrong-leaf":
        doc.pages[0].obj["DPart"] = doc.pages[1].obj["DPart"]
    elif case == "flat-children":
        root = doc.catalog["DPartRoot"]["DPartRootNode"]
        root["DParts"] = list(root["DParts"][0])
    elif case == "duplicate-intent":
        intent = doc.catalog["OutputIntents"][0]
        doc.catalog["OutputIntents"] = [intent, intent]
    elif case == "external-profile":
        doc.catalog["OutputIntents"][0]["DestOutputProfileRef"] = {"ProfileName": "External"}
    elif case == "wrong-x":
        assert doc.xmp is not None
        doc.xmp = doc.xmp.replace('GTS_PDFXVersion="PDF/X-4"', 'GTS_PDFXVersion="PDF/X-4p"')
    else:
        raise ValueError(case)
