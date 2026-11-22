"""Independent profile sources and public authoring controls for #911."""

from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path

import pikepdf
import pytest

import pyropdf

_ROOT = Path(__file__).resolve().parents[1]
_SOURCES = Path(__file__).parent / "assets" / "profile-sources"
_FONT = _ROOT / "crates/pyropdf-engine/src/fonts/testdata/variable-latin.ttf"
_TITLE = "Two sample points"
_PDFA = "http://www.aiim.org/pdfa/ns/id/"
_PDFUA = "http://www.aiim.org/pdfua/ns/id/"
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


def flavour(profile: str) -> str:
    """Translate the specification row into a veraPDF command-line flavour."""
    family, level = profile.split("-")
    return level if family == "pdfa" else "ua" + level


def source(flavor: str, verdict: str) -> Path:
    return _SOURCES / f"{flavor}-{verdict}.pdf"


def claim(doc: pyropdf.Document, profile: str) -> str | None:
    return doc.profiles.pdfa if profile.startswith("pdfa-") else doc.profiles.pdfua


def authored(profile: str) -> pyropdf.Document:
    """Create a tagged chart using the public page, font, structure, object and XMP writers."""
    code = flavour(profile)
    ua = code.startswith("ua")
    part, level = ("1", "") if ua else (code[0], code[1:].upper())
    version = "1.7" if ua else {"1": "1.4", "2": "1.7", "3": "1.7", "4": "2.0"}[part]
    doc = pyropdf.Document(version=version)
    doc.language = "en-US"
    doc.catalog["ViewerPreferences"] = {"DisplayDocTitle": True}
    profile_fields = f'<rdf:Description rdf:about="" xmlns:pdfuaid="{_PDFUA}" pdfuaid:part="1"/>'
    if not ua:
        attributes = f'pdfaid:part="{part}"'
        if part == "4":
            attributes += ' pdfaid:rev="2020"'
        if level:
            attributes += f' pdfaid:conformance="{level}"'
        profile_fields = f'<rdf:Description rdf:about="" xmlns:pdfaid="{_PDFA}" {attributes}/>'
    doc.xmp = (
        f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="{_RDF}">{profile_fields}'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{_TITLE}</rdf:li></rdf:Alt></dc:title>'
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    if not ua:
        icc = runpy.run_path(str(Path(__file__).parent / "assets/pdfa/generate.py"))["icc"]()
        number = doc.objects.add_stream(icc, {"N": 3})
        doc.catalog["OutputIntents"] = [
            {
                "Type": pyropdf.Name("OutputIntent"),
                "S": pyropdf.Name("GTS_PDFA1"),
                "OutputConditionIdentifier": "pyropdf RGB",
                "DestOutputProfile": doc.objects[number],
            }
        ]
    if code == "4f":
        doc.attachments.add("legend.txt", b"A and V mark two observations.\n", description="Chart legend.")
    page = doc.pages.new((180, 110))
    doc.structure.add("Document")
    face = pyropdf.Font.from_file(_FONT)
    with page.draw() as draw:
        with draw.tag("P"):
            draw.text("A V", (15, 20), font=face, size=14)
        with draw.tag("Figure", alt="The blue square is below sample A."):
            draw.rect((15, 40, 40, 65), fill=pyropdf.Color(0.2, 0.5, 0.8))
    return doc


@pytest.mark.spec("{profile}", "read")
@pytest.mark.parametrize(
    "profile",
    [
        "pdfa-1a",
        "pdfa-1b",
        "pdfa-2a",
        "pdfa-2b",
        "pdfa-2u",
        "pdfa-3a",
        "pdfa-3b",
        "pdfa-3u",
        "pdfa-4",
        "pdfa-4e",
        "pdfa-4f",
        "pdfua-1",
        "pdfua-2",
    ],
)
def test_external_sources_keep_their_declared_profile(profile: str) -> None:
    flavor = flavour(profile)
    for verdict in ("pass", "fail"):
        path = source(flavor, verdict)
        with pyropdf.open(path) as doc:
            expected = ("PDF/UA-" if flavor.startswith("ua") else "PDF/A-") + flavor.removeprefix("ua").upper()
            if flavor == "4e" and verdict == "fail":
                expected = "PDF/A-4"  # The corpus control omits the E identification.
            assert claim(doc, profile) == expected
            packet = doc.xmp
            doc.pages[0].rotation = 90
            for incremental in (False, True):
                saved = doc.to_bytes(incremental=incremental)
                if incremental:
                    assert saved.startswith(path.read_bytes())
                with pyropdf.open(saved) as reopened:
                    assert claim(reopened, profile) == expected
                    assert reopened.xmp == packet
                    assert reopened.pages[0].rotation == 90


@pytest.mark.spec("{profile}", "create")
@pytest.mark.parametrize(
    "profile",
    [
        "pdfa-1a",
        "pdfa-1b",
        "pdfa-2a",
        "pdfa-2b",
        "pdfa-2u",
        "pdfa-3a",
        "pdfa-3b",
        "pdfa-3u",
        "pdfa-4e",
        "pdfa-4f",
        "pdfua-1",
    ],
)
def test_public_authoring_embeds_text_and_a_tagged_drawing(profile: str) -> None:
    flavor = flavour(profile)
    doc = authored(profile)
    original = doc.to_bytes()
    expected = ("PDF/UA-" if flavor.startswith("ua") else "PDF/A-") + flavor.removeprefix("ua").upper()
    with pyropdf.open(original) as reopened:
        assert claim(reopened, profile) == expected
        assert "A V" in reopened.pages[0].text()
        reopened.pages[0].rotation = 90
        for incremental in (False, True):
            with pyropdf.open(reopened.to_bytes(incremental=incremental)) as saved:
                assert claim(saved, profile) == expected
                assert saved.pages[0].rotation == 90
                assert saved.structure[0].role == "Document"
                assert "A V" in saved.pages[0].text()


def test_profile_source_hashes_match_the_provenance_manifest() -> None:
    manifest = json.loads((_SOURCES / "sources.json").read_text())
    assert {p.name for p in _SOURCES.glob("*.pdf")} == {row["file"] for row in manifest["files"]}
    for row in manifest["files"]:
        data = (_SOURCES / row["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"]
        with pikepdf.Pdf.open(_SOURCES / row["file"]) as reader:
            assert len(reader.pages) == 1


@pytest.mark.spec("pdfa", "read")
@pytest.mark.spec("pdfua", "read")
def test_aliases_default_namespaces_and_decoys_in_real_rdf() -> None:
    doc = pyropdf.Document()
    doc.xmp = (
        f'<rdf:RDF xmlns:rdf="{_RDF}"><rdf:Description xmlns:archive="{_PDFA}" xmlns:access="{_PDFUA}" '
        'xmlns:pdfaid="https://example.org/unrelated" pdfaid:part="8" archive:part="3" access:part="1">'
        f'<!-- <pdfaid:conformance>B</pdfaid:conformance> --><conformance xmlns="{_PDFA}">u</conformance>'
        "</rdf:Description></rdf:RDF>"
    )
    assert doc.pdfa_claim == (3, "u")
    assert doc.profiles.pdfa == "PDF/A-3U"
    assert doc.profiles.pdfua == "PDF/UA-1"
    doc.xmp = (
        f'<rdf:RDF xmlns:rdf="{_RDF}"><rdf:Description xmlns:pdfaid="https://example.org/other" pdfaid:part="3"/>'
        "</rdf:RDF>"
    )
    assert doc.pdfa_claim is None
    assert doc.profiles.pdfa is None


@pytest.mark.spec("pdfa", "create")
@pytest.mark.spec("pdfua", "create")
def test_public_writers_set_only_the_requested_identification() -> None:
    archival = pyropdf.open(authored("pdfa-2b").to_bytes())
    accessible = pyropdf.open(authored("pdfua-1").to_bytes())
    assert archival.profiles.pdfa == "PDF/A-2B"
    assert archival.profiles.pdfua is None
    assert accessible.profiles.pdfa is None
    assert accessible.profiles.pdfua == "PDF/UA-1"
