"""The content model that Page.content() returns.

Ported from pylopdf's tests/test_content_model.py at
036be111cafe77755db4bb2be17bc70a8f2f9d82, with these intended differences:

- The assertions on ``page.text()``, ``page.words()``, and the layout, and
  ``test_glyph_origins_agree_with_the_layout``, wait for text extraction
  (yhay81/pyropdf#117).
- The tests that drew with ``page.draw`` build their pages from objects.
- pyropdf paints the appearance streams that annotations hold, where pylopdf
  also generates the missing ones, so the annotation test brings its own.
- pyropdf issues no warnings yet, so the Type 3 test expects none.
- A refusal's message reads "over the limit of 262144", as every limit's
  does.
"""

from __future__ import annotations

import dataclasses
import pickle
import time
import warnings
from pathlib import Path

import pytest
from test_content import build_raw_pdf

import pyropdf
from pyropdf import (
    BlendMode,
    CancellationToken,
    ClosePath,
    Color,
    Content,
    CurveTo,
    DocumentLimits,
    FillRule,
    FontRef,
    GraphicsContext,
    ImageMask,
    LimitError,
    LineCap,
    LineJoin,
    LineTo,
    MarkedContent,
    Matrix,
    MoveTo,
    PathCluster,
    Point,
    Rect,
    ShadingPattern,
    ShadingType,
    SoftMask,
    SoftMaskType,
    TextRenderMode,
    TilingPattern,
    WritingMode,
)

REAL_WORLD = Path(__file__).parent / "assets" / "real_world"
CORPUS = sorted(REAL_WORLD.glob("*.pdf"))


def stream_object(data: str, dictionary: str = "") -> str:
    """Return the text of a stream object holding ``data``."""
    return f"<< {dictionary} /Length {len(data.encode('latin-1'))} >>\nstream\n{data}\nendstream"


def page_with(
    content: str,
    *,
    resources: str = "",
    objects: dict[int, str] | None = None,
    rotate: int = 0,
    annots: str = "",
) -> pyropdf.Page:
    """Open a 200 by 100 page that paints ``content``, with Helvetica as /F1.

    Extra objects are numbered from 6.
    """
    rotation = f" /Rotate {rotate}" if rotate else ""
    annotations = f" /Annots [{annots}]" if annots else ""
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] "
                f"/Resources << /Font << /F1 5 0 R >> {resources} >> >>"
            ),
            3: f"<< /Type /Page /Parent 2 0 R /Contents 4 0 R{rotation}{annotations} >>",
            4: stream_object(content),
            5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            **(objects or {}),
        }
    )
    return pyropdf.open(pdf).pages[0]


def assert_color(paint: pyropdf.Paint | None, rgb: tuple[float, float, float], alpha: float = 1) -> None:
    """Check a color paint; hayro holds opacity in eight bits."""
    assert isinstance(paint, Color)
    assert paint.rgb == pytest.approx(rgb)
    assert paint.alpha == pytest.approx(alpha, abs=1 / 255)


def stencil_page(*, inline: bool, nested: bool = False) -> pyropdf.Page:
    """Paint the same two-by-two stencil twice, optionally through a Form XObject."""
    samples = "\x40\x00"
    objects = {}
    if inline:
        drawing = f"BI /W 2 /H 2 /IM true /BPC 1 ID {samples} EI"
        resources = ""
    else:
        drawing = "/Im Do"
        resources = "/XObject << /Im 6 0 R >>"
        objects[6] = stream_object(
            samples, "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ImageMask true /BitsPerComponent 1"
        )
    if nested:
        objects[7] = stream_object(
            drawing, f"/Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources << {resources} >>"
        )
        drawing, resources = "/Fm Do", "/XObject << /Fm 7 0 R >>"
    return page_with(
        f"q 0 0 1 rg 20 0 0 20 10 10 cm {drawing} Q\nq 0 0 1 rg 20 0 0 20 40 10 cm {drawing} Q",
        resources=resources,
        objects=objects,
    )


@pytest.mark.spec("graphics-state", "read", "interpret")
@pytest.mark.spec("paths", "read", "interpret")
def test_paths_carry_their_fill_stroke_and_line_style() -> None:
    page = page_with(
        "/GS1 gs 1 0 0 rg 0 0 1 RG 2 w 1 J 2 j [3 2] 1 d 10 10 40 20 re B\n0 1 0 rg 60 10 m 80 40 100 0 120 30 c f*",
        resources="/ExtGState << /GS1 6 0 R >>",
        objects={6: "<< /Type /ExtGState /ca 0.25 /CA 0.5 >>"},
    )
    content = page.content()
    assert len(content) == 2
    rectangle, curve = content.paths

    assert_color(rectangle.fill, (1, 0, 0), 0.25)
    assert rectangle.fill_rule is FillRule.NONZERO
    assert rectangle.stroke is not None
    assert_color(rectangle.stroke.paint, (0, 0, 1), 0.5)
    assert rectangle.stroke.width == pytest.approx(2)
    assert rectangle.stroke.cap is LineCap.ROUND
    assert rectangle.stroke.join is LineJoin.BEVEL
    assert rectangle.stroke.dashes == pytest.approx((3, 2))
    assert rectangle.stroke.dash_offset == pytest.approx(1)
    assert rectangle.bbox == pytest.approx(Rect(10, 70, 50, 90))
    assert [type(segment) for segment in rectangle.segments] == [MoveTo, LineTo, LineTo, LineTo, ClosePath]

    assert_color(curve.fill, (0, 1, 0), 0.25)
    assert curve.fill_rule is FillRule.EVEN_ODD
    assert curve.stroke is None
    start, cubic = curve.segments
    assert start == MoveTo(Point(60, 90))
    assert isinstance(cubic, CurveTo)
    assert cubic.to == pytest.approx(Point(120, 70))
    assert curve.bbox == pytest.approx(Rect(60, 70, 120, 90))


def test_one_fill_and_stroke_operator_is_one_path() -> None:
    (path,) = page_with("10 10 40 20 re B").content().paths
    assert path.fill is not None
    assert path.stroke is not None


def test_a_fill_and_a_stroke_in_other_contexts_stay_two_paths() -> None:
    content = page_with("10 10 40 20 re f /Artifact BMC 10 10 40 20 re S EMC").content()
    fill, stroke = content.paths
    assert (fill.fill is not None, fill.stroke) == (True, None)
    assert (stroke.fill, stroke.stroke is not None) == (None, True)
    assert stroke.context.marked_content == (MarkedContent(tag="Artifact", mcid=None),)


@pytest.mark.spec("text-objects", "read", "interpret")
def test_text_runs_report_glyphs_with_outline_boxes() -> None:
    content = page_with("BT /F1 24 Tf 20 40 Td (H i) Tj ET").content()
    (run,) = content.text_runs
    assert run.text == "H i"
    assert run.render_mode is TextRenderMode.FILL
    assert run.fill == Color(0, 0, 0, 1)
    assert run.stroke is None
    assert run.size == pytest.approx(24)
    assert not run.font.is_type3

    letter, space, dot = run.glyphs
    assert letter.text == "H"
    assert letter.glyph_id is not None
    assert letter.origin == pytest.approx(Point(20, 60))
    assert letter.size == pytest.approx(24)
    assert letter.matrix == pytest.approx((24, 0, 0, -24, 20, 60))
    # Helvetica's H stands on the baseline, reaches its cap height of 0.718 em,
    # and advances 0.722 em; the box comes from the outline, not from metrics.
    assert letter.advance is not None
    assert letter.advance == pytest.approx(0.722 * 24, abs=0.05)
    assert letter.bbox is not None
    assert letter.bbox.y1 == pytest.approx(60, abs=0.3)
    assert letter.bbox.y0 == pytest.approx(60 - 0.718 * 24, abs=0.5)
    assert letter.origin.x < letter.bbox.x0 < letter.bbox.x1 < letter.origin.x + letter.advance

    assert space.text == " "
    assert space.bbox is None
    assert space.origin.x == pytest.approx(letter.origin.x + letter.advance)
    assert dot.bbox is not None
    assert run.bbox == letter.bbox | dot.bbox


@pytest.mark.spec("text-state", "interpret")
def test_text_render_modes_follow_the_painting() -> None:
    content = page_with(
        "BT /F1 12 Tf 1 0 0 RG 0 0 1 rg 10 50 Td 2 Tr (A) Tj 1 Tr (B) Tj 3 Tr (C) Tj 7 Tr (D) Tj 0 Tr (E) Tj ET"
    ).content()
    runs = content.text_runs
    # Text in mode 7 only clips, and hayro passes it on as invisible text.
    assert [(run.text, run.render_mode) for run in runs] == [
        ("A", TextRenderMode.FILL_STROKE),
        ("B", TextRenderMode.STROKE),
        ("C", TextRenderMode.INVISIBLE),
        ("D", TextRenderMode.INVISIBLE),
        ("E", TextRenderMode.FILL),
    ]
    fill_stroke, stroke, invisible, clip, _ = runs
    assert (clip.fill, clip.stroke) == (None, None)
    assert fill_stroke.fill == Color(0, 0, 1, 1)
    assert fill_stroke.stroke is not None
    assert fill_stroke.stroke.paint == Color(1, 0, 0, 1)
    assert stroke.fill is None
    assert stroke.stroke is not None
    assert (invisible.fill, invisible.stroke) == (None, None)
    assert invisible.glyphs[0].bbox is not None


def test_text_painted_with_fill_and_stroke_is_one_run() -> None:
    page = page_with("BT /F1 12 Tf 10 50 Td 2 Tr (Hello) Tj 0 Tr 0 -20 Td (World) Tj ET")
    (run, _) = page.content().text_runs
    assert run.render_mode is TextRenderMode.FILL_STROKE


def test_a_stroke_of_other_glyphs_is_its_own_run() -> None:
    page = page_with("BT /F1 12 Tf 10 50 Td (Hello) Tj 1 Tr 0 -20 Td (Hello) Tj ET")
    assert [run.render_mode for run in page.content().text_runs] == [TextRenderMode.FILL, TextRenderMode.STROKE]


@pytest.mark.spec("blend-modes", "read", "interpret")
@pytest.mark.spec("marked-content", "read", "interpret")
def test_items_carry_clips_blend_modes_and_marked_content() -> None:
    page = page_with(
        "q 10 10 80 80 re W n /GS1 gs /Span << /MCID 7 >> BDC 1 0 0 rg 20 20 30 30 re f EMC Q "
        "0 0 1 rg 100 10 20 20 re f",
        resources="/ExtGState << /GS1 6 0 R >>",
        objects={6: "<< /Type /ExtGState /BM /Multiply /ca 0.5 >>"},
    )
    clipped, plain = page.content().paths

    assert clipped.blend_mode is BlendMode.MULTIPLY
    assert_color(clipped.fill, (1, 0, 0), 0.5)
    (clip,) = clipped.context.clips
    assert clip.bbox == pytest.approx(Rect(10, 10, 90, 90))
    assert clip.fill_rule is FillRule.NONZERO
    assert clipped.context.clip_bbox == pytest.approx(Rect(10, 10, 90, 90))
    assert clipped.context.marked_content == (MarkedContent(tag="Span", mcid=7),)
    assert clipped.context.groups == ()
    assert clipped.context.opacity == 1

    assert plain.blend_mode is BlendMode.NORMAL
    assert plain.context == GraphicsContext(clips=(), groups=(), marked_content=())
    assert plain.context.clip_bbox is None


@pytest.mark.spec("soft-masks", "read", "interpret")
@pytest.mark.spec("transparency-groups", "read", "interpret")
@pytest.mark.parametrize("subtype", [SoftMaskType.ALPHA, SoftMaskType.LUMINOSITY])
def test_transparency_groups_carry_opacity_and_soft_masks(subtype: SoftMaskType) -> None:
    group = "/Type /XObject /Subtype /Form /BBox [0 0 200 100] /Group << /S /Transparency >>"
    mask = "/Alpha" if subtype is SoftMaskType.ALPHA else "/Luminosity"
    page = page_with(
        "/GS1 gs /Fm Do",
        resources="/ExtGState << /GS1 6 0 R >> /XObject << /Fm 7 0 R >>",
        objects={
            6: f"<< /Type /ExtGState /ca 0.25 /SMask << /S {mask} /G 8 0 R >> >>",
            7: stream_object("0 1 0 rg 10 10 50 50 re f", group),
            # hayro reads a soft mask only when its group names a color space.
            8: stream_object("0 0 200 100 re f", group.replace(">>", "/CS /DeviceGray >>")),
        },
    )
    (path,) = page.content().paths
    outer = path.context.groups[0]
    assert outer.opacity == pytest.approx(0.25)
    # hayro 0.9 no longer identifies the mask's group, object 8.
    assert outer.soft_mask == SoftMask(subtype=subtype)
    assert outer.blend_mode is BlendMode.NORMAL
    assert path.context.opacity == pytest.approx(0.25)
    assert path.fill == Color(0, 1, 0, 1)


def test_soft_mask_holds_only_its_subtype() -> None:
    mask = SoftMask(subtype=SoftMaskType.ALPHA)
    assert [field.name for field in dataclasses.fields(mask)] == ["subtype"]
    assert not hasattr(mask, "xref")
    assert not hasattr(mask, "_xref")
    assert not hasattr(mask, "__dict__")
    with pytest.raises(TypeError, match="xref"):
        SoftMask(xref=8, subtype=SoftMaskType.ALPHA)  # ty: ignore[unknown-argument]
    with pytest.raises(TypeError):
        SoftMask(SoftMaskType.ALPHA)  # ty: ignore[too-many-positional-arguments, missing-argument]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert mask == SoftMask(subtype=SoftMaskType.ALPHA)
        assert mask != SoftMask(subtype=SoftMaskType.LUMINOSITY)
        assert len({mask, SoftMask(subtype=SoftMaskType.ALPHA)}) == 1
        assert repr(mask) == f"SoftMask(subtype={SoftMaskType.ALPHA!r})"
        assert pickle.loads(pickle.dumps(mask)) == mask  # noqa: S301 - a value pickled here.
    with pytest.raises(dataclasses.FrozenInstanceError):
        mask.subtype = SoftMaskType.LUMINOSITY  # ty: ignore[invalid-assignment]


def test_items_share_the_context_they_were_painted_in() -> None:
    content = page_with("10 10 10 10 re f 30 10 10 10 re f /P BMC 50 10 10 10 re f EMC").content()
    first, second, third = content.paths
    assert first.context is second.context
    assert third.context is not first.context


@pytest.mark.spec("pattern-color-space", "read", "interpret")
@pytest.mark.spec("shading-patterns", "read", "interpret")
@pytest.mark.spec("tiling-patterns", "read", "interpret")
def test_shadings_and_tiling_patterns_are_paints() -> None:
    tiling = "/PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 10 10] /XStep 10 /YStep 12 /Resources << >>"
    page = page_with(
        "q 20 10 60 60 re W n /Sh sh Q /Pattern cs /P1 scn 120 10 40 40 re f",
        resources="/Shading << /Sh 6 0 R >> /Pattern << /P1 7 0 R >>",
        objects={
            6: (
                "<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [20 0 80 0] /Extend [true true] "
                "/Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >>"
            ),
            7: stream_object("0 0 5 5 re f", tiling),
        },
    )
    shaded, tiled = page.content().paths

    assert isinstance(shaded.fill, ShadingPattern)
    assert shaded.fill.shading_type is ShadingType.AXIAL
    assert shaded.fill.opacity == pytest.approx(1)
    (clip,) = shaded.context.clips
    assert shaded.bbox == pytest.approx(clip.bbox)
    assert shaded.bbox == pytest.approx(Rect(20, 30, 80, 90))

    assert isinstance(tiled.fill, TilingPattern)
    assert tiled.fill.bbox == Rect(0, 0, 10, 10)
    assert (tiled.fill.x_step, tiled.fill.y_step) == (10, 12)
    assert tiled.fill.opacity == 1
    assert tiled.bbox == pytest.approx(Rect(120, 50, 160, 90))


def test_tiling_patterns_report_their_opacity() -> None:
    tiling = "/PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources << >>"
    page = page_with(
        "/Pattern cs /P1 scn 10 10 40 40 re f /Half gs 60 10 40 40 re f",
        resources="/Pattern << /P1 6 0 R >> /ExtGState << /Half 7 0 R >>",
        objects={
            6: stream_object("0 0 5 5 re f", tiling),
            7: "<< /Type /ExtGState /ca 0.5 >>",
        },
    )
    opaque, half = page.content().paths

    assert isinstance(opaque.fill, TilingPattern)
    assert opaque.fill.opacity == 1
    assert isinstance(half.fill, TilingPattern)
    assert half.fill.opacity == pytest.approx(0.5)
    assert [pattern.opacity for pattern in page.paths().patterns] == pytest.approx([1, 0.5])


def test_tiling_patterns_are_constructible_without_opacity() -> None:
    pattern = TilingPattern(bbox=Rect(0, 0, 10, 10), x_step=10, y_step=10, matrix=Matrix(1, 0, 0, 1, 0, 0))
    assert pattern.opacity == 1


@pytest.mark.spec("images", "interpret")
@pytest.mark.spec("xobjects", "read", "interpret")
def test_images_report_their_object_mask_and_placement() -> None:
    image = "/Type /XObject /Subtype /Image /Width 2 /Height 1 /BitsPerComponent 8"
    page = page_with(
        "q 40 0 0 20 10 60 cm /Im Do Q",
        resources="/XObject << /Im 6 0 R >>",
        objects={
            6: stream_object("abcdef", f"{image} /ColorSpace /DeviceRGB /SMask 7 0 R"),
            7: stream_object("((", f"{image} /ColorSpace /DeviceGray"),
        },
    )

    (image,) = page.content().images
    assert image.xref == 6
    assert (image.width, image.height) == (2, 1)
    assert (image.colorspace, image.components, image.bits) == ("DeviceRGB", 3, 8)
    assert image.mask is ImageMask.SOFT
    assert not image.is_stencil
    assert image.bbox == pytest.approx(Rect(10, 20, 50, 40))
    assert image.matrix == pytest.approx((40, 0, 0, 20, 10, 20))
    assert image.context.groups[-1].opacity == 1


@pytest.mark.spec("image-masks", "interpret")
def test_stencil_masks_are_images_without_an_object() -> None:
    page = page_with("q 0 0 1 rg 20 0 0 20 10 10 cm BI /W 2 /H 2 /IM true /BPC 1 ID \x00\x00 EI Q")
    (image,) = page.content().images
    assert image.is_stencil
    assert image.xref == 0
    assert image.colorspace == ""
    assert image.bbox == pytest.approx(Rect(10, 70, 30, 90))


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.spec("form-xobjects", "read", "interpret")
def test_stencil_masks_keep_their_stream_identity_and_placement(*, inline: bool, nested: bool) -> None:
    page = stencil_page(inline=inline, nested=nested)
    images = page.content().images
    assert len(images) == 2
    for image, x in zip(images, (10, 40), strict=True):
        assert image.is_stencil
        assert image.xref == (0 if inline else 6)
        # hayro's inline stream range includes the space before EI, as for raster images.
        assert image.encoded_size == (3 if inline else 2)
        assert (image.width, image.height, image.bits, image.components) == (2, 2, 1, 1)
        assert image.colorspace == ""
        assert image.mask is None
        assert image.bbox == pytest.approx(Rect(x, 70, x + 20, 90))


@pytest.mark.spec("type3-fonts", "interpret")
def test_type3_glyphs_take_their_boxes_from_the_procedure() -> None:
    page = pyropdf.open(REAL_WORLD / "pdfium-type3.pdf").pages[0]
    runs = page.content().text_runs
    assert runs
    for run in runs:
        assert run.font.is_type3
        for glyph in run.glyphs:
            assert glyph.glyph_id is None
            assert glyph.text is None
            assert glyph.advance is None
            assert glyph.bbox is not None
            assert glyph.bbox in run.bbox


def test_content_uses_rotated_display_coordinates() -> None:
    page = page_with("10 10 40 20 re f BT /F1 10 Tf 10 50 Td (A) Tj ET", rotate=90)
    (path,) = page.content().paths
    assert path.bbox == pytest.approx(Rect(10, 10, 30, 50))
    (run,) = page.content().text_runs
    a, b, c, d, _, _ = run.glyphs[0].matrix
    assert (a, b, c, d) == pytest.approx((0, 10, 10, 0))


def test_clip_keeps_the_items_that_meet_it() -> None:
    page = page_with("10 10 20 20 re f 150 10 20 20 re f")
    assert len(page.content()) == 2
    (kept,) = page.content(clip=(0, 0, 100, 100))
    assert kept.bbox.x0 == pytest.approx(10)
    assert len(page.content(clip=(30, 0, 150, 100))) == 2
    assert len(page.content(clip=(31, 0, 149, 100))) == 0


def test_annotations_can_be_left_out() -> None:
    # pylopdf generates the appearances that annotations lack, as the corpus's
    # pdfium-links-highlights-annots.pdf needs; pyropdf paints the appearance
    # streams that annotations hold until its annotation engine is ported.
    page = page_with(
        "10 10 20 20 re f",
        annots="6 0 R",
        objects={
            6: "<< /Type /Annot /Subtype /Square /Rect [100 10 150 60] /AP << /N 7 0 R >> >>",
            7: stream_object("0 0 1 rg 0 0 50 50 re f", "/Type /XObject /Subtype /Form /BBox [0 0 50 50]"),
        },
    )
    with_annotations = page.content()
    without = page.content(annotations=False)
    assert (len(without), len(with_annotations)) == (1, 2)
    assert with_annotations[: len(without)] == without.items
    assert with_annotations[-1].bbox == pytest.approx(Rect(100, 40, 150, 90))


def test_max_elements_bounds_the_interpretation() -> None:
    page = page_with("10 10 20 20 re f")
    with pytest.raises(pyropdf.LimitError) as caught:
        page.content(max_elements=3)
    assert caught.value.code == "content_element_count"
    assert len(page.content(max_elements=None)) == 1

    # 90,000 strokes of two segments each pass the default of 262,144.
    dense = page_with(" ".join(f"{index % 190} 10 m {index % 190 + 5} 20 l S" for index in range(90_000)))
    with pytest.raises(pyropdf.LimitError, match="over the limit of 262144"):
        dense.content()


def _smallest_budget(page: pyropdf.Page) -> int:
    budget = 1
    while True:
        try:
            page.content(max_elements=budget)
        except pyropdf.LimitError:
            budget += 1
        else:
            return budget


@pytest.mark.parametrize("operator", ["S", "B"])
def test_max_elements_counts_dash_lengths(operator: str) -> None:
    # A dash array repeated by every stroke must not multiply memory, so each
    # stroke charges its dash lengths along with its segments.
    dashes = " ".join(["1"] * 100)
    plain = page_with(f"0 0 m 10 10 l {operator}")
    dashed = page_with(f"[{dashes}] 0 d 0 0 m 10 10 l {operator}")
    assert _smallest_budget(dashed) == _smallest_budget(plain) + 100
    (path,) = dashed.content().paths
    assert path.stroke is not None
    assert len(path.stroke.dashes) == 100


def test_text_paints_no_paths() -> None:
    assert page_with("BT /F1 12 Tf 10 50 Td (Text only) Tj ET").content().paths == ()


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"max_elements": 0}, ValueError),
        ({"max_elements": True}, TypeError),
        ({"annotations": 1}, TypeError),
        ({"text_runs": 0}, TypeError),
        ({"paths": None}, TypeError),
        ({"images": "no"}, TypeError),
        ({"clip": (0, 0, 1)}, ValueError),
    ],
)
def test_content_validates_its_options(options: dict[str, object], error: type[Exception]) -> None:
    page = page_with("10 10 20 20 re f")
    with pytest.raises(error):
        page.content(**options)  # ty: ignore[invalid-argument-type]


def test_content_is_an_immutable_sequence() -> None:
    page = page_with("1 0 0 rg 10 10 20 20 re f BT /F1 12 Tf 10 50 Td (Hi) Tj ET")
    content = page.content()
    path, run = content
    assert content[0] is path
    assert content[-1] is run
    assert content[0:1] == (path,)
    assert content.paths == (path,)
    assert content.text_runs == (run,)
    assert content.images == ()
    assert content == page.content()
    assert hash(content) == hash(page.content())
    assert pickle.loads(pickle.dumps(content)) == content  # noqa: S301 - a value pickled here.
    assert repr(path).startswith("Path(segments=(MoveTo(to=Point(")
    with pytest.raises(dataclasses.FrozenInstanceError):
        content.items = ()  # ty: ignore[invalid-assignment]
    with pytest.raises(dataclasses.FrozenInstanceError):
        path.bbox = Rect()  # ty: ignore[invalid-assignment]


def test_content_reflects_edits() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 100))
    assert len(page.content()) == 0
    page.obj["Contents"] = doc.objects[doc.objects.add_stream(b"1 0 0 RG 10 70 40 20 re S")]
    (path,) = page.content().paths
    assert path.stroke is not None
    assert path.stroke.paint == Color(1, 0, 0, 1)
    assert path.bbox == pytest.approx(Rect(10, 10, 50, 30))


def test_a_deleted_page_is_stale() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 100))
    del doc.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.content()


@pytest.mark.parametrize("path", CORPUS, ids=lambda path: path.name)
def test_selected_kinds_equal_the_full_content_filtered(path: Path) -> None:
    doc = pyropdf.open(path)
    for page in doc.pages[:3]:
        full = page.content()
        for text_runs, paths, images in (
            (False, True, True),
            (True, False, True),
            (True, True, False),
            (False, True, False),
        ):
            kept = tuple(
                item
                for item in full
                if (text_runs or not isinstance(item, pyropdf.TextRun))
                and (paths or not isinstance(item, pyropdf.Path))
                and (images or not isinstance(item, pyropdf.Image))
            )
            selected = page.content(text_runs=text_runs, paths=paths, images=images)
            assert selected.items == kept, (text_runs, paths, images)


def test_no_kinds_give_empty_content() -> None:
    page = page_with("10 10 20 20 re f BT /F1 12 Tf 10 50 Td (Text) Tj ET")
    assert page.content(text_runs=False, paths=False, images=False).items == ()


def test_extracted_colors_expose_stored_colorspace_and_components() -> None:
    icc_dir = Path(__file__).parent / "assets" / "print-exchange"
    icc_gray = (icc_dir / "virtual-gray.icc").read_bytes().hex().upper() + ">"
    icc_rgb = (icc_dir / "virtual-rgb.icc").read_bytes().hex().upper() + ">"

    page = page_with(
        "0.25 g 0 0 10 10 re f "
        "0.2 0.4 0.6 rg 12 0 10 10 re f "
        "0.1 0.2 0.3 0.4 k 24 0 10 10 re f "
        "/CS_ICCG cs 0.5 scn 36 0 10 10 re f "
        "/CS_ICCR cs 0.25 0.5 0.75 scn 48 0 10 10 re f "
        "/CS_CALG cs 0.75 scn 60 0 10 10 re f "
        "/CS_CALR cs 0.1 0.5 0.9 scn 72 0 10 10 re f "
        "/CS_LAB cs 50 10 -20 scn 84 0 10 10 re f "
        "/CS_IDX cs 1 scn 96 0 10 10 re f "
        "/CS_SEP cs 0.6 scn 108 0 10 10 re f "
        "/CS_DEVN cs 0.25 0.75 scn 120 0 10 10 re f "
        "BT /F1 12 Tf 0.1 0.2 0.3 0.4 k 10 50 Td (CMYK) Tj ET",
        resources=(
            "/ColorSpace << "
            "/CS_ICCG [/ICCBased 6 0 R] "
            "/CS_ICCR [/ICCBased 7 0 R] "
            "/CS_CALG [/CalGray << /WhitePoint [0.9505 1.0 1.089] /Gamma 2.2 >>] "
            "/CS_CALR [/CalRGB << /WhitePoint [0.9505 1.0 1.089] >>] "
            "/CS_LAB [/Lab << /WhitePoint [0.9505 1.0 1.089] /Range [-100 100 -100 100] >>] "
            "/CS_IDX [/Indexed /DeviceRGB 1 <FF000000FF00>] "
            "/CS_SEP [/Separation /SpotRed /DeviceRGB 8 0 R] "
            "/CS_DEVN [/DeviceN [/Cyan /Spot] /DeviceRGB 9 0 R] "
            ">>"
        ),
        objects={
            6: stream_object(icc_gray, "/N 1 /Filter /ASCIIHexDecode"),
            7: stream_object(icc_rgb, "/N 3 /Filter /ASCIIHexDecode"),
            8: "<< /FunctionType 2 /Domain [0 1] /C0 [1 1 1] /C1 [1 0 0] /N 1 >>",
            9: stream_object("{ 1 exch sub 0 }", "/FunctionType 4 /Domain [0 1 0 1] /Range [0 1 0 1 0 1]"),
        },
    )

    expected = [
        ("DeviceGray", (0.25,)),
        ("DeviceRGB", (0.2, 0.4, 0.6)),
        ("DeviceCMYK", (0.1, 0.2, 0.3, 0.4)),
        ("ICCBased", (0.5,)),
        ("ICCBased", (0.25, 0.5, 0.75)),
        ("CalGray", (0.75,)),
        ("CalRGB", (0.1, 0.5, 0.9)),
        ("Lab", (50.0, 10.0, -20.0)),
        ("Indexed", (1.0,)),
        ("Separation", (0.6,)),
        ("DeviceN", (0.25, 0.75)),
    ]

    content = page.content()
    columnar = page.paths()
    assert len(content.paths) == len(columnar) == len(expected)
    for path, col_path, (space, comps) in zip(content.paths, columnar, expected, strict=True):
        for candidate in (path.fill, col_path.fill):
            assert isinstance(candidate, Color)
            assert candidate.colorspace == space
            assert candidate.components == pytest.approx(comps)
            assert all(0.0 <= channel <= 1.0 for channel in candidate.rgb)

    (run,) = content.text_runs
    assert isinstance(run.fill, Color)
    assert run.fill.colorspace == "DeviceCMYK"
    assert run.fill.components == pytest.approx((0.1, 0.2, 0.3, 0.4))

    # Identical sRGB values painted in distinct PDF color spaces keep their own stored metadata.
    blacks = page_with("0 g 0 0 10 10 re f 0 0 0 rg 12 0 10 10 re f 0 0 0 1 k 24 0 10 10 re f")
    for source in (blacks.content().paths, list(blacks.paths())):
        gray_black, rgb_black, cmyk_black = (p.fill for p in source)
        assert isinstance(gray_black, Color)
        assert isinstance(rgb_black, Color)
        assert isinstance(cmyk_black, Color)
        assert gray_black == rgb_black == Color(0, 0, 0, 1)
        assert [(c.colorspace, c.components) for c in (gray_black, rgb_black, cmyk_black)] == [
            ("DeviceGray", (0.0,)),
            ("DeviceRGB", (0.0, 0.0, 0.0)),
            ("DeviceCMYK", (0.0, 0.0, 0.0, 1.0)),
        ]


def test_color_equality_hashing_pickle_and_canvas_round_trip() -> None:
    plain = Color(0.0, 0.0, 0.0)
    gray = Color(0.0, 0.0, 0.0, 1.0, colorspace="DeviceGray", components=(0.0,))
    cmyk = Color(0.0, 0.0, 0.0, 1.0, colorspace="DeviceCMYK", components=(0.0, 0.0, 0.0, 1.0))

    assert plain.colorspace is None
    assert plain.components is None
    assert plain == gray == cmyk
    assert hash(plain) == hash(gray) == hash(cmyk)
    assert len({plain, gray, cmyk}) == 1
    assert {plain: "black"}[cmyk] == "black"

    restored = pickle.loads(pickle.dumps(cmyk))  # noqa: S301 - a value pickled here.
    assert restored == cmyk
    assert restored.colorspace == "DeviceCMYK"
    assert restored.components == (0.0, 0.0, 0.0, 1.0)

    source = page_with("0.1 0.2 0.3 0.4 k 10 10 40 20 re f")
    (extracted_path,) = source.content().paths
    assert isinstance(extracted_path.fill, Color)
    assert extracted_path.fill.colorspace == "DeviceCMYK"

    target = pyropdf.Document()
    page = target.pages.new((200, 100))
    with page.draw() as canvas:
        canvas.rect((10, 10, 50, 30), fill=extracted_path.fill)
        canvas.draw(extracted_path)
    assert len(page.content().paths) == 2


def test_text_run_writing_mode_reports_horizontal_and_vertical() -> None:
    page = page_with(
        "BT /FH 20 Tf 10 60 Td <65E5> Tj /FV 20 Tf 80 80 Td <65E5> Tj ET",
        resources="/Font << /FH 6 0 R /FV 7 0 R >>",
        objects={
            6: (
                "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Gothic /Encoding /UniJIS-UCS2-H "
                "/DescendantFonts [8 0 R] >>"
            ),
            7: (
                "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Gothic /Encoding /UniJIS-UCS2-V "
                "/DescendantFonts [8 0 R] >>"
            ),
            8: (
                "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MS-Gothic "
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 2 >> "
                "/FontDescriptor 9 0 R /DW 1000 >>"
            ),
            9: (
                "<< /Type /FontDescriptor /FontName /MS-Gothic /Flags 4 "
                "/FontBBox [0 -141 1000 859] /ItalicAngle 0 /Ascent 859 /Descent -141 "
                "/CapHeight 709 /StemV 80 >>"
            ),
        },
    )
    horiz, vert = page.content().text_runs
    assert horiz.writing_mode is WritingMode.HORIZONTAL
    assert horiz.writing_mode == "horizontal"
    assert vert.writing_mode is WritingMode.VERTICAL
    assert vert.writing_mode == "vertical"
    assert WritingMode.__module__ == "pyropdf"


def test_font_ref_reports_object_identity_across_font_types() -> None:
    # Standard 14 indirect Type 1 font.
    (std_run,) = page_with("BT /F1 12 Tf 10 50 Td (Hi) Tj ET").content().text_runs
    assert std_run.font.xref == 5
    assert std_run.font.subtype == "Type1"
    assert std_run.font.name == "Helvetica"
    assert not std_run.font.is_embedded
    assert not std_run.font.is_type3

    # Direct Type 1 font dictionary (xref is None).
    direct_page = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>",
                3: (
                    "<< /Type /Page /Parent 2 0 R /Contents 4 0 R "
                    "/Resources << /Font << /FD << /Type /Font /Subtype /Type1 /BaseFont /Courier >> >> >> >>"
                ),
                4: stream_object("BT /FD 12 Tf 10 50 Td (Direct) Tj ET"),
            }
        )
    ).pages[0]
    (direct_run,) = direct_page.content().text_runs
    assert direct_run.font.xref is None
    assert direct_run.font.subtype == "Type1"
    assert direct_run.font.name == "Courier"
    assert not direct_run.font.is_embedded

    # Real-world direct font dictionary (patent-us223898.pdf).
    patent_runs = pyropdf.open(REAL_WORLD / "patent-us223898.pdf").pages[0].content().text_runs
    assert patent_runs
    assert all(run.font.xref is None and run.font.name == "Courier" and not run.font.is_embedded for run in patent_runs)

    # Two distinct Type 3 fonts on one page keep separate xrefs and names.
    type3_page = page_with(
        "BT /T3A 20 Tf 10 50 Td (A) Tj /T3B 20 Tf 40 50 Td (A) Tj ET",
        resources="/Font << /T3A 6 0 R /T3B 7 0 R >>",
        objects={
            6: (
                "<< /Type /Font /Subtype /Type3 /Name /NamedT3 /FontBBox [0 0 1000 1000] "
                "/FontMatrix [0.001 0 0 0.001 0 0] /CharProcs << /A 8 0 R >> "
                "/Encoding << /Type /Encoding /Differences [65 /A] >> "
                "/FirstChar 65 /LastChar 65 /Widths [600] >>"
            ),
            7: (
                "<< /Type /Font /Subtype /Type3 /FontBBox [0 0 1000 1000] "
                "/FontMatrix [0.001 0 0 0.001 0 0] /CharProcs << /A 8 0 R >> "
                "/Encoding << /Type /Encoding /Differences [65 /A] >> "
                "/FirstChar 65 /LastChar 65 /Widths [600] >>"
            ),
            8: stream_object("600 0 d0 0 0 500 700 re f"),
        },
    )
    t3_a, t3_b = type3_page.content().text_runs
    assert (t3_a.font.xref, t3_a.font.subtype, t3_a.font.name, t3_a.font.is_type3, t3_a.font.is_embedded) == (
        6,
        "Type3",
        "NamedT3",
        True,
        False,
    )
    assert (t3_b.font.xref, t3_b.font.subtype, t3_b.font.name, t3_b.font.is_type3, t3_b.font.is_embedded) == (
        7,
        "Type3",
        "",
        True,
        False,
    )

    # Embedded Type 1 (usrguide.pdf), non-embedded Type 1 (pdf20-simple.pdf),
    # embedded TrueType and Type 0 (mhlw-doc.pdf, nics-background-checks-2015-11.pdf),
    # and Type 3 (pdfium-type3.pdf) agree with Page.fonts() on xref, subtype, and embedding.
    seen_subtypes: set[tuple[str, bool]] = set()
    for corpus_name in (
        "usrguide.pdf",
        "bill-hr815.pdf",
        "pdf20-simple.pdf",
        "mhlw-doc.pdf",
        "nics-background-checks-2015-11.pdf",
        "pdfium-type3.pdf",
    ):
        doc = pyropdf.open(REAL_WORLD / corpus_name)
        page = doc.pages[0]
        by_xref = {info.xref: info for info in page.fonts() if info.xref is not None}
        for run in page.content().text_runs:
            assert isinstance(run.font, FontRef)
            assert run.font.xref in by_xref
            info = by_xref[run.font.xref]
            assert run.font.subtype == info.subtype
            assert run.font.is_embedded == (info.program is not None)
            if info.subtype != "Type3":
                assert run.font.name != ""
            seen_subtypes.add((run.font.subtype, run.font.is_embedded))
    assert {
        ("Type1", False),
        ("Type1", True),
        ("TrueType", True),
        ("Type0", True),
        ("Type3", False),
    } <= seen_subtypes


def test_path_clustering_merges_diagrams_icons_charts_and_respects_tolerances() -> None:
    # Build a 200x100 page with:
    # 0-2: L-shaped chart axes (vertical + horizontal) plus an interior bar absorbed transitively
    # 3-4: two-stroke icon separated horizontally by 2 pt (merges at tol=3, splits at tol=1)
    # 5:   hairline stroked line (0 w) below the icon (kept)
    content_stream = (
        "2 w 10 20 m 10 80 l S\n"  # vertical axis (display x=9..11, y=19..81)
        "2 w 10 20 m 70 20 l S\n"  # horizontal axis (display x=9..71, y=79..81)
        "0.2 0.4 0.8 rg 30 35 12 25 re f\n"  # interior bar (display x=30..42, y=40..65)
        "1 w 120 60 15 15 re S\n"  # icon stroke A (display x=119.5..135.5, y=24.5..40.5)
        "1 w 138 60 15 15 re S\n"  # icon stroke B (display x=137.5..153.5, y=24.5..40.5; 2pt gap)
        "0 w 120 42 m 153 42 l S"  # hairline rule at y=58 (display x=120..153, y=58..58)
    )
    page = page_with(content_stream)
    paths = page.paths()
    content = page.content()

    clusters = paths.clusters()
    assert clusters == content.path_clusters()
    assert len(clusters) == 3

    # Cluster 0: chart (axes + interior bar) -> display x=9..71, y=19..81
    # Cluster 1: icon (two strokes merged across 2pt gap) -> display x=119.5..153.5, y=24.5..40.5
    # Cluster 2: hairline rule -> display x=120..153, y=58..58
    chart, icon, hairline = clusters
    assert chart.bbox == pytest.approx(Rect(9, 19, 71, 81))
    assert chart.path_indices == (0, 1, 2)
    assert chart.count == 3

    assert icon.bbox == pytest.approx(Rect(119.5, 24.5, 153.5, 40.5))
    assert icon.path_indices == (3, 4)
    assert icon.count == 2

    assert hairline.bbox == pytest.approx(Rect(120, 58, 153, 58))
    assert hairline.path_indices == (5,)
    assert hairline.count == 1

    # Tighter tolerance splits the two icon strokes apart.
    tight = paths.clusters(x_tolerance=1.0, y_tolerance=1.0)
    assert len(tight) == 4
    assert [c.path_indices for c in tight] == [(0, 1, 2), (3,), (4,), (5,)]

    # Slicing Paths returns path_indices relative to the slice.
    sliced = paths[3:5]
    sliced_clusters = sliced.clusters(x_tolerance=3.0, y_tolerance=3.0)
    assert len(sliced_clusters) == 1
    assert sliced_clusters[0].path_indices == (0, 1)
    assert sliced_clusters[0].bbox == pytest.approx(icon.bbox)

    # Record invariants: frozen dataclass with slots, kw-only, hashable, picklable, public module.
    assert PathCluster.__module__ == "pyropdf"
    assert dataclasses.is_dataclass(chart)
    assert "__slots__" in vars(PathCluster)
    assert hash(chart) == hash(PathCluster(bbox=chart.bbox, path_indices=chart.path_indices))
    assert pickle.loads(pickle.dumps(chart)) == chart  # noqa: S301 - a value pickled here.
    with pytest.raises(dataclasses.FrozenInstanceError):
        chart.bbox = Rect(0, 0, 1, 1)  # ty: ignore[invalid-assignment]

    # Manually constructed and unpickled Content run the same Rust clustering path,
    # and degenerate zero-area fill-only paths are omitted.
    degenerate_fill = pyropdf.Path(
        segments=(MoveTo(Point(170, 50)), LineTo(Point(190, 50))),
        fill=Color(0, 0, 0),
        fill_rule=FillRule.NONZERO,
        stroke=None,
        bbox=Rect(170, 50, 190, 50),
        context=GraphicsContext(clips=(), groups=(), marked_content=()),
        blend_mode=BlendMode.NORMAL,
        soft_mask=None,
    )
    constructed = Content(items=(*content.items, degenerate_fill))
    assert constructed.path_clusters() == clusters
    unpickled = pickle.loads(pickle.dumps(content))  # noqa: S301 - a value pickled here.
    assert unpickled.path_clusters() == clusters


def test_path_clustering_rotated_and_cropped_pages_and_background_exclusion() -> None:
    # Full-page background fill plus two separate figures; verify across rotation and CropBox.
    stream = (
        "1 1 1 rg 0 0 200 100 re f\n"  # full-page background rectangle
        "1 0 0 rg 20 20 30 20 re f\n"  # figure A (PDF x=20..50, y=20..40)
        "0 0 1 rg 140 60 30 20 re f"  # figure B (PDF x=140..170, y=60..80)
    )
    for rotate in (0, 90, 180, 270):
        page = page_with(stream, rotate=rotate)
        clusters = page.paths().clusters()
        assert len(clusters) == 2
        assert clusters == page.content().path_clusters()
        for cluster in clusters:
            assert cluster.count == 1
            assert 0 <= cluster.bbox.x0 < cluster.bbox.x1 <= page.rect.width
            assert 0 <= cluster.bbox.y0 < cluster.bbox.y1 <= page.rect.height
        # Disabling background exclusion collapses everything into the full-page background.
        with_bg = page.paths().clusters(exclude_background=False)
        assert len(with_bg) == 1
        assert with_bg[0].path_indices == (0, 1, 2)
        assert with_bg[0].bbox == pytest.approx(page.rect)

    # Cropped page: background covering the full visible CropBox is excluded in display space.
    cropped_pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>",
            3: "<< /Type /Page /Parent 2 0 R /CropBox [20 10 180 90] /Rotate 90 /Contents 4 0 R >>",
            4: stream_object(
                "1 1 1 rg 0 0 200 100 re f\n"  # bleeds past CropBox; covers 100% of visible page
                "1 0 0 rg 30 20 20 15 re f\n"
                "0 0 1 rg 130 55 20 15 re f"
            ),
        }
    )
    cropped_page = pyropdf.open(cropped_pdf).pages[0]
    cropped_clusters = cropped_page.paths().clusters()
    assert len(cropped_clusters) == 2
    for cluster in cropped_clusters:
        assert 0 <= cluster.bbox.x0 < cluster.bbox.x1 <= cropped_page.rect.width
        assert 0 <= cluster.bbox.y0 < cluster.bbox.y1 <= cropped_page.rect.height
    assert len(cropped_page.paths().clusters(exclude_background=False)) == 1

    # A full-page diagonal line is NOT a background rectangle and stays clustered.
    diag_page = page_with("2 w 0 0 m 200 100 l S")
    diag_clusters = diag_page.paths().clusters(exclude_background=True)
    assert len(diag_clusters) == 1
    assert diag_clusters[0].path_indices == (0,)


def test_path_clustering_validation_limits_and_near_linear_scaling() -> None:
    # Build a page with 4,000 paths arranged in 40 disjoint clusters of 100 overlapping paths each.
    ops: list[str] = []
    for cluster_idx in range(40):
        bx = (cluster_idx % 8) * 100
        by = (cluster_idx // 8) * 100
        for j in range(100):
            dx = (j % 10) * 4
            dy = (j // 10) * 4
            ops.append(f"{bx + dx} {by + dy} 5 5 re f")
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 1000 600] >>",
            3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
            4: stream_object("\n".join(ops)),
        }
    )

    doc = pyropdf.open(pdf)
    paths = doc.pages[0].paths()
    assert len(paths) == 4000

    t0 = time.perf_counter()
    clusters = paths.clusters()
    elapsed = time.perf_counter() - t0
    assert len(clusters) == 40
    assert all(cluster.count == 100 for cluster in clusters)
    # Near-linear Rust sweep should finish far below any quadratic runtime.
    assert elapsed < 5.0

    # max_clusters refuses with typed LimitError (content_element_count); None lifts the cap.
    with pytest.raises(LimitError) as exc_info:
        paths.clusters(max_clusters=10)
    assert exc_info.value.code == "content_element_count"
    assert len(paths.clusters(max_clusters=None)) == 40

    # DocumentLimits.max_memory_size refuses clustering working buffers with LimitError (memory_size).
    # Distinct Y coordinates per path ensure the segment tree exceeds a tight memory cap while
    # Page.paths() itself stays well under the cap.
    distinct_y_ops = "\n".join(f"{(i % 20) * 20} {i * 0.25:.2f} 4 0.2 re f" for i in range(4000))
    distinct_y_pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 500 1200] >>",
            3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
            4: stream_object(distinct_y_ops),
        }
    )
    low_mem_doc = pyropdf.open(distinct_y_pdf, limits=DocumentLimits(max_memory_size=320 * 1024))
    low_mem_paths = low_mem_doc.pages[0].paths()
    assert len(low_mem_paths) == 4000
    with pytest.raises(LimitError) as mem_err:
        low_mem_paths.clusters()
    assert mem_err.value.code == "memory_size"

    # DocumentLimits cancellation token refuses with LimitError (cancelled).
    token = CancellationToken()
    cancel_doc = pyropdf.open(distinct_y_pdf, limits=DocumentLimits(cancellation=token))
    cancel_paths = cancel_doc.pages[0].paths()
    token.cancel()
    with pytest.raises(LimitError) as cancel_err:
        cancel_paths.clusters()
    assert cancel_err.value.code == "cancelled"

    # Option validation errors.
    for bad_tol in (-1.0, float("nan"), float("inf"), True, "3"):
        with pytest.raises(ValueError, match="x_tolerance"):
            paths.clusters(x_tolerance=bad_tol)  # ty: ignore[invalid-argument-type]
        with pytest.raises(ValueError, match="y_tolerance"):
            paths.clusters(y_tolerance=bad_tol)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="exclude_background"):
        paths.clusters(exclude_background=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="max_clusters"):
        paths.clusters(max_clusters=0)
    with pytest.raises(TypeError, match="max_clusters"):
        paths.clusters(max_clusters=True)


def test_path_clustering_real_world_forms_tables_and_background() -> None:
    # Real-world forms and tables produce deterministic, bounded clusters in display space.
    for rel_path in (
        "f1040.pdf",
        "forms/mhlw-deposit-report.pdf",
        "nics-background-checks-2015-11.pdf",
        "bill-hr815.pdf",
    ):
        with pyropdf.open(REAL_WORLD / rel_path) as doc:
            page = doc.pages[0]
            paths = page.paths()
            assert len(paths) > 0
            clusters = paths.clusters()
            assert clusters == page.content().path_clusters()
            assert len(clusters) > 0
            assert len(clusters) < len(paths)
            # Every cluster's path_indices are strictly ascending valid indices and cover its bbox.
            seen: set[int] = set()
            for cluster in clusters:
                assert cluster.count == len(cluster.path_indices) > 0
                assert list(cluster.path_indices) == sorted(cluster.path_indices)
                assert all(0 <= idx < len(paths) for idx in cluster.path_indices)
                assert seen.isdisjoint(cluster.path_indices)
                seen.update(cluster.path_indices)
                assert cluster.bbox.x0 <= cluster.bbox.x1
                assert cluster.bbox.y0 <= cluster.bbox.y1

    # Full-page background rectangle in profile-sources/4-fail.pdf is excluded by default.
    bg_pdf = Path(__file__).parent / "assets" / "profile-sources" / "4-fail.pdf"
    with pyropdf.open(bg_pdf) as doc:
        paths = doc.pages[0].paths()
        assert len(paths) == 1
        assert paths.clusters(exclude_background=True) == []
        with_bg = paths.clusters(exclude_background=False)
        assert len(with_bg) == 1
        assert with_bg[0].path_indices == (0,)
        assert with_bg[0].bbox == pytest.approx(doc.pages[0].rect)
