"""Cleaning page content and appearance streams (issue 434)."""

from __future__ import annotations

import itertools
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import LimitError

if TYPE_CHECKING:
    from pathlib import Path


def build_raw_pdf(objects: dict[int, str | bytes]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        body = objects[number]
        out += f"{number} 0 obj\n".encode() + (body.encode() if isinstance(body, str) else body) + b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _stream(data: bytes | str, extra: str = "") -> bytes:
    data = data.encode() if isinstance(data, str) else data
    return f"<< /Length {len(data)} {extra}>>\nstream\n".encode() + data + b"\nendstream"


# Two content streams with a comment, an empty q Q, an empty BT ET, an
# unmatched Q, and a q left open, naming one entry of each category.
FIRST = "% a comment\nq\n  q Q  \n/GS1 gs 1 0 0 RG\nBT ET\n"
SECOND = (
    "5 w 10 190 m 190 190 l S\nQ\nQ\n"
    "BT /F1 12 Tf 20 150 Td (Hello clean) Tj ET\n"
    "q 30 0 0 30 100 100 cm /Im1 Do Q\n"
    "/Pattern cs /P1 scn 10 10 50 30 re f\n"
    "/CS1 cs 1 sc 70 10 20 20 re f\n"
    "q 150 10 40 40 re W n /Sh1 sh Q\n"
    "/Span /MC1 BDC EMC\n"
    "q 0 0 1 rg 120 60 10 10 re f\n"
)
SHADING = (
    "<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 200 0] "
    "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >>"
)
IMAGE = _stream(
    bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0]),
    "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB /BitsPerComponent 8 ",
)
RESOURCES = (
    "<< /ProcSet [/PDF /Text] "
    "/Font << /F1 10 0 R /F2 11 0 R >> "
    "/XObject << /Im1 12 0 R /Im2 12 0 R >> "
    "/ExtGState << /GS1 << /CA 0.5 >> /GS2 << /ca 0.2 >> >> "
    "/ColorSpace << /CS1 [/Indexed /DeviceRGB 1 <ff000000ff00>] /CS2 /DeviceGray "
    "/DefaultGray [/CalGray << /WhitePoint [0.9505 1 1.089] >>] >> "
    f"/Pattern << /P1 << /PatternType 2 /Shading {SHADING} >> /P2 << /PatternType 2 /Shading {SHADING} >> >> "
    f"/Shading << /Sh1 {SHADING} /Sh2 {SHADING} >> "
    "/Properties << /MC1 << /ActualText (x) >> /MC2 << /ActualText (y) >> >> >>"
)
KEPT = {
    "ProcSet": None,
    "Font": {"F1"},
    "XObject": {"Im1"},
    "ExtGState": {"GS1"},
    "ColorSpace": {"CS1", "DefaultGray"},
    "Pattern": {"P1"},
    "Shading": {"Sh1"},
    "Properties": {"MC1"},
}


def build(*, annots: str = "", extra: dict[int, str | bytes] | None = None) -> bytes:
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
            3: f"<< /Type /Page /Parent 2 0 R /Resources 9 0 R /Contents [4 0 R 5 0 R] {annots}>>",
            4: _stream(FIRST),
            5: _stream(SECOND),
            9: RESOURCES,
            10: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            11: "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
            12: IMAGE,
            **(extra or {}),
        }
    )


def _render(page: pyropdf.Page) -> bytes:
    return bytes(page.pixmap(background=(255, 255, 255)))


def _names(resources: pyropdf.Dictionary) -> dict[str, set[str] | None]:
    return {key: set(value) if isinstance(value, pyropdf.Dictionary) else None for key, value in resources.items()}


def _ghostscript(data: bytes, tmp_path: Path, name: str) -> bytes:
    gs = shutil.which("gs")
    assert gs is not None
    source, out = tmp_path / f"{name}.pdf", tmp_path / f"{name}.ppm"
    source.write_bytes(data)
    subprocess.run(  # noqa: S603 - a fixed command on a file of the test's own.
        [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=ppmraw", "-r72", f"-sOutputFile={out}", str(source)],
        check=True,
    )
    return out.read_bytes()


def test_cleaning_normalizes_content_and_prunes_resources() -> None:
    doc = pyropdf.open(build())
    page = doc.pages[0]
    before, text = _render(page), page.text()

    page.clean_contents()

    operators = [operation.operator for operation in page.operations]
    assert operators.count("q") == operators.count("Q")
    assert ("q", "Q") not in itertools.pairwise(operators)
    assert ("BT", "ET") not in itertools.pairwise(operators)
    data = page.obj["Contents"].read()
    assert b"%" not in data
    assert b"  " not in data
    assert _names(page.obj["Resources"]) == KEPT
    assert _render(page) == before
    assert page.text() == text
    reopened = pyropdf.open(doc.to_bytes()).pages[0]
    assert _render(reopened) == before
    assert reopened.text() == text
    assert _names(reopened.obj["Resources"]) == KEPT


@pytest.mark.skipif(shutil.which("gs") is None, reason="needs Ghostscript")
def test_an_independent_renderer_draws_the_same(tmp_path: Path) -> None:
    data = build()
    doc = pyropdf.open(data)

    doc.pages[0].clean_contents()

    assert _ghostscript(doc.to_bytes(), tmp_path, "after") == _ghostscript(data, tmp_path, "before")


def test_undo_restores_the_page() -> None:
    data = build()
    doc = pyropdf.open(data)
    steps = len(doc.history.steps)

    doc.pages[0].clean_contents()
    assert len(doc.history.steps) == steps + 1
    doc.history.undo()

    assert doc.to_bytes() == pyropdf.open(data).to_bytes()


def test_inherited_resources_that_another_page_uses_stay() -> None:
    second = "BT /F2 10 Tf 20 20 Td (Other) Tj ET q 20 0 0 20 50 50 cm /Im2 Do Q"
    data = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 /MediaBox [0 0 200 200] /Resources 9 0 R >>",
            3: "<< /Type /Page /Parent 2 0 R /Contents [4 0 R 5 0 R] >>",
            4: _stream(FIRST),
            5: _stream(SECOND),
            6: "<< /Type /Page /Parent 2 0 R /Contents 7 0 R >>",
            7: _stream(second),
            9: RESOURCES,
            10: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            11: "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
            12: IMAGE,
        }
    )
    doc = pyropdf.open(data)
    first, other = doc.pages
    before = (_render(first), _render(other), other.text())

    first.clean_contents()

    assert _names(first.obj["Resources"]) == KEPT
    assert "Resources" not in other.obj
    assert set(other.obj["Parent"]["Resources"]["Font"]) == {"F1", "F2"}
    assert (_render(first), _render(other), other.text()) == before


def test_a_form_without_resources_keeps_the_page_resources() -> None:
    form = _stream("BT /F2 10 Tf 0 0 Td (Form) Tj ET", "/Type /XObject /Subtype /Form /BBox [0 0 100 20] ")
    data = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
            3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R "
            "/Resources << /Font << /F2 11 0 R /F3 11 0 R >> /XObject << /Fm1 5 0 R >> >> >>",
            4: _stream("% comment\nq 1 0 0 1 20 20 cm /Fm1 Do Q"),
            5: form,
            11: "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        }
    )
    doc = pyropdf.open(data)
    page = doc.pages[0]
    before = _render(page)

    page.clean_contents()

    assert set(page.obj["Resources"]["Font"]) == {"F2", "F3"}
    assert b"%" not in page.obj["Contents"].read()
    assert _render(page) == before


APPEARANCE = _stream(
    "% appearance\n  q 0 1 0 rg 0 0 40 20 re f Q q Q BT /F1 8 Tf 2 6 Td (Note) Tj ET",
    "/Type /XObject /Subtype /Form /BBox [0 0 40 20] /Resources << /Font << /F1 10 0 R /F2 11 0 R >> >> ",
)


# The page content with its q closed: hayro draws appearances in the state
# that an open q leaves, which cleaning closes, while Ghostscript does not.
BALANCED = _stream(SECOND.replace("Q\nQ\n", "Q\n", 1) + "Q\n")
TWO_APPEARANCES = (
    "/Annots [<< /Type /Annot /Subtype /Square /Rect [20 40 60 60] /AP << /N 13 0 R >> >> "
    "<< /Type /Annot /Subtype /Square /Rect [100 40 140 60] /AP << /N << /On 13 0 R >> >> /AS /On >>]"
)


def test_appearance_streams_are_cleaned_with_their_resources() -> None:
    doc = pyropdf.open(build(annots=TWO_APPEARANCES, extra={5: BALANCED, 13: APPEARANCE}))
    page = doc.pages[0]
    before = _render(page)

    page.clean_contents()

    appearance = doc.objects[13]
    data = appearance.read()
    assert b"%" not in data
    assert b"q\nQ" not in data
    assert set(appearance["Resources"]["Font"]) == {"F1"}
    assert _render(page) == before
    assert _render(pyropdf.open(doc.to_bytes()).pages[0]) == before


@pytest.mark.skipif(shutil.which("gs") is None, reason="needs Ghostscript")
def test_appearances_after_an_open_q_draw_the_same_in_ghostscript(tmp_path: Path) -> None:
    data = build(annots=TWO_APPEARANCES, extra={13: APPEARANCE})
    doc = pyropdf.open(data)

    doc.pages[0].clean_contents()

    assert _ghostscript(doc.to_bytes(), tmp_path, "after") == _ghostscript(data, tmp_path, "before")


def test_appearances_stay_without_annotations() -> None:
    annots = "/Annots [<< /Type /Annot /Subtype /Square /Rect [20 40 60 60] /AP << /N 13 0 R >> >>]"
    doc = pyropdf.open(build(annots=annots, extra={13: APPEARANCE}))
    data = doc.objects[13].read()

    doc.pages[0].clean_contents(annotations=False)

    assert doc.objects[13].read() == data
    with pytest.raises(TypeError, match="annotations must be a bool"):
        doc.pages[0].clean_contents(annotations=1)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("where", ["page", "appearance"])
def test_unreadable_content_leaves_the_document_unchanged(where: str) -> None:
    unreadable = "0 0 1 rg 1 2 3 4 5 6 7 8 9 10 11 12 m 0 0 10 10 re f"
    annots = "/Annots [<< /Type /Annot /Subtype /Square /Rect [20 40 60 60] /AP << /N 13 0 R >> >>]"
    extra: dict[int, str | bytes] = {
        13: _stream(unreadable if where == "appearance" else "0 0 1 rg", "/Subtype /Form /BBox [0 0 40 20] ")
    }
    if where == "page":
        extra[5] = _stream(unreadable)
    doc = pyropdf.open(build(annots=annots, extra=extra))
    before, steps = doc.to_bytes(), len(doc.history.steps)

    with pytest.raises(ValueError, match="stops reading"):
        doc.pages[0].clean_contents()

    assert doc.to_bytes() == before
    assert len(doc.history.steps) == steps


def test_content_past_the_operation_limit_is_refused() -> None:
    doc = pyropdf.open(build(extra={5: _stream("n\n" * ((1 << 20) + 1))}))
    before = doc.to_bytes()

    with pytest.raises(LimitError) as raised:
        doc.pages[0].clean_contents()

    assert raised.value.code == "content_operation_count"
    assert doc.to_bytes() == before


def test_a_stale_page_raises() -> None:
    doc = pyropdf.open(build())
    page = doc.pages[0]
    doc.pages.insert(0, page)
    del doc.pages[1]
    del doc.pages[0]

    with pytest.raises(pyropdf.StalePageError):
        page.clean_contents()
