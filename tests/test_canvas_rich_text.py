"""Styled rich-text runs, UAX #9 BiDi, decorations, and inline /Link annotations on Canvas (#1246)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import get_args

import pytest

import pyropdf
from pyropdf import (
    Color,
    ColorStop,
    Destination,
    Font,
    LimitError,
    LinearGradient,
    Matrix,
    PdfError,
    Point,
    Stroke,
    TextAlign,
    TextDirection,
    TextRenderMode,
    TextSpan,
)

FONTS = Path(__file__).resolve().parent.parent / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
JAPANESE = FONTS / "NotoSansJP-subset.otf"
HEBREW = FONTS / "NotoSansHebrew-subset.ttf"
ARABIC = FONTS / "NotoSansArabic-Variable-subset.ttf"
LATIN_VAR = FONTS / "variable-latin.ttf"

RED = Color(0.85, 0.1, 0.1)
BLUE = Color(0.05, 0.32, 0.78)
GREEN = Color(0.1, 0.55, 0.2)


def _new_page(width: float = 400, height: float = 300) -> tuple[pyropdf.Document, pyropdf.Page]:
    doc = pyropdf.Document()
    return doc, doc.pages.new((width, height))


def _ink_box(page: pyropdf.Page) -> tuple[int, int, int, int]:
    pixmap = page.pixmap(dpi=72)
    data = bytes(pixmap)
    xs: list[int] = []
    ys: list[int] = []
    for y in range(pixmap.height):
        row = data[y * pixmap.stride : y * pixmap.stride + pixmap.width * pixmap.channels]
        dark = [x for x in range(pixmap.width) if min(row[x * pixmap.channels : x * pixmap.channels + 3]) < 220]
        if dark:
            xs.extend((dark[0], dark[-1]))
            ys.append(y)
    assert xs, "expected visible ink on page"
    assert ys, "expected visible ink on page"
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def test_public_types_and_textspan_validation() -> None:
    assert TextSpan.__module__ == "pyropdf"
    assert set(get_args(TextAlign)) == {"left", "center", "right", "justify", "start", "end"}
    assert set(get_args(TextDirection)) == {"auto", "ltr", "rtl"}

    span = TextSpan(
        text="hello",
        size=14,
        fill=BLUE,
        stroke=RED,
        render_mode="fill_stroke",
        underline=True,
        strikethrough=False,
        link="https://example.com",
    )
    assert span.size == 14.0
    assert span.render_mode is TextRenderMode.FILL_STROKE

    with pytest.raises(FrozenInstanceError):
        span.text = "changed"  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        TextSpan("positional")  # ty: ignore[too-many-positional-arguments, missing-argument]
    with pytest.raises(TypeError, match="text must be a str"):
        TextSpan(text=123)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="font must be"):
        TextSpan(text="a", font="Helvetica")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="size must be positive"):
        TextSpan(text="a", size=0)
    with pytest.raises(ValueError, match="size"):
        TextSpan(text="a", size=float("nan"))
    with pytest.raises(ValueError, match="render_mode"):
        TextSpan(text="a", render_mode="bogus")
    with pytest.raises(TypeError, match="underline"):
        TextSpan(text="a", underline=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="strikethrough"):
        TextSpan(text="a", strikethrough="yes")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="URI"):
        TextSpan(text="a", link="")
    with pytest.raises(TypeError, match="target"):
        TextSpan(text="a", link=42)  # ty: ignore[invalid-argument-type]


def test_multilingual_rich_spans_extract_and_render_standard14_plus_opentype() -> None:
    doc, page = _new_page(500, 300)
    jp = Font.from_file(JAPANESE)
    he = Font.from_file(HEBREW)
    ar = Font.from_file(ARABIC)
    helv = Font.standard("Helvetica")
    helv_bold = Font.standard("Helvetica-Bold")

    with page.draw() as canvas:
        fit = canvas.textbox(
            (30, 30, 470, 270),
            [
                TextSpan(text="Invoice #2026-042: ", font=helv_bold, size=14, fill=Color(0, 0, 0)),
                TextSpan(text="日本語 ", font=jp, size=13, fill=BLUE),
                TextSpan(text="שָׁלוֹם ", font=he, size=14, fill=GREEN),
                TextSpan(text="مرحبا\n", font=ar, size=14, fill=RED),
                TextSpan(text="שָׁלוֹם עולם\n", font=he, size=14, fill=GREEN),
                TextSpan(text="مرحبا بالعالم\n", font=ar, size=14, fill=RED),
                TextSpan(text="مرحبا ", font=ar, size=14, fill=RED),
                TextSpan(text="2026", font=helv, size=14, fill=Color(0, 0, 0)),
                TextSpan(text=" بالعالم", font=ar, size=14, fill=RED),
            ],
            font=helv,
            size=12,
            align="start",
            base_direction="auto",
        )
        assert fit.fits

    extracted = page.text()
    lines = extracted.splitlines()
    assert len(lines) == 4
    assert "Invoice #2026-042:" in lines[0]
    assert "日本語" in lines[0]
    # Pure RTL lines and RTL lines with embedded forward token restore exact logical order.
    assert lines[1] == "שָׁלוֹם עולם"
    assert lines[2] == "مرحبا بالعالم"
    assert lines[3] == "مرحبا 2026 بالعالم"

    # Visual UAX #9 order on line 0: Latin -> Japanese -> Arabic (left) -> Hebrew (right).
    line0_runs = [r for r in page.content().text_runs if r.bbox.y0 < 60]
    ar_x = min(r.bbox.x0 for r in line0_runs if any(c in "مرحبا" for c in r.text))
    he_x = min(r.bbox.x0 for r in line0_runs if any(c in "שלום" for c in r.text))
    assert ar_x < he_x

    # Verify visual ink renders cleanly inside the textbox.
    x0, y0, x1, y1 = _ink_box(page)
    assert 25 <= x0 < x1 <= 475
    assert 25 <= y0 < y1 <= 275

    # Round-trip save and reopen keeps exact text extraction (and Poppler reads logical BiDi).
    saved = doc.to_bytes()
    reopened = pyropdf.open(saved)
    re_lines = reopened.pages[0].text().splitlines()
    assert re_lines == lines
    pdftotext = shutil.which("pdftotext")
    if pdftotext is not None:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(saved)
            handle.flush()
            proc = subprocess.run(  # noqa: S603 - a fixed tool on a temp PDF owned by the test.
                [pdftotext, "-enc", "UTF-8", handle.name, "-"],
                capture_output=True,
                text=True,
                check=True,
            )
        poppler = proc.stdout.translate(dict.fromkeys(map(ord, "\u202a\u202b\u202c")))
        assert "Invoice #2026-042:" in poppler
        assert "日本語" in poppler
        assert "עולם" in poppler
        assert "مرحبا بالعالم" in poppler


def test_bidi_base_direction_and_start_end_justify_alignment() -> None:
    _doc, page = _new_page(400, 300)
    ar = Font.from_file(ARABIC)
    he = Font.from_file(HEBREW)
    helv = Font.standard("Helvetica")

    with page.draw() as canvas:
        # 1. RTL paragraph with align="start" -> right-aligned to x=360.
        fit_rtl = canvas.textbox(
            (40, 20, 360, 70),
            [TextSpan(text="שלום עולם", font=he, size=14)],
            align="start",
            base_direction="auto",
        )
        assert fit_rtl.fits

        # 2. RTL paragraph with align="end" -> left-aligned to x=40.
        fit_end = canvas.textbox(
            (40, 80, 360, 130),
            [TextSpan(text="שלום עולם", font=he, size=14)],
            align="end",
            base_direction="auto",
        )
        assert fit_end.fits

        # 3. Forced RTL on digits-first mixed line ("2026 مرحبا").
        fit_forced = canvas.textbox(
            (40, 140, 360, 190),
            [TextSpan(text="2026 ", font=helv, size=13), TextSpan(text="مرحبا", font=ar, size=13)],
            align="start",
            base_direction="rtl",
        )
        assert fit_forced.fits

        # 4. Justified multi-line Arabic paragraph stretches wrapped line across box width.
        fit_just = canvas.textbox(
            (40, 200, 180, 290),
            [TextSpan(text="مرحبا بالعالم مرحبا بالعالم مرحبا بالعالم", font=ar, size=13)],
            align="justify",
            base_direction="auto",
        )
        assert fit_just.fits

    runs = page.content().text_runs
    rtl_start_runs = [r for r in runs if 20 <= r.bbox.y0 <= 70]
    rtl_end_runs = [r for r in runs if 80 <= r.bbox.y0 <= 130]
    forced_runs = [r for r in runs if 140 <= r.bbox.y0 <= 190]

    assert rtl_start_runs
    assert max(r.bbox.x1 for r in rtl_start_runs) > 340
    assert rtl_end_runs
    assert min(r.bbox.x0 for r in rtl_end_runs) < 60
    assert forced_runs
    assert max(r.bbox.x1 for r in forced_runs) > 340
    # Justified paragraph wrapped onto at least 2 lines; first line stretches across [40, 180],
    # last line aligns to physical right (Start for RTL).
    just_lines = sorted(
        (line for block in page.layout().blocks for line in block.lines if 200 <= line.bbox.y0 <= 290),
        key=lambda line: line.bbox.y0,
    )
    assert len(just_lines) >= 2
    assert just_lines[0].bbox.x0 < 45
    assert just_lines[0].bbox.x1 > 175
    assert just_lines[-1].bbox.x1 > 175
    assert just_lines[-1].bbox.x0 > 90


def test_direction_vertical_validation_with_base_direction_and_spans() -> None:
    _doc, page = _new_page()
    jp = Font.from_file(JAPANESE)

    with page.draw() as canvas:
        # base_direction="rtl" is refused with direction="vertical" on both text() and textbox().
        with pytest.raises(ValueError, match="base_direction='rtl'"):
            canvas.text("日本語", (100, 30), font=jp, direction="vertical", base_direction="rtl")
        with pytest.raises(ValueError, match="base_direction='rtl'"):
            canvas.textbox((20, 20, 200, 200), "日本語", font=jp, direction="vertical", base_direction="rtl")

        # TextSpan sequences with direction="vertical" raise ValueError explicitly.
        with pytest.raises(ValueError, match="horizontal"):
            canvas.text([TextSpan(text="日本語", font=jp)], (100, 30), direction="vertical")
        with pytest.raises(ValueError, match="horizontal"):
            canvas.textbox((20, 20, 200, 200), [TextSpan(text="日本語", font=jp)], direction="vertical")

        # Valid vertical call with base_direction="auto" or "ltr" still works.
        canvas.text("日本語", (100, 30), font=jp, direction="vertical", base_direction="auto")
        canvas.text("かな", (60, 30), font=jp, direction="vertical", base_direction="ltr")

    assert "日本語" in page.text()
    assert "かな" in page.text()


def test_underline_and_strikethrough_roundtrip_in_page_layout_styles() -> None:
    _doc, page = _new_page(450, 200)
    jp = Font.from_file(JAPANESE)
    helv = Font.standard("Helvetica")

    with page.draw() as canvas:
        fit = canvas.textbox(
            (30, 30, 420, 170),
            [
                TextSpan(text="Plain ", font=helv, size=14),
                TextSpan(text="Underlined ", font=helv, size=14, fill=BLUE, underline=True),
                TextSpan(text="Struck ", font=helv, size=14, fill=RED, strikethrough=True),
                TextSpan(text="Both ", font=helv, size=14, underline=True, strikethrough=True),
                TextSpan(text="office", font=jp, size=14, underline=True, strikethrough=True),
            ],
        )
        assert fit.fits

    layout = page.layout(styles=True)
    spans = [span for block in layout.blocks for line in block.lines for span in line.spans]
    underlined_text = "".join(s.text for s in spans if s.is_underlined)
    struck_text = "".join(s.text for s in spans if s.is_struck)
    plain_only = [s for s in spans if "Plain" in s.text]

    assert plain_only
    assert all(not s.is_underlined and not s.is_struck for s in plain_only)
    assert "Underlined" in underlined_text
    assert "Struck" in struck_text
    assert "Both" in underlined_text
    assert "Both" in struck_text
    assert "office" in underlined_text
    assert "office" in struck_text


def test_inline_render_mode_stroke_and_invisible_inside_filled_paragraph() -> None:
    _doc, page = _new_page(450, 150)
    helv = Font.standard("Helvetica")

    with page.draw() as canvas:
        canvas.text(
            [
                TextSpan(text="Filled "),
                TextSpan(text="OUTLINE ", stroke=Stroke(paint=RED, width=0.75), render_mode=TextRenderMode.STROKE),
                TextSpan(text="HIDDEN ", render_mode=TextRenderMode.INVISIBLE, underline=True),
                TextSpan(text="Normal", fill=BLUE),
            ],
            (30, 60),
            font=helv,
            size=18,
            fill=Color(0, 0, 0),
        )

    runs = page.content().text_runs
    modes = [r.render_mode for r in runs]
    assert TextRenderMode.FILL in modes
    assert TextRenderMode.STROKE in modes
    assert TextRenderMode.INVISIBLE in modes

    stroke_run = next(r for r in runs if r.render_mode is TextRenderMode.STROKE)
    assert stroke_run.fill is None
    assert stroke_run.stroke is not None
    assert stroke_run.stroke.width == pytest.approx(0.75)

    # Invisible span emits no underline path (api-spec §2.5), while text remains extractable.
    assert page.content().paths == ()
    extracted = page.text()
    assert "Filled" in extracted
    assert "OUTLINE" in extracted
    assert "HIDDEN" in extracted
    assert "Normal" in extracted


def test_inline_links_atomic_commit_wrap_bidi_transform_and_rollback() -> None:
    doc, page = _new_page(400, 300)
    doc.pages.new((400, 300))
    he = Font.from_file(HEBREW)
    helv = Font.standard("Helvetica")

    # 1. Overflow textbox draws nothing and emits ZERO /Link annotations.
    before_objs = len(doc.objects)
    with page.draw() as canvas:
        overflow = canvas.textbox(
            (30, 20, 90, 32),
            [
                TextSpan(
                    text="Too much linked text to ever fit inside a tiny box",
                    font=helv,
                    size=14,
                    underline=True,
                    link="https://example.com/overflow",
                )
            ],
        )
        assert not overflow.fits
    assert len(page.links) == 0
    assert page.text() == ""
    assert len(doc.objects) == before_objs

    # 2. Exception inside Canvas aborts BOTH drawn text and queued /Link annotations.
    def _abort_draw() -> None:
        with page.draw() as canvas:
            canvas.text(
                [TextSpan(text="Rolled back link", font=helv, link="https://example.com/abort")],
                (30, 50),
            )
            msg = "abort"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="abort"):
        _abort_draw()
    assert len(page.links) == 0
    assert page.text() == ""

    # 3. Clean commit emits /Link annotations across line wrap, BiDi split, and canvas.transform()
    #    in ONE single undo step.
    undo_before = len(doc.history.steps)
    with page.draw() as canvas:
        fit = canvas.textbox(
            (30, 30, 145, 200),
            [
                TextSpan(text="Visit ", font=helv, size=12),
                TextSpan(
                    text="click here שלום click here again",
                    font=helv,
                    size=12,
                    fill=BLUE,
                    underline=True,
                    link="https://example.com/wrapped",
                ),
            ],
            fallback_fonts=(he,),
        )
        assert fit.fits
        with canvas.transform(Matrix(1, 0, 0, 1, 100, 200)):
            canvas.text(
                [TextSpan(text="Jump p2", font=helv, size=12, link=Destination(1))],
                (10, 20),
            )

    assert len(doc.history.steps) == undo_before + 1
    links = list(page.links)
    wrapped_links = [lk for lk in links if lk.target == "https://example.com/wrapped"]
    dest_links = [lk for lk in links if lk.target == Destination(1)]
    # Wrapped across lines + BiDi split produces >= 3 link hit-boxes.
    assert len(wrapped_links) >= 3
    for lk in wrapped_links:
        assert 25 <= lk.rect.x0 < lk.rect.x1 <= 150
        assert 25 <= lk.rect.y0 < lk.rect.y1 <= 205
    # Transformed link translated by (+100, +200) from origin (10, 20) -> around x=110, y=220.
    assert len(dest_links) == 1
    assert 108 <= dest_links[0].rect.x0 <= 112
    assert 200 <= dest_links[0].rect.y0 < 220 < dest_links[0].rect.y1 <= 230

    # Single undo removes BOTH the text AND every /Link annotation atomically!
    doc.history.undo()
    assert len(page.links) == 0
    assert page.text() == ""


def test_rich_text_gradient_and_rotate() -> None:
    _doc, page = _new_page(300, 300)
    helv = Font.standard("Helvetica")
    grad = LinearGradient(
        start=Point(0, 0),
        end=Point(200, 0),
        stops=(ColorStop(0.0, RED), ColorStop(1.0, BLUE)),
    )

    with page.draw() as canvas:
        fit = canvas.textbox(
            (40, 40, 240, 240),
            [
                TextSpan(text="Gradient ", font=helv, size=16, fill=grad, underline=True),
                TextSpan(text="Rotated", font=helv, size=16, fill=GREEN, link="https://example.com/rot"),
            ],
            rotate=90,
        )
        assert fit.fits

    assert len(page.links) == 1
    lk = page.links[0]
    assert 35 <= lk.rect.x0 < lk.rect.x1 <= 245
    assert 35 <= lk.rect.y0 < lk.rect.y1 <= 245
    assert "Gradient" in page.text()
    assert "Rotated" in page.text()


def test_rich_text_f6_limits_and_error_handling() -> None:
    helv = Font.standard("Helvetica")
    jp = Font.from_file(JAPANESE)

    # 1. text_input_size limit across combined spans
    _doc, page = _new_page()
    with page.draw() as canvas, pytest.raises(LimitError) as exc_info:
        canvas.textbox(
            (20, 20, 200, 200),
            [TextSpan(text="123456"), TextSpan(text="78901")],
            font=helv,
            max_text_size=10,
        )
    assert exc_info.value.code == "text_input_size"

    # 2. Span count limit (content_element_count)
    with page.draw() as canvas, pytest.raises(LimitError) as exc_info:
        canvas.text([TextSpan(text="")] * 12, (20, 40), font=helv, max_text_size=10)
    assert exc_info.value.code == "content_element_count"

    # 3. text_line_count limit (default 4,096 lines)
    with page.draw() as canvas, pytest.raises(LimitError) as exc_info:
        canvas.textbox((10, 10, 200, 250), [TextSpan(text="\n" * 4096 + "x")], font=helv, size=14)
    assert exc_info.value.code == "text_line_count"

    # 4. shaped_glyph_count limit (default 1,048,576 glyphs when max_text_size is lifted)
    with page.draw() as canvas, pytest.raises(LimitError) as exc_info:
        canvas.text([TextSpan(text="A" * 1_048_577, font=jp)], (20, 50), size=12, max_text_size=None)
    assert exc_info.value.code == "shaped_glyph_count"

    # 5. Standard 14 span without fallback refuses non-WinAnsi with ValueError
    with page.draw() as canvas, pytest.raises(ValueError, match="WinAnsi"):
        canvas.text([TextSpan(text="日本", font=helv)], (20, 50))

    # 6. Narrow textbox refuses with PdfError
    with page.draw() as canvas, pytest.raises(PdfError, match="too narrow"):
        canvas.textbox((10, 10, 12, 100), [TextSpan(text="Wide", font=helv, size=14)])

    # 7. Out-of-range destination link target refuses before drawing anything
    with page.draw() as canvas, pytest.raises(ValueError, match="out of range"):
        canvas.text([TextSpan(text="Bad link", font=helv, link=Destination(99))], (20, 50))
    assert page.text() == ""
    assert len(page.links) == 0


def test_text_mixed_sizes_and_variable_font_instance() -> None:
    _doc, page = _new_page(400, 200)
    regular = Font.from_file(LATIN_VAR, weight=400)
    bold = Font.from_file(LATIN_VAR, weight=700)

    with page.draw() as canvas:
        canvas.text(
            [
                TextSpan(text="AV ", font=regular, size=10),
                TextSpan(text="AV", font=bold, size=24),
            ],
            (30, 80),
        )

    runs = page.content().text_runs
    assert len(runs) == 2
    # Both runs share the exact same baseline y = 80.
    assert runs[0].glyphs[0].origin.y == pytest.approx(80.0, abs=0.05)
    assert runs[1].glyphs[0].origin.y == pytest.approx(80.0, abs=0.05)
    assert runs[1].glyphs[0].size == pytest.approx(24.0, abs=0.05)
    assert page.text().strip() == "AV AV"
