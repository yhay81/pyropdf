"""Opt-in ML layout semantic enrichment for ``markdown()``, ``to_json()``, and ``to_chunks()`` (#1248)."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest
from corpus import ASSETS
from test_content import build_raw_pdf
from test_layout_engine import _build_synthetic_rten

import pyropdf
from pyropdf._layout import _default_layout_engine

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path


def _text_pdf(
    *pages_content: str,
    size: tuple[int, int] = (400, 500),
    extra_objects: dict[int, str] | None = None,
) -> bytes:
    """Build a minimal untagged PDF with Helvetica text streams on each page."""
    width, height = size
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    kids: list[str] = []
    next_id = 4
    for content in pages_content:
        page_id = next_id
        stream_id = next_id + 1
        next_id += 2
        kids.append(f"{page_id} 0 R")
        objects[page_id] = f"<< /Type /Page /Parent 2 0 R /Contents {stream_id} 0 R >>"
        objects[stream_id] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    objects[2] = (
        f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)}"
        f" /MediaBox [0 0 {width} {height}] /Resources << /Font << /F1 3 0 R >> >> >>"
    )
    if extra_objects:
        objects.update(extra_objects)
    return build_raw_pdf(objects)


def _line_op(text: str, x: float, y: float, size: float = 10.5, *, page_height: float = 500.0) -> str:
    """Emit one PDF text operator placing ``text`` at top-left display baseline ``(x, y)``."""
    return f"BT /F1 {size} Tf {x} {page_height - y} Td ({text}) Tj ET"


def _tagged_pdf() -> bytes:
    """Build a trustworthy single-page tagged PDF (Marked=true, full text coverage)."""
    content = (
        "BT /F1 12 Tf 40 460 Td /H1 << /MCID 0 >> BDC (Tagged Heading) Tj EMC ET\n"
        "BT /F1 12 Tf 40 420 Td /P << /MCID 1 >> BDC (Tagged body paragraph.) Tj EMC ET"
    )
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Version /2.0 /Pages 2 0 R /StructTreeRoot 7 0 R /MarkInfo << /Marked true >> >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 400 500]"
                " /Resources << /Font << /F1 6 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
            5: f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            6: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            7: "<< /Type /StructTreeRoot /K 8 0 R >>",
            8: "<< /Type /StructElem /S /Document /P 7 0 R /K [9 0 R 10 0 R] >>",
            9: "<< /Type /StructElem /S /H1 /P 8 0 R /Pg 3 0 R /K 0 >>",
            10: "<< /Type /StructElem /S /P /P 8 0 R /Pg 3 0 R /K 1 >>",
        }
    )


def _mixed_tagged_and_untagged_pdf() -> bytes:
    """Build a 2-page PDF: page 0 is tagged with full coverage; page 1 is untagged."""
    p0_content = "BT /F1 12 Tf 40 460 Td /P << /MCID 0 >> BDC (Tagged page zero.) Tj EMC ET"
    p1_content = "\n".join(
        [
            _line_op("Untagged Section Heading", 40, 60, 10.5),
            _line_op("Untagged body text on page one.", 40, 100, 10.5),
        ]
    )
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Version /2.0 /Pages 2 0 R /StructTreeRoot 8 0 R /MarkInfo << /Marked true >> >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 400 500]"
                " /Resources << /Font << /F1 7 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
            4: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R >>",
            5: f"<< /Length {len(p0_content)} >>\nstream\n{p0_content}\nendstream",
            6: f"<< /Length {len(p1_content)} >>\nstream\n{p1_content}\nendstream",
            7: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            8: "<< /Type /StructTreeRoot /K 9 0 R >>",
            9: "<< /Type /StructElem /S /Document /P 8 0 R /K [10 0 R] >>",
            10: "<< /Type /StructElem /S /P /P 9 0 R /Pg 3 0 R /K 0 >>",
        }
    )


def _stub_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    regions_fn: Callable[[pyropdf.Page], list[pyropdf.LayoutRegion]],
) -> tuple[pyropdf.LayoutEngine, list[int]]:
    """Return a real ``LayoutEngine`` whose ``_detect_page`` calls ``regions_fn`` and records page indices."""
    model = tmp_path / "stub.rten"
    model.write_bytes(_build_synthetic_rten([], cols=6))
    engine = pyropdf.LayoutEngine(model, threads=1)
    called_pages: list[int] = []

    def detect_page(
        page: pyropdf.Page,
        *,
        dpi: float,
        min_confidence: float,
        clip: tuple[float, float, float, float] | None,
    ) -> list[pyropdf.LayoutRegion]:
        _ = dpi, min_confidence, clip
        called_pages.append(page.index)
        return regions_fn(page)

    monkeypatch.setattr(engine, "_detect_page", detect_page)
    return engine, called_pages


def _rten_engine_from_display_boxes(
    tmp_path: Path,
    detections: Sequence[tuple[int, float, float, float, float, float]],
    *,
    dpi: float = 150.0,
) -> pyropdf.LayoutEngine:
    """Create a real RTen ``LayoutEngine`` emitting ``detections`` (class_id, conf, x0, y0, x1, y1 in pt)."""
    scale = dpi / 72.0
    rows = [
        [float(cls_id), conf, x0 * scale, y0 * scale, x1 * scale, y1 * scale]
        for cls_id, conf, x0, y0, x1, y1 in detections
    ]
    model = tmp_path / "real_rten.rten"
    model.write_bytes(_build_synthetic_rten(rows))
    return pyropdf.LayoutEngine(model, threads=1)


def test_default_exports_never_import_layout_models_or_run_ml(monkeypatch: pytest.MonkeyPatch) -> None:
    _default_layout_engine.cache_clear()
    monkeypatch.setitem(sys.modules, "pyropdf_layout_models", None)

    def fail_regions(*_args: object, **_kwargs: object) -> list[pyropdf.LayoutRegion]:
        pytest.fail("Page.regions must not be invoked when use_layout=False and layout_engine=None")

    monkeypatch.setattr(pyropdf.Page, "regions", fail_regions)
    pdf = _text_pdf("\n".join([_line_op("Plain Title", 40, 50, 10.5), _line_op("Body line.", 40, 90, 10.5)]))

    with pyropdf.open(pdf) as doc:
        page_md = doc.pages[0].markdown()
        doc_md = doc.markdown()
        raw_json = doc.to_json()
        chunks = doc.to_chunks()

    assert "Plain Title" in page_md
    assert "#" not in page_md
    assert doc_md == page_md
    parsed = json.loads(raw_json)
    assert all(el["kind"] == "paragraph" for el in parsed["pages"][0]["elements"])
    assert len(chunks) == 1


def test_layout_option_validation_across_export_methods() -> None:
    pdf = _text_pdf(_line_op("Hello", 40, 50, 12.0))
    with pyropdf.open(pdf) as doc:
        page = doc.pages[0]
        for bad_bool in ("yes", 1, None):
            with pytest.raises(TypeError, match="use_layout"):
                page.markdown(use_layout=bad_bool)  # ty: ignore[invalid-argument-type]
            with pytest.raises(TypeError, match="use_layout"):
                doc.markdown(use_layout=bad_bool)  # ty: ignore[invalid-argument-type]
            with pytest.raises(TypeError, match="use_layout"):
                doc.to_json(use_layout=bad_bool)  # ty: ignore[invalid-argument-type]
            with pytest.raises(TypeError, match="use_layout"):
                doc.to_chunks(use_layout=bad_bool)  # ty: ignore[invalid-argument-type]
        for bad_engine in ("model.rten", object(), 123):
            with pytest.raises(TypeError, match="layout_engine"):
                page.markdown(layout_engine=bad_engine)  # ty: ignore[invalid-argument-type]
            with pytest.raises(TypeError, match="layout_engine"):
                doc.markdown(layout_engine=bad_engine)  # ty: ignore[invalid-argument-type]
            with pytest.raises(TypeError, match="layout_engine"):
                doc.to_json(layout_engine=bad_engine)  # ty: ignore[invalid-argument-type]
            with pytest.raises(TypeError, match="layout_engine"):
                doc.to_chunks(layout_engine=bad_engine)  # ty: ignore[invalid-argument-type]


def test_use_layout_without_models_raises_layout_error_on_untagged_page(monkeypatch: pytest.MonkeyPatch) -> None:
    _default_layout_engine.cache_clear()
    monkeypatch.setitem(sys.modules, "pyropdf_layout_models", None)
    pdf = _text_pdf(_line_op("Untagged body", 40, 60, 10.5))
    try:
        with pyropdf.open(pdf) as doc:
            with pytest.raises(pyropdf.LayoutError, match=r"pyropdf\[layout\]"):
                doc.pages[0].markdown(use_layout=True)
            with pytest.raises(pyropdf.LayoutError, match=r"pyropdf\[layout\]"):
                doc.to_json(use_layout=True)
            with pytest.raises(pyropdf.LayoutError, match=r"pyropdf\[layout\]"):
                doc.to_chunks(use_layout=True)
    finally:
        _default_layout_engine.cache_clear()


def test_tagged_pdf_skips_ml_inference_under_use_structure_auto_and_always(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _default_layout_engine.cache_clear()
    monkeypatch.setitem(sys.modules, "pyropdf_layout_models", None)
    engine, called = _stub_engine(tmp_path, monkeypatch, lambda _p: [])

    with pyropdf.open(_tagged_pdf()) as doc:
        for mode in ("auto", "always"):
            # Even with pyropdf_layout_models missing, use_layout=True does NOT invoke or load ML on tagged pages.
            md_auto = doc.markdown(use_structure=mode, use_layout=True)
            assert "# Tagged Heading" in md_auto
            assert "Tagged body paragraph." in md_auto

            # Explicit layout_engine is also never called on tagged pages.
            assert "# Tagged Heading" in doc.pages[0].markdown(use_structure=mode, layout_engine=engine)
            assert "# Tagged Heading" in doc.markdown(use_structure=mode, layout_engine=engine)
            payload = json.loads(doc.to_json(use_structure=mode, layout_engine=engine))
            assert payload["pages"][0]["elements"][0]["kind"] == "heading"
            chunks = doc.to_chunks(use_structure=mode, layout_engine=engine)
            assert chunks[0].section == ("Tagged Heading",)
            assert called == []

        # Under use_structure="never", ML layout IS invoked on the geometric fallback.
        doc.markdown(use_structure="never", layout_engine=engine)
        assert called == [0]

    # Mixed PDF: page 0 (tagged) skips ML; page 1 (untagged) invokes ML exactly once.
    called.clear()
    mixed_engine, mixed_calls = _stub_engine(
        tmp_path,
        monkeypatch,
        lambda _p: [
            pyropdf.LayoutRegion(
                kind="heading",
                label="paragraph_title",
                bbox=pyropdf.Rect(30, 40, 300, 75),
                confidence=0.92,
            )
        ],
    )
    with pyropdf.open(_mixed_tagged_and_untagged_pdf()) as doc:
        md = doc.markdown(use_structure="auto", layout_engine=mixed_engine)
    assert mixed_calls == [1]
    assert "Tagged page zero." in md
    assert "## Untagged Section Heading" in md
    assert "Untagged body text on page one." in md


def test_uniform_font_heading_promotion_across_markdown_json_and_chunks(tmp_path: Path) -> None:
    # All lines share the exact same 10.5pt font size (like Japanese government docs such as mhlw-doc).
    content = "\n".join(
        [
            _line_op("Annual Policy Report", 40, 50, 10.5),
            _line_op("FY 2026 Edition", 40, 66, 10.5),
            _line_op("1. Overview", 40, 110, 10.5),
            _line_op("First section body sentence.", 40, 140, 10.5),
            _line_op("2. Findings", 40, 200, 10.5),
            _line_op("Second section body sentence.", 40, 230, 10.5),
        ]
    )
    pdf = _text_pdf(content)
    # Use a real RTen synthetic model! Class 10 = doc_title -> title (#), class 0 = paragraph_title -> heading (##).
    engine = _rten_engine_from_display_boxes(
        tmp_path,
        [
            (10, 0.96, 30.0, 35.0, 320.0, 78.0),  # doc_title spanning both wrapped title lines
            (0, 0.91, 30.0, 95.0, 220.0, 122.0),  # paragraph_title over "1. Overview"
            (0, 0.89, 30.0, 185.0, 220.0, 212.0),  # paragraph_title over "2. Findings"
        ],
    )

    with pyropdf.open(pdf) as doc:
        # Baseline without ML: uniform font -> no headings, numbered lines become list items.
        plain_md = doc.markdown()
        assert "#" not in plain_md

        # Passing layout_engine alone implicitly enables layout even with default use_layout=False.
        page_md = doc.pages[0].markdown(layout_engine=engine)
        doc_md = doc.markdown(layout_engine=engine)
        assert page_md == doc_md
        assert "# Annual Policy Report FY 2026 Edition" in doc_md
        assert "## 1. Overview" in doc_md
        assert "## 2. Findings" in doc_md
        assert "First section body sentence." in doc_md
        assert "Second section body sentence." in doc_md

        # Document.to_json() emits heading elements with level 1 & 2.
        data = json.loads(doc.to_json(layout_engine=engine))
        elements = data["pages"][0]["elements"]
        headings = [(el["level"], el["text"]) for el in elements if el["kind"] == "heading"]
        assert headings == [
            (1, "Annual Policy Report FY 2026 Edition"),
            (2, "1. Overview"),
            (2, "2. Findings"),
        ]

        # Document.to_chunks() splits sections on ML-promoted headings.
        chunks = doc.to_chunks(layout_engine=engine)
        assert [chunk.section for chunk in chunks] == [
            ("Annual Policy Report FY 2026 Edition",),
            ("Annual Policy Report FY 2026 Edition", "1. Overview"),
            ("Annual Policy Report FY 2026 Edition", "2. Findings"),
        ]
        assert "First section body sentence." in chunks[1].text
        assert "Second section body sentence." in chunks[2].text


def test_single_page_header_footer_and_page_number_suppression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body_sentence = "Important body text establishing the dominant body size."
    content = "\n".join(
        [
            _line_op("CONFIDENTIAL HEADER", 40, 30, 9.0),
            _line_op("Main Section", 40, 90, 10.5),
            _line_op(body_sentence, 40, 130, 10.5),
            _line_op("Footer copyright notice", 40, 450, 9.0),
            _line_op("1", 200, 480, 9.0),
        ]
    )
    pdf = _text_pdf(content)
    engine, calls = _stub_engine(
        tmp_path,
        monkeypatch,
        lambda _p: [
            pyropdf.LayoutRegion(kind="header", label="header", bbox=pyropdf.Rect(20, 15, 360, 42), confidence=0.93),
            pyropdf.LayoutRegion(
                kind="heading", label="paragraph_title", bbox=pyropdf.Rect(20, 75, 250, 102), confidence=0.90
            ),
            pyropdf.LayoutRegion(kind="footer", label="footer", bbox=pyropdf.Rect(20, 435, 360, 462), confidence=0.91),
            pyropdf.LayoutRegion(
                kind="page_number", label="number", bbox=pyropdf.Rect(180, 465, 230, 492), confidence=0.95
            ),
        ],
    )

    with pyropdf.open(pdf) as doc:
        # Without ignore_headers_footers, ML chrome is kept.
        kept_md = doc.pages[0].markdown(layout_engine=engine)
        assert "CONFIDENTIAL HEADER" in kept_md
        assert "Footer copyright notice" in kept_md
        assert "## Main Section" in kept_md

        # Single-page ignore_headers_footers=True suppresses header, footer, and page_number across all exports!
        calls.clear()
        clean_page_md = doc.pages[0].markdown(ignore_headers_footers=True, layout_engine=engine)
        assert calls == [0]  # Cached across passes: only 1 inference call per page.
        assert "CONFIDENTIAL HEADER" not in clean_page_md
        assert "Footer copyright notice" not in clean_page_md
        assert "\n1" not in clean_page_md
        assert "## Main Section" in clean_page_md
        assert body_sentence in clean_page_md

        calls.clear()
        clean_doc_md = doc.markdown(ignore_headers_footers=True, layout_engine=engine)
        assert calls == [0]
        assert clean_doc_md == clean_page_md

        calls.clear()
        clean_json = json.loads(doc.to_json(ignore_headers_footers=True, layout_engine=engine))
        assert calls == [0]
        texts = [el["text"] for el in clean_json["pages"][0]["elements"]]
        assert texts == ["Main Section", body_sentence]

        calls.clear()
        clean_chunks = doc.to_chunks(ignore_headers_footers=True, layout_engine=engine)
        assert calls == [0]
        assert len(clean_chunks) == 1
        assert clean_chunks[0].section == ("Main Section",)
        assert "CONFIDENTIAL HEADER" not in clean_chunks[0].text
        assert "Footer copyright notice" not in clean_chunks[0].text


def test_preserves_native_geometric_reading_order_over_scrambled_ml_regions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two-column layout: left column (x=30..170), wide gutter, right column (x=230..370).
    content = "\n".join(
        [
            _line_op("Left Heading", 30, 60, 10.5),
            _line_op("Left paragraph one.", 30, 90, 10.5),
            _line_op("Left paragraph two.", 30, 120, 10.5),
            _line_op("Right Heading", 230, 60, 10.5),
            _line_op("Right paragraph one.", 230, 90, 10.5),
            _line_op("Right paragraph two.", 230, 120, 10.5),
        ]
    )
    pdf = _text_pdf(content)
    # Intentionally return right-column regions FIRST and with inverted reading_order.
    engine, _ = _stub_engine(
        tmp_path,
        monkeypatch,
        lambda _p: [
            pyropdf.LayoutRegion(
                kind="heading",
                label="paragraph_title",
                bbox=pyropdf.Rect(220, 45, 380, 72),
                confidence=0.95,
                reading_order=0,
            ),
            pyropdf.LayoutRegion(
                kind="heading",
                label="paragraph_title",
                bbox=pyropdf.Rect(20, 45, 180, 72),
                confidence=0.95,
                reading_order=5,
            ),
        ],
    )

    with pyropdf.open(pdf) as doc:
        md = doc.markdown(layout_engine=engine)
        assert "## Left Heading" in md
        assert "## Right Heading" in md
        assert md.index("Left Heading") < md.index("Left paragraph one.") < md.index("Left paragraph two.")
        assert md.index("Left paragraph two.") < md.index("Right Heading") < md.index("Right paragraph one.")

        chunks = doc.to_chunks(layout_engine=engine)
        assert [c.section for c in chunks] == [("Left Heading",), ("Right Heading",)]


def test_preserves_geometric_tables_margins_images_ruby_and_max_graphics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. ML "table" box over plain non-table form lines does NOT collapse them into a broken Markdown table.
    form_content = "\n".join(
        [
            _line_op("Form Title", 40, 50, 10.5),
            _line_op("Name: Jane Doe", 40, 90, 10.5),
            _line_op("Address: 123 Main St", 40, 115, 10.5),
            _line_op("Margin note outside crop", 40, 480, 10.5),
        ]
    )
    engine, _ = _stub_engine(
        tmp_path,
        monkeypatch,
        lambda _p: [
            pyropdf.LayoutRegion(kind="title", label="doc_title", bbox=pyropdf.Rect(30, 35, 250, 62), confidence=0.94),
            pyropdf.LayoutRegion(kind="table", label="table", bbox=pyropdf.Rect(30, 75, 350, 140), confidence=0.92),
        ],
    )
    with pyropdf.open(_text_pdf(form_content)) as doc:
        md = doc.markdown(margins=(0, 0, 0, 40), layout_engine=engine)
        assert "# Form Title" in md
        assert "Name: Jane Doe" in md
        assert "Address: 123 Main St" in md
        assert "| ---" not in md
        assert "Margin note outside crop" not in md

    # 2. Real bordered geometric table is preserved alongside ML heading promotion + images + max_graphics.
    with pyropdf.Document() as builder:
        page = builder.pages.new((400, 400))
        with page.draw() as canvas:
            canvas.text("Table Report", (40, 45), size=11)
            # Draw a 2x2 bordered table at (40, 80, 240, 140).
            border = pyropdf.Stroke(paint=pyropdf.Color(0, 0, 0), width=1)
            for y in (80, 110, 140):
                canvas.line((40, y), (240, y), stroke=border)
            for x in (40, 140, 240):
                canvas.line((x, 80), (x, 140), stroke=border)
            canvas.text("Col A", (50, 100), size=11)
            canvas.text("Col B", (150, 100), size=11)
            canvas.text("Val 1", (50, 130), size=11)
            canvas.text("Val 2", (150, 130), size=11)
            swatch = pyropdf.Document().pages.new((8, 8)).pixmap(dpi=72).to_bytes("png")
            canvas.image((40, 180, 100, 220), swatch)
        table_pdf = builder.to_bytes()

    table_engine, _ = _stub_engine(
        tmp_path,
        monkeypatch,
        lambda _p: [
            pyropdf.LayoutRegion(
                kind="heading", label="paragraph_title", bbox=pyropdf.Rect(30, 25, 220, 60), confidence=0.95
            )
        ],
    )
    exported: list[str] = []
    with pyropdf.open(table_pdf) as doc:
        md = doc.markdown(
            layout_engine=table_engine,
            images=lambda img: (exported.append(img.filename), img.filename)[1],
        )
        assert "## Table Report" in md
        assert "| Col A | Col B |" in md
        assert "| Val 1 | Val 2 |" in md
        assert "![](page-0-image-0.png)" in md
        assert exported == ["page-0-image-0.png"]

        with pytest.raises(pyropdf.LimitError) as limit_err:
            doc.markdown(layout_engine=table_engine, max_graphics=1)
        assert limit_err.value.code == "content_element_count"

    # 3. Ruby option composes cleanly with ML layout enrichment on Japanese ruby documents.
    ruby_engine, _ = _stub_engine(
        tmp_path,
        monkeypatch,
        lambda _p: [
            pyropdf.LayoutRegion(
                kind="heading", label="paragraph_title", bbox=pyropdf.Rect(0, 0, 500, 90), confidence=0.90
            )
        ],
    )
    with pyropdf.open(ASSETS / "mhlw-doc.pdf") as doc:
        md_omit = doc.pages[0].markdown(ruby="omit", layout_engine=ruby_engine)
        md_keep = doc.pages[0].markdown(ruby="keep", layout_engine=ruby_engine)
        assert md_omit
        assert md_keep


def test_layout_enrichment_real_package_integration() -> None:
    pytest.importorskip("pyropdf_layout_models")
    with pyropdf.open(ASSETS / "mhlw-doc.pdf") as doc:
        # mhlw-doc.pdf has a trustworthy structure tree, so use_structure="auto" skips ML.
        plain_auto = doc.markdown(ignore_headers_footers=True)
        enriched_auto = doc.markdown(use_layout=True, ignore_headers_footers=True)
        assert enriched_auto == plain_auto

        # Disabling structure-tree reading exercises the real PP-DocLayout_plus-L INT8 model.
        plain_geom = doc.markdown(use_structure="never", ignore_headers_footers=True)
        enriched_geom = doc.markdown(use_layout=True, use_structure="never", ignore_headers_footers=True)
        assert enriched_geom != plain_geom
        assert any(line.startswith("#") for line in enriched_geom.splitlines())
        assert "| ---" not in enriched_geom

    # usrguide.pdf falls back to geometric layout under use_structure="auto" and enriches headings.
    with pyropdf.open(ASSETS / "usrguide.pdf") as doc:
        plain_p0 = doc.pages[0].markdown(ignore_headers_footers=True)
        enriched_p0 = doc.pages[0].markdown(use_layout=True, ignore_headers_footers=True)
        assert enriched_p0 != plain_p0
        assert any(line.startswith("#") for line in enriched_p0.splitlines())
