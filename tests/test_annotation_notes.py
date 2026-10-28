"""Adding notes: Note specifications and the icons that the appearance settler draws.

The add cases of pylopdf's tests/test_annotation_notes.py at ebb01d3c;
tests/test_annotations.py ports its cases of popups, open states, and
replies to notes that a raw PDF holds. Intended difference: the core's
check of an open state names a highlight, since pyropdf adds no square yet
(#167).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import pyropdf
from pyropdf import AnnotationFlags, Color, Note, NoteIcon, Point, Rect, _core

WHITE = (255, 255, 255)


def _pixel(pixmap: pyropdf.Pixmap, x: float, y: float) -> tuple[int, int, int]:
    offset = int(y) * pixmap.stride + int(x) * pixmap.channels
    red, green, blue = bytes(pixmap)[offset : offset + 3]
    return red, green, blue


def _page() -> pyropdf.Page:
    return pyropdf.Document().pages.new((300, 200))


@pytest.mark.parametrize("icon", list(NoteIcon))
def test_every_note_icon_draws_in_its_square(icon: NoteIcon) -> None:
    page = _page()

    note = page.annotations.add(Note((40, 40), icon=icon, contents="Look here", color=Color(0, 1, 0)))

    assert note.type is pyropdf.AnnotationType.TEXT
    assert note.rect == Rect(40, 40, 60, 60)
    assert note.contents == "Look here"
    assert note.icon == "".join(part.capitalize() for part in icon.value.split("_"))
    assert note.flags == AnnotationFlags.PRINT | AnnotationFlags.NO_ZOOM | AnnotationFlags.NO_ROTATE
    assert note.is_open is False
    pixmap = page.pixmap(dpi=72)
    inside = {_pixel(pixmap, x, y) for x in range(40, 60) for y in range(40, 60)}
    assert (0, 0, 0) in inside or any(red < 64 and blue < 64 for red, _, blue in inside)
    assert any(green > 200 and red < 64 for red, green, _ in inside)
    outside = {_pixel(pixmap, x, y) for x in range(0, 300, 7) for y in (30, 70)}
    assert outside == {WHITE}


def test_notes_survive_save_and_reopen() -> None:
    page = _page()
    page.annotations.add(Note(Point(100, 20), icon=NoteIcon.KEY, is_open=True, opacity=0.5))

    (note,) = pyropdf.open(page.document.to_bytes()).pages[0].annotations

    assert note.icon == "Key"
    assert note.is_open is True
    assert note.opacity == pytest.approx(0.5)
    assert note.color == Color(1, 1, 0)
    assert note.obj["Open"] is True


def test_note_specifications_validate_their_fields() -> None:
    note = Note((1, 2))
    assert (note.at, note.icon, note.color, note.opacity, note.is_open) == (
        Point(1, 2),
        NoteIcon.NOTE,
        Color(1, 1, 0),
        1.0,
        False,
    )
    assert Note((1, 2), icon=cast("Any", "help")).icon is NoteIcon.HELP
    with pytest.raises(ValueError, match="'wave'"):
        Note((1, 2), icon=cast("Any", "wave"))
    with pytest.raises(TypeError, match="is_open"):
        Note((1, 2), is_open=cast("Any", 1))
    with pytest.raises(pyropdf.LimitError) as raised:
        Note((1, 2), contents="x" * (1024 * 1024))
    assert raised.value.code == "annotation_input_size"
    page = pyropdf.Document().pages.new((100, 100))
    core = page.document._core()  # noqa: SLF001
    location = (core, page._number, page._generation)  # noqa: SLF001
    with pytest.raises(pyropdf.PdfError, match="unknown note icon"):
        _core.annotation_add(*location, {"subtype": "Text", "rect": (0.0, 0.0, 20.0, 20.0), "icon": "Wave"})
    with pytest.raises(pyropdf.PdfError, match="belong to a note"):
        _core.annotation_add(*location, {"subtype": "Highlight", "rects": [(0.0, 0.0, 20.0, 20.0)], "open": True})
    assert "Annots" not in page.obj
