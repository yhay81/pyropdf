"""Write a tagged air-flow chart from an empty document using a font and an RGB ICC profile."""

# ruff: noqa: INP001 - standalone documentation download.
from __future__ import annotations

import argparse
from pathlib import Path

import pyropdf
from pyropdf import typed

TITLE = "Air flow across two trials"
DESCRIPTION = "Air flow fell from 2 in the first trial to 1 in the second."

# PDF/A-2 requires an extension schema for the additional PDF/UA identification property.
UA1_SCHEMA = """<rdf:Description rdf:about=""
    xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/"
    xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#"
    xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">
  <pdfaExtension:schemas><rdf:Bag><rdf:li rdf:parseType="Resource">
    <pdfaSchema:schema>PDF/UA identification</pdfaSchema:schema>
    <pdfaSchema:namespaceURI>http://www.aiim.org/pdfua/ns/id/</pdfaSchema:namespaceURI>
    <pdfaSchema:prefix>pdfuaid</pdfaSchema:prefix>
    <pdfaSchema:property><rdf:Seq><rdf:li rdf:parseType="Resource">
      <pdfaProperty:name>part</pdfaProperty:name><pdfaProperty:valueType>Integer</pdfaProperty:valueType>
      <pdfaProperty:category>internal</pdfaProperty:category>
      <pdfaProperty:description>Part of ISO 14289</pdfaProperty:description>
    </rdf:li></rdf:Seq></pdfaSchema:property>
  </rdf:li></rdf:Bag></pdfaExtension:schemas>
</rdf:Description>"""


def create(font_path: Path, rgb_profile: bytes, *, earlier: bool = False) -> pyropdf.Document:
    """Build PDF/A-4 and PDF/UA-2, or PDF/A-2a and PDF/UA-1 on the earlier PDF 1.7 base."""
    doc = pyropdf.Document(version="1.7" if earlier else "2.0")
    doc.language = "en-US"
    doc.viewer_preferences = pyropdf.ViewerPreferences(display_title=True)
    claims = (
        'pdfaid:part="2" pdfaid:conformance="A" pdfuaid:part="1"'
        if earlier
        else 'pdfaid:part="4" pdfaid:rev="2020" pdfuaid:part="2" pdfuaid:rev="2024"'
    )
    doc.xmp = f"""<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
      <rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"
        xmlns:pdfuaid="http://www.aiim.org/pdfua/ns/id/" xmlns:dc="http://purl.org/dc/elements/1.1/" {claims}>
        <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{TITLE}</rdf:li></rdf:Alt></dc:title>
      </rdf:Description>{UA1_SCHEMA if earlier else ""}</rdf:RDF></x:xmpmeta>"""
    profile = typed.ICCProfileStream(doc.objects[doc.objects.add_stream(rgb_profile)])
    profile.N = 3
    intent = typed.OutputIntents(doc.objects[doc.objects.add({})])
    intent.Type = "OutputIntent"
    intent.S = "GTS_PDFA1"
    intent.OutputConditionIdentifier = "Chart RGB"
    intent.DestOutputProfile = profile
    doc.catalog["OutputIntents"] = [intent.obj]
    root = doc.structure.add("Document")
    face = pyropdf.Font.from_file(font_path)
    page = doc.pages.new((230, 166))
    with page.draw() as draw:
        with draw.tag("H1"):
            draw.text("Air flow", (18, 32), font=face, size=22)
        with draw.tag("P"):
            draw.text("Air flow fell to 1", (18, 58), font=face, size=13)
        with draw.tag("Figure", alt=DESCRIPTION):
            color = pyropdf.Color(0.10, 0.35, 0.55)
            for value, y in ((2, 82), (1, 116)):
                draw.rect((18, y, 18 + value * 62, y + 17), fill=color)
                draw.text(str(value), (24 + value * 62, y + 14), font=face, size=13)
    if not earlier:
        namespace = doc.structure.namespaces.add(pyropdf.PDF_2_0_NAMESPACE)
        for element in (root, *root.children):
            element.namespace = namespace
    return doc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("font", type=Path)
    parser.add_argument("rgb_profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--earlier", action="store_true")
    args = parser.parse_args()
    with create(args.font, args.rgb_profile.read_bytes(), earlier=args.earlier) as document:
        document.save(args.output)
