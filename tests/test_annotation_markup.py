"""Underline, StrikeOut, and Squiggly annotations, drawn by the appearance settler as Highlight is.

pylopdf's tests/test_annotation_markup.py at ebb01d3c. Intended difference:
the core's checks are called as ``_core.annotation_add`` with the page's
object, not ``add_annotation`` with its number.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import pyropdf
from pyropdf import Color, Highlight, Rect, Squiggly, StrikeOut, Underline, _core

WHITE = (255, 255, 255)


def _pixel(pixmap: pyropdf.Pixmap, x: float, y: float) -> tuple[int, int, int]:
    offset = int(y) * pixmap.stride + int(x) * pixmap.channels
    red, green, blue = bytes(pixmap)[offset : offset + 3]
    return red, green, blue


def _column(pixmap: pyropdf.Pixmap, x: int, y0: int, y1: int) -> list[int]:
    """Return the rows between y0 and y1 where column x is not white."""
    return [y for y in range(y0, y1) if _pixel(pixmap, x, y) != WHITE]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_text_markup_lines_land_on_their_rectangles(rotation: int) -> None:
    page = pyropdf.Document().pages.new((300, 300))
    page.rotation = rotation

    page.annotations.add(Underline((20, 20, 120, 40)))
    page.annotations.add(StrikeOut((20, 60, 120, 80)))
    page.annotations.add(Squiggly((20, 100, 120, 120)))
    page.annotations.add(Highlight(Rect(20, 140, 120, 160).quad, color=Color(0, 1, 0), opacity=1.0))

    assert [annotation.type.name for annotation in page.annotations] == [
        "UNDERLINE",
        "STRIKE_OUT",
        "SQUIGGLY",
        "HIGHLIGHT",
    ]
    assert tuple(page.annotations[0].rect) == pytest.approx((20, 20, 120, 40), abs=1e-3)
    pixmap = page.pixmap(dpi=72)
    # The underline sits one point above the bottom edge, the strikeout in the middle.
    assert _column(pixmap, 70, 18, 42) in ([38], [38, 39], [39])
    assert _column(pixmap, 70, 58, 82) in ([69], [69, 70], [70])
    squiggle = _column(pixmap, 70, 98, 122)
    assert squiggle
    assert min(squiggle) >= 115
    assert _pixel(pixmap, 70, 150) == (0, 255, 0)
    assert _column(pixmap, 130, 0, 200) == []


def test_text_markup_survives_save_and_reopen() -> None:
    page = pyropdf.Document().pages.new((200, 200))
    page.annotations.add(Underline([(10, 10, 60, 25), (10, 30, 90, 45)], color=Color(0, 0, 0.5), contents="check"))
    page.annotations.add(Squiggly((10, 60, 90, 75), opacity=0.5))

    reopened = pyropdf.open(page.document.to_bytes()).pages[0]

    underline, squiggly = reopened.annotations
    assert underline.color == Color(0, 0, 0.5)
    assert underline.contents == "check"
    assert len(underline.obj["QuadPoints"]) == 16
    assert tuple(underline.rect) == pytest.approx((10, 10, 90, 45), abs=1e-3)
    assert underline.created is not None
    assert squiggly.color == Color(1, 0, 0)
    assert squiggly.opacity == pytest.approx(0.5)
    pixmap = reopened.pixmap(dpi=72)
    assert _column(pixmap, 30, 8, 27) in ([23], [23, 24], [24])


def test_text_markup_defaults_and_validation() -> None:
    assert Highlight((0, 0, 1, 1)).color == Color(1, 1, 0)
    assert Highlight((0, 0, 1, 1)).opacity == pytest.approx(0.4)
    assert Underline((0, 0, 1, 1)).color == Color(0, 0, 1)
    assert StrikeOut((0, 0, 1, 1)).color == Color(1, 0, 0)
    assert Squiggly((0, 0, 1, 1)).color == Color(1, 0, 0)
    assert Underline((0, 0, 1, 1)).opacity == 1.0
    with pytest.raises(ValueError, match="at least one"):
        Underline([])
    with pytest.raises(ValueError, match="greater than 0"):
        StrikeOut((0, 0, 1, 1), opacity=0)
    with pytest.raises(TypeError, match="contents"):
        Squiggly((0, 0, 1, 1), contents=cast("Any", 3))
    with pytest.raises(pyropdf.LimitError) as raised:
        Underline((0, 0, 1, 1), contents="x" * (1024 * 1024))
    assert raised.value.code == "annotation_input_size"


def test_a_squiggly_line_stops_at_the_point_limit() -> None:
    page = pyropdf.Document().pages.new((200, 200))

    with pytest.raises(pyropdf.LimitError) as raised:
        page.annotations.add(Squiggly([(0, 0, 50000, 10)] * 3))
    assert raised.value.code == "annotation_point_count"
    assert "Annots" not in page.obj


def test_the_core_repeats_the_text_markup_checks() -> None:
    page = pyropdf.Document().pages.new((100, 100))
    core = page.document._core()  # noqa: SLF001

    for spec, message in [
        ({"subtype": "Underline", "rects": []}, "1 to 4096 rectangles"),
        ({"subtype": "Underline", "rects": [(0.0, 0.0, 1.0, 1.0)] * 4097}, "too many quadrilaterals"),
        ({"subtype": "Underline", "rects": [(0.0, 0.0, float("nan"), 1.0)]}, "finite"),
        ({"subtype": "Square", "rect": (0.0, 0.0, 1.0, 1.0), "rects": [(0.0, 0.0, 1.0, 1.0)]}, "not a Square"),
        ({"subtype": "Underline", "rect": (0.0, 0.0, 1.0, 1.0)}, "not a Underline"),
    ]:
        with pytest.raises(pyropdf.PdfError, match=message):
            _core.annotation_add(core, page._number, page._generation, cast("dict[str, object]", spec))  # noqa: SLF001
    assert "Annots" not in page.obj
