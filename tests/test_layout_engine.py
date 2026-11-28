"""The pure-Rust RTen document layout engine: ``LayoutEngine``, ``LayoutRegion``, and ``Page.regions``."""

from __future__ import annotations

import json
import struct
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Barrier, Event, Lock
from typing import TYPE_CHECKING, Any, get_args

import pytest
from corpus import ASSETS
from test_content import build_raw_pdf

import pyropdf
from pyropdf._core import _LayoutEngine
from pyropdf._layout import _default_layout_engine

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _FbBuilder:
    """Minimal zero-dependency back-to-front FlatBuffers writer for synthetic ``.rten`` v1 test models."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def _pad_for(self, payload_len: int, alignment: int) -> None:
        rem = (len(self.buf) + payload_len) % alignment
        if rem:
            self.buf.extend(b"\x00" * (alignment - rem))

    def _push_rev(self, raw: bytes) -> None:
        self.buf.extend(raw[::-1])

    def write_u8(self, value: int) -> int:
        self.buf.append(value & 0xFF)
        return len(self.buf)

    def write_i32(self, value: int) -> int:
        self._pad_for(4, 4)
        self._push_rev(struct.pack("<i", value))
        return len(self.buf)

    def write_string(self, text: str) -> int:
        raw = text.encode("utf-8")
        self._pad_for(4 + len(raw) + 1, 4)
        self.buf.append(0)
        self._push_rev(raw)
        self._push_rev(struct.pack("<I", len(raw)))
        return len(self.buf)

    def write_vec_u32(self, items: Sequence[int]) -> int:
        self._pad_for(4 * (1 + len(items)), 4)
        for value in reversed(items):
            self._push_rev(struct.pack("<I", value))
        self._push_rev(struct.pack("<I", len(items)))
        return len(self.buf)

    def write_vec_i32(self, items: Sequence[int]) -> int:
        self._pad_for(4 * (1 + len(items)), 4)
        for value in reversed(items):
            self._push_rev(struct.pack("<i", value))
        self._push_rev(struct.pack("<I", len(items)))
        return len(self.buf)

    def write_vec_f32(self, items: Sequence[float]) -> int:
        self._pad_for(4 * (1 + len(items)), 4)
        for value in reversed(items):
            self._push_rev(struct.pack("<f", value))
        self._push_rev(struct.pack("<I", len(items)))
        return len(self.buf)

    def write_vec_offsets(self, targets: Sequence[int]) -> int:
        self._pad_for(4 * (1 + len(targets)), 4)
        for target in reversed(targets):
            slot_pos = len(self.buf) + 4
            self._push_rev(struct.pack("<I", slot_pos - target))
        self._push_rev(struct.pack("<I", len(targets)))
        return len(self.buf)

    def write_table(self, num_fields: int, fields: Sequence[tuple[int, str, int]]) -> int:
        table_bottom = len(self.buf)
        field_positions = [0] * num_fields
        for field_id, kind, value in reversed(fields):
            if kind == "u8":
                field_pos = self.write_u8(value)
            elif kind == "i32":
                field_pos = self.write_i32(value)
            elif kind == "off":
                self._pad_for(4, 4)
                slot_pos = len(self.buf) + 4
                self._push_rev(struct.pack("<I", slot_pos - value))
                field_pos = len(self.buf)
            else:
                msg = f"unknown field kind: {kind}"
                raise ValueError(msg)
            field_positions[field_id] = field_pos

        self._pad_for(4, 4)
        soffset_idx = len(self.buf)
        self.buf.extend(b"\x00\x00\x00\x00")
        table_pos = len(self.buf)
        body_size = table_pos - table_bottom

        vtable_size = 4 + 2 * num_fields
        self._pad_for(vtable_size, 2)
        for field_pos in reversed(field_positions):
            rel = 0 if field_pos == 0 else table_pos - field_pos
            self._push_rev(struct.pack("<H", rel))
        self._push_rev(struct.pack("<H", body_size))
        self._push_rev(struct.pack("<H", vtable_size))
        vtable_pos = len(self.buf)

        self.buf[soffset_idx : soffset_idx + 4] = struct.pack("<i", vtable_pos - table_pos)[::-1]
        return table_pos

    def finish(self, root_pos: int) -> bytes:
        self._pad_for(8, 8)
        self._push_rev(b"RTEN")
        slot_pos = len(self.buf) + 4
        self._push_rev(struct.pack("<I", slot_pos - root_pos))
        return bytes(self.buf[::-1])


def _build_synthetic_rten(
    rows: Sequence[Sequence[float]],
    *,
    cols: int | None = None,
    count: int | None = None,
) -> bytes:
    """Build a valid ``.rten`` v1 model returning ``rows`` and optional ``count``."""
    resolved_cols = cols if cols is not None else (len(rows[0]) if rows else 6)
    flat: list[float] = []
    for row in rows:
        assert len(row) == resolved_cols
        flat.extend(row)

    fb = _FbBuilder()
    node_offsets: list[int] = []
    for name in ("im_shape", "image", "scale_factor"):
        name_off = fb.write_string(name)
        value_node = fb.write_table(2, ())
        node_offsets.append(fb.write_table(3, ((0, "off", name_off), (1, "u8", 3), (2, "off", value_node))))

    det_name = fb.write_string("dets")
    det_shape = fb.write_vec_u32((len(rows), resolved_cols))
    det_vec = fb.write_vec_f32(flat)
    float_data = fb.write_table(1, ((0, "off", det_vec),))
    det_const = fb.write_table(5, ((0, "off", det_shape), (1, "u8", 1), (2, "off", float_data)))
    node_offsets.append(fb.write_table(3, ((0, "off", det_name), (1, "u8", 2), (2, "off", det_const))))

    outputs = [3]
    if count is not None:
        cnt_name = fb.write_string("bbox_num")
        cnt_shape = fb.write_vec_u32((1,))
        cnt_vec = fb.write_vec_i32((count,))
        int_data = fb.write_table(1, ((0, "off", cnt_vec),))
        cnt_const = fb.write_table(5, ((0, "off", cnt_shape), (1, "u8", 2), (2, "off", int_data)))
        node_offsets.append(fb.write_table(3, ((0, "off", cnt_name), (1, "u8", 2), (2, "off", cnt_const))))
        outputs.append(4)

    nodes_vec = fb.write_vec_offsets(node_offsets)
    inputs_vec = fb.write_vec_u32((0, 1, 2))
    outputs_vec = fb.write_vec_u32(outputs)
    graph = fb.write_table(4, ((0, "off", nodes_vec), (1, "off", inputs_vec), (2, "off", outputs_vec)))
    model = fb.write_table(3, ((0, "i32", 1), (1, "off", graph)))
    return fb.finish(model)


def _blank_page(size: tuple[int, int] = (200, 300)) -> pyropdf.Page:
    doc = pyropdf.Document()
    return doc.pages.new(size)


def _write_model(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_layout_public_types_and_error_hierarchy() -> None:
    assert issubclass(pyropdf.LayoutError, pyropdf.PdfError)
    assert pyropdf.LayoutError.__module__ == "pyropdf"
    assert pyropdf.LayoutEngine.__module__ == pyropdf.LayoutRegion.__module__ == "pyropdf"
    assert set(get_args(pyropdf.LayoutKind)) == {
        "title",
        "heading",
        "text",
        "table",
        "figure",
        "caption",
        "header",
        "footer",
        "footnote",
        "formula",
        "code",
        "toc",
        "reference",
        "page_number",
        "abstract",
        "aside",
    }

    region = pyropdf.LayoutRegion(
        kind="title",
        label="doc_title",
        bbox=pyropdf.Rect(10, 20, 100, 50),
        confidence=0.95,
    )
    assert region.reading_order is None
    with pytest.raises(FrozenInstanceError):
        region.label = "other"  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        pyropdf.LayoutRegion("title", "doc_title", pyropdf.Rect(0, 0, 1, 1), 0.9)  # ty: ignore[too-many-positional-arguments, missing-argument]


def test_layout_engine_raises_clear_error_when_models_package_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _default_layout_engine.cache_clear()
    monkeypatch.setitem(sys.modules, "pyropdf_layout_models", None)
    try:
        with pytest.raises(pyropdf.LayoutError, match=r"pyropdf\[layout\]"):
            pyropdf.LayoutEngine()
        with pytest.raises(pyropdf.LayoutError, match=r"pyropdf\[layout\]"):
            _blank_page().regions()
    finally:
        _default_layout_engine.cache_clear()


@pytest.mark.parametrize(
    ("threads", "error"),
    [
        (0, pyropdf.LayoutError),
        (17, pyropdf.LayoutError),
        (-1, pyropdf.LayoutError),
        (True, TypeError),
        (1.5, TypeError),
    ],
)
def test_layout_engine_validates_threads(threads: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="threads"):
        pyropdf.LayoutEngine(threads=threads)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("max_concurrent", "error"),
    [
        (0, pyropdf.LayoutError),
        (17, pyropdf.LayoutError),
        (-1, pyropdf.LayoutError),
        (True, TypeError),
        (1.5, TypeError),
    ],
)
def test_layout_engine_validates_max_concurrent(max_concurrent: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="max_concurrent"):
        pyropdf.LayoutEngine(max_concurrent=max_concurrent)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("max_model_size", "error"),
    [(0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError)],
)
def test_layout_engine_validates_max_model_size(max_model_size: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="max_model_size"):
        pyropdf.LayoutEngine(max_model_size=max_model_size)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("labels", "error"),
    [
        ("text", TypeError),
        (b"text", TypeError),
        (123, TypeError),
        ([123], TypeError),
        ([], pyropdf.LayoutError),
        ([""], pyropdf.LayoutError),
    ],
)
def test_layout_engine_validates_labels(tmp_path: Path, labels: object, error: type[Exception]) -> None:
    model = _write_model(tmp_path, "empty.rten", _build_synthetic_rten([], cols=6))
    with pytest.raises(error, match="labels"):
        pyropdf.LayoutEngine(model, labels=labels)  # ty: ignore[invalid-argument-type]


def test_layout_engine_handles_missing_corrupt_and_oversized_models(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"missing\.rten"):
        pyropdf.LayoutEngine(tmp_path / "missing.rten")

    corrupt = _write_model(tmp_path, "corrupt.rten", b"not a valid rten file")
    with pytest.raises(pyropdf.LimitError) as caught:
        pyropdf.LayoutEngine(corrupt, max_model_size=8)
    assert caught.value.code == "layout_model_size"

    with pytest.raises(pyropdf.LayoutError, match="failed to load layout model"):
        pyropdf.LayoutEngine(corrupt, max_model_size=1024)
    with pytest.raises(pyropdf.LayoutError, match="failed to load layout model"):
        pyropdf.LayoutEngine(corrupt, max_model_size=None)

    with pytest.raises(ValueError, match="max_model_size"):
        _LayoutEngine(str(corrupt), 1, 0)
    with pytest.raises(pyropdf.LayoutError, match="threads"):
        _LayoutEngine(str(corrupt), 17, None)
    with pytest.raises(pyropdf.LayoutError, match="input_size"):
        _LayoutEngine(str(corrupt), 1, None, 0)


@pytest.mark.parametrize("alpha", [False, True])
def test_layout_engine_detects_6_and_7_col_models_end_to_end(tmp_path: Path, *, alpha: bool) -> None:
    # 7-column output (PP-DocLayoutV3 style) with valid_n=2 capping row 2 and box clamping.
    model7 = _write_model(
        tmp_path,
        "v3.rten",
        _build_synthetic_rten(
            [
                [10.0, 0.94, -10.0, 12.0, 400.0, 90.0, 1.0],
                [0.0, 0.40, 10.0, 100.0, 120.0, 130.0, 2.0],
                [8.0, 0.99, 10.0, 140.0, 180.0, 200.0, 3.0],
            ],
            count=2,
        ),
    )
    engine = pyropdf.LayoutEngine(model7, threads=2, max_concurrent=1)
    assert engine.threads == 2
    assert engine.max_concurrent == 1
    assert engine.max_model_size == 64 * 1024 * 1024

    pixmap = _blank_page((100, 120)).pixmap(dpi=72, alpha=alpha)
    regions = engine.detect(pixmap, min_confidence=0.5)
    assert len(regions) == 1
    assert regions[0].kind == "title"
    assert regions[0].label == "doc_title"
    assert regions[0].bbox == pyropdf.Rect(0.0, 12.0, 100.0, 90.0)
    assert regions[0].confidence == pytest.approx(0.94, abs=1e-5)
    assert regions[0].reading_order == 1

    # Lowering min_confidence surfaces row 1 (paragraph_title -> heading) while count=2 still hides row 2.
    regions_low = engine.detect(pixmap, min_confidence=0.35)
    assert [(r.kind, r.label, r.reading_order) for r in regions_low] == [
        ("title", "doc_title", 1),
        ("heading", "paragraph_title", 2),
    ]

    # Grayscale pixmap works without error.
    gray_pixmap = _blank_page((100, 120)).pixmap(dpi=72, colorspace="gray")
    assert len(engine.detect(gray_pixmap, min_confidence=0.5)) == 1

    # 6-column model sets reading_order=None.
    model6 = _write_model(
        tmp_path,
        "plus_l.rten",
        _build_synthetic_rten([[8.0, 0.88, 5.0, 15.0, 95.0, 105.0]]),
    )
    engine6 = pyropdf.LayoutEngine(model6, threads=1)
    regions6 = engine6.detect(pixmap)
    assert len(regions6) == 1
    assert regions6[0].kind == "table"
    assert regions6[0].label == "table"
    assert regions6[0].reading_order is None

    with pytest.raises(TypeError, match=r"pixmap must be a pyropdf\.Pixmap"):
        engine.detect(_blank_page())  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="min_confidence"):
        engine.detect(pixmap, min_confidence=True)
    with pytest.raises(pyropdf.LayoutError, match="min_confidence"):
        engine.detect(pixmap, min_confidence=1.5)
    with pytest.raises(pyropdf.LayoutError, match="min_confidence"):
        engine.detect(pixmap, min_confidence=float("nan"))


def test_layout_engine_normalizes_all_pp_doclayout_tables_and_custom_labels(tmp_path: Path) -> None:
    # Mix of 20/23/25-class PP-DocLayout names + an unknown custom label.
    custom_labels = (
        "doc_title",
        "paragraph_title",
        "text",
        "vertical_text",
        "table",
        "image",
        "chart",
        "seal",
        "header_image",
        "footer_image",
        "figure_title",
        "table_title",
        "chart_title",
        "header",
        "footer",
        "footnote",
        "vision_footnote",
        "formula",
        "display_formula",
        "inline_formula",
        "formula_number",
        "algorithm",
        "content",
        "reference",
        "reference_content",
        "number",
        "abstract",
        "aside_text",
        "custom_stamp",
    )
    rows = [[float(idx), 0.9, 1.0, 1.0, 10.0, 10.0] for idx in range(len(custom_labels))]
    rows.append([99.0, 0.9, 1.0, 1.0, 10.0, 10.0])  # Out-of-range class id.
    model = _write_model(tmp_path, "labels.rten", _build_synthetic_rten(rows))
    engine = pyropdf.LayoutEngine(model, labels=custom_labels, threads=1)

    regions = engine.detect(_blank_page((50, 50)).pixmap(dpi=72))
    expected_kinds = [
        "title",
        "heading",
        "text",
        "text",
        "table",
        "figure",
        "figure",
        "figure",
        "figure",
        "figure",
        "caption",
        "caption",
        "caption",
        "header",
        "footer",
        "footnote",
        "footnote",
        "formula",
        "formula",
        "formula",
        "formula",
        "code",
        "toc",
        "reference",
        "reference",
        "page_number",
        "abstract",
        "aside",
        "text",
        "text",
    ]
    assert [r.kind for r in regions] == expected_kinds
    assert regions[-2].label == "custom_stamp"
    assert regions[-1].label == "class_99"


def test_layout_engine_discovers_optional_package_and_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = _write_model(tmp_path, "discovered.rten", _build_synthetic_rten([[0.0, 0.91, 10.0, 20.0, 50.0, 40.0]]))
    metadata = tmp_path / "model.json"
    metadata.write_text(
        json.dumps({"labels": ["doc_title", "text"], "input_size": 32}),
        encoding="utf-8",
    )

    fake_mod = types.ModuleType("pyropdf_layout_models")
    fake_paths = types.SimpleNamespace(model=model, metadata=metadata, version="0.1.0")
    fake_mod.model_paths = lambda: fake_paths  # ty: ignore[missing-attribute]

    _default_layout_engine.cache_clear()
    monkeypatch.setitem(sys.modules, "pyropdf_layout_models", fake_mod)
    try:
        page = _blank_page((100, 100))
        regions = page.regions(dpi=72)
        assert len(regions) == 1
        assert regions[0].kind == "title"
        assert regions[0].label == "doc_title"

        # Explicit labels override metadata labels.
        override = pyropdf.LayoutEngine(labels=["algorithm"], threads=1)
        assert override.detect(page.pixmap(dpi=72))[0].kind == "code"

        # Bad metadata JSON raises LayoutError.
        metadata.write_text("{not-json", encoding="utf-8")
        with pytest.raises(pyropdf.LayoutError, match="metadata"):
            pyropdf.LayoutEngine()

        # Incompatible model_paths() result raises LayoutError.
        fake_mod.model_paths = object  # ty: ignore[missing-attribute]
        with pytest.raises(pyropdf.LayoutError, match="compatible model_paths"):
            pyropdf.LayoutEngine()
    finally:
        _default_layout_engine.cache_clear()


def test_layout_engine_enforces_detection_cap_cancellation_and_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    many_rows = [[2.0, 0.9, 1.0, 1.0, 10.0, 10.0] for _ in range(1_025)]
    many_model = _write_model(tmp_path, "many.rten", _build_synthetic_rten(many_rows))
    many_engine = pyropdf.LayoutEngine(many_model, threads=1)
    with pytest.raises(pyropdf.LimitError) as caught:
        many_engine.detect(_blank_page((50, 50)).pixmap(dpi=72))
    assert caught.value.code == "layout_detection_count"

    ok_model = _write_model(tmp_path, "ok.rten", _build_synthetic_rten([[2.0, 0.9, 1.0, 1.0, 10.0, 10.0]]))
    ok_engine = pyropdf.LayoutEngine(ok_model, threads=1)

    # Cancelled before render.
    token = pyropdf.CancellationToken()
    doc = pyropdf.Document(limits=pyropdf.DocumentLimits(cancellation=token))
    page = doc.pages.new((100, 100))
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as cancelled_before:
        page.regions(engine=ok_engine, dpi=72)
    assert cancelled_before.value.code == "cancelled"

    # Cancelled after render, before RTen inference.
    token2 = pyropdf.CancellationToken()
    doc2 = pyropdf.Document(limits=pyropdf.DocumentLimits(cancellation=token2))
    page2 = doc2.pages.new((100, 100))
    real_pixmap = pyropdf.Page.pixmap

    def cancel_after_render(self: pyropdf.Page, **kwargs: Any) -> pyropdf.Pixmap:  # noqa: ANN401
        pm = real_pixmap(self, **kwargs)
        token2.cancel()
        return pm

    with monkeypatch.context() as mp:
        mp.setattr(pyropdf.Page, "pixmap", cancel_after_render)
        with pytest.raises(pyropdf.LimitError) as cancelled_after:
            page2.regions(engine=ok_engine, dpi=72)
        assert cancelled_after.value.code == "cancelled"

    # Expired deadline observed before inference.
    doc3 = pyropdf.Document(limits=pyropdf.DocumentLimits(max_duration=0.005))
    page3 = doc3.pages.new((100, 100))

    def slow_render(self: pyropdf.Page, **kwargs: Any) -> pyropdf.Pixmap:  # noqa: ANN401
        pm = real_pixmap(self, **kwargs)
        time.sleep(0.02)
        return pm

    with monkeypatch.context() as mp:
        mp.setattr(pyropdf.Page, "pixmap", slow_render)
        with pytest.raises(pyropdf.LimitError) as expired:
            page3.regions(engine=ok_engine, dpi=72)
        assert expired.value.code == "deadline"


def test_page_regions_maps_display_coordinates_with_clip_rotation_crop_and_user_unit(tmp_path: Path) -> None:
    # At 144 DPI, pixel_to_page = 72 / 144 = 0.5 pt/px.
    # Model outputs pixel box [20, 40, 120, 100] -> [10, 20, 60, 50] pt without clip.
    model = _write_model(tmp_path, "box.rten", _build_synthetic_rten([[10.0, 0.95, 20.0, 40.0, 120.0, 100.0]]))
    engine = pyropdf.LayoutEngine(model, threads=1)

    page = _blank_page((300, 400))
    (full,) = page.regions(engine=engine, dpi=144)
    assert full.kind == "title"
    assert tuple(full.bbox) == pytest.approx((10.0, 20.0, 60.0, 50.0), abs=1e-4)

    # With clip=(50, 60, 250, 260) at 144 DPI, offset = (50.0, 60.0).
    (clipped,) = page.regions(engine=engine, dpi=144, clip=(50, 60, 250, 260))
    assert tuple(clipped.bbox) == pytest.approx((60.0, 80.0, 110.0, 110.0), abs=1e-4)

    # Rotated pages: page.rect swaps width/height on 90/270 and stays top-left origin.
    for rot in (90, 180, 270):
        page.rotation = rot
        (rot_region,) = page.regions(engine=engine, dpi=144)
        assert tuple(rot_region.bbox) == pytest.approx((10.0, 20.0, 60.0, 50.0), abs=1e-4)
        assert rot_region.bbox.x1 <= page.rect.width
        assert rot_region.bbox.y1 <= page.rect.height

    # Cropped page (/CropBox offset from MediaBox origin) & /UserUnit 2.0.
    cropped_pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 400 500] >>",
            3: "<< /Type /Page /Parent 2 0 R /CropBox [40 50 340 450] /UserUnit 2 >>",
        }
    )
    cropped_page = pyropdf.open(cropped_pdf).pages[0]
    assert cropped_page.rect == pyropdf.Rect(0, 0, 600, 800)
    (cropped_region,) = cropped_page.regions(engine=engine, dpi=144, clip=(100, 120, 400, 500))
    assert tuple(cropped_region.bbox) == pytest.approx((110.0, 140.0, 160.0, 170.0), abs=1e-4)

    # Validation errors on Page.regions.
    page.rotation = 0
    for bad_dpi in (0, -1, float("inf"), float("nan"), "bad"):
        with pytest.raises(pyropdf.LayoutError, match="dpi"):
            page.regions(engine=engine, dpi=bad_dpi)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="dpi"):
        page.regions(engine=engine, dpi=True)
    with pytest.raises(TypeError, match="min_confidence"):
        page.regions(engine=engine, min_confidence=False)
    with pytest.raises(pyropdf.LayoutError, match="min_confidence"):
        page.regions(engine=engine, min_confidence=-0.1)
    with pytest.raises(TypeError, match="engine must be a LayoutEngine"):
        page.regions(engine=object())  # ty: ignore[invalid-argument-type]
    with pytest.raises(pyropdf.PdfError, match="clip does not intersect"):
        page.regions(engine=engine, dpi=72, clip=(500, 500, 600, 600))


def test_layout_engine_concurrency_and_thread_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = _write_model(tmp_path, "concurrent.rten", _build_synthetic_rten([[0.0, 0.9, 5.0, 5.0, 45.0, 25.0]]))

    # 1. Real multi-threaded inference across distinct documents sharing one engine.
    shared = pyropdf.LayoutEngine(model, threads=1, max_concurrent=2)
    pages = [_blank_page((100, 100)) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda p: p.regions(engine=shared, dpi=72), pages))
    assert all(len(r) == 1 and r[0].kind == "heading" for r in results)

    # 2. Default max_concurrent=1 strictly serializes concurrent calls.
    class SerialProbe:
        def __init__(self) -> None:
            self.active = 0
            self.calls = 0
            self.max_active = 0
            self.lock = Lock()
            self.first_entered = Event()
            self.second_entered = Event()
            self.release_first = Event()

        def detect_pixmap(self, *_args: object, **_kwargs: object) -> list[object]:
            with self.lock:
                self.calls += 1
                call = self.calls
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            if call == 1:
                self.first_entered.set()
                assert self.release_first.wait(timeout=5)
            else:
                self.second_entered.set()
            with self.lock:
                self.active -= 1
            return []

    serial_engine = pyropdf.LayoutEngine(model, threads=1)
    probe = SerialProbe()
    monkeypatch.setattr(serial_engine, "_engine", probe)
    second_started = Event()

    def second_call() -> list[pyropdf.LayoutRegion]:
        second_started.set()
        return _blank_page().regions(engine=serial_engine, dpi=72)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_blank_page().regions, engine=serial_engine, dpi=72)
        assert probe.first_entered.wait(timeout=5)
        second = pool.submit(second_call)
        assert second_started.wait(timeout=5)
        assert not probe.second_entered.wait(timeout=0.2)
        probe.release_first.set()
        assert first.result(timeout=5) == []
        assert second.result(timeout=5) == []
    assert probe.second_entered.is_set()
    assert probe.max_active == 1
    assert serial_engine.max_concurrent == 1

    # 3. max_concurrent=2 admits two calls concurrently.
    class ParallelProbe:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = Lock()
            self.barrier = Barrier(2)

        def detect_pixmap(self, *_args: object, **_kwargs: object) -> list[object]:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            self.barrier.wait(timeout=5)
            with self.lock:
                self.active -= 1
            return []

    parallel_engine = pyropdf.LayoutEngine(model, threads=1, max_concurrent=2)
    par_probe = ParallelProbe()
    monkeypatch.setattr(parallel_engine, "_engine", par_probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(lambda p: p.regions(engine=parallel_engine, dpi=72), [_blank_page(), _blank_page()]))
    assert out == [[], []]
    assert par_probe.max_active == 2
    assert parallel_engine.max_concurrent == 2


def test_layout_engine_real_package_integration() -> None:
    pytest.importorskip("pyropdf_layout_models")
    page = pyropdf.open(ASSETS / "mhlw-doc.pdf").pages[0]
    regions = page.regions()
    assert regions
    assert any(region.kind in {"title", "heading"} for region in regions)
