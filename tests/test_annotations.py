"""page.annotations: Annotation handles that add, read, edit, and delete annotations.

The cases of pylopdf's tests/test_annotations.py and
tests/test_annotation_notes.py at ebb01d3c that read, edit, or delete
annotations, ported to annotations that a raw PDF holds, and the cases of
the former that add highlights; tests/test_annotation_notes.py adds notes.
Intended differences from pylopdf:

- Removing an annotation leaves its objects in the document, since a save
  writes every live object; the page's ``/Annots`` no longer names them.
- A handle of a page that another page's deletion moved keeps working, as
  pages do (tests/test_pages.py); deleting its own page makes it stale.
- pymupdf's names, such as ``PDF_ANNOT_3D``, are absent with no hint of the
  pyropdf name.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING, Any, cast

import pytest

import pyropdf
from pyropdf import (
    AnnotationFlags,
    AnnotationType,
    BlendMode,
    Border,
    BorderStyle,
    Color,
    Highlight,
    NoteIcon,
    Rect,
    StampIcon,
    _core,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


#: A page that shows "Highlight me" in Helvetica.
_TEXT_PAGE = {
    1: "<< /Type /Catalog /Pages 2 0 R >>",
    2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    3: (
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>"
    ),
    4: "<< /Length 43 >>\nstream\nBT /F1 24 Tf 72 720 Td (Highlight me) Tj ET\nendstream",
}


def _page_of(annots: str, objects: dict[int, str] | None = None) -> pyropdf.Page:
    """Return the page of a one-page PDF whose ``/Annots`` is ``annots``, with more objects from 4."""
    return pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots {annots} >>",
                **(objects or {}),
            }
        )
    ).pages[0]


def _annotated() -> pyropdf.Document:
    text = (
        "<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] /Contents (Note) /Name /Comment "
        "/T (Ann) /CreationDate (D:20260101) /M (D:20260102) /Subj (Review) /NM (n-1) "
        "/F 4 /C [1 0 0] /IC [0 0 1] /CA 0.5 /Popup 5 0 R >>"
    )
    return _page_of(
        "[4 0 R 5 0 R << /Type /Annot /Subtype /Link /Rect [0 0 5 5] >> "
        "<< /Type /Annot /Subtype /Widget /Rect [0 0 5 5] >> "
        "<< /Type /Annot /Subtype /FreeText /IT /FreeTextCallout /Rect [50 50 80 80] /F 2 "
        "/C [0.5] /IC [0 0 0 1] >> << /Type /Annot /Rect [1 1 2 2] >>]",
        {4: text, 5: "<< /Type /Annot /Subtype /Popup /Rect [100 10 190 60] /Parent 4 0 R >>"},
    ).document


def _notes(count: int = 1, subtype: str = "Text") -> pyropdf.Page:
    """Return a page with ``count`` annotations of ``subtype`` as objects 4 onwards."""
    objects = {
        4 + i: f"<< /Type /Annot /Subtype /{subtype} /Rect [{10 + 30 * i} 10 {30 + 30 * i} 30] /C [1 0 0] >>"
        for i in range(count)
    }
    return _page_of("[" + " ".join(f"{number} 0 R" for number in objects) + "]", objects)


def _subtypes(page: pyropdf.Page) -> list[str | None]:
    return [entry.get("Subtype") for entry in page.obj["Annots"]]


def test_annotations_are_typed_handles() -> None:
    page = _annotated().pages[0]

    text, free_text, untyped = page.annotations

    assert isinstance(text, pyropdf.Annotation)
    assert (text.type, free_text.type, untyped.type) == (
        AnnotationType.TEXT,
        AnnotationType.FREE_TEXT,
        AnnotationType.UNKNOWN,
    )
    assert text.rect == Rect(10, 70, 30, 90)
    assert (text.contents, text.author, text.subject, text.name, text.icon) == (
        "Note",
        "Ann",
        "Review",
        "n-1",
        "Comment",
    )
    assert (text.created, text.modified, text.intent) == ("D:20260101", "D:20260102", None)
    assert free_text.intent == "FreeTextCallout"
    assert (text.color, text.interior_color) == (Color(1, 0, 0), Color(0, 0, 1))
    assert (free_text.color, free_text.interior_color) == (Color(0.5, 0.5, 0.5), Color(0, 0, 0))
    assert untyped.color is untyped.contents is None
    assert (text.opacity, free_text.opacity) == (0.5, 1.0)
    assert text.flags == AnnotationFlags.PRINT
    assert AnnotationFlags.HIDDEN in free_text.flags
    assert (text.xref, free_text.xref) == (4, 0)
    assert text.page == page
    assert text.obj["Subtype"] == "Text"
    assert free_text.obj["IT"] == "FreeTextCallout"
    assert repr(text) == "Annotation(type=AnnotationType.TEXT, rect=Rect(x0=10.0, y0=70.0, x1=30.0, y1=90.0))"
    text.rect = Rect(0, 0, 20, 20)
    assert text.rect == Rect(0, 0, 20, 20)
    assert list(text.obj["Rect"]) == [0, 80, 20, 100]


def test_links_widgets_and_popups_are_not_listed() -> None:
    page = _annotated().pages[0]

    assert [annotation.type for annotation in page.annotations] == [
        AnnotationType.TEXT,
        AnnotationType.FREE_TEXT,
        AnnotationType.UNKNOWN,
    ]
    assert _subtypes(page) == ["Text", "Popup", "Link", "Widget", "FreeText", None]
    assert len(page.links) == 1


def test_handles_compare_by_annotation() -> None:
    page = _annotated().pages[0]

    first, second = list(page.annotations), list(page.annotations)

    assert first == second
    assert len(set(first) | set(second)) == 3
    assert first[0] != first[1]


def test_handles_go_stale() -> None:
    doc = _annotated()
    doc.pages.new()
    text, free_text, _ = doc.pages[0].annotations

    del doc.pages[0].obj["Annots"][0]
    with pytest.raises(pyropdf.StaleObjectError):
        _ = text.rect
    assert repr(text) == "<stale Annotation>"
    assert free_text.xref == 0

    # Removing an annotation stores direct ones as objects, so a handle that
    # found one by its position goes stale instead of naming another.
    del doc.pages[0].annotations[1]
    with pytest.raises(pyropdf.StaleObjectError):
        _ = free_text.type
    assert all(annotation.xref > 0 for annotation in doc.pages[0].annotations)

    remaining = doc.pages[0].annotations[0]
    del doc.pages[1]
    assert remaining.type == AnnotationType.FREE_TEXT
    del doc.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        _ = remaining.type


def test_a_handle_works_again_after_undo_restores_its_annotation() -> None:
    doc = _annotated()
    page = doc.pages[0]
    text = page.annotations[0]

    del page.annotations[0]
    with pytest.raises(pyropdf.StaleObjectError):
        _ = text.contents

    doc.history.undo()
    assert text.contents == "Note"
    assert text.popup_rect == Rect(100, 40, 190, 90)
    doc.history.redo()
    with pytest.raises(pyropdf.StaleObjectError):
        _ = text.contents


def test_deleting_an_annotation_removes_its_popup() -> None:
    doc = _annotated()
    page = doc.pages[0]
    free_text = page.annotations[1]

    del page.annotations[0]

    assert [annotation.type for annotation in page.annotations] == [AnnotationType.FREE_TEXT, AnnotationType.UNKNOWN]
    assert page.annotations[0].intent == "FreeTextCallout"
    with pytest.raises(pyropdf.StaleObjectError):
        _ = free_text.intent
    assert _subtypes(page) == ["Link", "Widget", "FreeText", None]
    reopened = pyropdf.open(doc.to_bytes()).pages[0]
    assert len(reopened.annotations) == 2
    assert _subtypes(reopened) == ["Link", "Widget", "FreeText", None]

    del page.annotations[:]
    assert len(page.annotations) == 0
    assert _subtypes(page) == ["Link", "Widget"]
    assert [link.xref for link in page.links] == [page.obj["Annots"][0].xref]


def test_annotation_enums_keep_pymupdf_values() -> None:
    assert (AnnotationType.TEXT, AnnotationType.WIDGET, AnnotationType.THREE_D) == (0, 21, 26)
    assert (AnnotationType.UNKNOWN, AnnotationType.PROJECTION) == (-1, 27)
    assert AnnotationFlags.LOCKED_CONTENTS == 512
    assert AnnotationFlags(6) == AnnotationFlags.PRINT | AnnotationFlags.HIDDEN
    for name in ("PDF_ANNOT_TEXT", "PDF_ANNOT_3D", "PDF_ANNOT_IS_HIDDEN", "Annot", "AnnotType", "AnnotFlags"):
        assert not hasattr(pyropdf, name)
        assert name not in pyropdf.__all__
    assert repr(AnnotationType.TEXT) == "<AnnotationType.TEXT: 0>"
    assert pyropdf.Annotation.__module__ == "pyropdf"


def test_text_properties_are_set_and_removed() -> None:
    (note,) = _notes().annotations

    note.contents = "Body"
    note.author = "Author"
    note.subject = "Subject"
    note.name = "note-1"
    note.language = "ja-JP"
    note.icon = NoteIcon.KEY
    note.flags = AnnotationFlags.PRINT | AnnotationFlags.LOCKED

    assert (note.contents, note.author, note.subject, note.name, note.language) == (
        "Body",
        "Author",
        "Subject",
        "note-1",
        "ja-JP",
    )
    assert note.icon == "Key"
    assert note.flags == AnnotationFlags.PRINT | AnnotationFlags.LOCKED
    assert note.modified is not None
    assert note.modified.startswith("D:")
    note.contents = "日本語"
    assert note.contents == "日本語"
    del note.contents, note.author, note.subject, note.name, note.language
    assert (note.contents, note.author, note.subject, note.name, note.language) == (None, None, None, None, None)
    with pytest.raises(TypeError, match="contents must be a str"):
        note.contents = cast("Any", 5)
    with pytest.raises(pyropdf.LimitError) as error:
        note.contents = "x" * (1024 * 1024 + 1)
    assert error.value.code == "annotation_input_size"
    with pytest.raises(ValueError, match="icon is a"):
        note.icon = cast("Any", "Key")
    with pytest.raises(pyropdf.PdfError, match="unknown icon"):
        note.icon = StampIcon.SOLD


def test_style_properties_are_set_and_removed() -> None:
    (square,) = _notes(subtype="Square").annotations

    square.color = Color(0, 0, 1)
    square.interior_color = Color(0, 1, 0)
    square.opacity = 0.25
    square.border = Border(2, dashes=(3, 1))
    square.clouds = 1.5
    square.blend_mode = BlendMode.MULTIPLY

    assert (square.color, square.interior_color, square.opacity) == (Color(0, 0, 1), Color(0, 1, 0), 0.25)
    assert square.border == Border(2, style=BorderStyle.DASHED, dashes=(3, 1))
    assert (square.clouds, square.blend_mode) == (1.5, BlendMode.MULTIPLY)
    del square.color, square.interior_color, square.border, square.clouds, square.blend_mode
    assert square.color is square.interior_color is square.border is square.blend_mode is None
    assert square.clouds == 0
    with pytest.raises(ValueError, match="opacity"):
        square.opacity = 0
    with pytest.raises(ValueError, match="no transparency"):
        square.color = Color(1, 0, 0, 0.5)
    with pytest.raises(ValueError, match="dashed border needs"):
        Border(1, style=BorderStyle.DASHED)


def test_edits_of_annotations_that_other_producers_wrote() -> None:
    page = _page_of(
        "[<< /Type /Annot /Subtype /Square /Rect [10 10 50 50] /C [1 0 0] >> "
        "<< /Type /Annot /Subtype /Sound /Rect [60 10 80 30] >>]"
    )
    square, sound = page.annotations

    square.interior_color = Color(0, 1, 0)
    # The edit stored the page's direct annotations as objects, so a handle
    # of another one found by its index goes stale; read the page again.
    with pytest.raises(pyropdf.StaleObjectError):
        _ = sound.contents
    sound = page.annotations[1]
    sound.contents = "A sound"

    assert square.xref > 0
    assert square.interior_color == Color(0, 1, 0)
    assert sound.contents == "A sound"
    assert "AP" not in sound.obj
    with pytest.raises(TypeError, match="Border or None"):
        square.border = cast("Any", 2)
    with pytest.raises(TypeError, match="AnnotationFlags"):
        square.flags = cast("Any", True)  # noqa: FBT003 - a bool is not flags.
    with pytest.raises(ValueError, match="'odd'"):
        square.blend_mode = cast("Any", "odd")


def test_the_core_refuses_bad_edits() -> None:
    page = _notes(subtype="Square")
    core = page.document._core()  # noqa: SLF001

    for edit, message in [
        ({"opacity": 0.0}, "opacity"),
        ({"flags": 4096}, "bits 1 to 10"),
        ({"border": (1.0, "D", [])}, "border"),
        ({"blend_mode": "Odd"}, "blend mode"),
        ({"clear": ["rect"]}, "cannot be removed"),
        ({"modified": "2026"}, "PDF date"),
        ({"color": (2.0, 0.0, 0.0)}, "components"),
    ]:
        with pytest.raises(pyropdf.PdfError, match=message):
            _core.annotation_edit(core, 3, 0, 0, (4, 0), {"clear": [], **edit})
    assert page.annotations[0].color == Color(1, 0, 0)


def test_every_edit_draws_the_appearance_again() -> None:
    note = "<< /Type /Annot /Subtype /Text /Rect [{0} 10 {1} 30] /C [1 0 0] >>"
    page = _page_of(
        "[<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] >> 4 0 R 5 0 R]",
        {4: note.format(60, 80), 5: note.format(90, 110)},
    )
    _, first, second = page.annotations
    assert "AP" not in first.obj

    first.color = Color(0, 1, 0)
    drawn = first.obj["AP"]["N"]
    assert b"0 1 0 rg" in drawn.read()
    direct = page.annotations[0]
    direct.rect = Rect(0, 0, 10, 10)
    assert "AP" in direct.obj
    assert first.obj["AP"]["N"].xref == drawn.xref

    # Popups, the open state, and replies do not change how an annotation looks.
    page.document.history.undo()
    page.document.history.undo()
    assert "AP" not in first.obj
    first.popup_rect = (40, 10, 160, 80)
    first.is_open = True
    second.in_reply_to = first
    assert "AP" not in first.obj
    assert "AP" not in second.obj


def test_edits_in_one_transaction_draw_the_appearance_once() -> None:
    page = _notes()
    (note,) = page.annotations
    objects = page.document.objects
    count = len(objects)

    with page.document.transaction("restyle"):
        note.color = Color(0, 0, 1)
        note.opacity = 0.5
        note.contents = "drawn once"
    # The settler writes one form, which names the last edit's color.
    assert len(objects) == count + 1
    drawn = note.obj["AP"]["N"]
    assert b"0 0 1 rg" in drawn.read()
    # A read in the transaction draws what the edits before it changed.
    with page.document.transaction("read"):
        note.color = Color(1, 0, 0)
        assert b"1 0 0 rg" in note.obj["AP"]["N"].read()
        note.color = Color(0, 1, 0)
    assert len(objects) == count + 3
    assert b"0 1 0 rg" in note.obj["AP"]["N"].read()


def test_adding_a_highlight_returns_its_handle() -> None:
    doc = pyropdf.open(build_raw_pdf(_TEXT_PAGE))
    page = doc.pages[0]
    hit = page.search("Highlight")[0]

    annotation = page.annotations.add(Highlight(hit, contents="note"))

    assert annotation.type is AnnotationType.HIGHLIGHT
    assert (annotation.contents, annotation.color, annotation.flags) == ("note", Color(1, 1, 0), AnnotationFlags.PRINT)
    assert annotation.opacity == pytest.approx(0.4)
    assert annotation.xref > 0
    assert annotation in page.annotations
    assert annotation.rect == pytest.approx(hit, abs=1e-3)
    from_quad = page.annotations.add(Highlight(hit.quad, color=Color(0, 1, 0), opacity=1))
    assert from_quad.rect == pytest.approx(annotation.rect, abs=1e-3)
    assert (from_quad.color, from_quad.opacity) == (Color(0, 1, 0), 1.0)
    several = page.annotations.add(Highlight([hit.quad, Rect(10, 10, 20, 20)]))
    assert len(several.obj["QuadPoints"]) == 16
    reopened = pyropdf.open(doc.to_bytes()).pages[0]
    assert [annotation.contents for annotation in reopened.annotations] == ["note", None, None]


@pytest.mark.parametrize(
    ("arguments", "error", "match"),
    [
        (((),), ValueError, "at least one rectangle"),
        (((10, 10, 5, 20),), ValueError, "x0 < x1"),
        (("rect",), TypeError, "rects must be a rectangle"),
        (((10, 10, 20, 20), {"color": (1, 0, 0)}), TypeError, "color must be a Color"),
        (((10, 10, 20, 20), {"color": Color(1, 0, 0, 0.5)}), ValueError, "no transparency"),
        (((10, 10, 20, 20), {"opacity": 0}), ValueError, "greater than 0"),
        (((10, 10, 20, 20), {"opacity": True}), TypeError, "opacity must be a number"),
        (((10, 10, 20, 20), {"contents": 5}), TypeError, "contents must be a str"),
        (([(10, 10, 20, 20)] * 4097,), ValueError, "4096 rectangles"),
    ],
)
def test_highlight_specifications_validate_their_fields(
    arguments: tuple[object, ...], error: type[Exception], match: str
) -> None:
    rects, *rest = arguments
    keywords = cast("dict[str, object]", rest[0] if rest else {})

    with pytest.raises(error, match=match):
        Highlight(rects, **keywords)  # ty: ignore[invalid-argument-type]


def test_highlight_bounds_contents_before_reading_rectangles() -> None:
    def unreadable() -> Iterator[tuple[int, int, int, int]]:
        msg = "oversized contents must fail before rectangle iteration"
        raise AssertionError(msg)
        yield (10, 10, 20, 20)

    with pytest.raises(pyropdf.LimitError) as caught:
        Highlight(cast("Sequence[Sequence[float]]", unreadable()), contents="x" * (1024 * 1024 + 1))
    assert caught.value.code == "annotation_input_size"

    def excessive() -> Iterator[tuple[int, int, int, int]]:
        yield from [(10, 10, 20, 20)] * 4097
        msg = "the bounded reader consumed beyond item 4,097"
        raise AssertionError(msg)

    with pytest.raises(ValueError, match="4096 rectangles"):
        Highlight(cast("Sequence[Sequence[float]]", excessive()))


def test_add_takes_specifications_only() -> None:
    page = _notes()

    with pytest.raises(TypeError, match="takes an annotation specification"):
        page.annotations.add((10, 10, 20, 20))  # ty: ignore[invalid-argument-type]
    assert not hasattr(page, "annots")
    assert not hasattr(page, "add_highlight_annot")


def test_popups_are_added_moved_opened_and_removed() -> None:
    page = _page_of(
        "[4 0 R 5 0 R]",
        {
            4: "<< /Type /Annot /Subtype /Text /Rect [10 70 30 90] /Contents (first) >>",
            5: "<< /Type /Annot /Subtype /Square /Rect [100 0 150 50] >>",
        },
    )
    note, square = page.annotations

    assert note.popup_rect is None
    note.popup_rect = (40, 10, 160, 80)
    assert note.popup_rect == Rect(40, 10, 160, 80)
    # The popup belongs to its annotation, which page.annotations lists alone.
    assert len(page.annotations) == 2
    popup = note.obj["Popup"]
    assert popup["Subtype"] == "Popup"
    assert popup["Parent"] == note.obj
    assert _subtypes(page) == ["Text", "Square", "Popup"]

    note.popup_rect = Rect(50, 20, 170, 90)
    assert note.popup_rect == Rect(50, 20, 170, 90)
    assert len(page.obj["Annots"]) == 3

    note.is_open = True
    assert note.is_open is True
    assert popup["Open"] is True
    assert note.obj["Open"] is True
    square.popup_rect = (160, 50, 200, 100)
    square.is_open = True
    assert square.is_open is True
    assert "Open" not in square.obj

    reopened = pyropdf.open(page.document.to_bytes()).pages[0]
    assert reopened.annotations[0].popup_rect == Rect(50, 20, 170, 90)
    assert reopened.annotations[1].is_open is True

    del note.popup_rect
    assert note.popup_rect is None
    assert "Popup" not in note.obj
    assert _subtypes(page) == ["Text", "Square", "Popup"]
    # Removing an annotation removes its popup.
    del page.annotations[1]
    assert _subtypes(page) == ["Text"]


def test_open_needs_a_popup_or_a_note() -> None:
    (square,) = _notes(subtype="Square").annotations

    assert square.is_open is False
    with pytest.raises(pyropdf.PdfError, match="no popup"):
        square.is_open = True
    with pytest.raises(TypeError, match="bool"):
        square.is_open = cast("Any", 1)
    with pytest.raises(pyropdf.PdfError, match="4 geometry coordinates"):
        square.popup_rect = cast("Any", (1, 2, 3))


def test_replies_name_the_annotation_they_answer() -> None:
    page = _notes(3)
    question, answer, other = page.annotations

    assert answer.in_reply_to is None
    answer.in_reply_to = question
    assert answer.in_reply_to == question
    assert question.replies == [answer]
    assert other.replies == []
    assert answer.obj["IRT"] == question.obj

    reopened = pyropdf.open(page.document.to_bytes()).pages[0]
    assert reopened.annotations[1].in_reply_to == reopened.annotations[0]

    del answer.in_reply_to
    assert answer.in_reply_to is None
    assert question.replies == []
    with pytest.raises(pyropdf.PdfError, match="another annotation"):
        answer.in_reply_to = answer
    with pytest.raises(TypeError, match="Annotation or None"):
        answer.in_reply_to = cast("Any", 3)
    elsewhere = _notes().annotations[0]
    with pytest.raises(ValueError, match="its own page"):
        answer.in_reply_to = elsewhere


def test_direct_annotations_become_objects_for_popups() -> None:
    page = _page_of("[<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] /Open true >>]")
    (note,) = page.annotations
    assert note.xref == 0
    assert note.is_open is True

    note.popup_rect = (40, 10, 120, 60)

    assert note.xref > 0
    assert note.obj["Popup"]["Parent"] == note.obj
    assert note.popup_rect == Rect(40, 10, 120, 60)


@pytest.mark.parametrize("operation", ["subject", "popup_rect", "is_open", "in_reply_to"])
def test_a_waiting_edit_reads_after_the_writer_lock(operation: str) -> None:
    page = _notes(2)
    note = page.annotations[0]
    started = Event()

    def edit() -> None:
        started.set()
        setattr(
            note,
            operation,
            {
                "subject": "Second writer",
                "popup_rect": Rect(40, 10, 160, 80),
                "is_open": True,
                "in_reply_to": page.annotations[1],
            }[operation],
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with page.document.transaction("First writer"):
            future = pool.submit(edit)
            assert started.wait(timeout=10)
            # The worker must wait for this transaction's writer lock.
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            note.author = "First writer"
        future.result(timeout=10)
    assert note.author == "First writer"
    if operation == "subject":
        assert note.subject == "Second writer"


def test_annotation_text_rejects_excessive_character_count_without_encoding() -> None:
    class Unencoded(str):
        __slots__ = ()

        def encode(self, *_args: object, **_kwargs: object) -> bytes:
            msg = "An oversized annotation must not allocate an encoded copy"
            raise AssertionError(msg)

    note = _notes().annotations[0]
    with pytest.raises(pyropdf.LimitError) as caught:
        note.contents = Unencoded("a" * (1024 * 1024 + 1))
    assert caught.value.code == "annotation_input_size"


def test_annotation_count_refuses_with_a_registered_limit() -> None:
    page = _page_of(
        "[" + "4 0 R " * 4097 + "]",
        {4: "<< /Subtype /Text /Rect [0 0 10 10] >>"},
    )
    with pytest.raises(pyropdf.LimitError) as caught:
        list(page.annotations)
    assert caught.value.code == "annotation_count"


def test_annotation_text_budget_is_shared_by_the_page() -> None:
    page = _page_of(
        "[4 0 R 5 0 R]",
        dict.fromkeys((4, 5), f"<< /Subtype /Text /Rect [0 0 10 10] /Contents ({'a' * 600_000}) >>"),
    )
    with pytest.raises(pyropdf.LimitError) as caught:
        list(page.annotations)
    assert caught.value.code == "annotation_text_size"


def test_a_long_valid_dash_pattern_is_refused_without_substitution() -> None:
    page = _page_of(
        "[4 0 R]",
        {4: "<< /Subtype /Square /Rect [0 0 10 10] /BS << /S /D /D [" + "1 " * 33 + "] >> >>"},
    )
    with pytest.raises(pyropdf.LimitError) as caught:
        _ = page.annotations[0].border
    assert caught.value.code == "annotation_dash_count"


def test_a_waiting_removal_keeps_a_popup_added_by_the_first_writer() -> None:
    page = _notes(2)
    remaining = page.annotations[1]
    started = Event()

    def remove() -> None:
        started.set()
        del page.annotations[0]

    with ThreadPoolExecutor(max_workers=1) as pool:
        with page.document.transaction("Add popup"):
            future = pool.submit(remove)
            assert started.wait(timeout=10)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            remaining.popup_rect = Rect(40, 10, 160, 80)
        future.result(timeout=10)
    assert len(page.annotations) == 1
    assert _subtypes(page) == ["Text", "Popup"]
    assert remaining.popup_rect == Rect(40, 10, 160, 80)


def test_a_waiting_add_preserves_the_first_writers_annotation() -> None:
    page = _notes()
    started = Event()

    def add() -> None:
        started.set()
        page.annotations.add(Highlight((10, 10, 20, 20), contents="second"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with page.document.transaction("Add first"):
            future = pool.submit(add)
            assert started.wait(timeout=10)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            page.annotations.add(Highlight((30, 10, 40, 20), contents="first"))
        future.result(timeout=10)
    assert [annotation.contents for annotation in page.annotations] == [None, "first", "second"]


def test_an_excessive_existing_squiggly_rolls_back_a_restyle() -> None:
    page = _page_of(
        "[4 0 R]",
        {
            4: "<< /Type /Annot /Subtype /Squiggly /Rect [0 0 150000 10] "
            "/C [1 0 0] /QuadPoints [0 10 150000 10 0 0 150000 0] >>"
        },
    )
    before = page.document.to_bytes()
    with pytest.raises(pyropdf.LimitError) as raised:
        page.annotations[0].color = Color(0, 1, 0)
    assert raised.value.code == "annotation_point_count"
    assert page.document.to_bytes() == before


def test_highlight_appearance_preserves_subpixel_coordinates() -> None:
    page = pyropdf.Document().pages.new((100, 100))
    annotation = page.annotations.add(Highlight((0, 0, 0.000001, 1)))
    assert b"0.000001 1 l" in annotation.obj["AP"]["N"].read()


def test_highlight_text_rejects_excessive_character_count_without_encoding() -> None:
    class Unencoded(str):
        __slots__ = ()

        def encode(self, *_args: object, **_kwargs: object) -> bytes:
            msg = "An oversized annotation specification must not allocate an encoded copy"
            raise AssertionError(msg)

    with pytest.raises(pyropdf.LimitError) as caught:
        Highlight((0, 0, 1, 1), contents=Unencoded("a" * (1024 * 1024 + 1)))
    assert caught.value.code == "annotation_input_size"
