"""FreeText annotations: text, fonts, alignment, rotation, and callouts.

pylopdf's tests/test_annotation_free_text.py at ebb01d3c. Intended
differences: the core's checks are called as ``_core.annotation_add`` with
the page's object; ``Page.text`` does not draw annotations stored without
an appearance, so the text that rendering draws for them is read with
``Annotation.text``, which does; free text draws in a Standard 14 font
only, so an embedded face and text outside WinAnsi raise ``ValueError``
(the last two tests are pyropdf's own).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import pytest

import pyropdf
from pyropdf import Border, Color, Font, FreeText, LineEnding, Point, Rect, _core

if TYPE_CHECKING:
    from collections.abc import Iterator

WHITE = (255, 255, 255)


def test_callout_stops_after_four_points_even_when_sequence_misreports_length() -> None:
    class Endless(Sequence[tuple[int, int]]):
        def __init__(self) -> None:
            self.read = 0

        def __len__(self) -> int:
            return 2

        def __getitem__(self, index: int) -> tuple[int, int]:  # ty: ignore[invalid-method-override]
            self.read += 1
            return (10, 10)

    callout = Endless()
    with pytest.raises(ValueError, match="2 or 3 points"):
        FreeText((10, 10, 100, 50), "Hello", callout=callout)
    assert callout.read == 4


def test_core_callout_stops_after_four_points() -> None:
    class Endless:
        def __init__(self) -> None:
            self.read = 0

        def __iter__(self) -> Iterator[tuple[float, float]]:
            while True:
                self.read += 1
                if self.read > 4:
                    msg = "the core continued past the fourth point"
                    raise AssertionError(msg)
                yield (10.0, 10.0)

    doc = pyropdf.Document()
    page = doc.pages.new((200, 100))
    callout = Endless()
    before = doc.history.position
    with pytest.raises(pyropdf.PdfError, match="2 or 3 finite points"):
        _core.annotation_add(
            doc._core(),  # noqa: SLF001 - exercise the core's input boundary.
            page._number,  # noqa: SLF001
            page._generation,  # noqa: SLF001
            {"subtype": "FreeText", "rect": (10.0, 10.0, 100.0, 50.0), "callout": callout},
        )
    assert callout.read == 4
    assert doc.history.position == before
    assert "Annots" not in page.obj


@pytest.mark.parametrize("text", ["x" * (1024 * 1024 + 1), "漢" * 350_000], ids=["ascii", "multibyte"])
def test_free_text_input_limit_uses_actual_string_contents(text: str) -> None:
    class Misleading(str):
        __slots__ = ()

        def __len__(self) -> int:
            return 0

        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:  # noqa: ARG002
            return b""

    with pytest.raises(pyropdf.LimitError) as caught:
        FreeText((10, 10, 100, 50), Misleading(text))
    assert caught.value.code == "annotation_input_size"
    with pytest.raises(pyropdf.LimitError) as caught:
        pyropdf.Stamp((10, 10, 100, 50), contents=Misleading(text))
    assert caught.value.code == "annotation_input_size"


def test_free_text_encoding_checks_actual_string_contents() -> None:
    class Misleading(str):
        __slots__ = ()

        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:  # noqa: ARG002
            return b""

    page = pyropdf.Document().pages.new((200, 100))
    with pytest.raises(ValueError, match="WinAnsi"):
        page.annotations.add(FreeText((10, 10, 190, 90), Misleading("日本語")))
    annotation = page.annotations.add(FreeText((10, 10, 190, 90), Misleading("Hello")))
    assert annotation.contents == "Hello"


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


def _pixel(pixmap: pyropdf.Pixmap, x: float, y: float) -> tuple[int, int, int]:
    offset = int(y) * pixmap.stride + int(x) * pixmap.channels
    red, green, blue = bytes(pixmap)[offset : offset + 3]
    return red, green, blue


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_free_text_reads_upright_on_every_page_rotation(rotation: int) -> None:
    page = pyropdf.Document().pages.new((300, 300))
    page.rotation = rotation

    annotation = page.annotations.add(
        FreeText((20, 20, 200, 80), "Upright words", size=14, color=Color(1, 1, 0), border=Border(1))
    )

    assert annotation.type is pyropdf.AnnotationType.FREE_TEXT
    assert annotation.rect == pytest.approx(Rect(20, 20, 200, 80), abs=1e-3)
    assert annotation.contents == "Upright words"
    assert annotation.rotation == 0
    words = [(word.text, word.bbox) for word in page.words()]
    assert [text for text, _ in words] == ["Upright", "words"]
    first = words[0][1]
    # The words run left to right along the top of the box.
    assert first.x0 >= 20
    assert first.x0 < words[1][1].x0
    assert first.y0 >= 20
    assert first.y1 <= 50
    pixmap = page.pixmap(dpi=72)
    assert _pixel(pixmap, 150, 70) == (255, 255, 0)
    assert _pixel(pixmap, 20, 50) == (0, 0, 0)


def test_free_text_turns_and_aligns() -> None:
    page = pyropdf.Document().pages.new((300, 300))

    turned = page.annotations.add(FreeText((20, 20, 60, 280), "Down the side", rotate=90))
    centered = page.annotations.add(FreeText((100, 20, 280, 60), "Mid", align="center", size=20))

    assert turned.rotation == 90
    assert turned.obj["Rotate"] == 270
    assert centered.obj["Q"] == 1
    lines = [(line.text, line.direction) for block in page.layout().blocks for line in block.lines]
    assert ("Down the side", pytest.approx((0, 1), abs=1e-6)) in [(text, tuple(d)) for text, d in lines]
    (mid,) = [word for word in page.words() if word.text == "Mid"]
    assert abs((mid.bbox.x0 + mid.bbox.x1) / 2 - 190) < 3


def test_callouts_point_at_what_the_text_explains() -> None:
    page = pyropdf.Document().pages.new((300, 300))

    annotation = page.annotations.add(
        FreeText(
            (150, 150, 280, 200),
            "Look",
            callout=[Point(40, 250), (100, 220), (150, 175)],
            callout_ending=LineEnding.CLOSED_ARROW,
            border=Border(1),
        )
    )

    assert annotation.intent == "FreeTextCallout"
    # The rectangle holds the box, the line, and its ending.
    assert annotation.rect.contains(Rect(35, 150, 280, 255))
    assert len(annotation.obj["CL"]) == 6
    assert annotation.obj["LE"] == "ClosedArrow"
    pixmap = page.pixmap(dpi=72)
    assert _pixel(pixmap, 70, 235) != WHITE
    assert _pixel(pixmap, 100, 220) != WHITE


def test_free_text_survives_save_and_reopen() -> None:
    page = pyropdf.Document().pages.new((300, 300))
    page.annotations.add(
        FreeText(
            (20, 20, 200, 80),
            "Saved",
            font=Font.standard("Courier-Bold"),
            size=13,
            text_color=Color(0, 0, 1),
            opacity=0.5,
        )
    )

    (annotation,) = pyropdf.open(page.document.to_bytes()).pages[0].annotations

    assert annotation.contents == "Saved"
    assert annotation.obj["DA"].text == "/CoBo 13 Tf 0 0 1 rg"
    assert annotation.opacity == pytest.approx(0.5)
    assert "Saved" in pyropdf.open(page.document.to_bytes()).pages[0].text()


def test_free_text_validates_its_fields() -> None:
    text = FreeText((0, 0, 100, 50), "x")
    assert (text.size, text.align, text.rotate, text.callout, text.color) == (11.0, "left", 0, None, None)
    assert text.border == Border(0)
    assert FreeText((0, 0, 100, 50), "x", rotate=-90).rotate == 270
    for kwargs, error in [
        ({"size": 0}, ValueError),
        ({"align": "justify"}, ValueError),
        ({"rotate": 45}, ValueError),
        ({"callout": [(0, 0)]}, ValueError),
        ({"font": "Helvetica"}, TypeError),
        ({"text_color": (0, 0, 0)}, TypeError),
        ({"callout_ending": "wave"}, ValueError),
    ]:
        with pytest.raises(error):
            FreeText((0, 0, 100, 50), "x", **cast("dict[str, Any]", kwargs))
    with pytest.raises(TypeError, match="text must be a str"):
        FreeText((0, 0, 100, 50), cast("Any", None))
    with pytest.raises(pyropdf.LimitError) as raised:
        FreeText((0, 0, 100, 50), "x" * (1024 * 1024))
    assert raised.value.code == "annotation_input_size"


def test_text_that_does_not_fit_changes_nothing() -> None:
    page = pyropdf.Document().pages.new((300, 300))

    with pytest.raises(pyropdf.PdfError, match="does not fit"):
        page.annotations.add(FreeText((20, 20, 60, 30), "far too much text for so small a box", size=20))
    assert "Annots" not in page.obj


def test_the_core_repeats_the_free_text_checks() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((100, 100))
    location = (doc._core(), page._number, page._generation)  # noqa: SLF001
    base: dict[str, Any] = {"subtype": "FreeText", "rect": (0.0, 0.0, 90.0, 40.0), "contents": "x"}
    for spec, message in [
        ({"font_size": 0.0}, "font size"),
        ({"font_name": "Comic"}, "unknown Standard 14 font"),
        ({"align": 3}, "aligns left"),
        ({"rotate": 45}, "multiple of 90"),
        ({"callout": [(0.0, 0.0)]}, "2 or 3 finite points"),
        ({"callout_ending": "ClosedArrow"}, "needs a callout line"),
        ({"text_color": (2.0, 0.0, 0.0)}, "components from 0 to 1"),
    ]:
        with pytest.raises(pyropdf.PdfError, match=message):
            _core.annotation_add(*location, {**base, **spec})
    with pytest.raises(pyropdf.PdfError, match="belong to free text"):
        _core.annotation_add(*location, {"subtype": "Square", "rect": (0.0, 0.0, 9.0, 9.0), "font_size": 9.0})
    assert "Annots" not in doc.pages[0].obj


def test_rendering_draws_a_missing_free_text_appearance() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [4 0 R] >>",
                4: (
                    "<< /Type /Annot /Subtype /FreeText /Rect [10 10 190 90] /Contents (Drawn later) "
                    "/DA (/TiRo 18 Tf 1 0 0 rg) /C [0 0 1] >>"
                ),
            }
        )
    )
    before = doc.to_bytes()

    pixmap = doc.pages[0].pixmap(dpi=72)

    assert _pixel(pixmap, 100, 80) == (0, 0, 255)
    assert doc.pages[0].annotations[0].text().strip() == "Drawn later"
    assert doc.to_bytes() == before


@pytest.mark.parametrize(
    "default_appearance",
    ["(0 g /HeBo 0 Tf)", "(1 0 0 1 k)", "(garbage Tf rg)", "<FFFE>", "7"],
)
def test_odd_default_appearances_still_draw(default_appearance: str) -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [4 0 R] >>",
                4: (
                    "<< /Type /Annot /Subtype /FreeText /Rect [10 10 190 90] /Contents (Odd) "
                    f"/DA {default_appearance} >>"
                ),
            }
        )
    )

    assert doc.pages[0].annotations[0].text().strip() == "Odd"


def test_free_text_draws_in_a_standard_14_font_only() -> None:
    page = pyropdf.Document().pages.new((300, 300))
    core = (page.document._core(), page._number, page._generation)  # noqa: SLF001

    with pytest.raises(ValueError, match="WinAnsi"):
        page.annotations.add(FreeText((20, 20, 200, 60), "日本語"))
    with pytest.raises(pyropdf.PdfError, match="WinAnsi"):
        _core.annotation_add(*core, {"subtype": "FreeText", "rect": (20.0, 20.0, 200.0, 60.0), "contents": "日本語"})
    free_text = page.annotations.add(FreeText((20, 20, 200, 60), "Latin"))
    with pytest.raises(pyropdf.PdfError, match="WinAnsi"):
        free_text.contents = "更新"
    assert free_text.contents == "Latin"
    assert "Annots" in page.obj
    assert len(page.annotations) == 1


def test_free_text_appearance_names_its_font() -> None:
    page = pyropdf.Document().pages.new((300, 300))

    annotation = page.annotations.add(FreeText((20, 20, 200, 60), "Named", font=Font.standard("Times-Roman")))

    form = annotation.obj["AP"]["N"]
    font = form["Resources"]["Font"]["TiRo"]
    assert (font["BaseFont"], font["Encoding"]) == ("Times-Roman", "WinAnsiEncoding")
    assert page.text().strip() == "Named"
