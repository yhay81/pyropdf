"""Existing rich fields whose embedded PDF character codes differ from glyph IDs."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pytest

import pyropdf

_FONTS = Path(__file__).resolve().parents[1] / "crates/pyropdf-engine/src/fonts/testdata"
Name = pikepdf.Name
Dictionary = pikepdf.Dictionary


def _embedded(
    pdf: pikepdf.Pdf, family: str, name: str, filename: str, entries: list[tuple[int, int, str, int]]
) -> pikepdf.Object:
    """Embed a supplied TrueType face using independent PDF CIDs, glyph indices and widths."""
    program = (_FONTS / filename).read_bytes()
    file = pdf.make_stream(program)
    file.Length1 = len(program)
    descriptor = pdf.make_indirect(
        Dictionary(
            Type=Name.FontDescriptor,
            FontName=Name("/" + name),
            FontFamily=family,
            Flags=4,
            FontBBox=[-400, -300, 1400, 1300],
            ItalicAngle=0,
            Ascent=1000,
            Descent=-300,
            CapHeight=750,
            StemV=80,
            FontFile2=file,
        )
    )
    mapping = bytearray(2 * (max(row[0] for row in entries) + 1))
    widths = pikepdf.Array()
    for cid, gid, _, width in entries:
        mapping[2 * cid : 2 * cid + 2] = gid.to_bytes(2, "big")
        widths.extend([cid, pikepdf.Array([width])])
    descendant = pdf.make_indirect(
        Dictionary(
            Type=Name.Font,
            Subtype=Name.CIDFontType2,
            BaseFont=Name("/" + name),
            CIDSystemInfo=Dictionary(Registry="Adobe", Ordering="Identity", Supplement=0),
            FontDescriptor=descriptor,
            CIDToGIDMap=pdf.make_stream(bytes(mapping)),
            DW=1000,
            W=widths,
        )
    )
    pairs = "\n".join(f"<{cid:04X}> <{ord(char):04X}>" for cid, _, char, _ in entries)
    cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Fixture-UCS def\n/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(entries)} beginbfchar\n{pairs}\nendbfchar\nendcmap\n"
        "CMapName currentdict /CMap defineresource pop\nend\nend\n"
    )
    return pdf.make_indirect(
        Dictionary(
            Type=Name.Font,
            Subtype=Name.Type0,
            BaseFont=Name("/" + name),
            Encoding=Name("/Identity-H"),
            DescendantFonts=[descendant],
            ToUnicode=pdf.make_stream(cmap.encode("ascii")),
        )
    )


def _producer() -> bytes:
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(245, 85))
        # Glyph IDs and advances come from the two committed fonts' cmap and hmtx tables.
        latin = _embedded(
            pdf,
            "Variable Latin",
            "VariableLatin-Regular",
            "variable-latin.ttf",
            [(77, 1, "A", 1000), (78, 2, "V", 1000)],
        )
        hebrew = _embedded(
            pdf,
            "Noto Sans Hebrew",
            "NotoSansHebrew-Regular",
            "NotoSansHebrew-subset.ttf",
            [(101, 9, "ש", 703), (102, 7, "ל", 510), (103, 11, "ו", 260), (104, 5, "ם", 652)],
        )
        helvetica = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
        resources = Dictionary(Font=Dictionary(Alpha=latin, Beta=hebrew, Helv=helvetica))
        widget = pdf.make_indirect(
            Dictionary(
                Type=Name.Annot,
                Subtype=Name.Widget,
                FT=Name.Tx,
                T="mixed",
                F=4,
                Ff=1 << 25,
                Rect=[3, 3, 238, 75],
                V="AVשלום",
                DS="font:16pt Helvetica",
                P=page.obj,
            )
        )
        widget.RV = (
            '<body xmlns="http://www.w3.org/1999/xhtml"><p>'
            '<span style="font-family:Missing,Variable Latin,Helvetica">AV</span>'
            '<span style="font-family:Noto Sans Hebrew">שלום</span></p></body>'
        )
        page.Annots = [widget]
        pdf.Root.AcroForm = pdf.make_indirect(Dictionary(Fields=[widget], DR=resources, DA="/Helv 16 Tf 0 g"))
        appearance = pdf.make_stream(
            b"q BT /Alpha 16 Tf 1 0 0 1 2 42 Tm <004D004E> Tj ET "
            b"BT /Beta 16 Tf 1 0 0 1 40 42 Tm <0068006700660065> Tj ET Q"
        )
        appearance.Type, appearance.Subtype = Name.XObject, Name.Form
        appearance.BBox, appearance.Resources = [0, 0, 235, 72], resources
        widget.AP = Dictionary(N=appearance)
        output = io.BytesIO()
        pdf.save(output, min_version="1.7")
        return output.getvalue()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_rich_edit_uses_the_existing_fonts_and_their_pdf_codes(*, incremental: bool) -> None:
    raw = _producer()
    with pyropdf.open(raw) as doc:
        field = doc.form["mixed"]
        assert field.rich_text is not None
        field.rich_text = field.rich_text.replace(">AV<", ">VA<")
        assert field.value == "VAשלום"
        saved = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        form = pdf.Root.AcroForm
        field = form.Fields[0]
        assert str(field.V) == "VAשלום"
        assert str(field.DS) == "font:16pt Helvetica"
        appearance = field.AP.N
        fonts = {font.objgen for font in appearance.Resources.Font.values()}
        assert {form.DR.Font.Alpha.objgen, form.DR.Font.Beta.objgen} <= fonts
