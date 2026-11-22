"""Styled and shaped line captions, independently extracted and rendered by Poppler."""

from __future__ import annotations

import io
import math
import shutil
import subprocess
from dataclasses import replace
from typing import Literal

import pikepdf
import pytest
from test_font_embedded import HEBREW, JAPANESE
from test_interactive_appearances import form, saved, source

import pyropdf as p


def markup(content: str) -> str:
    return f'<body xmlns="http://www.w3.org/1999/xhtml"><p>{content}</p></body>'


def poppler(tool: str, *args: str, data: bytes) -> bytes:
    executable = shutil.which(tool)
    if executable is None:
        pytest.skip(f"{tool} is unavailable")
    return subprocess.run([executable, *args], input=data, capture_output=True, check=True).stdout  # noqa: S603


def caption_text(normal: pikepdf.Object, pdf: pikepdf.Pdf) -> str:
    # Place the unchanged appearance in an otherwise blank page. Undo its
    # orientation so independent text ordering is unaffected by the line angle.
    page = pdf.add_blank_page(page_size=(400, 400))
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Caption=normal))
    orientation = next(
        [float(n) for n in op.operands] for op in pikepdf.parse_content_stream(normal) if str(op.operator) == "cm"
    )
    a, b, c, d, e, f = orientation
    page.Contents = pdf.make_stream(
        f"q {a} {c} {b} {d} {200 - a * e - b * f} {200 - c * e - d * f} cm /Caption Do Q".encode()
    )
    out = io.BytesIO()
    pdf.save(out)
    which = str(len(pdf.pages))
    return poppler("pdftotext", "-f", which, "-l", which, "-layout", "-", "-", data=out.getvalue()).decode().strip()


def pixels(data: bytes, channel: int) -> list[tuple[int, int]]:
    ppm = poppler("pdftoppm", "-r", "72", "-singlefile", "-", data=data)
    magic, size, maximum, raw = ppm.split(b"\n", 3)
    assert (magic, size, maximum) == (b"P6", b"200 200", b"255")
    return [
        (i // 3 % 200, i // 600)
        for i in range(0, len(raw), 3)
        if raw[i + channel] > 140 and all(raw[i + j] < 100 for j in range(3) if j != channel)
    ]


@pytest.mark.spec("line-annotations", "read", "interpret", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize(("leader", "position"), [(16, "Top"), (-16, "Inline")])
def test_rotated_caption_retains_styles_geometry_and_interactions(
    *, incremental: bool, leader: int, position: Literal["Top", "Inline"]
) -> None:
    original = source()
    doc = p.open(original)
    page = doc.pages[0]
    page.obj["Private"] = 27
    rich = markup('<b style="color:#00ff00;font-size:16pt">Area</b> <i style="color:#0000ff">east</i>')
    details = p.LineDetails(
        caption=True,
        rich_text=rich,
        leader_length=leader,
        leader_extension=3,
        leader_offset=2,
        position=position,
        caption_offset=(7, 4),
    )
    line = page.annotations.add(
        p.LineAnnotation(
            (25, 145), (165, 75), details=details, line_endings=(p.LineEnding.NONE, p.LineEnding.CLOSED_ARROW)
        )
    )
    line.obj["Private"] = 81
    line.set_appearance("rollover", form(doc))
    line.set_appearance("down", form(doc, "0 0 1"))
    line.line_details = replace(details, rich_text=rich.replace("east", "west"))
    data = saved(doc, original, incremental=incremental)
    reopened = p.open(data)
    assert reopened.pages[0].annotations[0].line_details == replace(details, rich_text=rich.replace("east", "west"))
    assert "original text" in reopened.pages[0].text()
    with pikepdf.open(io.BytesIO(original)) as before, pikepdf.open(io.BytesIO(data)) as pdf:
        ann = pdf.pages[0].Annots[0]
        assert "/Contents" not in ann
        assert str(ann.RC) == rich.replace("east", "west")
        assert list(ann.L) == [25, 55, 165, 125]
        assert ann.LE[1] == "/ClosedArrow"
        assert (float(ann.LL), list(ann.CO), ann.Private, pdf.pages[0].Private) == (leader, [7, 4], 81, 27)
        assert b"0 1 0 rg" in ann.AP.R.read_bytes()
        assert b"0 0 1 rg" in ann.AP.D.read_bytes()
        assert [s.read_bytes() for s in pdf.pages[0].Contents] == [s.read_bytes() for s in before.pages[0].Contents]
        normal = ann.AP.N
        faces = {str(font.BaseFont) for font in normal.Resources.Font.as_dict().values()}
        assert {"/Helvetica-Bold", "/Helvetica-Oblique"} <= faces
        assert caption_text(normal, pdf) == "Area west"
        x0, y0, x1, y1 = map(float, ann.Rect)
        assert list(map(float, normal.BBox)) == pytest.approx([0, 0, x1 - x0, y1 - y0], abs=0.001)
    green, blue = pixels(data, 1), pixels(data, 2)
    assert len(green) > 35
    assert len(blue) > 8
    # Project colored glyphs into the actual line frame. Both styles lie
    # above the positive leader or on the negative leader with its offset.
    norm = math.hypot(140, 70)
    a, b = 140 / norm, 70 / norm
    centers = []
    for points in (green, blue):
        assert all(x0 - 1 < x < x1 + 1 and y0 - 1 < 200 - y < y1 + 1 for x, y in points)
        x, y = (sum(point[k] for point in points) / len(points) for k in range(2))
        along = a * (x - 25) + b * (200 - y - 55)
        across = -b * (x - 25) + a * (200 - y - 55)
        assert leader - 4 < across < leader + 24
        centers.append(along)
    assert centers[0] < norm / 2 + 7 < centers[1]


@pytest.mark.spec("line-annotations", "read", "interpret", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_cjk_caption_reopens_with_editable_font_and_adds_unused_glyphs(*, incremental: bool) -> None:
    doc = p.open(source())
    line = doc.pages[0].annotations.add(
        p.LineAnnotation(
            (75, 105),
            (100, 105),
            contents="AVA office",
            details=p.LineDetails(caption=True, rich_text=markup("A日"), font=p.Font.from_file(JAPANESE)),
        )
    )
    line.set_appearance("down", form(doc))
    original = doc.to_bytes()
    doc = p.open(original)
    line = doc.pages[0].annotations[0]
    details = line.line_details
    assert details is not None
    assert details.font is not None
    assert details.font.name == p.Font.from_file(JAPANESE).name
    new = markup('<span style="font-size:18pt;color:#0000ff">A日本語</span>')
    line.line_details = replace(details, rich_text=new)
    line.vertices = ((65, 105), (90, 105))
    data = saved(doc, original, incremental=incremental)
    assert len(pixels(data, 2)) > 70
    with pikepdf.open(io.BytesIO(data)) as pdf:
        ann = pdf.pages[0].Annots[0]
        assert str(ann.Contents) == "AVA office"
        assert str(ann.RC) == new
        assert b"0 1 0 rg" in ann.AP.D.read_bytes()
        normal = ann.AP.N
        assert float(ann.Rect[2]) - float(ann.Rect[0]) > 57
        font = normal.Resources.Font.PyroFreeText
        assert font.Subtype == "/Type0"
        assert font.ToUnicode.read_bytes()
        program = font.DescendantFonts[0].FontDescriptor.FontFile3.read_bytes()
        embedded, full = p.Font.from_bytes(program), p.Font.from_file(JAPANESE)
        assert (embedded.glyph_count, embedded.chars()) == (full.glyph_count, full.chars())
        assert caption_text(normal, pdf) == "A日本語"
    line.line_details = replace(details, rich_text=None)
    with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
        ann = pdf.pages[0].Annots[0]
        assert "/RC" not in ann
        assert caption_text(ann.AP.N, pdf) == "AVA office"


@pytest.mark.spec("line-annotations", "read", "interpret", "create", "edit")
def test_right_to_left_styles_draw_first_word_on_the_right() -> None:
    doc = p.open(source())
    markup_text = markup('<span style="color:#00ff00">שלום</span> <b style="color:#0000ff">עולם</b>')
    doc.pages[0].annotations.add(
        p.LineAnnotation(
            (20, 110),
            (180, 110),
            details=p.LineDetails(caption=True, rich_text=markup_text, font=p.Font.from_file(HEBREW)),
        )
    )
    data = doc.to_bytes()
    with pikepdf.open(io.BytesIO(data)) as pdf:
        normal = pdf.pages[0].Annots[0].AP.N
        font = normal.Resources.Font.PyroFreeText
        program = font.DescendantFonts[0].FontDescriptor.FontFile2
        assert program.Length1 == len(program.read_bytes())
        logical = caption_text(normal, pdf).translate(dict.fromkeys(map(ord, "\u202a\u202b\u202c")))
        assert logical == "שלום עולם"
    green, blue = pixels(data, 1), pixels(data, 2)
    assert min(x for x, _ in green) > max(x for x, _ in blue)


@pytest.mark.spec("line-annotations", "read", "interpret", "create", "edit")
def test_multiline_caption_keeps_paragraph_alignment_and_decoration() -> None:
    doc = p.open(source())
    rich = (
        '<body xmlns="http://www.w3.org/1999/xhtml">'
        '<p style="text-align:left"><u style="color:#00ff00">A</u></p>'
        '<p style="text-align:right"><span style="color:#0000ff">East</span></p>'
        '<p style="font-family:serif">Longer line</p></body>'
    )
    doc.pages[0].annotations.add(
        p.LineAnnotation((20, 150), (180, 150), details=p.LineDetails(caption=True, position="Top", rich_text=rich))
    )
    data = doc.to_bytes()
    with pikepdf.open(io.BytesIO(data)) as pdf:
        normal = pdf.pages[0].Annots[0].AP.N
        assert [s.strip() for s in caption_text(normal, pdf).splitlines() if s.strip()] == ["A", "East", "Longer line"]
        assert any(font.BaseFont == "/Times-Roman" for font in normal.Resources.Font.as_dict().values())
    green, blue = pixels(data, 1), pixels(data, 2)
    assert max(x for x, _ in green) < min(x for x, _ in blue)
    assert max(y for _, y in green) < min(y for _, y in blue)


@pytest.mark.spec("line-annotations", "read", "interpret", "create", "edit")
def test_stream_markup_and_atomic_failures() -> None:
    doc = p.open(source())
    line = doc.pages[0].annotations.add(p.LineAnnotation((35, 100), (160, 100), contents="fallback"))
    other = doc.pages[0].annotations.add(p.LineAnnotation((35, 80), (160, 80)))
    shared = doc.objects[doc.objects.add_stream(markup("stream").encode("utf-16"), {"Private": 5})]
    line.obj["RC"] = other.obj["RC"] = shared
    assert line.line_details is not None
    assert line.line_details.rich_text == markup("stream")
    details = p.LineDetails(caption=True, rich_text=markup("First"))
    line.line_details = details
    assert isinstance(line.obj["RC"], p.Stream)
    assert line.obj["RC"]["Private"] == 5
    assert other.obj["RC"] == shared
    assert shared.read() == markup("stream").encode("utf-16")
    assert line.line_details == details
    old_ap = line.appearance_stream()
    assert old_ap is not None
    old_bytes = old_ap.read()
    for bad, match in [
        (replace(details, rich_text=markup("<b>bad")), "rich|XML|end tag"),
        (replace(details, caption=False, rich_text=markup("<b>hidden")), "rich|XML|end tag"),
        (replace(details, rich_text=markup("🐉"), font=p.Font.from_file(JAPANESE)), "no glyph"),
        (replace(details, rich_text=markup("日本語")), "WinAnsi"),
    ]:
        with pytest.raises(p.PdfError, match=match):
            line.line_details = bad
        assert line.line_details == details
        assert line.appearance_stream() == old_ap
        assert old_ap.read() == old_bytes
    line.line_details = replace(details, rich_text=None)
    with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
        assert caption_text(pdf.pages[0].Annots[0].AP.N, pdf) == "fallback"
