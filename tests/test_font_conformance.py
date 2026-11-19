"""Conformance invariants for saved CFF and TrueType font subsets (#799).

Covers CFF Type 2 charstring widths against CIDFont ``/W`` entries, PDF/A-1
``/CIDSet`` bitmaps versus PDF 2.0 deprecation, text extraction, rendering,
untouched font preservation, and claim reporting across Canvas and form saves.
"""

from __future__ import annotations

import struct
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import Array, Dictionary, Font, FreeText, Stream, TextField

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
PDFA = Path(__file__).resolve().parent / "assets" / "pdfa"
PDFUA = Path(__file__).resolve().parent / "assets" / "pdfua"
REAL_WORLD = Path(__file__).resolve().parent / "assets" / "real_world"

ALL_CONFORMANCE_FIXTURES = (
    PDFA / "pdfa-1b.pdf",
    PDFA / "pdfa-2b.pdf",
    PDFA / "pdfa-2u.pdf",
    PDFA / "pdfa-3b.pdf",
    PDFA / "pdfa-4.pdf",
    PDFUA / "pdfua-1.pdf",
)


@cache
def japanese() -> Font:
    return Font.from_file(FONTS / "NotoSansJP-subset.otf")


@cache
def hebrew() -> Font:
    return Font.from_file(FONTS / "NotoSansHebrew-subset.ttf")


def _cff_index(data: bytes, at: int) -> tuple[list[bytes], int]:
    (count,) = struct.unpack_from(">H", data, at)
    if count == 0:
        return [], at + 2
    off_size = data[at + 2]
    offsets_at = at + 3
    offsets = [
        int.from_bytes(data[offsets_at + i * off_size : offsets_at + (i + 1) * off_size], "big")
        for i in range(count + 1)
    ]
    base = offsets_at + (count + 1) * off_size - 1
    return [data[base + offsets[i] : base + offsets[i + 1]] for i in range(count)], base + offsets[-1]


def _cff_int_operand(data: bytes, at: int) -> tuple[float, int] | None:
    b0 = data[at]
    if b0 == 28:
        (val,) = struct.unpack_from(">h", data, at + 1)
        return float(val), at + 3
    if 32 <= b0 <= 246:
        return float(b0 - 139), at + 1
    if 247 <= b0 <= 250:
        return float((b0 - 247) * 256 + data[at + 1] + 108), at + 2
    if 251 <= b0 <= 254:
        return float(-(b0 - 251) * 256 - data[at + 1] - 108), at + 2
    return None


def _cff_real_operand(data: bytes, at: int) -> tuple[float, int]:
    nibbles = {0xA: ".", 0xB: "E", 0xC: "E-", 0xE: "-"}
    text = ""
    pos = at + 1
    while True:
        byte = data[pos]
        pos += 1
        for nib in (byte >> 4, byte & 0xF):
            if nib == 0xF:
                return float(text), pos
            text += str(nib) if nib < 10 else nibbles[nib]


def _cff_dict(data: bytes) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    operands: list[float] = []
    at = 0
    while at < len(data):
        b0 = data[at]
        if b0 <= 21:
            op = 1200 + data[at + 1] if b0 == 12 else b0
            at += 2 if b0 == 12 else 1
            out[op] = operands
            operands = []
        elif (parsed := _cff_int_operand(data, at)) is not None:
            operands.append(parsed[0])
            at = parsed[1]
        elif b0 == 29:
            (val,) = struct.unpack_from(">i", data, at + 1)
            operands.append(float(val))
            at += 5
        elif b0 == 30:
            val, at = _cff_real_operand(data, at)
            operands.append(val)
        else:
            msg = f"unexpected CFF DICT byte {b0}"
            raise AssertionError(msg)
    return out


def _cff_charset(data: bytes, at: int, glyph_count: int) -> list[int]:
    fmt = data[at]
    cids = [0]
    pos = at + 1
    if fmt == 0:
        for _ in range(glyph_count - 1):
            (cid,) = struct.unpack_from(">H", data, pos)
            cids.append(cid)
            pos += 2
    elif fmt in (1, 2):
        while len(cids) < glyph_count:
            (first,) = struct.unpack_from(">H", data, pos)
            if fmt == 1:
                left = data[pos + 2]
                pos += 3
            else:
                (left,) = struct.unpack_from(">H", data, pos + 2)
                pos += 4
            cids.extend(range(first, first + left + 1))
    else:
        msg = f"unexpected CFF charset format {fmt}"
        raise AssertionError(msg)
    return cids[:glyph_count]


def _cff_fd_select(data: bytes, at: int, glyph_count: int) -> list[int]:
    fmt = data[at]
    if fmt == 0:
        return list(data[at + 1 : at + 1 + glyph_count])
    assert fmt == 3
    (ranges,) = struct.unpack_from(">H", data, at + 1)
    fds = [0] * glyph_count
    pos = at + 3
    for _ in range(ranges):
        first, fd = struct.unpack_from(">HB", data, pos)
        (end,) = struct.unpack_from(">H", data, pos + 3)
        for gid in range(first, end):
            fds[gid] = fd
        pos += 3
    return fds


def _type2_operator_width(b0: int, stack: list[float]) -> float | None:
    if b0 in (1, 3, 18, 19, 20, 23):
        assert len(stack) <= 48
        return stack[0] if len(stack) % 2 == 1 else None
    if b0 in (4, 22):
        return stack[0] if len(stack) > 1 else None
    if b0 == 21:
        return stack[0] if len(stack) > 2 else None
    return stack[0] if b0 == 14 and stack and len(stack) != 4 else None


def _type2_width(charstring: bytes) -> float | None:
    stack: list[float] = []
    at = 0
    while at < len(charstring):
        b0 = charstring[at]
        if (parsed := _cff_int_operand(charstring, at)) is not None:
            stack.append(parsed[0])
            at = parsed[1]
        elif b0 == 255:
            (fixed,) = struct.unpack_from(">i", charstring, at + 1)
            stack.append(fixed / 65536.0)
            at += 5
        else:
            return _type2_operator_width(b0, stack)
    return None


def _parse_cid_cff_widths(cff: bytes) -> tuple[bytes, dict[int, float]]:
    """Return ``(top_dict_bytes, {cid: advance_width})`` from a bare CID-keyed CFF."""
    assert cff[0] == 1
    header_size = cff[2]
    _names, after_names = _cff_index(cff, header_size)
    tops, _after_tops = _cff_index(cff, after_names)
    assert len(tops) == 1
    top_raw = tops[0]
    top = _cff_dict(top_raw)
    assert 1230 in top, "expected CID-keyed CFF with ROS operator"

    charstrings, _ = _cff_index(cff, int(top[17][0]))
    glyph_count = len(charstrings)
    cids = _cff_charset(cff, int(top[15][0]), glyph_count)
    fds = _cff_fd_select(cff, int(top[1237][0]), glyph_count)
    fd_dicts, _ = _cff_index(cff, int(top[1236][0]))

    privates: list[tuple[float, float]] = []
    for fd_bytes in fd_dicts:
        fd_dict = _cff_dict(fd_bytes)
        priv_size, priv_offset = int(fd_dict[18][0]), int(fd_dict[18][1])
        priv = _cff_dict(cff[priv_offset : priv_offset + priv_size])
        default_w = priv.get(20, [0.0])[0]
        nominal_w = priv.get(21, [0.0])[0]
        privates.append((default_w, nominal_w))

    widths: dict[int, float] = {}
    for gid, (cid, cs) in enumerate(zip(cids, charstrings, strict=True)):
        default_w, nominal_w = privates[fds[gid]]
        delta = _type2_width(cs)
        widths[cid] = default_w if delta is None else nominal_w + delta
    return top_raw, widths


def _pdf_cid_widths(cid_font: Dictionary) -> dict[int, float]:
    out: dict[int, float] = {}
    entries = list(cid_font["W"])
    at = 0
    while at < len(entries):
        first = int(entries[at])
        second = entries[at + 1]
        if isinstance(second, Array):
            for offset, width in enumerate(second):
                out[first + offset] = float(width)
            at += 2
        else:
            last = int(second)
            width = float(entries[at + 2])
            for cid in range(first, last + 1):
                out[cid] = width
            at += 3
    return out


def _cid_set_bits(data: bytes) -> set[int]:
    return {byte_index * 8 + bit for byte_index, byte in enumerate(data) for bit in range(8) if byte & (0x80 >> bit)}


def _cid_fonts_in_doc(doc: pyropdf.Document) -> list[tuple[Dictionary, Dictionary, Stream]]:
    found: list[tuple[Dictionary, Dictionary, Stream]] = []
    for num in doc.objects:
        obj = doc.objects.get(num)
        if not isinstance(obj, Dictionary) or obj.get("Subtype") != "Type0":
            continue
        descendants = obj.get("DescendantFonts")
        if not isinstance(descendants, Array) or len(descendants) != 1:
            continue
        cid_font = descendants[0]
        assert isinstance(cid_font, Dictionary)
        descriptor = cid_font["FontDescriptor"]
        assert isinstance(descriptor, Dictionary)
        program = descriptor.get("FontFile3") or descriptor.get("FontFile2")
        assert isinstance(program, Stream)
        found.append((cid_font, descriptor, program))
    return found


def _canvas_text(source: Path, *, incremental: bool) -> bytes:
    with pyropdf.open(source) as doc:
        with doc.pages[0].draw() as canvas:
            canvas.text("AVA office", (10, 40), font=japanese(), size=10)
        assert doc.changes(incremental=incremental).claims == ()
        return doc.to_bytes(incremental=incremental)


def _add_field(source: Path, *, incremental: bool) -> bytes:
    with pyropdf.open(source) as doc:
        doc.pages[0].widgets.add(TextField("pyropdf_field", (10, 50, 100, 70), label="lbl"))
        doc.form.font = japanese()
        doc.form["pyropdf_field"].value = "AVA"
        claims = doc.changes(incremental=incremental).claims
        if source.name == "pdfua-1.pdf":
            assert len(claims) == 1
            assert claims[0].claim == "PDF/UA-1"
            assert set(claims[0].objects) == {w.xref for w in doc.pages[0].widgets}
        else:
            assert claims == ()
        return doc.to_bytes(incremental=incremental)


def _fill_field_reopened(source: Path, *, incremental: bool) -> bytes:
    base = _add_field(source, incremental=True)
    with pyropdf.open(base) as doc:
        doc.form.font = japanese()
        doc.form["pyropdf_field"].value = "AVA office"
        return doc.to_bytes(incremental=incremental)


def _flatten_widgets_reopened(source: Path, *, incremental: bool) -> bytes:
    base = _add_field(source, incremental=True)
    with pyropdf.open(base) as doc:
        doc.pages[0].flatten(annotations=False)
        return doc.to_bytes(incremental=incremental)


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize(
    ("case_name", "build", "expected_text"),
    [
        ("canvas_text", _canvas_text, "AVA office"),
        ("add_field", _add_field, "AVA"),
        ("fill_field", _fill_field_reopened, "AVA office"),
        ("flatten_widgets", _flatten_widgets_reopened, "AVA"),
    ],
)
@pytest.mark.parametrize("fixture", ALL_CONFORMANCE_FIXTURES, ids=lambda p: p.stem)
@pytest.mark.spec("font-descriptors", "edit")
@pytest.mark.spec("pdfa", "edit", "preserve")
@pytest.mark.spec("pdfua", "edit", "preserve")
def test_saved_cff_subsets_preserve_widths_cidset_and_text_across_conformance_fixtures(
    fixture: Path,
    case_name: str,
    build: Callable[..., bytes],
    expected_text: str,
    *,
    incremental: bool,
) -> None:
    saved = build(fixture, incremental=incremental)
    with pyropdf.open(saved) as doc:
        assert expected_text in doc.pages[0].text(), case_name
        fonts = _cid_fonts_in_doc(doc)
        assert fonts
        for cid_font, descriptor, program in fonts:
            assert cid_font["Subtype"] == "CIDFontType0"
            assert str(descriptor["FontName"]).startswith(tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
            assert str(descriptor["FontName"])[6] == "+"
            assert program["Subtype"] == "CIDFontType0C"

            top_raw, cff_widths = _parse_cid_cff_widths(program.read())
            # Canonical compact ROS (391, 392, 0, 12 30) begins Top DICT.
            assert top_raw[:7] == bytes([248, 27, 248, 28, 139, 12, 30])

            pdf_widths = _pdf_cid_widths(cid_font)
            assert pdf_widths
            for cid, width in pdf_widths.items():
                assert cid in cff_widths
                assert cff_widths[cid] == pytest.approx(width, abs=0.01)

            if fixture.name == "pdfa-1b.pdf":
                cid_set = descriptor.get("CIDSet")
                assert isinstance(cid_set, Stream)
                assert cid_set.xref is not None
                assert _cid_set_bits(cid_set.read()) == set(cff_widths)
            else:
                assert "CIDSet" not in descriptor

        if case_name == "canvas_text":
            assert doc.check().defects == ()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_pdfa1_truetype_subset_writes_matching_cidset_and_pdf20_omits_it(*, incremental: bool) -> None:
    with pyropdf.open(PDFA / "pdfa-1b.pdf") as doc:
        with doc.pages[0].draw() as canvas:
            canvas.text("שלום", (10, 40), font=hebrew(), size=12)
        live = bytes(doc.pages[0].pixmap(dpi=72))
        first = doc.to_bytes(incremental=incremental)

        # A second drawing on the same open document updates CIDSet at the next save.
        with doc.pages[0].draw() as canvas:
            canvas.text("עולם", (10, 70), font=hebrew(), size=12)
        second = doc.to_bytes(incremental=incremental)

    with pyropdf.open(first) as reopened:
        assert "שלום" in reopened.pages[0].text()
        assert bytes(reopened.pages[0].pixmap(dpi=72)) == live
        assert reopened.check().defects == ()
        [(cid_font, descriptor, _program)] = _cid_fonts_in_doc(reopened)
        cid_map = cid_font["CIDToGIDMap"].read()
        mapped_cids = {0} | {
            cid for cid in range(len(cid_map) // 2) if int.from_bytes(cid_map[2 * cid : 2 * cid + 2], "big") != 0
        }
        cid_set = descriptor["CIDSet"]
        assert isinstance(cid_set, Stream)
        assert cid_set.xref is not None
        first_bits = _cid_set_bits(cid_set.read())
        assert first_bits == mapped_cids

    with pyropdf.open(second) as reopened_second:
        assert reopened_second.check().defects == ()
        [(_cid_font2, descriptor2, _program2)] = _cid_fonts_in_doc(reopened_second)
        second_bits = _cid_set_bits(descriptor2["CIDSet"].read())
        assert first_bits < second_bits

    for pdf20 in (PDFA / "pdfa-4.pdf", REAL_WORLD / "pdf20-simple.pdf"):
        with pyropdf.open(pdf20) as doc20:
            before = {(d.xref, d.path, d.clause, d.kind) for d in doc20.check().defects}
            with doc20.pages[0].draw() as canvas:
                canvas.text("AVA office", (10, 40), font=japanese(), size=10)
                canvas.text("שלום", (10, 70), font=hebrew(), size=10)
            saved20 = doc20.to_bytes(incremental=incremental)
        with pyropdf.open(saved20) as reopened20:
            after = {(d.xref, d.path, d.clause, d.kind) for d in reopened20.check().defects}
            assert after == before
            for _cid_font, descriptor, _program in _cid_fonts_in_doc(reopened20):
                assert "CIDSet" not in descriptor


def test_empty_freetext_with_cff_face_settles_to_cidfonttype0c_on_pdf14() -> None:
    with pyropdf.open(PDFA / "pdfa-1b.pdf") as doc:
        annot = doc.pages[0].annotations.add(FreeText((10, 10, 120, 40), "", font=japanese(), size=10))
        annot_xref = annot.xref
        saved = doc.to_bytes()

    with pyropdf.open(saved) as reopened:
        assert [defect for defect in reopened.check().defects if defect.xref != annot_xref] == []
        [(_cid_font, descriptor, program)] = _cid_fonts_in_doc(reopened)
        assert program["Subtype"] == "CIDFontType0C"
        assert _cid_set_bits(descriptor["CIDSet"].read()) == {0}


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("cid-fonts", "preserve")
@pytest.mark.spec("embedded-fonts", "preserve")
@pytest.mark.spec("font-descriptors", "preserve")
@pytest.mark.spec("type0-fonts", "preserve")
def test_untouched_embedded_fonts_and_rendering_stay_intact(*, incremental: bool) -> None:
    with pyropdf.open(REAL_WORLD / "mhlw-doc.pdf") as baseline_doc:
        existing_xrefs = sorted(
            {info.xref for page in baseline_doc.pages for info in page.fonts() if info.xref is not None}
        )
        assert existing_xrefs
        expected_programs = {
            xref: (prog.type, prog.data)
            for xref in existing_xrefs
            if (prog := baseline_doc.font_program(xref)) is not None
        }
        assert expected_programs
        untouched_page_text = baseline_doc.pages[1].text()
        untouched_page_pixels = bytes(baseline_doc.pages[1].pixmap(dpi=72))

        with baseline_doc.pages[0].draw() as canvas:
            canvas.text("AVA office", (10, 40), font=japanese(), size=10)
        live_page0 = bytes(baseline_doc.pages[0].pixmap(dpi=72))
        saved = baseline_doc.to_bytes(incremental=incremental)

    with pyropdf.open(saved) as reopened:
        assert "AVA office" in reopened.pages[0].text()
        assert bytes(reopened.pages[0].pixmap(dpi=72)) == live_page0
        assert reopened.pages[1].text() == untouched_page_text
        assert bytes(reopened.pages[1].pixmap(dpi=72)) == untouched_page_pixels
        for xref, expected in expected_programs.items():
            prog = reopened.font_program(xref)
            assert prog is not None
            assert (prog.type, prog.data) == expected
