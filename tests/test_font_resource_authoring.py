"""Public font authoring and editing, checked with Poppler and qpdf after both save modes."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pikepdf
import pytest
from corpus import ASSETS
from test_canvas import _hayro, _poppler
from test_font_embedded import JAPANESE

import pyropdf
from pyropdf import Name, Operation

_UNRELATED = b"unrelated stream\x00\xff"


def _doc() -> pyropdf.Document:
    doc = pyropdf.Document()
    doc.pages.new((150, 80))
    doc.catalog["Unrelated"] = doc.objects[doc.objects.add_stream(_UNRELATED, filter=None)]
    return doc


def _show(page: pyropdf.Page, font: str, codes: bytes) -> None:
    page.operations = [
        Operation("BT"),
        Operation("Tf", (Name(font), 24)),
        Operation("Tm", (1, 0, 0, 1, 10, 35)),
        Operation("Tj", (codes,)),
        Operation("ET"),
    ]


def _text(data: bytes) -> str:
    tool = shutil.which("pdftotext")
    if tool is None:
        pytest.skip("Poppler pdftotext is unavailable")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "fonts.pdf"
        path.write_bytes(data)
        result = subprocess.run([tool, "-enc", "UTF-8", str(path), "-"], check=True, capture_output=True)  # noqa: S603
    return result.stdout.decode().strip()


def _wire(doc: pyropdf.Document, base: bytes, text: str) -> list[bytes]:
    results = []
    for incremental in (False, True):
        data = doc.to_bytes(incremental=incremental)
        if incremental:
            assert data.startswith(base)
        with pikepdf.Pdf.open(io.BytesIO(data)) as pdf:
            assert pdf.Root.Unrelated.read_raw_bytes() == _UNRELATED
            assert not pdf.check_pdf_syntax()
        assert _text(data) == text
        assert pyropdf.open(data).pages[0].text().strip() == text
        results.append(data)
    return results


def _unicode_map(codes: tuple[bytes, bytes], labels: str) -> bytes:
    entries = [
        b"<" + code.hex().encode() + b"> <" + label.encode("utf-16-be").hex().encode() + b">"
        for code, label in zip(codes, labels, strict=True)
    ]
    return b"\n".join(
        [
            b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
            b"/CMapName /XX-Unicode def /CMapType 2 def",
            b"2 begincodespacerange",
            b"<41> <41>",
            b"<8123> <8123>",
            b"endcodespacerange 2 beginbfchar",
            *entries,
            b"endbfchar endcmap",
            b"CMapName currentdict /CMap defineresource pop end end",
        ]
    )


@pytest.mark.spec("cmaps", "create", "edit")
def test_custom_cmap_maps_variable_length_codes_and_edits_the_existing_stream() -> None:
    doc = _doc()
    with doc.pages[0].draw() as canvas:
        canvas.text("AV", (10, 45), font=pyropdf.Font.from_file(JAPANESE), size=24)
    doc = pyropdf.open(doc.to_bytes())
    page = doc.pages[0]
    font = page.obj["Resources"]["Font"]["PyroF1"]
    originals = {
        bytes.fromhex(label.decode()).decode("utf-16-be"): int(cid, 16)
        for cid, label in re.findall(rb"<([a-fA-F0-9]{4})> <(0041|0056)>", font["ToUnicode"].read())
    }
    codes = (b"A", b"\x81\x23")
    cmap = doc.write_cmap(dict(zip(codes, (originals["A"], originals["V"]), strict=True)), name="XX-Mixed")
    font["Encoding"] = cmap
    font["ToUnicode"].write(_unicode_map(codes, "AV"))
    _show(page, "PyroF1", b"".join(codes))
    base = doc.to_bytes()
    assert _text(base) == "AV"
    assert pyropdf.open(base).pages[0].text().strip() == "AV"
    edited = pyropdf.open(base)
    font = edited.pages[0].obj["Resources"]["Font"]["PyroF1"]
    original = font["Encoding"]
    original["Private"] = b"kept"
    before = original.read()
    with pytest.raises(ValueError, match="prefix"):
        edited.write_cmap({b"A": 0, b"AB": 1}, name="XX-Broken", stream=original)
    assert original.read() == before
    updated = edited.write_cmap(
        dict(zip(codes, (originals["V"], originals["A"]), strict=True)), name="XX-Swapped", stream=original
    )
    assert updated.xref == cmap.xref
    font["ToUnicode"].write(_unicode_map(codes, "VA"))
    for result in _wire(edited, base, "VA"):
        with pikepdf.Pdf.open(io.BytesIO(result)) as pdf:
            encoding = pdf.pages[0].Resources.Font.PyroF1.Encoding
            assert encoding.CMapName == pikepdf.Name("/XX-Swapped")
            assert bytes(encoding.Private) == b"kept"
            assert b"2 begincidchar" in encoding.read_bytes()
        for render in (_hayro, _poppler):
            assert render(result)[1] != render(base)[1]


def test_cmap_code_and_cid_limits_and_postscript_blocks() -> None:
    doc = _doc()
    for mapping in ({}, {b"": 1}, {b"12345": 2}, {b"x": -1}, {b"x": 65536}, {b"x": True}):
        with pytest.raises(ValueError, match=r"code|CID"):
            doc.write_cmap(mapping, name="XX-Bad")
    table = {bytes([i]): i for i in range(101)}
    table[b"\xff\xfe\xfd\xfc"] = 65535
    stream = doc.write_cmap(
        table, name="XX-Vertical", registry=b"R(test)", ordering=b"Set", supplement=1, vertical=True
    )
    assert stream["WMode"] == 1
    assert stream.read().count(b"begincidchar") == 2
    assert b"<00> <64>" in stream.read()
    assert b"<fffefdfc> 65535" in stream.read()
    assert stream["CIDSystemInfo"]["Registry"] == b"R(test)"
    with pytest.raises(ValueError, match="this document"):
        _doc().write_cmap({b"A": 1}, name="XX-Foreign", stream=stream)


def test_cmap_memory_limit_rolls_back_and_keeps_the_original_encoding() -> None:
    doc = _doc()
    doc.catalog["TestCMap"] = doc.write_cmap({b"A": 65}, name="XX-Original")
    limited = pyropdf.open(doc.to_bytes(), limits=pyropdf.DocumentLimits(max_memory_size=100_000))
    stream = limited.catalog["TestCMap"]
    original = stream.read()
    codes = {index.to_bytes(2, "big"): index for index in range(512)}
    with pytest.raises(pyropdf.LimitError) as error:
        limited.write_cmap(codes, name="XX-Limited", stream=stream)
    assert error.value.code == "memory_size"
    assert stream.read() == original
    assert stream["CMapName"] == "XX-Original"
    assert limited.write_cmap({b"B": 66}, name="XX-Next", stream=stream).xref == stream.xref


def _encrypt(data: bytes, seed: int) -> bytes:
    out = bytearray()
    for value in data:
        cipher = value ^ (seed >> 8)
        out.append(cipher)
        seed = ((cipher + seed) * 52845 + 22719) & 0xFFFF
    return bytes(out)


def _integer(value: int) -> bytes:
    if -107 <= value <= 107:
        return bytes([value + 139])
    magnitude = abs(value) - 108
    return bytes([(247 if value > 0 else 251) + magnitude // 256, magnitude % 256])


def _rectangle(left: int, width: int, *, blend: bool = False) -> bytes:
    outline = bytearray(_integer(0) + _integer(600) + b"\x0d")  # hsbw
    outline.extend(_integer(left) + _integer(0) + b"\x15")
    for dx, dy in ((width, 0), (0, 600), (-width, 0)):
        if blend and dx:
            # Multiple Master OtherSubr 14 blends one value from two master designs.
            outline.extend(
                _integer(dx) + _integer(180 if dx > 0 else -180) + _integer(2) + _integer(14) + b"\x0c\x10\x0c\x11"
            )
        else:
            outline.extend(_integer(dx))
        outline.extend(_integer(dy) + b"\x05")
    return bytes(outline) + b"\x09\x0e"


def _type1_program(
    width: int, *, composite: bool = False, iv: int = -1, weight: int | None = None
) -> tuple[bytes, dict[str, int]]:
    """Build an original Type 1 program with a binary eexec segment and three distinct glyphs."""
    header = b"""%!FontType1-1.0: XX-Blocks 1.0
10 dict begin
/FontName /XX-Blocks def /FontType 1 def /PaintType 0 def
/FontMatrix [.001 0 0 .001 0 0] def /FontBBox [0 0 600 600] def
/Encoding StandardEncoding def
currentdict end
currentfile eexec
"""
    if weight is not None:
        header = header.replace(
            b"/FontName /XX-Blocks", b"/FontName /XX-BlocksMM_" + str(100 + weight * 800).encode()
        ).replace(
            b"/Encoding StandardEncoding def",
            b"/Encoding StandardEncoding def\n/FontInfo 4 dict dup begin "
            b"/BlendAxisTypes [/Weight] def /BlendDesignPositions [[0] [1]] def "
            b"/BlendDesignMap [[[100 0] [900 1]]] def end def\n"
            + f"/WeightVector [{1 - weight} {weight}] def".encode(),
        )
    glyphs = {
        ".notdef": _integer(0) + _integer(600) + b"\x0d\x0e",
        "A": _rectangle(25, width, blend=weight is not None),
        "B": _rectangle(300, 200),
        "C": _rectangle(180, 240),
    }
    glyphs["D"] = _rectangle(50, 330)
    if composite:
        # C paints the StandardEncoding A and B through a seac instruction.
        glyphs["C"] = (
            _integer(0) + _integer(600) + b"\x0d" + b"".join(_integer(n) for n in (0, 150, 50, 65, 66)) + b"\x0c\x06"
        )
    # A calls a local subroutine. Keep its path in the private dictionary, including
    # a multiple-master blend for a font with two designs.
    metrics = _integer(0) + _integer(600) + b"\x0d"
    subroutine = glyphs["A"][len(metrics) : -1] + b"\x0b"
    glyphs["A"] = metrics + _integer(0) + b"\x0a\x0e"
    if iv >= 0:
        subroutine = _encrypt(bytes(iv) + subroutine, 4330)
        glyphs = {name: _encrypt(bytes(iv) + outline, 4330) for name, outline in glyphs.items()}
    private = bytearray(
        b"dup /Private 9 dict dup begin\n/RD {string currentfile exch readstring pop} executeonly def\n"
        b"/ND {noaccess def} executeonly def\n/lenIV -1 def\n/BlueValues [] def\n/MinFeature {16 16} def\n"
        b"/password 5839 def\n/NP {noaccess put} executeonly def\n/Subrs 1 array dup 0 "
        + str(len(subroutine)).encode()
        + b" RD "
        + subroutine
        + b" NP def\n2 index /CharStrings 8 dict dup begin\n"
    )
    private = private.replace(b"/lenIV -1", f"/lenIV {iv}".encode())
    for name, string in glyphs.items():
        private.extend(b"/" + name.encode() + b" " + str(len(string)).encode() + b" RD " + string + b" ND\n")
    private.extend(
        b"end end readonly put noaccess put\ndup /FontName get exch definefont pop\nmark currentfile closefile\n"
    )
    encrypted = _encrypt(b"abcd" + private, 55665)
    tail = b"0" * 512 + b"\ncleartomark\n"
    return header + encrypted + tail, {"Length1": len(header), "Length2": len(encrypted), "Length3": len(tail)}


def _type1(doc: pyropdf.Document, *, composite: bool = False, iv: int = -1) -> pyropdf.Dictionary:
    data, lengths = _type1_program(150, composite=composite, iv=iv)
    program = doc.objects[doc.objects.add_stream(data, lengths)]
    metrics = {
        "Type": Name("FontDescriptor"),
        "FontName": Name("XX-Blocks"),
        "Flags": 32,
        "FontBBox": [0, 0, 600, 600],
        "ItalicAngle": 0,
        "Ascent": 700,
        "Descent": -200,
        "CapHeight": 600,
        "StemV": 60,
        "FontFile": program,
    }
    font = {
        "Type": Name("Font"),
        "Subtype": Name("Type1"),
        "BaseFont": Name("XX-Blocks"),
        "FirstChar": 65,
        "LastChar": 67,
        "Widths": [600] * 3,
        "FontDescriptor": doc.objects[doc.objects.add(metrics)],
        "Encoding": {
            "Type": Name("Encoding"),
            "BaseEncoding": Name("WinAnsiEncoding"),
            "Differences": [65, Name("A"), Name("B"), Name("C")],
        },
    }
    result = doc.objects[doc.objects.add(font)]
    doc.pages[0].obj["Resources"] = {"Font": {"Test": result}}
    return result


@pytest.mark.spec("type1-fonts", "create", "edit")
@pytest.mark.spec("simple-font-encodings", "create", "edit")
@pytest.mark.spec("embedded-fonts", "create")
def test_custom_type1_program_and_differences_edit_glyphs_without_moving_their_origins() -> None:
    doc = _doc()
    _type1(doc)
    _show(doc.pages[0], "Test", b"AB")
    base = doc.to_bytes()
    assert _text(base) == "AB"
    edited = pyropdf.open(base)
    page = edited.pages[0]
    font = page.obj["Resources"]["Font"]["Test"]
    original_glyphs = page.content().text_runs[0].glyphs
    with pytest.raises(TypeError, match="array"):
        pyropdf.typed.Encoding(font["Encoding"])["Differences"] = Name("bad")
    program = font["FontDescriptor"]["FontFile"]
    embedded = edited.font_program(font.xref)
    assert embedded is not None
    assert embedded.data == _type1_program(150)[0]
    data, lengths = _type1_program(235)
    with edited.transaction("Edit the Type 1 program and its encoding"):
        program.write(data)
        for key, value in lengths.items():
            program[key] = value
        font["Encoding"]["Differences"] = [65, Name("B"), Name("A"), Name("C")]
    for saved in _wire(edited, base, "BA"):
        reopened = pyropdf.open(saved)
        glyphs = reopened.pages[0].content().text_runs[0].glyphs
        assert [g.matrix for g in glyphs] == [g.matrix for g in original_glyphs]
        assert [g.glyph_id for g in glyphs] == [g.glyph_id for g in reversed(original_glyphs)]
        for render in (_hayro, _poppler):
            stride, samples = render(saved)
            channels = stride // 150
            assert samples[38 * stride + 18 * channels] < 30  # B's right-hand stem.
            assert samples[38 * stride + 11 * channels] > 225
        with pikepdf.Pdf.open(io.BytesIO(saved)) as pdf:
            assert pdf.pages[0].Resources.Font.Test.FontDescriptor.FontFile.read_bytes() == data


@pytest.mark.spec("embedded-fonts", "edit")
@pytest.mark.parametrize("iv", [-1, 4], ids=["plain-charstrings", "encrypted-charstrings"])
@pytest.mark.parametrize("composite", [False, True], ids=["simple", "seac"])
def test_subset_type1_keeps_used_outlines_subroutines_and_seac_components(iv: int, *, composite: bool) -> None:
    doc = _doc()
    _type1(doc, composite=composite, iv=iv)
    shown = "C" if composite else "A"
    _show(doc.pages[0], "Test", shown.encode())
    base = doc.to_bytes()
    edited = pyropdf.open(base)
    font = edited.pages[0].obj["Resources"]["Font"]["Test"]
    program = font["FontDescriptor"]["FontFile"]
    size1, size2, size3 = (program[key] for key in ("Length1", "Length2", "Length3"))
    original = program.read()
    (subset,) = edited.subset_fonts()
    assert subset.glyphs == 5
    assert subset.kept == (4 if composite else 2)
    assert subset.size_after < subset.size_before
    assert program["Length2"] == size2 - (len(original) - subset.size_after)
    assert program["Length1"] == size1
    assert program["Length3"] == size3
    for saved in _wire(edited, base, shown):
        for render in (_hayro, _poppler):
            assert render(saved) == render(base)
    assert edited.subset_fonts() == ()


def test_invalid_type1_segment_length_preserves_program_and_font_names() -> None:
    doc = _doc()
    font = _type1(doc)
    _show(doc.pages[0], "Test", b"A")
    program = font["FontDescriptor"]["FontFile"]
    original = program.read()
    program["Length2"] = len(original) * 2
    assert doc.subset_fonts() == ()
    assert program.read() == original
    assert font["BaseFont"] == "XX-Blocks"


@pytest.mark.spec("embedded-fonts", "edit")
def test_real_type1_subsets_preserve_a_latex_page_in_both_renderers() -> None:
    source = pyropdf.open(ASSETS / "usrguide.pdf")
    doc = pyropdf.Document()
    doc.pages.extend([source.pages[0]])
    base = doc.to_bytes()
    edited = pyropdf.open(base)
    subsets = edited.subset_fonts()
    assert len(subsets) >= 5
    assert all(item.size_after < item.size_before and item.kept < item.glyphs for item in subsets)
    for incremental in (False, True):
        data = edited.to_bytes(incremental=incremental)
        assert _text(data) == _text(base)
        for render in (_hayro, _poppler):
            assert render(data) == render(base)


def test_type1_subset_respects_memory_limit_and_rolls_back_font_changes() -> None:
    source = _doc()
    _type1(source)
    _show(source.pages[0], "Test", b"A")
    limited = pyropdf.open(source.to_bytes(), limits=pyropdf.DocumentLimits(max_memory_size=24_000))
    font = limited.pages[0].obj["Resources"]["Font"]["Test"]
    original = font["FontDescriptor"]["FontFile"].read()
    with pytest.raises(pyropdf.LimitError) as error:
        limited.subset_fonts()
    assert error.value.code == "memory_size"
    assert font["BaseFont"] == "XX-Blocks"
    assert font["FontDescriptor"]["FontFile"].read() == original


@pytest.mark.spec("truetype-fonts", "create", "edit")
def test_simple_truetype_width_and_program_after_reopening() -> None:
    from test_font_embedded import VARIABLE_LATIN  # noqa: PLC0415 - this witness uses the original synthetic font.

    doc = _doc()
    data = VARIABLE_LATIN.read_bytes()
    stream = doc.objects[doc.objects.add_stream(data, {"Length1": len(data)})]
    metrics = {
        "Type": Name("FontDescriptor"),
        "FontName": Name("VariableLatin"),
        "Flags": 32,
        "FontBBox": [-100, -200, 1000, 900],
        "ItalicAngle": 0,
        "Ascent": 800,
        "Descent": -200,
        "CapHeight": 700,
        "StemV": 80,
        "FontFile2": stream,
    }
    font = doc.objects[
        doc.objects.add(
            {
                "Type": Name("Font"),
                "Subtype": Name("TrueType"),
                "BaseFont": Name("VariableLatin"),
                "FontDescriptor": doc.objects[doc.objects.add(metrics)],
                "FirstChar": 65,
                "LastChar": 86,
                "Widths": [600] * 22,
                "Encoding": Name("WinAnsiEncoding"),
            }
        )
    ]
    doc.pages[0].obj["Resources"] = {"Font": {"Test": font}}
    _show(doc.pages[0], "Test", b"AV")
    base = doc.to_bytes()
    edited = pyropdf.open(base)
    font = edited.pages[0].obj["Resources"]["Font"]["Test"]
    with pytest.raises(TypeError, match="integer"):
        pyropdf.typed.FontTrueType(font)["FirstChar"] = Name("bad")
    font["Widths"][0] = 900
    for saved in _wire(edited, base, "AV"):
        page = pyropdf.open(saved).pages[0]
        glyphs = page.content().text_runs[0].glyphs
        assert glyphs[0].matrix.e == 10
        assert glyphs[1].matrix.e == pytest.approx(10 + 24 * 0.9, abs=0.01)
        with pikepdf.Pdf.open(io.BytesIO(saved)) as pdf:
            assert pdf.pages[0].Resources.Font.Test.FontDescriptor.FontFile2.read_bytes() == data
        for render in (_hayro, _poppler):
            assert render(saved) != render(base)


@pytest.mark.spec("type3-fonts", "create", "edit")
def test_type3_charproc_changes_glyph_ink_with_fixed_position() -> None:
    doc = _doc()
    proc = doc.objects[doc.objects.add_stream(b"600 0 0 0 500 600 d1 0 0 150 600 re f")]
    font = {
        "Type": Name("Font"),
        "Subtype": Name("Type3"),
        "FontBBox": [0, 0, 500, 600],
        "FontMatrix": [0.001, 0, 0, 0.001, 0, 0],
        "CharProcs": {"A": proc},
        "Encoding": {"Type": Name("Encoding"), "Differences": [65, Name("A")]},
        "FirstChar": 65,
        "LastChar": 65,
        "Widths": [600],
        "Resources": {},
    }
    doc.pages[0].obj["Resources"] = {"Font": {"Test": doc.objects[doc.objects.add(font)]}}
    _show(doc.pages[0], "Test", b"AA")
    base = doc.to_bytes()
    edited = pyropdf.open(base)
    font = edited.pages[0].obj["Resources"]["Font"]["Test"]
    original = edited.pages[0].content().text_runs[0].glyphs
    with pytest.raises(TypeError, match="dictionary"):
        pyropdf.typed.FontType3(font)["CharProcs"] = Name("bad")
    font["CharProcs"]["A"].write(b"600 0 0 0 500 600 d1 300 0 150 600 re f")
    for saved in _wire(edited, base, "AA"):
        glyphs = pyropdf.open(saved).pages[0].content().text_runs[0].glyphs
        assert [g.matrix for g in glyphs] == [g.matrix for g in original]
        for render in (_hayro, _poppler):
            stride, pixels = render(saved)
            channels = stride // 150
            assert pixels[38 * stride + 18 * channels] < 32
            assert pixels[38 * stride + 11 * channels] > 224


@pytest.mark.spec("character-tables", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize(
    ("name", "code", "text"),
    [
        ("WinAnsiEncoding", b"\x80", "€"),
        ("MacRomanEncoding", b"\x80", "Ä"),
        ("MacExpertEncoding", b"\xda", "¹"),
        (None, b"\xa1", "¡"),
        ("Symbol", b"A", "Α"),
        ("ZapfDingbats", b"!", "✁"),
    ],
)
def test_standard_character_tables_and_pdfdocencoding(name: str | None, code: bytes, text: str) -> None:
    doc = _doc()
    symbolic = name in ("Symbol", "ZapfDingbats")
    font = {
        "Type": Name("Font"),
        "Subtype": Name("Type1"),
        "BaseFont": Name(name if name in ("Symbol", "ZapfDingbats") else "Helvetica"),
    }
    if name is not None and not symbolic:
        font["Encoding"] = Name(name)
    doc.pages[0].obj["Resources"] = {"Font": {"Test": doc.objects[doc.objects.add(font)]}}
    _show(doc.pages[0], "Test", code)
    # PDFDocEncoding 0x80 denotes bullet; WinAnsi 0x80 denotes Euro.
    doc.catalog["TitleSample"] = b"\x80"
    doc.catalog["UndefinedSample"] = b"\x7f"  # Undefined in PDFDocEncoding.
    base = doc.to_bytes()
    edited = pyropdf.open(base)
    assert edited.catalog["TitleSample"].text == "•"
    assert edited.catalog["UndefinedSample"].text == "\ufffd"
    assert edited.pages[0].text().strip() == text
    edited.catalog["TitleSample"] = b"\xa0"  # Euro in PDFDocEncoding.
    for saved in _wire(edited, base, text):
        with pikepdf.Pdf.open(io.BytesIO(saved)) as pdf:
            assert str(pdf.Root.TitleSample) == "€"
            assert bytes(pdf.Root.UndefinedSample) == b"\x7f"
            font = pdf.pages[0].Resources.Font.Test
            if name is not None and not symbolic:
                assert str(font.Encoding) == "/" + name


@pytest.mark.spec("type1-fonts", "create", "edit")
def test_multiple_master_type1_design_changes_blended_outlines() -> None:
    doc = _doc()
    font = _type1(doc)
    font["Subtype"] = Name("MMType1")
    program = font["FontDescriptor"]["FontFile"]
    for weight in (0, 1):
        data, lengths = _type1_program(150, weight=weight)
        program.write(data)
        for key, value in lengths.items():
            program[key] = value
        design = Name(f"XX-BlocksMM_{100 + weight * 800}")
        font["BaseFont"] = font["FontDescriptor"]["FontName"] = design
        if weight == 0:
            _show(doc.pages[0], "Test", b"A")
            base = doc.to_bytes()
            doc = pyropdf.open(base)
            font = doc.pages[0].obj["Resources"]["Font"]["Test"]
            program = font["FontDescriptor"]["FontFile"]
    for saved in _wire(doc, base, "A"):
        for render in (_hayro, _poppler):
            stride, old = render(base)
            _, new = render(saved)
            # x=16 falls beyond the narrow master and within the wide one.
            offset = 37 * stride + 16 * (stride // 150)
            assert old[offset] > 220, render.__name__
            assert new[offset] < 35, render.__name__
