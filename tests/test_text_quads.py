"""Quads of extracted text, which follow rotated baselines.

Ported from pylopdf 036be111 ``tests/test_text_quads.py``; the search tests
belong to #100.
"""

from __future__ import annotations

import math

import pytest
from test_text import build_text_pdf

import pyropdf


def _rotated(degrees: float, text: str = "Rotated words") -> pyropdf.Page:
    """Return a page with ``text`` at 20 points, turned ``degrees`` counterclockwise in user space."""
    cos, sin = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return pyropdf.open(
        build_text_pdf(f"BT /F1 20 Tf {cos:.6f} {sin:.6f} {-sin:.6f} {cos:.6f} 200 300 Tm ({text}) Tj ET")
    ).pages[0]


def _inside(point: pyropdf.Point, quad: pyropdf.Quad, tolerance: float = 1e-6) -> bool:
    """Return whether ``point`` lies in a rectangular quad, up to rounding."""
    along = quad.ur - quad.ul
    across = quad.ll - quad.ul
    offset = point - quad.ul
    u = (offset.x * along.x + offset.y * along.y) / (along.x**2 + along.y**2)
    v = (offset.x * across.x + offset.y * across.y) / (across.x**2 + across.y**2)
    return -tolerance <= u <= 1 + tolerance and -tolerance <= v <= 1 + tolerance


@pytest.mark.parametrize("degrees", [0, 30, 90, 135, 180, 270])
def test_quads_follow_the_baseline_of_rotated_text(degrees: int) -> None:
    page = _rotated(degrees)
    (line,) = page.layout(chars=True).blocks[0].lines
    span = line.spans[0]

    # The quad's top edge runs along the baseline direction, one em tall.
    angle = math.atan2(line.quad.ur.y - line.quad.ul.y, line.quad.ur.x - line.quad.ul.x)
    assert math.atan2(line.direction.y, line.direction.x) == pytest.approx(angle, abs=1e-9)
    assert math.dist(line.quad.ul, line.quad.ll) == pytest.approx(20)
    assert math.dist(line.quad.ur, line.quad.lr) == pytest.approx(20)
    # Every glyph's origin lies in the quad.
    for char in span.chars:
        assert _inside(char.origin, line.quad)
    assert line.quad.rect == pytest.approx(line.bbox)
    assert span.quad == line.quad


def test_horizontal_quads_are_the_rectangles() -> None:
    page = pyropdf.open(build_text_pdf("BT /F1 20 Tf 72 700 Td (one two one) Tj ET")).pages[0]

    (line,) = page.layout().blocks[0].lines

    assert line.quad.is_rectangular
    assert line.quad.rect == line.bbox
