"""Text replacement for composite (Type 0 / CID), Type 3, vertical-writing, fallback, and /ActualText (#1180)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from test_replace_text import HELVETICA, assert_same_box, build_raw_pdf, stream

import pyropdf
from pyropdf import Font, Name, Operation, Operations, String

ROOT = Path(__file__).resolve().parent.parent
JP_FONT_PATH = ROOT / "crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf"
LATIN_TTF_PATH = ROOT / "crates/pyropdf-engine/src/fonts/testdata/variable-latin.ttf"
HEBREW_TTF_PATH = ROOT / "crates/pyropdf-engine/src/fonts/testdata/NotoSansHebrew-subset.ttf"

REQUIRES_POPPLER = pytest.mark.skipif(
    shutil.which("pdftoppm") is None or shutil.which("pdftotext") is None,
    reason="poppler's pdftoppm and pdftotext are required",
)


def _jp_font() -> Font:
    return Font.from_file(JP_FONT_PATH)


def _latin_ttf() -> Font:
    return Font.from_file(LATIN_TTF_PATH)


def _hebrew_ttf() -> Font:
    return Font.from_file(HEBREW_TTF_PATH)


def _char_boxes(page: pyropdf.Page) -> list[tuple[str, pyropdf.Rect]]:
    return [(char.text, char.bbox) for char in page.chars() if not char.is_synthetic]


def _poppler_text(pdf_path: Path) -> str:
    return subprocess.run(  # noqa: S603 - fixed executable on test-owned path.
        [shutil.which("pdftotext") or "pdftotext", str(pdf_path), "-"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")


def _poppler_gray(pdf_path: Path, dpi: int = 72) -> tuple[int, int, bytes]:
    pgm = subprocess.run(  # noqa: S603 - fixed executable on test-owned path.
        [shutil.which("pdftoppm") or "pdftoppm", "-gray", "-r", str(dpi), "-singlefile", str(pdf_path)],
        check=True,
        capture_output=True,
    ).stdout
    header = pgm.split(maxsplit=4)
    width, height = int(header[1]), int(header[2])
    return width, height, pgm[-width * height :]


def _ink_box(samples: bytes, width: int, height: int) -> tuple[int, int, int, int]:
    dark = [i for i in range(width * height) if samples[i] < 128]
    assert dark, "expected visible ink"
    return (
        min(i % width for i in dark),
        min(i // width for i in dark),
        max(i % width for i in dark),
        max(i // width for i in dark),
    )


def _assert_poppler_roundtrip(
    tmp_path: Path,
    name: str,
    data: bytes,
    expected_substrings: list[str],
    *,
    min_overlap: float = 0.55,
) -> None:
    box_tol = 3
    pdf_path = tmp_path / f"{name}.pdf"
    pdf_path.write_bytes(data)
    extracted = _poppler_text(pdf_path)
    compact = "".join(extracted.split())
    for expected in expected_substrings:
        assert expected in compact, f"{expected!r} missing from Poppler text {extracted!r}"

    reopened = pyropdf.open(data)
    pyro_compact = "".join(reopened.pages[0].text().split())
    for expected in expected_substrings:
        assert expected in pyro_compact, f"{expected!r} missing from pyropdf text {pyro_compact!r}"

    width, height, poppler = _poppler_gray(pdf_path)
    hayro = reopened.pages[0].pixmap(dpi=72).to_gray()
    assert (width, height) == (hayro.width, hayro.height)
    hayro_samples = bytes(hayro)
    pop_box = _ink_box(poppler, width, height)
    hayro_box = _ink_box(hayro_samples, width, height)
    assert all(abs(a - b) <= box_tol for a, b in zip(pop_box, hayro_box, strict=True)), (pop_box, hayro_box)
    pop_ink = {i for i, v in enumerate(poppler) if v < 128}
    hayro_ink = {i for i, v in enumerate(hayro_samples) if v < 128}
    overlap = len(pop_ink & hayro_ink) / len(pop_ink | hayro_ink)
    assert overlap >= min_overlap, f"overlap {overlap:.3f} < {min_overlap}"


def _build_type3_pdf(content: str, *, include_d: bool = True) -> bytes:
    """Return a PDF with a custom Type 3 font using non-standard FontMatrix [0.02 0 0 0.02 0 0]."""
    # Each glyph draws a distinct rectangle; width in font space is 25, 35, 40, 30 ->
    # at FontMatrix 0.02, PDF text-space advances are 500, 700, 800, 600 thousandths.
    procs = {
        "A": (6, "25 0 d0 2 0 20 30 re f"),
        "B": (7, "35 0 d0 2 0 30 30 re f"),
        "C": (8, "40 0 d0 2 0 35 30 re f"),
    }
    if include_d:
        procs["D"] = (9, "30 0 d0 2 0 25 30 re f")
    char_procs = " ".join(f"/{name} {xref} 0 R" for name, (xref, _) in procs.items())
    tu = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        "5 beginbfchar\n"
        "<41> <0041>\n<42> <0042>\n<43> <0043>\n<44> <0044>\n<01> <00660069>\n"
        "endbfchar\n"
        "endcmap\nend\nend\n"
    )
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 300 120] /Resources << /Font << /T3 3 0 R >> >> >>",
        3: (
            "<< /Type /Font /Subtype /Type3 /FontBBox [0 0 50 40] "
            "/FontMatrix [0.02 0 0 0.02 0 0] "
            f"/CharProcs << {char_procs} /fi 10 0 R >> "
            "/Encoding << /Type /Encoding /Differences [1 /fi 65 /A /B /C /D] >> "
            "/FirstChar 1 /LastChar 68 "
            "/Widths [30 " + "0 " * 63 + "25 35 40 30] "
            "/ToUnicode 11 0 R >>"
        ),
        4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
        5: stream(content),
        10: stream("30 0 d0 2 0 24 30 re f"),
        11: stream(tu),
    }
    for xref, body in procs.values():
        objects[xref] = stream(body)
    return build_raw_pdf(objects)


# ── Identity-H embedded composite fonts (CFF & TrueType) ───────────────────


@REQUIRES_POPPLER
def test_identity_h_cff_editable_extension_and_poppler_roundtrip(tmp_path: Path) -> None:
    jp = _jp_font()
    doc = pyropdf.Document()
    page = doc.pages.new((320, 120))
    with page.draw() as canvas:
        canvas.text("日本語テキスト", (20, 60), font=jp, size=20)
    before = dict(_char_boxes(page))

    # "かな" was not drawn initially; the live embedded face extends /W and /ToUnicode.
    assert page.replace_text("日本語", "かな") == 1
    assert "".join(page.text().split()) == "かなテキスト"
    assert "日本語" not in page.text()
    after = dict(_char_boxes(page))
    for ch in ("テ", "キ", "ス", "ト"):
        assert_same_box(before[ch], after[ch])

    full_bytes = doc.to_bytes()
    _assert_poppler_roundtrip(tmp_path, "id-h-cff-full", full_bytes, ["かな", "テキスト"])

    # Incremental save after another replacement on the reopened (subsetted) doc.
    reopened = pyropdf.open(full_bytes)
    assert reopened.pages[0].replace_text("かな", "テキ") == 1
    inc_bytes = reopened.to_bytes(incremental=True)
    _assert_poppler_roundtrip(tmp_path, "id-h-cff-inc", inc_bytes, ["テキ", "テキスト"])


@REQUIRES_POPPLER
def test_identity_h_truetype_embedded_and_fallback(tmp_path: Path) -> None:
    ttf = _latin_ttf()
    doc = pyropdf.Document()
    page = doc.pages.new((300, 120))
    with page.draw() as canvas:
        canvas.text("AVA VA", (20, 60), font=ttf, size=22)

    saved_bytes = doc.to_bytes()
    reopened = pyropdf.open(saved_bytes)
    page = reopened.pages[0]
    (font_info,) = page.fonts()
    assert font_info.program == "ttf"
    before_tail = _char_boxes(page)[-1][1]

    # Existing subset glyph replacement works directly.
    assert page.replace_text("AVA", "VV") == 1
    assert "VV VA" in page.text()
    assert_same_box(before_tail, _char_boxes(page)[-1][1])

    # Missing glyph refuses atomically without fallback.
    snap = reopened.to_bytes()
    with pytest.raises(ValueError, match="no code for"):
        page.replace_text("VV", "日本語")
    assert reopened.to_bytes() == snap

    # Passing fallback cleanly switches fonts and preserves tail alignment.
    assert page.replace_text("VV", "日本語", fallback=_jp_font()) == 1
    assert "日本語 VA" in page.text()
    assert_same_box(before_tail, _char_boxes(page)[-1][1])
    _assert_poppler_roundtrip(tmp_path, "id-h-ttf-fb", reopened.to_bytes(), ["日本語", "VA"])


@REQUIRES_POPPLER
def test_subsetted_identity_h_refuses_missing_and_accepts_fallback(tmp_path: Path) -> None:
    jp = _jp_font()
    doc = pyropdf.Document()
    page = doc.pages.new((320, 120))
    with page.draw() as canvas:
        canvas.text("日本語テキスト", (20, 60), font=jp, size=20)
    saved = doc.to_bytes()

    # After save + reopen, the font is subset-tagged and compacted.
    reopened = pyropdf.open(saved)
    page = reopened.pages[0]
    before = dict(_char_boxes(page))
    snap = reopened.to_bytes()
    steps = len(reopened.history.steps)

    # "かな" is missing from the compacted subset -> atomic refusal.
    with pytest.raises(ValueError, match="no code for"):
        page.replace_text("日本語", "かな")
    assert reopened.to_bytes() == snap
    assert len(reopened.history.steps) == steps

    # With fallback=_jp_font(), "日かな" mixes existing '日' and fallback 'かな'.
    assert page.replace_text("日本語", "日かな", fallback=jp) == 1
    assert "日かなテキスト" in page.text()
    after = dict(_char_boxes(page))
    for ch in ("テ", "キ", "ス", "ト"):
        assert_same_box(before[ch], after[ch])

    # Single undo step restores the exact document bytes.
    assert len(reopened.history.steps) == steps + 1
    _assert_poppler_roundtrip(tmp_path, "subset-fb-full", reopened.to_bytes(), ["日かなテキスト"])
    _assert_poppler_roundtrip(tmp_path, "subset-fb-inc", reopened.to_bytes(incremental=True), ["日かなテキスト"])
    reopened.history.undo()
    assert reopened.to_bytes() == snap


def test_subset_font_program_emptied_glyph_is_detected_as_missing() -> None:
    """A glyph whose outline was emptied by subset_font_program is refused even if ToUnicode still lists it."""
    jp = _jp_font()
    doc = pyropdf.Document()
    page = doc.pages.new((300, 120))
    with page.draw() as canvas:
        canvas.text("日本語かな", (20, 60), font=jp, size=18)
    saved = doc.to_bytes()

    # Keep only "日本語" on the page, then run subset_fonts() so "かな" outlines are emptied.
    reopened = pyropdf.open(saved)
    page = reopened.pages[0]
    assert page.replace_text("かな", "") == 1
    reopened.subset_fonts()
    # Reopen after subsetting so the emptied program is on disk.
    subset_doc = pyropdf.open(reopened.to_bytes())
    sub_page = subset_doc.pages[0]
    before = subset_doc.to_bytes()
    with pytest.raises(ValueError, match="no code for"):
        sub_page.replace_text("本", "か")
    assert subset_doc.to_bytes() == before
    # Works with fallback.
    assert sub_page.replace_text("本", "か", fallback=jp) == 1
    assert "日か語" in sub_page.text()


def test_identity_h_latin_ligature_and_multichar_replacement() -> None:
    jp = _jp_font()
    doc = pyropdf.Document()
    page = doc.pages.new((300, 120))
    with page.draw() as canvas:
        canvas.text("file filter world", (20, 60), font=jp, size=18)
    before_world = next(box for ch, box in _char_boxes(page) if ch == "w")

    assert page.replace_text("fi", "fl", count=1) == 1
    assert "flle filter world" in page.text()
    after_world = next(box for ch, box in _char_boxes(page) if ch == "w")
    assert_same_box(before_world, after_world)


# ── Vertical writing (Identity-V, WMode = 1, /DW2, /W2) ────────────────────


@REQUIRES_POPPLER
def test_vertical_identity_v_editable_and_position_compensation(tmp_path: Path) -> None:
    jp = _jp_font()
    doc = pyropdf.Document()
    page = doc.pages.new((160, 320))
    with page.draw() as canvas:
        canvas.text("日本語テキスト", (80, 30), font=jp, size=20, direction="vertical")

    before = dict(_char_boxes(page))
    # Verify glyphs actually progress vertically downward (y0 increases in display space).
    assert before["本"].y0 > before["日"].y0
    assert before["テ"].y0 > before["語"].y0

    # Replace 2 chars ("本語") with 1 new char ("か") — shorter vertical run;
    # vertical TJ compensation must keep "テキスト" at its exact Y coordinates.
    assert page.replace_text("本語", "か") == 1
    after = dict(_char_boxes(page))
    for ch in ("テ", "キ", "ス", "ト"):
        assert_same_box(before[ch], after[ch])

    # Replace 1 char ("か") with 3 chars ("かなの") — longer vertical run;
    # negative vertical TJ compensation must still keep "テキスト" exact.
    assert page.replace_text("か", "かなの") == 1
    after_long = dict(_char_boxes(page))
    for ch in ("テ", "キ", "ス", "ト"):
        assert_same_box(before[ch], after_long[ch])

    full_bytes = doc.to_bytes()
    _assert_poppler_roundtrip(tmp_path, "id-v-full", full_bytes, ["日", "か", "な", "の", "テ", "キ", "ス", "ト"])


@REQUIRES_POPPLER
def test_vertical_identity_v_subsetted_refuses_and_uses_vertical_fallback(tmp_path: Path) -> None:
    jp = _jp_font()
    doc = pyropdf.Document()
    page = doc.pages.new((160, 320))
    with page.draw() as canvas:
        canvas.text("日本語テキスト", (80, 30), font=jp, size=20, direction="vertical")
    saved = doc.to_bytes()

    reopened = pyropdf.open(saved)
    page = reopened.pages[0]
    before = dict(_char_boxes(page))
    snap = reopened.to_bytes()

    # Missing glyph refuses atomically without fallback.
    with pytest.raises(ValueError, match="no code for"):
        page.replace_text("本語", "かな")
    assert reopened.to_bytes() == snap

    # Standard 14 fallback is rejected for vertical text atomically.
    with pytest.raises(ValueError, match="Standard 14"):
        page.replace_text("本語", "かな", fallback=Font.standard("Helvetica"))
    assert reopened.to_bytes() == snap

    # Embedded OpenType fallback switches to Identity-V and preserves tail Y boxes.
    assert page.replace_text("本語", "かな", fallback=jp) == 1
    after = dict(_char_boxes(page))
    for ch in ("テ", "キ", "ス", "ト"):
        assert_same_box(before[ch], after[ch])

    inc_bytes = reopened.to_bytes(incremental=True)
    _assert_poppler_roundtrip(tmp_path, "id-v-fb-inc", inc_bytes, ["日", "か", "な", "テ", "キ", "ス", "ト"])


# ── Predefined CMaps and custom CMap streams ───────────────────────────────


def test_predefined_unijis_utf16_h_and_90ms_rksj_v() -> None:
    utf16_hex = "日本語テスト".encode("utf-16-be").hex().upper()
    doc_h = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 400 120] "
                "/Resources << /Font << /F1 3 0 R >> >> >>",
                3: "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Mincho /Encoding /UniJIS-UTF16-H "
                "/DescendantFonts [6 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
                5: stream(f"BT /F1 20 Tf 20 60 Td <{utf16_hex}> Tj ET"),
                6: "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MS-Mincho"
                " /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >>"
                " /FontDescriptor 7 0 R /DW 1000 >>",
                7: "<< /Type /FontDescriptor /FontName /MS-Mincho /Flags 6 /FontBBox [0 -137 1000 859]"
                " /ItalicAngle 0 /Ascent 859 /Descent -140 /CapHeight 769 /StemV 78 >>",
            }
        )
    )
    page_h = doc_h.pages[0]
    before_h = dict(_char_boxes(page_h))
    assert page_h.replace_text("本語", "東京") == 1
    assert "日東京テスト" in page_h.text()
    after_h = dict(_char_boxes(page_h))
    assert_same_box(before_h["テ"], after_h["テ"])

    # Vertical predefined CMap: 90ms-RKSJ-V with /DW2 [880 -1000].
    sjis_hex = "日本語テスト".encode("cp932").hex().upper()
    doc_v = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 160 320] "
                "/Resources << /Font << /F1 3 0 R >> >> >>",
                3: "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Mincho /Encoding /90ms-RKSJ-V "
                "/DescendantFonts [6 0 R] >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
                5: stream(f"BT /F1 20 Tf 80 280 Td <{sjis_hex}> Tj ET"),
                6: "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MS-Mincho"
                " /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >>"
                " /FontDescriptor 7 0 R /DW 1000 /DW2 [880 -1000] >>",
                7: "<< /Type /FontDescriptor /FontName /MS-Mincho /Flags 6 /FontBBox [0 -137 1000 859]"
                " /ItalicAngle 0 /Ascent 859 /Descent -140 /CapHeight 769 /StemV 78 >>",
            }
        )
    )
    page_v = doc_v.pages[0]
    before_v = dict(_char_boxes(page_v))
    assert before_v["テ"].y0 > before_v["日"].y0
    assert page_v.replace_text("本語", "京") == 1
    assert "".join(page_v.text().split()) == "日京テスト"
    after_v = dict(_char_boxes(page_v))
    assert_same_box(before_v["テ"], after_v["テ"])


def test_custom_embedded_cmap_stream_with_ligatures() -> None:
    cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CMapName /Custom-H def\n/CMapType 1 def\n/WMode 0 def\n"
        "1 begincodespacerange\n<0001> <0010>\nendcodespacerange\n"
        "4 begincidchar\n<0001> 1\n<0002> 2\n<0003> 3\n<0004> 4\nendcidchar\n"
        "endcmap\nend\nend\n"
    )
    tounicode = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "1 begincodespacerange\n<0001> <0010>\nendcodespacerange\n"
        "4 beginbfchar\n<0001> <00660069>\n<0002> <0066006C>\n<0003> <006E>\n<0004> <0064>\nendbfchar\n"
        "endcmap\nend\nend\n"
    )
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 300 120] "
                "/Resources << /Font << /F1 3 0 R >> >> >>",
                3: "<< /Type /Font /Subtype /Type0 /BaseFont /Custom /Encoding 8 0 R "
                "/DescendantFonts [6 0 R] /ToUnicode 9 0 R >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
                5: stream("BT /F1 20 Tf 20 60 Td <000100030004> Tj ET"),
                6: "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Custom"
                " /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
                " /FontDescriptor 7 0 R /DW 600 /W [1 [500 700 550 550]] >>",
                7: "<< /Type /FontDescriptor /FontName /Custom /Flags 4 /FontBBox [0 0 1000 1000]"
                " /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>",
                8: stream(cmap),
                9: stream(tounicode),
            }
        )
    )
    page = doc.pages[0]
    assert page.text().strip() == "find"
    before = dict(_char_boxes(page))
    # Replace ligature "fi" (width 500) with ligature "fl" (width 700); 'n' and 'd' stay aligned.
    assert page.replace_text("fi", "fl") == 1
    assert page.text().strip() == "flnd"
    after = dict(_char_boxes(page))
    assert_same_box(before["n"], after["n"])
    assert_same_box(before["d"], after["d"])


# ── Type 3 fonts ───────────────────────────────────────────────────────────


@REQUIRES_POPPLER
def test_type3_font_matrix_widths_multiglyph_fallback_and_poppler(tmp_path: Path) -> None:
    doc = pyropdf.open(_build_type3_pdf("BT /T3 20 Tf 20 60 Td (ABCD) Tj ET"))
    page = doc.pages[0]
    assert page.text().strip() == "ABCD"
    before_d = next(box for ch, box in _char_boxes(page) if ch == "D")

    # Replace 2 glyphs 'AB' (advance 500 + 700 = 1200) with 2 glyphs 'DA' (advance 600 + 500 = 1100);
    # 'D' at the end must keep its exact display position via TJ compensation.
    assert page.replace_text("AB", "DA") == 1
    assert "".join(page.text().split()) == "DACD"
    after_d = [box for ch, box in _char_boxes(page) if ch == "D"][-1]
    assert_same_box(before_d, after_d)
    _assert_poppler_roundtrip(tmp_path, "type3-full", doc.to_bytes(), ["DACD"])

    # Also test 1-to-multi-glyph replacement ("A" -> "BD") keeping tail glyph C's box exact.
    doc_multi = pyropdf.open(_build_type3_pdf("BT /T3 20 Tf 20 60 Td (ABC) Tj ET"))
    page_multi = doc_multi.pages[0]
    before_c = next(box for ch, box in _char_boxes(page_multi) if ch == "C")
    assert page_multi.replace_text("A", "BD") == 1
    assert "".join(page_multi.text().split()) == "BDBC"
    after_c = next(box for ch, box in _char_boxes(page_multi) if ch == "C")
    assert_same_box(before_c, after_c)

    # Type 3 font missing /D from /CharProcs refuses 'D' atomically unless fallback is given.
    doc_missing = pyropdf.open(_build_type3_pdf("BT /T3 20 Tf 20 60 Td (ABC) Tj ET", include_d=False))
    page_m = doc_missing.pages[0]
    snap = doc_missing.to_bytes()
    with pytest.raises(ValueError, match="no code for"):
        page_m.replace_text("A", "D")
    assert doc_missing.to_bytes() == snap
    assert page_m.replace_text("A", "D", fallback=Font.standard("Helvetica")) == 1
    assert "".join(page_m.text().split()) == "DBC"
    _assert_poppler_roundtrip(tmp_path, "type3-fb", doc_missing.to_bytes(), ["DBC"])


def test_type3_tounicode_ligature_replacement() -> None:
    doc = pyropdf.open(_build_type3_pdf("BT /T3 20 Tf 20 60 Td <0143> Tj ET"))
    page = doc.pages[0]
    assert page.text().strip() == "fiC"
    before_c = next(box for ch, box in _char_boxes(page) if ch == "C")
    assert page.replace_text("fi", "AB") == 1
    assert page.text().strip() == "ABC"
    after_c = next(box for ch, box in _char_boxes(page) if ch == "C")
    assert_same_box(before_c, after_c)


# ── /ActualText spans and /MCID structure preservation ─────────────────────


@REQUIRES_POPPLER
def test_actual_text_single_and_multi_op_spans_and_mcid_preservation(tmp_path: Path) -> None:
    # Multi-op /ActualText span carrying /MCID 7 followed by trailing text.
    content = (
        "BT /F1 20 Tf 20 60 Td /Span << /MCID 7 /ActualText (Hello PDF) >> BDC (Hello ) Tj (PDF) Tj EMC ( tail) Tj ET"
    )
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 360 120] "
                "/Resources << /Font << /F1 3 0 R >> >> >>",
                3: HELVETICA,
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
                5: stream(content),
            }
        )
    )
    page = doc.pages[0]
    assert "Hello PDF tail" in page.text()
    before_tail = next(box for ch, box in _char_boxes(page) if ch == "t")

    assert page.replace_text("PDF", "World") == 1
    assert "Hello World tail" in page.text()
    after_tail = next(box for ch, box in _char_boxes(page) if ch == "t")
    assert_same_box(before_tail, after_tail)

    # Verify /MCID 7 is preserved on the updated BDC dictionary.
    bdc_ops = [op for op in page.operations if isinstance(op, Operation) and op.operator == "BDC"]
    assert len(bdc_ops) == 1
    assert bdc_ops[0].operands[1]["MCID"] == 7
    _assert_poppler_roundtrip(tmp_path, "actual-text-multi", doc.to_bytes(), ["HelloWorldtail"])


@REQUIRES_POPPLER
def test_enclosing_structure_mcid_preserved_across_fallback_switch(tmp_path: Path) -> None:
    jp = _jp_font()
    doc = pyropdf.Document()
    page = doc.pages.new((320, 120))
    with page.draw() as canvas, canvas.tag("P"):
        canvas.text("日本語テキスト", (20, 60), font=jp, size=18)
    saved = doc.to_bytes()

    reopened = pyropdf.open(saved)
    page = reopened.pages[0]
    assert page.replace_text("日本語", "かなの", fallback=jp) == 1
    assert "かなのテキスト" in page.text()

    # Check that the copied tagged Form XObject keeps /P << /MCID 0 >> BDC ... EMC
    # around the edited text + Tf switches, AND StructElem's MCR /Stm repointed to it.
    resaved_bytes = reopened.to_bytes()
    resaved = pyropdf.open(resaved_bytes)
    assert resaved.check().defects == ()
    copied_form = resaved.pages[0].obj["Resources"]["XObject"]["PyroX1_1"]
    form_ops = [op for op in Operations.parse(copied_form.read()) if isinstance(op, Operation)]
    bdc_idx = next(i for i, op in enumerate(form_ops) if op.operator == "BDC" and op.operands[0] == Name("P"))
    emc_idx = next(i for i in range(bdc_idx + 1, len(form_ops)) if form_ops[i].operator == "EMC")
    inner_ops = [op.operator for op in form_ops[bdc_idx + 1 : emc_idx]]
    assert "TJ" in inner_ops
    assert "Tf" in inner_ops
    assert form_ops[bdc_idx].operands[1]["MCID"] == 0
    (elem,) = list(resaved.structure)
    mcr = elem.obj["K"][0]
    assert mcr["MCID"] == 0
    assert mcr["Stm"].xref == copied_form.xref
    _assert_poppler_roundtrip(tmp_path, "mcid-fallback", resaved_bytes, ["かなのテキスト"])


# ── Shared Form XObject + fallback & validation ────────────────────────────


def test_shared_form_xobject_copies_with_fallback_font() -> None:
    jp = _jp_font()
    form_content = "BT /F1 18 Tf 0 0 Td (Form PDF) Tj ET"
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [4 0 R 6 0 R] /Count 2 /MediaBox [0 0 320 120] "
                "/Resources << /Font << /F1 3 0 R >> /XObject << /X1 8 0 R >> >> >>",
                3: HELVETICA,
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
                5: stream("q 1 0 0 1 20 60 cm /X1 Do Q"),
                6: "<< /Type /Page /Parent 2 0 R /Contents 7 0 R >>",
                7: stream("q 1 0 0 1 20 60 cm /X1 Do Q"),
                8: stream(
                    form_content,
                    "/Type /XObject /Subtype /Form /BBox [0 -10 300 40] /Resources << /Font << /F1 3 0 R >> >> ",
                ),
            }
        )
    )

    assert doc.pages[0].replace_text("PDF", "日本語", fallback=jp) == 1
    assert "Form 日本語" in doc.pages[0].text()
    assert "Form PDF" in doc.pages[1].text()
    reopened = pyropdf.open(doc.to_bytes())
    assert "Form 日本語" in reopened.pages[0].text()
    assert "Form PDF" in reopened.pages[1].text()


def test_replace_text_validates_fallback_argument_and_missing_fallback_glyph() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 100))
    page.obj["Resources"] = {
        "Font": {"F1": {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica")}}
    }
    page.operations = Operations(
        [
            Operation("BT"),
            Operation("Tf", (Name("F1"), 14)),
            Operation("Td", (20, 50)),
            Operation("Tj", (String(b"Hello PDF"),)),
            Operation("ET"),
        ]
    )
    snap = doc.to_bytes()

    with pytest.raises(TypeError, match="fallback"):
        page.replace_text("PDF", "Cat", fallback="Helvetica")  # ty: ignore[invalid-argument-type]
    assert doc.to_bytes() == snap

    # Fallback font itself lacks Hebrew/emoji -> atomic ValueError.
    with pytest.raises(ValueError, match="fallback font"):
        page.replace_text("PDF", "🦀", fallback=_hebrew_ttf())
    assert doc.to_bytes() == snap
