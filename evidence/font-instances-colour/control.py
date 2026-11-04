# ruff: noqa: INP001, T201, S603, S607, D103, E501 - measurement scripts.
"""Controls for the readers of this directory's measurements (issue #598).

Usage: uv run --with pypdfium2 --with pymupdf python control.py ARABIC OUT.json

- ``actual_text_over_a_glyph``: ``/ActualText`` around a real glyph, to show
  which readers honour it at all. If a reader answers ``replacement`` here and
  nothing over paths, the empty result over paths is the reader's rule and not
  a mistake in the measured files.
- ``arabic_at_the_default_instance``: the Arabic test word at the default
  instance, which pyropdf draws today, to separate what those readers make of
  pyropdf's Arabic from anything that instancing does.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf
import pypdfium2

import pyropdf


def read(data: bytes) -> dict[str, str]:
    """Return what each reader extracts from the first page."""
    found = {"pyropdf": pyropdf.open(data).pages[0].text()}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "probe.pdf"
        path.write_bytes(data)
        out = Path(folder) / "out.txt"
        subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), str(out)], check=False, capture_output=True)
        found["xpdf"] = out.read_text("utf-8", "replace") if out.exists() else ""
    found["mupdf"] = pymupdf.open(stream=data, filetype="pdf")[0].get_text()
    found["pdfium"] = pypdfium2.PdfDocument(data)[0].get_textpage().get_text_bounded()
    return {reader: "".join(value.replace("\x00", "").split()) for reader, value in found.items()}


def over_a_glyph() -> bytes:
    """Return a page whose only text is ``x`` under ``/ActualText`` that says ``A``."""
    content = b"/Span<</ActualText<FEFF0041>>>BDC BT /F0 24 Tf 10 40 Td (x) Tj ET EMC"
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Count 1/Kids[3 0 R]>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]/Resources<</Font<</F0<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>/Contents 4 0 R>>",
        b"<</Length %d>>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 5\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % at for at in offsets)
    out += b"trailer\n<</Size 5/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def main() -> None:
    document = pyropdf.Document()
    page = document.pages.new((300, 100))
    with page.draw() as canvas:
        canvas.text("مرحبا", (10, 40), font=pyropdf.Font.from_file(sys.argv[1]), size=24)
    result = {
        "actual_text_over_a_glyph": read(over_a_glyph()),
        "arabic_at_the_default_instance": read(document.to_bytes()),
    }
    Path(sys.argv[2]).write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", "utf-8")
    print(result)


if __name__ == "__main__":
    main()
