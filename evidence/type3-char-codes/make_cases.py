# ruff: noqa: INP001 - a script, not a package.
"""Write the Type 3 mapping cases of issue #437 to one PDF.

Run: ``python3 evidence/type3-char-codes/make_cases.py <out.pdf>``.
Each case shows codes 65 and 66 with its own Type 3 font, in CASES order;
the probe lists glyphs in the same order.
"""

import sys
from pathlib import Path

# name, /Differences entries, ToUnicode bfchar entries (code -> UTF-16BE hex)
CASES = [
    ("custom names, no ToUnicode", "65 /C1 /C2", None),
    ("standard names", "65 /A /B", None),
    ("uniXXXX and AGL non-Latin names", "65 /uni3042 /afii10017", None),
    ("valid ToUnicode over custom names", "65 /C1 /C2", {65: "0042", 66: "3042"}),
    ("ToUnicode to U+0000 over standard names", "65 /A /B", {65: "0000", 66: "0000"}),
    ("suffixed and malformed names", "65 /A.swash /uni004", None),
]

GLYPH = b"0 0 m 10 0 l 10 40 l h f"


def build() -> bytes:
    """Return the PDF bytes, one font object per case."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    proc = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(GLYPH), GLYPH))
    fonts = []
    for _, differences, to_unicode in CASES:
        names = differences.split()[1:]
        procs = b" ".join(b"%s %d 0 R" % (n.encode(), proc) for n in names)
        extra = b""
        if to_unicode is not None:
            lines = b"\n".join(b"<%02X> <%s>" % (c, u.encode()) for c, u in to_unicode.items())
            cmap = (
                b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                b"/CMapName /Case def 1 begincodespacerange <00> <FF> endcodespacerange\n"
                b"%d beginbfchar\n%s\nendbfchar endcmap CMapName currentdict /CMap "
                b"defineresource pop end end" % (len(to_unicode), lines)
            )
            cmap_obj = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap))
            extra = b" /ToUnicode %d 0 R" % cmap_obj
        fonts.append(
            add(
                b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 10 40] "
                b"/FontMatrix [0.02 0 0 0.02 0 0] /CharProcs << %s >> "
                b"/Encoding << /Type /Encoding /Differences [%s] >> "
                b"/FirstChar 65 /LastChar 66 /Widths [18 18] /Resources << >>%s >>"
                % (procs, differences.encode(), extra)
            )
        )
    content = (
        b"BT\n"
        + b"".join(b"/F%d 10 Tf 1 0 0 1 20 %d Tm (AB) Tj\n" % (i, 280 - 40 * i) for i in range(len(fonts)))
        + b"ET"
    )
    contents = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
    font_dict = b" ".join(b"/F%d %d 0 R" % (i, f) for i, f in enumerate(fonts))
    n = len(objects)
    page = n + 1
    pages = n + 2
    catalog = n + 3
    add(
        b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 200 300] /Contents %d 0 R "
        b"/Resources << /Font << %s >> >> >>" % (pages, contents, font_dict)
    )
    add(b"<< /Type /Pages /Count 1 /Kids [%d 0 R] >>" % page)
    add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages)

    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer << /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog,
        xref,
    )
    return bytes(out)


if __name__ == "__main__":
    Path(sys.argv[1]).write_bytes(build())
