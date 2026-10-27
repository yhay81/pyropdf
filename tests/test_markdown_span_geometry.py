"""Markdown word gaps between the font spans of rotated text.

Ported from pylopdf 036be111 ``tests/test_markdown_span_geometry.py``.
"""

from __future__ import annotations

import math

import pytest
from corpus import ASSETS
from test_content import build_raw_pdf

import pyropdf


@pytest.mark.parametrize("rotation", [-30, -5, 0, 5, 30, 45, 90, 180, 270])
@pytest.mark.parametrize("gap", [0.0, 0.3])
@pytest.mark.parametrize("sizes", [(12, 12), (12, 14), (14, 12)])
def test_rotated_font_spans_preserve_word_gaps(
    rotation: int,
    gap: float,
    sizes: tuple[int, int],
) -> None:
    radians = math.radians(rotation)
    cosine, sine = math.cos(radians), math.sin(radians)
    # Short glyphs keep the complete rotated text within one extracted line.
    stream = (
        f"BT /F1 {sizes[0]} Tf "
        f"{cosine:.8f} {sine:.8f} {-sine:.8f} {cosine:.8f} 200 300 Tm "
        f"[(i) {-1000 * gap:.4f}] TJ /F2 {sizes[1]} Tf (l) Tj ET"
    )
    data = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                "<< /Type /Pages /Kids [5 0 R] /Count 1 /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>"
            ),
            3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
            5: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R >>",
            6: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        }
    )
    with pyropdf.open(data) as document:
        expected = "i l" if gap else "il"
        assert document.pages[0].text() == expected + "\n"
        assert document.markdown() == expected


@pytest.mark.parametrize("rotation", [-45, -30, 30, 45, 135])
@pytest.mark.parametrize("gap", [0.0, 0.2])
def test_rotated_embedded_font_spans_use_their_actual_line_height(rotation: int, gap: float) -> None:
    document = pyropdf.open(ASSETS / "nics-background-checks-2015-11.pdf")
    resources = document.pages[0].obj["Resources"]
    page = document.pages.new()
    page.obj["Resources"] = resources
    radians = math.radians(rotation)
    cosine, sine = math.cos(radians), math.sin(radians)
    stream = (
        f"BT /TT2 20 Tf {cosine:.8f} {sine:.8f} {-sine:.8f} {cosine:.8f} 200 300 Tm "
        f"[(1) {-1000 * gap}] TJ 1 0 0 rg (2) Tj ET"
    )
    number = document.objects.add_stream(stream.encode())
    page.obj["Contents"] = document.objects[number]

    spans = page.layout().blocks[0].lines[0].spans
    assert len(spans) == 2
    assert all(span.ascender - span.descender > 1 for span in spans)
    expected = "1 2" if gap else "12"
    assert page.text() == expected + "\n"
    assert page.markdown(table_strategy=None) == expected
