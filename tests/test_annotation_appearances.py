"""Appearances of added annotations, and the limits of adding them.

pylopdf's tests/test_annotation_appearances.py at ebb01d3c, apart from the
cases of links (tests/test_links.py) and page copies. Created highlights,
and the appearances that a render draws for annotations that a file stores
without one, are checked through rendered pixels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

YELLOWISH_BLUE_MAX = 210
WHITE_MIN = 240


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


def build_pdf(texts: list[str]) -> bytes:
    """Return pylopdf's test document: one page per text, with MediaBox and Resources on the tree's root."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(texts)))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(texts)} /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 3 0 R >> >> >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(texts):
        content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET"
        objects[4 + 2 * i] = f"<< /Type /Page /Parent 2 0 R /Contents {5 + 2 * i} 0 R >>"
        objects[5 + 2 * i] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    return build_raw_pdf(objects)


def _region_pixels(page: pyropdf.Page, rect: pyropdf.Rect) -> list[tuple[int, int, int]]:
    """Return RGB pixels inside ``rect`` after rendering on white."""
    pix = page.pixmap(background=(255, 255, 255), alpha=True)
    out = []
    for y in range(int(rect.y0), min(int(rect.y1), pix.height)):
        for x in range(int(rect.x0), min(int(rect.x1), pix.width)):
            offset = y * pix.stride + x * 4
            r, g, b = bytes(pix)[offset : offset + 3]
            out.append((r, g, b))
    return out


def _has_yellowish(pixels: list[tuple[int, int, int]]) -> bool:
    """Return whether the sample contains yellowish pixels."""
    return any(r > WHITE_MIN and g > WHITE_MIN and b < YELLOWISH_BLUE_MAX for r, g, b in pixels)


def _has_dominant_color(pixels: list[tuple[int, int, int]], channel: int) -> bool:
    """Return whether one RGB channel visibly dominates in the sample."""
    return any(
        pixel[channel] > 220 and all(pixel[index] < 180 for index in range(3) if index != channel) for pixel in pixels
    )


def _one_page(annots: str, objects: dict[int, str] | None = None) -> pyropdf.Document:
    """Open a 100 x 100 page whose ``/Annots`` is ``annots``, with more objects from 4."""
    return pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots {annots} >>",
                **(objects or {}),
            }
        )
    )


def test_search_and_highlight() -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    page = doc.pages[0]
    hits = page.search("Hello")
    assert hits
    page.annotations.add(pyropdf.Highlight(hits))

    annots = list(page.annotations)
    assert len(annots) == 1
    annot = annots[0]
    assert annot.type is pyropdf.AnnotationType.HIGHLIGHT
    hit = hits[0]
    # The annotation covers the hit; allow small storage differences.
    assert annot.rect.x0 <= hit.x0 + 0.01
    assert annot.rect.x1 >= hit.x1 - 0.01
    # The appearance stream makes the highlight visible in hayro.
    assert _has_yellowish(_region_pixels(page, hit))
    # Multiply blending preserves the black text pixels.
    assert any(r < 128 for r, _, _ in _region_pixels(page, hit))


def test_highlight_survives_save_roundtrip() -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    hits = doc.pages[0].search("PDF")
    doc.pages[0].annotations.add(pyropdf.Highlight(hits, contents="review note"))
    reopened = pyropdf.open(doc.to_bytes())
    annots = list(reopened.pages[0].annotations)
    assert len(annots) == 1
    assert annots[0].type is pyropdf.AnnotationType.HIGHLIGHT
    assert annots[0].contents == "review note"
    assert _has_yellowish(_region_pixels(reopened.pages[0], hits[0]))


def test_highlight_multiple_rects_in_one_annot() -> None:
    doc = pyropdf.open(build_pdf(["Hello PDF"]))
    page = doc.pages[0]
    rects = [pyropdf.Rect(10, 10, 40, 20), pyropdf.Rect(60, 40, 90, 50)]
    page.annotations.add(pyropdf.Highlight(rects))
    annots = list(page.annotations)
    assert len(annots) == 1
    # The bounding rectangle covers both input rectangles.
    assert annots[0].rect.x0 <= 10
    assert annots[0].rect.x1 >= 90
    assert annots[0].rect.y1 >= 50
    assert _has_yellowish(_region_pixels(page, rects[0]))
    assert _has_yellowish(_region_pixels(page, rects[1]))


def test_highlight_single_rect_argument() -> None:
    page = pyropdf.Document().pages.new((100, 100))
    page.annotations.add(pyropdf.Highlight((20, 20, 80, 40)))
    assert page.annotations[0].type is pyropdf.AnnotationType.HIGHLIGHT
    assert _has_yellowish(_region_pixels(page, pyropdf.Rect(20, 20, 80, 40)))


def test_highlight_on_rotated_page_uses_display_coordinates() -> None:
    page = pyropdf.Document().pages.new((100, 200))
    page.rotation = 90  # Display coordinates are 200 x 100.
    target = pyropdf.Rect(120, 30, 180, 70)
    page.annotations.add(pyropdf.Highlight(target))
    assert _has_yellowish(_region_pixels(page, target))
    assert not _has_yellowish(_region_pixels(page, pyropdf.Rect(10, 10, 60, 60)))
    # Readback also uses display coordinates.
    annot_rect = page.annotations[0].rect
    assert abs(annot_rect.x0 - target.x0) < 1
    assert abs(annot_rect.y1 - target.y1) < 1


def test_missing_highlight_appearance_rejects_malformed_opacity() -> None:
    """Do not invent an appearance when an authored opacity is malformed."""
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Annots [5 0 R] >>"),
            4: "<< /Length 0 >>\nstream\n\nendstream",
            5: (
                "<< /Type /Annot /Subtype /Highlight /Rect [10 10 30 20] "
                "/QuadPoints [10 20 30 20 10 10 30 10] /C [1 1 0] /CA /Invalid >>"
            ),
        }
    )
    page = pyropdf.open(pdf).pages[0]

    assert not _has_yellowish(_region_pixels(page, page.rect))


@pytest.mark.parametrize("max_interpretation_size", [None, 1024 * 1024])
def test_missing_text_markup_appearances_render_without_mutating_source(
    max_interpretation_size: int | None,
) -> None:
    """Render Underline, StrikeOut, and Squiggly dictionaries without `/AP`."""
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Annots [5 0 R 6 0 R 7 0 R] >>"),
            4: "<< /Length 0 >>\nstream\n\nendstream",
            5: (
                "<< /Type /Annot /Subtype /Underline /Rect [10 70 40 80] "
                "/QuadPoints [10 80 40 80 10 70 40 70] /C [1 0 0] >>"
            ),
            6: (
                "<< /Type /Annot /Subtype /StrikeOut /Rect [10 50 40 60] "
                "/QuadPoints [10 60 40 60 10 50 40 50] /C [0 1 0] >>"
            ),
            7: (
                "<< /Type /Annot /Subtype /Squiggly /Rect [10 30 40 40] "
                "/QuadPoints [10 40 40 40 10 30 40 30] /C [0 0 1] >>"
            ),
        }
    )
    document = pyropdf.open(
        pdf,
        limits=pyropdf.DocumentLimits(
            max_interpretation_size=max_interpretation_size,
        ),
    )
    page = document.pages[0]

    assert [annotation.type for annotation in page.annotations] == [
        pyropdf.AnnotationType.UNDERLINE,
        pyropdf.AnnotationType.STRIKE_OUT,
        pyropdf.AnnotationType.SQUIGGLY,
    ]
    assert _has_dominant_color(_region_pixels(page, pyropdf.Rect(8, 18, 43, 32)), 0)
    assert _has_dominant_color(_region_pixels(page, pyropdf.Rect(8, 38, 43, 52)), 1)
    assert _has_dominant_color(_region_pixels(page, pyropdf.Rect(8, 58, 43, 72)), 2)
    assert b"/AP" not in document.to_bytes()


def test_missing_underline_appearance_follows_slanted_quad() -> None:
    """Keep the lower QuadPoints edge instead of flattening the underline."""
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Annots [5 0 R] >>"),
            4: "<< /Length 0 >>\nstream\n\nendstream",
            5: (
                "<< /Type /Annot /Subtype /Underline /Rect [10 60 40 80] "
                "/QuadPoints [10 80 40 70 10 70 40 60] /C [1 0 0] >>"
            ),
        }
    )
    page = pyropdf.open(pdf).pages[0]

    assert _has_dominant_color(_region_pixels(page, pyropdf.Rect(8, 26, 16, 34)), 0)
    assert _has_dominant_color(_region_pixels(page, pyropdf.Rect(34, 35, 43, 43)), 0)


def test_missing_text_markup_quad_budget_is_atomic() -> None:
    """Do not synthesize a partial set when one annotation holds more quads than the engine draws."""
    excessive_quads = "10 20 30 20 10 10 30 10 " * 4097
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Annots [5 0 R 6 0 R] >>"),
            4: "<< /Length 0 >>\nstream\n\nendstream",
            5: (
                "<< /Type /Annot /Subtype /Highlight /Rect [10 10 30 20] "
                "/QuadPoints [10 20 30 20 10 10 30 10] /C [1 1 0] >>"
            ),
            6: (
                f"<< /Type /Annot /Subtype /Highlight /Rect [10 10 30 20] /QuadPoints [{excessive_quads}] /C [1 1 0] >>"
            ),
        }
    )
    page = pyropdf.open(pdf).pages[0]

    assert not _has_yellowish(_region_pixels(page, page.rect))


def test_missing_text_markup_segment_budget_is_atomic() -> None:
    """Do not amplify an extreme Squiggly width or synthesize its peer."""
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Annots [5 0 R 6 0 R] >>"),
            4: "<< /Length 0 >>\nstream\n\nendstream",
            5: (
                "<< /Type /Annot /Subtype /Highlight /Rect [10 10 30 20] "
                "/QuadPoints [10 20 30 20 10 10 30 10] /C [1 1 0] >>"
            ),
            6: (
                "<< /Type /Annot /Subtype /Squiggly /Rect [0 30 200000 40] "
                "/QuadPoints [0 40 200000 40 0 30 200000 30] /C [1 0 0] >>"
            ),
        }
    )
    page = pyropdf.open(pdf).pages[0]

    assert not _has_yellowish(_region_pixels(page, page.rect))


def _squiggles(count: int) -> pyropdf.Page:
    """Open a page with a highlight and `count` squiggly lines of 65,535 zigzag points each, all without appearances."""
    squiggles = {
        6 + index: (
            f"<< /Type /Annot /Subtype /Squiggly /Rect [0 {index} 131068 {index + 1}] "
            f"/QuadPoints [0 {index + 1} 131068 {index + 1} 0 {index} 131068 {index}] /C [1 0 0] >>"
        )
        for index in range(count)
    }
    references = " ".join(f"{number} 0 R" for number in range(5, 6 + count))
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Annots [{references}] >>",
            4: "<< /Length 0 >>\nstream\n\nendstream",
            5: (
                "<< /Type /Annot /Subtype /Highlight /Rect [10 10 30 20] "
                "/QuadPoints [10 20 30 20 10 10 30 10] /C [1 1 0] >>"
            ),
            **squiggles,
        }
    )
    return pyropdf.open(pdf).pages[0]


def test_missing_appearance_point_budget_is_atomic() -> None:
    """Do not synthesize a partial set above the aggregate point budget of a rendering snapshot."""
    page = _squiggles(5)
    assert not _has_yellowish(_region_pixels(page, page.rect))
    # Four of them fit in the budget, and their peer is drawn.
    page = _squiggles(4)
    assert _has_yellowish(_region_pixels(page, page.rect))


def _highlights(count: int) -> pyropdf.Page:
    """Open a page with ``count`` highlights without appearances."""
    highlights = {
        5 + index: (
            "<< /Type /Annot /Subtype /Highlight /Rect [10 10 30 20] "
            "/QuadPoints [10 20 30 20 10 10 30 10] /C [1 1 0] >>"
        )
        for index in range(count)
    }
    references = " ".join(f"{number} 0 R" for number in highlights)
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Annots [{references}] >>",
            4: "<< /Length 0 >>\nstream\n\nendstream",
            **highlights,
        }
    )
    return pyropdf.open(pdf).pages[0]


def test_missing_appearance_count_budget_is_atomic() -> None:
    """Draw none of the appearances that a render lacks when there are more than 4,096 of them."""
    assert not _has_yellowish(_region_pixels(_highlights(4097), pyropdf.Rect(0, 0, 100, 100)))
    assert _has_yellowish(_region_pixels(_highlights(4096), pyropdf.Rect(0, 0, 100, 100)))


def test_annotation_add_rejects_malformed_indirect_array_atomically() -> None:
    doc = _one_page("4 0 R", {4: "<< /NotAnArray true >>"})
    before = doc.to_bytes()
    with pytest.raises(pyropdf.PdfError):
        doc.pages[0].annotations.add(pyropdf.Highlight((20, 20, 30, 30)))
    assert doc.to_bytes() == before


def test_annots_empty_on_fresh_page() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    assert len(doc.pages[0].annotations) == 0


def test_annots_rejects_bad_input() -> None:
    page = pyropdf.Document().pages.new()
    with pytest.raises(ValueError, match="at least one rectangle"):
        page.annotations.add(pyropdf.Highlight([]))
    with pytest.raises(ValueError, match="opacity"):
        page.annotations.add(pyropdf.Highlight((10, 10, 50, 20), opacity=0))


def test_annotation_add_refuses_over_limit_page_atomically() -> None:
    doc = _one_page(
        "[" + "5 0 R " * 4096 + "]",
        {4: "<< >>", 5: "<< /Type /Annot /Subtype /Text /Rect [1 1 10 10] >>"},
    )
    before = doc.to_bytes()
    with pytest.raises(pyropdf.PdfError, match="4096-entry safety limit"):
        doc.pages[0].annotations.add(pyropdf.Highlight((10, 10, 20, 20)))
    assert doc.to_bytes() == before


def test_annotation_add_bounds_inputs_atomically() -> None:
    doc = pyropdf.open(build_pdf([""]))
    before = doc.to_bytes()
    with pytest.raises(pyropdf.LimitError, match=r"content.*1048576-byte") as content_limit:
        doc.pages[0].annotations.add(pyropdf.Highlight((10, 10, 20, 20), contents="x" * (1024 * 1024 + 1)))
    assert content_limit.value.code == "annotation_input_size"
    with pytest.raises(ValueError, match="4096 rectangles"):
        doc.pages[0].annotations.add(pyropdf.Highlight([(10, 10, 20, 20)] * 4097))
    assert doc.to_bytes() == before


def test_highlight_input_stops_before_unbounded_materialization() -> None:
    doc = pyropdf.open(build_pdf([""]))

    def excessive_rects() -> Iterator[tuple[int, int, int, int]]:
        yield from [(10, 10, 20, 20)] * 4097
        msg = "the bounded reader consumed beyond item 4,097"
        raise AssertionError(msg)

    with pytest.raises(ValueError, match="4096 rectangles"):
        doc.pages[0].annotations.add(pyropdf.Highlight(cast("Sequence[Sequence[float]]", excessive_rects())))

    def unreadable_rects() -> Iterator[tuple[int, int, int, int]]:
        msg = "oversized text must fail before rectangle iteration"
        raise AssertionError(msg)
        yield (10, 10, 20, 20)

    with pytest.raises(pyropdf.LimitError) as content_limit:
        pyropdf.Highlight(cast("Sequence[Sequence[float]]", unreadable_rects()), contents="x" * (1024 * 1024 + 1))
    assert content_limit.value.code == "annotation_input_size"


def test_annotation_add_at_metadata_boundary_remains_readable() -> None:
    # The subtype, the contents, and the creation and modification dates.
    content = "x" * (1024 * 1024 - len("Highlight") - 2 * len("D:20260101000000Z"))
    doc = pyropdf.open(build_pdf([""]))
    doc.pages[0].annotations.add(pyropdf.Highlight((10, 10, 20, 20), contents=content))
    assert doc.pages[0].annotations[0].contents == content
