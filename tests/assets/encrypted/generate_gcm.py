"""Recreate the ISO/TS 32003 fixture with pyHanko (fresh file key and nonces)."""

from __future__ import annotations

from pathlib import Path

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.writer import PageObject, PdfFileWriter


def generate(path: Path) -> None:
    """Write a password-encrypted page with separate string and stream objects."""
    writer = PdfFileWriter(stream_xrefs=False)
    content = writer.add_object(generic.StreamObject(stream_data=b"0 0 1 rg 1 1 6 5 re f\n"))
    page = PageObject(contents=content, media_box=(0, 0, 100, 100))
    page[generic.pdf_name("/XXLabel")] = generic.TextStringObject("independent GCM label")
    writer.insert_page(page)
    writer.encrypt("ownerpw", "userpw", use_gcm=True, pdf_mac=False)
    with path.open("wb") as out:
        writer.write(out)


if __name__ == "__main__":
    generate(Path(__file__).with_name("user-aes-gcm.pdf"))
