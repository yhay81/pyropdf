"""Existing rich fields whose embedded PDF character codes differ from glyph IDs."""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pikepdf
import pytest

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable

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
    widths: list[object] = []
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
            W=pikepdf.Array(widths),
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
        resources = Dictionary(
            Font=Dictionary(Alpha=latin, Beta=hebrew, Helv=helvetica), ColorSpace=Dictionary(Spare=Name.DeviceRGB)
        )
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


def _codes(stream: pikepdf.Object, custom_width: int = 2) -> dict[tuple[int, int], list[int]]:
    """Read the codes as actually painted and identify the selected indirect PDF font."""
    result: dict[tuple[int, int], list[int]] = {}
    active: list[int] | None = None
    length = 2
    for operands, operator in pikepdf.parse_content_stream(stream):
        if str(operator) == "Tf":
            font = stream.Resources.Font[str(operands[0])]
            length = (2 if font.Encoding == Name("/Identity-H") else custom_width) if font.Subtype == Name.Type0 else 1
            active = result.setdefault(font.objgen, [])
        elif str(operator) in {"Tj", "TJ"}:
            assert active is not None
            parts = [operands[0]] if str(operator) == "Tj" else operands[0]
            for part in parts:
                if isinstance(part, pikepdf.String):
                    raw = bytes(part)
                    assert len(raw) % length == 0
                    active.extend(int.from_bytes(raw[i : i + length], "big") for i in range(0, len(raw), length))
    return result


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="requires Poppler")
def test_independent_producer_displays_its_two_embedded_fonts(tmp_path: Path) -> None:
    raw = _producer()
    path = tmp_path / "font-selection.pdf"
    path.write_bytes(raw)
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        form = pdf.Root.AcroForm
        codes = _codes(form.Fields[0].AP.N)
        assert codes[form.DR.Font.Alpha.objgen] == [77, 78]
        assert codes[form.DR.Font.Beta.objgen] == [104, 103, 102, 101]
    text = subprocess.run(  # noqa: S603 - the test PDF is the only input to the installed Poppler reader.
        [shutil.which("pdftotext") or "pdftotext", "-raw", str(path), "-"], check=True, capture_output=True
    ).stdout.decode()
    assert "AV" in text
    assert "שלום" in text


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("variable-text", "interpret")
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
        assert form.DR.ColorSpace.Spare == Name.DeviceRGB
        appearance = field.AP.N
        assert len(appearance.Resources.Font) == 2
        # HarfBuzz advances for VA and the visually ordered Hebrew glyphs,
        # in a 1000 unit em: 1000, 1000, 652, 260, 510, 703.
        matrix = [ops for ops, op in pikepdf.parse_content_stream(appearance) if str(op) == "Tm"]
        assert [float(m[4]) - float(matrix[0][4]) for m in matrix] == pytest.approx(
            [0, 16, 32, 42.432, 46.592, 54.752], abs=0.001
        )
        codes = _codes(appearance)
        assert codes[form.DR.Font.Alpha.objgen] == [78, 77]
        assert codes[form.DR.Font.Beta.objgen] == [104, 103, 102, 101]


def _variant(change: Callable[[pikepdf.Pdf], None]) -> bytes:
    with pikepdf.open(io.BytesIO(_producer())) as pdf:
        change(pdf)
        output = io.BytesIO()
        pdf.save(output)
        return output.getvalue()


def _edit(raw: bytes, spans: str, *, incremental: bool = False) -> bytes:
    with pyropdf.open(raw) as doc:
        doc.form["mixed"].rich_text = '<body xmlns="http://www.w3.org/1999/xhtml"><p>' + spans + "</p></body>"
        return doc.to_bytes(incremental=incremental)


def _simple_bold(pdf: pikepdf.Pdf) -> None:
    form = pdf.Root.AcroForm
    descriptor = pdf.make_indirect(Dictionary(dict(form.DR.Font.Alpha.DescendantFonts[0].FontDescriptor.items())))
    descriptor.FontWeight = 700
    descriptor.FontName = Name("/ABCDEF+ProducerPSBold")
    form.DR.Font.BoldAlias = pdf.make_indirect(
        Dictionary(
            Type=Name.Font,
            Subtype=Name.TrueType,
            BaseFont=Name("/NewBoldResource"),
            FontDescriptor=descriptor,
            FirstChar=208,
            LastChar=209,
            Widths=[1000, 1000],
            Encoding=Dictionary(BaseEncoding=Name.WinAnsiEncoding, Differences=[208, Name.A, Name.V]),
        )
    )


@pytest.mark.spec("variable-text", "interpret")
def test_nested_weight_and_descriptor_postscript_names_select_original_simple_font() -> None:
    raw = _variant(_simple_bold)
    data = _edit(
        raw,
        '<span style="font-family:Variable Latin, Helvetica">A<b>VA</b>A</span>'
        '<span style="font-family:ProducerPSBold">V</span>',
    )
    with pikepdf.open(io.BytesIO(data)) as pdf:
        form = pdf.Root.AcroForm
        codes = _codes(form.Fields[0].AP.N)
        assert codes == {form.DR.Font.Alpha.objgen: [77, 77], form.DR.Font.BoldAlias.objgen: [209, 208, 209]}
        assert str(form.Fields[0].V) == "AVAAV"
        assert b"/ActualText" in form.Fields[0].AP.N.read_bytes()


def _extended_codes(pdf: pikepdf.Pdf) -> None:
    alpha = pdf.Root.AcroForm.DR.Font.Alpha
    descriptor = alpha.DescendantFonts[0].FontDescriptor
    descriptor.FontName = Name("/ABCDEF+AlternatePS")
    # Two codes map to the same first CID, but only A00010 represents A.
    alpha.Encoding = pdf.make_stream(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        b"/CMapName /Original def /CMapType 1 def /WMode 0 def "
        b"1 begincodespacerange <000000> <FFFFFF> endcodespacerange "
        b"25 begincidchar "
        + b" ".join(f"<{0x800000 + i:06X}> 78".encode("ascii") for i in range(24))
        + b" <100001> 77 endcidchar "
        b"1 begincidrange <A00010> <A00011> 77 endcidrange endcmap "
        b"CMapName currentdict /CMap defineresource pop end end"
    )
    alpha.ToUnicode = pdf.make_stream(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        b"/CMapName /Unicode def /CMapType 2 def "
        b"1 begincodespacerange <000000> <FFFFFF> endcodespacerange "
        b"3 beginbfchar <100001> <01CD> <A00010> <0041> <A00011> <0056> endbfchar "
        b"endcmap CMapName currentdict /CMap defineresource pop end end"
    )


@pytest.mark.spec("variable-text", "interpret")
def test_nonidentity_three_byte_encoding_prefers_the_code_with_the_matching_unicode() -> None:
    raw = _variant(_extended_codes)
    data = _edit(raw, '<span style="font-family:AlternatePS">VA</span>')
    with pikepdf.open(io.BytesIO(data)) as pdf:
        form = pdf.Root.AcroForm
        assert _codes(form.Fields[0].AP.N, custom_width=3) == {form.DR.Font.Alpha.objgen: [0xA00011, 0xA00010]}
        assert b"/ActualText" not in form.Fields[0].AP.N.read_bytes()


def _broken_font(pdf: pikepdf.Pdf) -> None:
    font = pdf.Root.AcroForm.DR.Font.Alpha
    font.DescendantFonts[0].FontDescriptor.FontFile2 = pdf.make_stream(b"invalid font program")


@pytest.mark.spec("variable-text", "interpret")
def test_unavailable_font_uses_next_declared_family_and_removes_unrenderable_appearance() -> None:
    raw = _variant(_broken_font)
    for families, expected in [("Variable Latin,Helvetica", "Helvetica"), ("Variable Latin", None)]:
        output = _edit(raw, f'<span style="font-family:{families}">VA</span>')
        with pikepdf.open(io.BytesIO(output)) as pdf:
            field = pdf.Root.AcroForm.Fields[0]
            assert str(field.V) == "VA"
            if expected:
                assert {str(font.BaseFont) for _, font in field.AP.N.Resources.Font.items()} == {"/" + expected}
            else:
                assert "/AP" not in field or "/N" not in field.AP


@pytest.mark.spec("variable-text", "interpret")
def test_long_family_and_resource_cross_product_refuses_before_publishing() -> None:
    def aliases(pdf: pikepdf.Pdf) -> None:
        fonts = pdf.Root.AcroForm.DR.Font
        for i in range(180):
            fonts[f"/Alias{i}"] = fonts.Alpha

    with pyropdf.open(_variant(aliases)) as doc:
        field = doc.form["mixed"]
        before = field.rich_text
        families = "a-family-that-does-not-exist," * 2200
        with pytest.raises(pyropdf.LimitError) as caught:
            field.rich_text = (
                f'<body xmlns="http://www.w3.org/1999/xhtml"><p style="font-family:{families}Helvetica">V</p></body>'
            )
        assert caught.value.code == "interpretation_fuel"
        assert field.rich_text == before
        assert field.value == "AVשלום"


@pytest.mark.skipif(not shutil.which("pdftoppm") or not shutil.which("pdftotext"), reason="requires Poppler")
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("variable-text", "interpret")
def test_edited_text_and_raster_match_independent_font_stream(tmp_path: Path, *, incremental: bool) -> None:
    raw = _producer()
    result = _edit(
        raw,
        '<span style="font-family:Missing,Variable Latin,Helvetica">VA</span>'
        '<span style="font-family:Noto Sans Hebrew">שלום</span>',
        incremental=incremental,
    )
    output = tmp_path / "edited.pdf"
    output.write_bytes(result)
    reference = tmp_path / "reference.pdf"
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        ap = pdf.Root.AcroForm.Fields[0].AP.N
        # Independent PDF text runs using the face metrics verified by hb-shape.
        # The baseline is the content top (70) minus the Hebrew ascender (1068).
        ap.write(b"BT /Alpha 16 Tf 2 52.912 Td <004E004D> Tj ET BT /Beta 16 Tf 34 52.912 Td <0068006700660065> Tj ET")
        pdf.save(reference)

    def raster(path: Path) -> bytes:
        args = [
            shutil.which("pdftoppm") or "pdftoppm",
            "-gray",
            "-r",
            "144",
            "-x",
            "12",
            "-y",
            "32",
            "-W",
            "180",
            "-H",
            "65",
            str(path),
        ]
        return subprocess.run(args, check=True, capture_output=True).stdout  # noqa: S603

    bitmap = raster(output)
    assert bitmap == raster(reference)
    assert len(set(bitmap[-10000:])) > 20
    text = subprocess.run(  # noqa: S603
        [shutil.which("pdftotext") or "pdftotext", "-raw", str(output), "-"], check=True, capture_output=True
    ).stdout.decode()
    assert "VA" in text
    assert "שלום" in text


@pytest.mark.spec("variable-text", "interpret")
def test_embedded_comb_keeps_a_positioned_mark_with_its_grapheme() -> None:
    def comb(pdf: pikepdf.Pdf) -> None:
        form = pdf.Root.AcroForm
        form.Fields[0].Ff = (1 << 25) | (1 << 24)
        form.Fields[0].MaxLen = 2
        form.DR.Font.Beta = _embedded(
            pdf,
            "Noto Sans Hebrew",
            "NotoSansHebrew-Regular",
            "NotoSansHebrew-subset.ttf",
            [(101, 9, "ש", 703), (104, 5, "ם", 652), (105, 10, "ׁ", 0)],
        )

    output = _edit(_variant(comb), '<span style="font-family:Beta">שׁם</span>')
    with pikepdf.open(io.BytesIO(output)) as pdf:
        form = pdf.Root.AcroForm
        appearance = form.Fields[0].AP.N
        assert _codes(appearance)[form.DR.Font.Beta.objgen] == [105, 101, 104]
        positions = [float(ops[4]) for ops, op in pikepdf.parse_content_stream(appearance) if str(op) == "Tm"]
        # HarfBuzz positions the zero-width mark 544 font units from the shin.
        assert positions[0] - positions[1] == pytest.approx(0.544 * 16, abs=0.001)
        assert positions[2] - positions[1] > 110


@pytest.mark.spec("variable-text", "interpret")
def test_repeated_full_cmap_ranges_have_a_bounded_expansion_and_rollback() -> None:
    def ranges(pdf: pikepdf.Pdf) -> None:
        font = pdf.Root.AcroForm.DR.Font.Alpha
        font.Encoding = pdf.make_stream(b"17 begincidrange\n" + b"<0000> <FFFF> 0\n" * 17 + b"endcidrange\n")

    with pyropdf.open(_variant(ranges)) as doc:
        f = doc.form["mixed"]
        before = f.rich_text
        with pytest.raises(pyropdf.LimitError) as failure:
            f.rich_text = '<body xmlns="http://www.w3.org/1999/xhtml"><p style="font-family:Alpha">V</p></body>'
        assert failure.value.code == "interpretation_fuel"
        assert f.rich_text == before


@pytest.mark.spec("variable-text", "interpret")
def test_default_appearance_uses_its_named_embedded_font_and_recovers_broken_unicode_map() -> None:
    def defaults(pdf: pikepdf.Pdf) -> None:
        form = pdf.Root.AcroForm
        form.DA = "/Beta 16 Tf 0 g"
        del form.Fields[0].DS
        bad = pdf.make_stream(b"invalid compressed Unicode map")
        bad.Filter = Name.FlateDecode
        form.DR.Font.Beta.ToUnicode = bad

    output = _edit(_variant(defaults), "שלום")
    with pikepdf.open(io.BytesIO(output)) as pdf:
        form = pdf.Root.AcroForm
        appearance = form.Fields[0].AP.N
        assert _codes(appearance) == {form.DR.Font.Beta.objgen: [104, 103, 102, 101]}
        assert appearance.read_bytes().count(b"/ActualText") == 4


@pytest.mark.spec("variable-text", "interpret")
def test_visual_order_crosses_coloured_hebrew_spans_while_value_stays_logical() -> None:
    output = _edit(_producer(), '<span style="font-family:Beta">של<span style="color:#ff0000">ום</span></span>')
    with pikepdf.open(io.BytesIO(output)) as pdf:
        form = pdf.Root.AcroForm
        assert str(form.Fields[0].V) == "שלום"
        assert _codes(form.Fields[0].AP.N) == {form.DR.Font.Beta.objgen: [104, 103, 102, 101]}
