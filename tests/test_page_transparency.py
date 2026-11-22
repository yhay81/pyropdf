"""Page transparency under ISO 32000-2 Annex Q and its published corrections."""

from __future__ import annotations

import pytest
from test_recolor import pages_pdf, stream

import pyropdf

_GS = (
    "/ExtGState << /Fill << /ca .5 >> /Stroke << /CA .5 >> /Blend << /BM /Multiply >> "
    "/Reset << /ca 1 /CA 1 /BM /Normal /SMask /None >> >>"
)
_RECT = "0 0 10 10 re "
_SINGLE = {
    "unused-resource": ("0 0 5 5 re f", False),
    "selected-without-paint": ("/Fill gs", False),
    "restore-before-paint": ("q /Fill gs Q 0 0 1 1 re f", False),
    "fill": ("/Fill gs " + _RECT + "f", True),
    "stroke": ("/Stroke gs " + _RECT + "S", True),
    "irrelevant-stroke": ("/Stroke gs " + _RECT + "f", False),
    "irrelevant-fill": ("/Fill gs " + _RECT + "S", False),
    "fill-and-stroke": ("/Stroke gs " + _RECT + "B*", True),
    "blend": ("/Blend gs " + _RECT + "f", True),
    "reset": ("/Blend gs /Fill gs /Reset gs " + _RECT + "B", False),
    "clipping-only": ("/Fill gs " + _RECT + "W n", False),
    "inline-image": ("q /Fill gs 80 0 0 80 10 10 cm BI /W 1 /H 1 /BPC 8 /CS /G ID x\nEI Q", True),
}
_FONT = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def _vector(case: str) -> bytes:
    return pages_pdf(_SINGLE[case][0], resources=_GS)


@pytest.mark.spec("page-transparency", "read", "interpret")
@pytest.mark.parametrize("case", list(_SINGLE))
def test_only_effective_graphics_state_at_paint_time_counts(case: str) -> None:
    assert pyropdf.open(_vector(case)).pages[0].uses_transparency() is _SINGLE[case][1]


@pytest.mark.parametrize(("mode", "expected"), [(0, False), (1, True), (2, True), (3, False), (5, True), (7, False)])
def test_text_rendering_mode_selects_the_relevant_alpha(mode: int, *, expected: bool) -> None:
    source = pages_pdf(f"/Stroke gs BT /F1 10 Tf {mode} Tr (A) Tj ET", resources=_GS + f" /Font << /F1 {_FONT} >>")
    assert pyropdf.open(source).pages[0].uses_transparency() is expected


def _form(content: str, extra: str = "") -> str:
    return stream(content, f"/Type /XObject /Subtype /Form /BBox [0 0 10 10] {extra}")


def _nested(kind: str) -> bytes:  # noqa: PLR0911 - one independent fixture per resource kind.
    if kind == "appearance":
        return pages_pdf(
            "",
            page="/Annots [20 0 R]",
            extra={
                20: "<< /Type /Annot /Subtype /Square /Rect [0 0 5 5] /AP << /N 21 0 R /R << /On 22 0 R >> >> >>",
                21: _form("0 0 1 1 re f"),
                22: _form("/T gs 0 0 1 1 re f", "/Resources << /ExtGState << /T << /ca .3 >> >> >>"),
            },
        )
    if kind == "annotation-blend":
        return pages_pdf(
            "", page="/Annots [20 0 R]", extra={20: "<< /Type /Annot /Subtype /Square /Rect [0 0 5 5] /BM /Multiply >>"}
        )
    if kind == "form":
        return pages_pdf(
            "/Fm Do",
            resources=_GS + " /XObject << /Fm 20 0 R /Inner 21 0 R >>",
            extra={20: _form("/Inner Do"), 21: _form("/Fill gs 0 0 4 4 re f")},
        )
    if kind == "group":
        return pages_pdf(
            "/Fm Do",
            resources="/XObject << /Fm 20 0 R >>",
            extra={20: _form("0 0 1 1 re f", "/Group << /S /Transparency /I true >>")},
        )
    if kind == "pattern":
        pattern = stream(
            "q /Fill gs 0 0 2 2 re f Q",
            "/Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 2 2] /XStep 2 /YStep 2 "
            f"/Resources << {_GS} >> ",
        )
        return pages_pdf(
            "/Pattern cs /Tile scn 0 0 4 4 re f", resources="/Pattern << /Tile 20 0 R >>", extra={20: pattern}
        )
    if kind == "type3":
        font = (
            "<< /Type /Font /Subtype /Type3 /FontBBox [0 0 5 5] /FontMatrix [1 0 0 1 0 0] /FirstChar 65 /LastChar 65 "
            "/Widths [5] /Encoding << /Type /Encoding /Differences [65 /A] >> /CharProcs << /A 21 0 R >> "
            f"/Resources << {_GS} >> >>"
        )
        return pages_pdf(
            "BT /F 1 Tf (A) Tj ET",
            resources="/Font << /F 20 0 R >>",
            extra={20: font, 21: stream("5 0 d0 /Fill gs 0 0 5 5 re f")},
        )
    image = stream(
        b"\xff", "/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 "
    )
    return pages_pdf(
        "/I Do",
        resources="/XObject << /I 20 0 R >>",
        extra={20: image.replace("/Width", "/SMask 21 0 R /Width"), 21: image},
    )


@pytest.mark.spec("page-transparency", "read", "interpret", "preserve")
@pytest.mark.parametrize("kind", ["appearance", "annotation-blend", "form", "group", "pattern", "type3", "soft-image"])
def test_used_resources_and_appearances_are_followed_through_both_saves(kind: str) -> None:
    doc = pyropdf.open(_nested(kind))
    assert doc.pages[0].uses_transparency()
    for incremental in (False, True):
        assert pyropdf.open(doc.to_bytes(incremental=incremental)).pages[0].uses_transparency()


@pytest.mark.spec("page-transparency", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_resource_edit_changes_only_pages_that_paint_it(*, incremental: bool) -> None:
    source = pages_pdf(
        "/Fm Do",
        "0 0 1 1 re f",
        resources="/XObject << /Fm 20 0 R >>",
        extra={20: _form("0 0 1 1 re f", "/Resources << /ExtGState << /T << /ca .75 >> >> >>")},
    )
    doc = pyropdf.open(source)
    assert [p.uses_transparency() for p in doc.pages] == [False, False]
    doc.objects[20].write(b"/T gs 0 0 1 1 re f")
    saved = doc.to_bytes(incremental=incremental)
    if incremental:
        assert saved.startswith(source)
    reread = pyropdf.open(saved)
    assert [p.uses_transparency() for p in reread.pages] == [True, False]
    reread.objects[20]["Resources"]["ExtGState"]["T"]["ca"] = 1
    assert [p.uses_transparency() for p in reread.pages] == [False, False]


def test_a_cycle_or_exhausted_budget_cannot_be_reported_as_opaque() -> None:
    cycle = pages_pdf("/Loop Do", resources="/XObject << /Loop 20 0 R >>", extra={20: _form("/Loop Do")})
    with pytest.raises(pyropdf.PdfError, match="cycle"):
        pyropdf.open(cycle).pages[0].uses_transparency()
    deep_state = pages_pdf("q " * 2000 + "Q " * 2000)
    with pyropdf.open(deep_state, limits=pyropdf.DocumentLimits(max_memory_size=12000)) as limited:
        with pytest.raises(pyropdf.LimitError) as refused:
            limited.pages[0].uses_transparency()
        assert refused.value.code == "memory_size"
