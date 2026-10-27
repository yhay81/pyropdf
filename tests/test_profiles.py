"""Profiles: the PDF/A, PDF/UA, PDF/X, and PDF/VT claims of a document, with its output intents."""

from __future__ import annotations

import pytest
from test_pages import build_raw_pdf

import pyropdf
from pyropdf import OutputIntent, Profiles

PAGES = "<< /Type /Pages /Kids [] /Count 0 >>"


def with_xmp(xmp: str, catalog: str = "") -> pyropdf.Document:
    """Return a document whose catalog has ``xmp`` as its metadata, plus ``catalog`` entries."""
    data = xmp.encode()
    return pyropdf.open(
        build_raw_pdf(
            {
                1: f"<< /Type /Catalog /Pages 2 0 R /Metadata 3 0 R {catalog} >>",
                2: PAGES,
                3: f"<< /Type /Metadata /Subtype /XML /Length {len(data)} >>\nstream\n{xmp}\nendstream",
                4: "<< /Length 0 /N 3 >>\nstream\n\nendstream",
            }
        )
    )


ATTRIBUTES = """<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description
 pdfaid:part="2" pdfaid:conformance="b" pdfaid:amd="2005"
 pdfuaid:part='1' pdfxid:GTS_PDFXVersion="PDF/X-4" pdfvtid:GTS_PDFVTVersion="PDF/VT-1"/>
</rdf:RDF></x:xmpmeta>"""

ELEMENTS = """<rdf:Description>
 <pdfaid:part> 3 </pdfaid:part><pdfaid:conformance>U</pdfaid:conformance>
 <pdfuaid:part>2</pdfuaid:part><pdfxid:GTS_PDFXVersion>PDF/X-1:2001</pdfxid:GTS_PDFXVersion>
</rdf:Description>"""


def test_claims_written_as_attributes() -> None:
    doc = with_xmp(ATTRIBUTES)
    assert doc.profiles == Profiles("PDF/A-2B", "PDF/UA-1", "PDF/X-4", "PDF/VT-1", ())
    assert doc.pdfa_claim == (2, "b")


def test_claims_written_as_elements() -> None:
    doc = with_xmp(ELEMENTS)
    assert doc.profiles == Profiles("PDF/A-3U", "PDF/UA-2", "PDF/X-1:2001", None, ())
    assert doc.pdfa_claim == (3, "U")


def test_a_name_that_continues_a_longer_name_is_not_a_claim() -> None:
    doc = with_xmp('<rdf:Description notpdfaid:part="1" my:pdfuaid:part="1"/>')
    assert doc.profiles == Profiles(None, None, None, None, ())
    assert not doc.profiles
    assert doc.pdfa_claim is None


def test_a_part_that_is_not_a_number_is_not_a_claim() -> None:
    doc = with_xmp('<rdf:Description pdfaid:part="two" pdfuaid:part=""/>')
    assert doc.pdfa_claim is None
    assert doc.profiles.pdfa is None
    assert doc.profiles.pdfua is None


def test_output_intents() -> None:
    intents = (
        "/OutputIntents [<< /Type /OutputIntent /S /GTS_PDFA1 /OutputConditionIdentifier (sRGB IEC61966-2.1)"
        " /RegistryName (http://www.color.org) /Info <FEFF00490043004300200076003400> /DestOutputProfile 4 0 R >>"
        " 5 << /S /GTS_PDFX /OutputCondition /Coated >>]"
    )
    profiles = with_xmp(ATTRIBUTES, intents).profiles
    assert profiles.output_intents == (
        OutputIntent("GTS_PDFA1", "sRGB IEC61966-2.1", None, "http://www.color.org", "ICC v4", 4),
        OutputIntent("GTS_PDFX", None, "Coated", None, None, None),
    )
    assert bool(Profiles(None, None, None, None, profiles.output_intents[1:]))


def test_no_metadata_yields_empty_profiles() -> None:
    doc = pyropdf.open(build_raw_pdf({1: "<< /Type /Catalog /Pages 2 0 R /Metadata 2 0 R >>", 2: PAGES}))
    assert doc.profiles == Profiles(None, None, None, None, ())
    assert doc.pdfa_claim is None
    assert pyropdf.Document().profiles == Profiles(None, None, None, None, ())


def test_metadata_beyond_the_bound_raises_limit_error() -> None:
    doc = with_xmp(ATTRIBUTES + " " * (16 * 1024 * 1024))
    with pytest.raises(pyropdf.LimitError) as caught:
        _ = doc.profiles
    assert caught.value.code == "stream_data_size"
    with pytest.raises(pyropdf.LimitError):
        _ = doc.pdfa_claim


def test_a_closed_document_refuses() -> None:
    doc = with_xmp(ATTRIBUTES)
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        _ = doc.profiles
