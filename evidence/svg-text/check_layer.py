# ruff: noqa: INP001 - a script, not a package.
"""Checks the text layer of the draft hayro-svg patch (#435).

Usage: python3 check_layer.py <out-dir>, after `layer` wrote it. Each SVG
is parsed with the standard library's XML parser, independently of
hayro-svg, and the text of its layer is compared with the text that
`Glyph::as_unicode` gives for the page's glyphs.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SVG = "{http://www.w3.org/2000/svg}"
# XML 1.0 forbids the controls below a space, surrogates, U+FFFE and U+FFFF.
FIRST_XML_CHAR = 0x20
SURROGATES = range(0xD800, 0xE000)
NONCHARACTERS = (0xFFFE, 0xFFFF)


def xml_text(text: str) -> str:
    """Write the text as the layer does: XML characters, whitespace as spaces."""
    out = []
    for c in text:
        code = ord(c)
        if c.isspace():
            out.append(" ")
        elif code >= FIRST_XML_CHAR and code not in SURROGATES and code not in NONCHARACTERS:
            out.append(c)
    return "".join(out)


def main(out: Path) -> int:
    """Compare every page in `out`; return 1 when one differs."""
    pages = mismatched = 0
    for svg in sorted(out.glob("*.svg")):
        pages += 1
        # The SVGs are this script's own output, not untrusted input.
        root = ET.parse(svg).getroot()  # noqa: S314
        layer = root.find(f"{SVG}g[@id='text-layer']")
        texts = [] if layer is None else list(layer.iter(f"{SVG}text"))
        got = "".join(t.text or "" for t in texts)
        positioned = all(len(t.get("x", "").split()) == len(t.text or "") for t in texts)
        expected = xml_text(svg.with_suffix(".txt").read_text())
        if got != expected or not positioned:
            mismatched += 1
            sys.stdout.write(f"{svg.name}: {len(got)} characters, expected {len(expected)}\n")
    sys.stdout.write(f"{pages} pages, {mismatched} mismatched\n")
    return 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
