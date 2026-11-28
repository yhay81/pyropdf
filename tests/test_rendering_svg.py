"""page.svg: the current version of a page as SVG through hayro-svg.

Ported from the ``svg`` assertions of pylopdf's tests/test_rendering_api.py
at 036be111. pyropdf has no ``page.annotations.add`` yet, so the annotated
page is built with a Highlight whose appearance paints its rectangle red.
That the bridge converts every corpus page as hayro-svg converts the
original bytes is crates/pyropdf-engine/src/render/svg.rs.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest
from test_content import build_raw_pdf
from test_content_model import page_with, stream_object

import pyropdf
from pyropdf import _core

ROOT = Path(__file__).resolve().parent.parent
REAL_WORLD = Path(__file__).parent / "assets" / "real_world"
TEXT_ASSETS = Path(__file__).parent / "assets" / "text"
CORPUS = sorted(REAL_WORLD.glob("*.pdf"))
SVG = "{http://www.w3.org/2000/svg}"


def _xml(value: str) -> ET.Element:
    return ET.fromstring(value)  # noqa: S314 - generated SVG of local fixtures.


def _layer_text(svg: str) -> str:
    return "".join("".join(element.itertext()) for element in _xml(svg).iter(f"{SVG}text"))


def _page() -> pyropdf.Page:
    """Open a 200 by 100 page that shows one line of text."""
    return page_with("BT /F1 24 Tf 10 40 Td (Rendered) Tj ET")


def _annotated_page() -> pyropdf.Page:
    """Open a blank 200 by 100 page with a Highlight that paints its rectangle red."""
    return page_with(
        "",
        annots="6 0 R",
        objects={
            6: "<< /Type /Annot /Subtype /Highlight /Rect [20 20 180 80] /AP << /N 7 0 R >> >>",
            7: stream_object("1 0 0 rg 0 0 160 60 re f", "/Type /XObject /Subtype /Form /BBox [0 0 160 60]"),
        },
    )


def test_svg_draws_text_as_paths() -> None:
    svg = _page().svg()

    assert svg.startswith("<svg")
    assert 'viewBox="0 0 200 100"' in svg
    assert "<text" not in svg
    assert "<path" in svg


def test_annotations_false_leaves_annotation_appearances_out() -> None:
    page = _annotated_page()

    assert page.svg(annotations=False) != page.svg()
    assert page.svg(annotations=False) == page_with("").svg()


@pytest.mark.parametrize("value", [1, None, "yes"])
def test_annotations_must_be_a_bool(value: object) -> None:
    with pytest.raises(TypeError, match="annotations must be a bool"):
        _page().svg(annotations=value)  # ty: ignore[invalid-argument-type]


def test_max_size_bounds_the_utf8_output() -> None:
    page = _page()
    size = len(page.svg().encode())

    assert page.svg(max_size=size) == page.svg(max_size=None)
    with pytest.raises(pyropdf.LimitError, match=f"exceeds the {size - 1}-byte UTF-8 output limit") as raised:
        page.svg(max_size=size - 1)
    assert raised.value.code == "svg_output_size"


@pytest.mark.parametrize(("value", "error"), [(0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError)])
def test_max_size_is_a_positive_integer_or_none(value: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="max_size"):
        _page().svg(max_size=value)  # ty: ignore[invalid-argument-type]


def test_svg_converts_the_current_version() -> None:
    page = _page()
    before = page.svg()

    page.rotation = 90

    after = page.svg()
    assert after != before
    assert 'viewBox="0 0 100 200"' in after


def test_old_names_are_removed() -> None:
    for name in ("render_svg", "get_svg_image"):
        assert not hasattr(pyropdf.Page, name)
    assert not hasattr(pyropdf.Document, "render_page_svg")
    with pytest.raises(TypeError):
        _page().svg(None)  # ty: ignore[too-many-positional-arguments]


def test_a_deleted_page_or_a_closed_document_refuses_to_convert() -> None:
    doc = _page().document
    doc.pages.new()
    page = doc.pages[0]
    del doc.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.svg()
    page = doc.pages[0]
    doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        page.svg()


@pytest.mark.parametrize("path", CORPUS, ids=[path.name for path in CORPUS])
def test_every_corpus_page_converts_within_the_default_limit(path: Path) -> None:
    doc = pyropdf.open(path.read_bytes())

    for page in doc.pages:
        svg = page.svg()
        assert svg.startswith("<svg")
        assert svg.rstrip().endswith("</svg>")


def _to_unicode_cmap(mappings: dict[int, str], *, bytes_per_code: int = 1) -> str:
    width = bytes_per_code * 2
    hi = "F" * width
    lo = "0" * width
    entries = "\n".join(
        f"<{code:0{width}X}> <{text.encode('utf-16-be').hex().upper()}>" for code, text in mappings.items()
    )
    return (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        f"/CMapName /Test def 1 begincodespacerange <{lo}> <{hi}> endcodespacerange "
        f"{len(mappings)} beginbfchar\n{entries}\nendbfchar endcmap "
        "CMapName currentdict /CMap defineresource pop end end"
    )


def _page_with_to_unicode(stream: str, mappings: dict[int, str]) -> pyropdf.Page:
    cmap = _to_unicode_cmap(mappings, bytes_per_code=1)
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
                "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
            ),
            4: stream_object(stream),
            5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
            6: stream_object(cmap),
        }
    )
    return pyropdf.open(pdf).pages[0]


def _vertical_cjk_page() -> pyropdf.Page:
    cmap = _to_unicode_cmap({1: "縦", 2: "書"}, bytes_per_code=2)
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 120 120] "
                "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
            ),
            4: stream_object("BT /F1 12 Tf 40 90 Td <00010002> Tj ET"),
            5: (
                "<< /Type /Font /Subtype /Type0 /BaseFont /TestVertical "
                "/Encoding /Identity-V /DescendantFonts [6 0 R] /ToUnicode 8 0 R >>"
            ),
            6: (
                "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /TestVertical "
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >> "
                "/FontDescriptor 7 0 R /DW 1000 /DW2 [880 -1000] >>"
            ),
            7: (
                "<< /Type /FontDescriptor /FontName /TestVertical /Flags 4 "
                "/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 880 /Descent -120 "
                "/CapHeight 750 /StemV 80 >>"
            ),
            8: stream_object(cmap),
        }
    )
    return pyropdf.open(pdf).pages[0]


def test_text_layer_default_is_byte_identical_to_false() -> None:
    page = _page()
    default = page.svg()
    assert page.svg(text_layer=False) == default
    assert list(_xml(default).iter(f"{SVG}text")) == []

    # Direct 5-argument native call keeps its default path-only behavior.
    annotations = True
    native = _core.render_page_svg(
        page.document._core(),  # noqa: SLF001
        page._number,  # noqa: SLF001
        page._generation,  # noqa: SLF001
        annotations,
        None,
    )
    assert native == default


@pytest.mark.parametrize("value", [1, None, "yes"])
def test_text_layer_must_be_a_bool(value: object) -> None:
    with pytest.raises(TypeError, match="text_layer must be a bool"):
        _page().svg(text_layer=value)  # ty: ignore[invalid-argument-type]


def test_text_layer_positions_keeps_painted_paths_and_emits_all_modes() -> None:
    with pyropdf.open(ROOT / "evidence/svg-text/browser/positions.pdf") as doc:
        page = doc.pages[0]
        default = page.svg()
        assert page.svg(text_layer=False) == default
        paths = _xml(default)
        assert list(paths.iter(f"{SVG}text")) == []

        layered = _xml(page.svg(text_layer=True))
        layer_groups = [el for el in layered.iter(f"{SVG}g") if el.get("id") == "text-layer"]
        assert len(layer_groups) == 1
        assert layer_groups[0].get("fill") == "none"
        assert layer_groups[0].get("fill-opacity") == "0"
        assert layer_groups[0].get("stroke") == "none"

        texts = {element.text: element for element in layered.iter(f"{SVG}text")}
        assert set(texts) == {"Searchable", "Rotated", "Slanted", "Invisible"}
        assert len({element.get("transform") for element in texts.values()}) == 4
        for tag in ("path", "use"):
            before = [element.attrib for element in paths.iter(f"{SVG}{tag}")]
            assert before
            assert [element.attrib for element in layered.iter(f"{SVG}{tag}")] == before


@pytest.mark.parametrize(
    ("name", "expected"),
    [("hebrew-niqqud.pdf", "שָׁלוֹם עולם"), ("nonembedded-cjk.pdf", "計画地")],
)
def test_text_layer_embedded_and_substituted_fonts_keep_unicode(name: str, expected: str) -> None:
    with pyropdf.open(TEXT_ASSETS / name) as doc:
        text = _layer_text(doc.pages[0].svg(text_layer=True))
        # Glyph painting order and logical reading order may differ for Hebrew.
        assert Counter(text) == Counter(expected)
        if name == "nonembedded-cjk.pdf":
            assert text == expected


def test_text_layer_nonembedded_latin_preserves_stretched_runs_and_fill_stroke() -> None:
    with pyropdf.open(TEXT_ASSETS / "nonembedded-latin.pdf") as doc:
        first_svg = doc.pages[0].svg(text_layer=True)
        first_texts = [el.text for el in _xml(first_svg).iter(f"{SVG}text")]
        assert first_texts == ["Hello big world"]

        for page in doc.pages:
            svg = page.svg(text_layer=True)
            parsed = _xml(svg)
            extracted = "".join("".join(el.itertext()) for el in parsed.iter(f"{SVG}text"))
            assert extracted.strip()
            # FillStroke runs must not duplicate text in the layer.
            assert "".join(extracted.split()) == "".join(page.text().split())


def test_text_layer_vertical_cjk_sets_distinct_y_coordinates() -> None:
    page = _vertical_cjk_page()
    svg = page.svg(text_layer=True)
    root = _xml(svg)
    texts = list(root.iter(f"{SVG}text"))
    assert len(texts) == 1
    assert texts[0].text == "縦書"
    assert page.text().strip() == "縦書"
    xs = texts[0].get("x", "").split()
    ys = texts[0].get("y", "").split()
    assert len(xs) == 2
    assert len(ys) == 2
    assert float(ys[1]) > float(ys[0])


def test_text_layer_actual_text_replacement() -> None:
    page = page_with(
        "BT /F1 12 Tf 10 80 Td "
        "/Span << /ActualText (Hello) >> BDC (X) Tj EMC "
        "0 -15 Td /Span << /ActualText (fi) >> BDC (A) Tj (B) Tj EMC "
        "0 -15 Td /Span << /ActualText <FEFF8A08753B5730> >> BDC (Z) Tj EMC "
        "0 -15 Td (a) Tj /Span << /ActualText () >> BDC (skip) Tj EMC (b) Tj "
        "0 -15 Td /Span << /ActualText (Outer) >> BDC /Span << /ActualText (Inner) >> BDC (Q) Tj EMC EMC "
        "0 -15 Td /Span << /ActualText (R&D <1> \\000\\013\\027) >> BDC (W) Tj EMC "
        "0 -15 Td /Span << /ActualText <FEFF00410000000B001F0042> >> BDC (U) Tj EMC "
        "ET"
    )
    svg = page.svg(text_layer=True)
    root = _xml(svg)
    texts = [el.text for el in root.iter(f"{SVG}text")]
    assert texts == ["Hello", "fi", "計画地", "a", "b", "Outer", "R&D <1> ", "AB"]
    fi_node = next(el for el in root.iter(f"{SVG}text") if el.text == "fi")
    fi_xs = [float(v) for v in fi_node.get("x", "").split()]
    assert len(fi_xs) == 2
    assert fi_xs[1] > fi_xs[0]


def test_text_layer_ligatures_xml_escaping_and_control_char_filtering() -> None:
    page = _page_with_to_unicode(
        "BT /F1 12 Tf 10 50 Td (AB) Tj ET",
        {
            ord("A"): "fi",
            ord("B"): "A\x00\x01\x08\x0b\x0c\x0e\x1f&<B>",
        },
    )
    svg = page.svg(text_layer=True)
    root = _xml(svg)
    texts = list(root.iter(f"{SVG}text"))
    assert len(texts) == 1
    assert texts[0].text == "fiA&<B>"
    xs = [float(v) for v in texts[0].get("x", "").split()]
    assert len(xs) == len("fiA&<B>")
    assert xs[1] > xs[0]


def test_text_layer_render_modes_fill_stroke_invisible_and_clip() -> None:
    page = page_with("BT /F1 12 Tf 10 80 Td 2 Tr (Bold) Tj 0 -20 Td 3 Tr (Hidden) Tj 0 -20 Td 7 Tr (Clipped) Tj ET")
    svg = page.svg(text_layer=True)
    texts = [el.text for el in _xml(svg).iter(f"{SVG}text")]
    assert texts == ["Bold", "Hidden", "Clipped"]


def test_text_layer_annotations_user_unit_rotation_and_undo() -> None:
    page = page_with(
        "BT /F1 12 Tf 10 40 Td (Page & <body>) Tj ET",
        annots="6 0 R",
        objects={
            6: "<< /Type /Annot /Subtype /Stamp /Rect [10 10 110 40] /AP << /N 7 0 R >> >>",
            7: stream_object(
                "BT /F1 12 Tf 2 10 Td (Stamp) Tj ET",
                "/Type /XObject /Subtype /Form /BBox [0 0 100 30] /Resources << /Font << /F1 5 0 R >> >>",
            ),
        },
    )
    page.rotation = 90
    page.obj["UserUnit"] = 2

    without_annots = page.svg(annotations=False, text_layer=True)
    with_annots = page.svg(text_layer=True)
    assert _layer_text(without_annots) == "Page & <body>"
    assert _layer_text(with_annots) == "Page & <body>Stamp"

    # Viewport reflects /Rotate 90 (100x200 native) scaled by /UserUnit 2 (200x400 points).
    root = _xml(with_annots)
    assert root.get("viewBox") == "0 0 100 200"
    assert root.get("width") == "200"
    assert root.get("height") == "400"
    # Text coordinates live in native viewBox space (0..100 x 0..200), matching hayro-svg paths.
    for el in root.iter(f"{SVG}text"):
        transform = el.get("transform", "")
        assert transform.startswith("matrix(")
        coeffs = [float(v) for v in transform.removeprefix("matrix(").removesuffix(")").split()]
        assert 0.0 <= coeffs[4] <= 100.0
        assert 0.0 <= coeffs[5] <= 200.0

    del page.obj["Annots"]
    assert _layer_text(page.svg(text_layer=True)) == "Page & <body>"
    page.document.history.undo()
    assert _layer_text(page.svg(text_layer=True)) == "Page & <body>Stamp"


def test_text_layer_enforces_svg_output_size_and_memory_size_limits() -> None:
    with pyropdf.open(TEXT_ASSETS / "nonembedded-cjk.pdf") as doc:
        page = doc.pages[0]
        layered = page.svg(text_layer=True, max_size=None)
        assert _layer_text(layered) == "計画地"
        size = len(layered.encode())
        assert size > len(layered)
        assert len(page.svg().encode()) < size
        assert page.svg(text_layer=True, max_size=size) == layered
        with pytest.raises(pyropdf.LimitError) as exc:
            page.svg(text_layer=True, max_size=size - 1)
        assert exc.value.code == "svg_output_size"

    # Tight memory budget lets open() and path-only svg() succeed, while
    # text_layer=True exceeds max_memory_size cleanly.
    stream = "BT /F1 8 Tf 10 90 Td " + " ".join("(Hello world 1234567890) Tj 0 -1 Td" for _ in range(300)) + " ET"
    dense_pdf = page_with(stream).document.to_bytes()
    with pyropdf.open(dense_pdf, limits=pyropdf.DocumentLimits(max_memory_size=40_000)) as tight:
        assert tight.pages[0].svg().startswith("<svg")
        with pytest.raises(pyropdf.LimitError) as mem_exc:
            tight.pages[0].svg(text_layer=True)
        assert mem_exc.value.code == "memory_size"


def test_text_layer_real_world_ocr_and_japanese_corpus() -> None:
    with pyropdf.open(REAL_WORLD / "patent-us223898.pdf") as doc:
        page = doc.pages[0]
        assert list(_xml(page.svg()).iter(f"{SVG}text")) == []
        layered_text = _layer_text(page.svg(text_layer=True))
        assert layered_text.strip()
        first_word = page.words()[0].text
        assert first_word in layered_text

    with pyropdf.open(REAL_WORLD / "mhlw-doc.pdf") as doc:
        page = doc.pages[0]
        layered_text = _layer_text(page.svg(text_layer=True))
        assert any("\u3040" <= ch <= "\u9fff" for ch in layered_text)

    for path in CORPUS:
        with pyropdf.open(path) as doc:
            svg = doc.pages[0].svg(text_layer=True)
            root = _xml(svg)
            assert root.tag == f"{SVG}svg"
