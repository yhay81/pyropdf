# ruff: noqa: INP001 - standalone reproducible corpus evaluation script.
"""Evaluate PP-DocLayout models on RTen 0.28.0 against pyropdf heuristic and pymupdf-layout.

Supports two modes:
- `--capture-pyropdf`: render 150 DPI PNGs and capture `pyropdf` heuristic layout/text/markdown/table
  metrics for every `"mode": "native"` page in `evidence/pymupdf-comparison/quality/*.json`.
- Default scoring mode: read captured RTen detection JSONs (produced by
  `cargo run --release --locked --manifest-path evidence/layout-model/Cargo.toml`), `pyropdf`
  heuristic outputs, and `pymupdf-layout` oracle outputs, score reading order and table/heading
  detection across all 12 native labeled pages + 2 OCR scan fixtures, and write
  `results/benchmark.{json,md}` and `results/comparison.{json,md}`.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024

# Ground-truth table taxonomy across the 12 native labeled pages:
# - 3 true-table pages (`table` key present in quality/*.json)
# - 7 non-table pages (prose, legislation, TOC/code, multi-column patent)
# - 2 gridded IRS tax forms (`f1040-p1`, `f1040-p2`) excluded from both buckets and shown per page.
TRUE_TABLE_PAGES = frozenset(
    {
        "mhlw-deposit-report-p1",
        "nics-background-checks-2015-11-p1",
        "senate-expenditures-p1",
    }
)
NON_TABLE_PAGES = frozenset(
    {
        "bill-hr815-p1",
        "mhlw-doc-p1",
        "mhlw-doc-p2",
        "patent-us223898-p2",
        "patent-us223898-p3",
        "usrguide-p1",
        "usrguide-p2",
    }
)
JP_PAGES = frozenset({"mhlw-doc-p1", "mhlw-doc-p2"})

LABELS_23: tuple[str, ...] = (
    "paragraph_title",
    "image",
    "text",
    "number",
    "abstract",
    "content",
    "figure_title",
    "formula",
    "table",
    "table_title",
    "reference",
    "doc_title",
    "footnote",
    "header",
    "algorithm",
    "footer",
    "seal",
    "chart_title",
    "chart",
    "formula_number",
    "header_image",
    "footer_image",
    "aside_text",
)
LABELS_20: tuple[str, ...] = (
    "paragraph_title",
    "image",
    "text",
    "number",
    "abstract",
    "content",
    "figure_title",
    "formula",
    "table",
    "reference",
    "doc_title",
    "footnote",
    "header",
    "algorithm",
    "footer",
    "seal",
    "chart",
    "formula_number",
    "aside_text",
    "reference_content",
)
LABELS_25: tuple[str, ...] = (
    "abstract",
    "algorithm",
    "aside_text",
    "chart",
    "content",
    "display_formula",
    "doc_title",
    "figure_title",
    "footer",
    "footer_image",
    "footnote",
    "formula_number",
    "header",
    "header_image",
    "image",
    "inline_formula",
    "number",
    "paragraph_title",
    "reference",
    "reference_content",
    "seal",
    "table",
    "text",
    "vertical_text",
    "vision_footnote",
)

# Recorded macOS `/usr/bin/time -l` maximum resident set size (bytes) across the 12-page run.
PEAK_RSS_BYTES: dict[tuple[str, int], int] = {
    ("doclayout_s_fp32", 1): 57_442_304,
    ("doclayout_s_fp32", 4): 60_276_736,
    ("doclayout_plus_l_fp32", 1): 569_098_240,
    ("doclayout_plus_l_fp32", 4): 577_257_472,
    ("doclayout_plus_l_int8", 1): 430_800_896,
    ("doclayout_plus_l_int8", 4): 432_177_152,
    ("doclayout_v3_fp32", 1): 1_018_953_728,
    ("doclayout_v3_fp32", 4): 1_024_802_816,
    ("doclayout_v3_int8_conv", 1): 981_172_224,
    ("doclayout_v3_int8_conv", 4): 986_284_032,
}

# Identical-tensor ORT vs RTen 0.28.0 numerical verification on `mhlw-doc-p1` (score >= 0.5).
IDENTICAL_TENSOR_FIDELITY: dict[str, dict[str, Any]] = {
    "doclayout_s_fp32": {"ort": 17, "rten": 17, "matched": 17, "max_score_diff": 0.00005, "max_box_diff_px": 0.005},
    "doclayout_plus_l_fp32": {
        "ort": 20,
        "rten": 20,
        "matched": 20,
        "max_score_diff": 0.00005,
        "max_box_diff_px": 0.005,
    },
    "doclayout_plus_l_int8": {
        "ort": 21,
        "rten": 21,
        "matched": 21,
        "max_score_diff": 0.01489,
        "max_box_diff_px": 0.726,
    },
    "doclayout_v3_fp32": {"ort": 20, "rten": 20, "matched": 20, "max_score_diff": 0.00007, "max_box_diff_px": 0.005},
    "doclayout_v3_int8_conv": {
        "ort": 18,
        "rten": 19,
        "matched": 17,
        "max_score_diff": 0.08053,
        "max_box_diff_px": 5.263,
    },
}

# Exact Hugging Face model provenance captured at evaluation time.
MODEL_PROVENANCE: list[dict[str, Any]] = [
    {
        "repo": "PaddlePaddle/PP-DocLayout-S",
        "sha": "8ac289e66575bb9bba6e15c53719d8b15cc9b3b2",
        "license": "apache-2.0",
        "last_modified": "2025-07-22",
    },
    {
        "repo": "stefanj0/PP-DocLayout-S-ONNX",
        "sha": "7e851b9e5ad66613d7318a3213ab3336ebe0a514",
        "license": "apache-2.0",
        "last_modified": "2026-05-26",
    },
    {
        "repo": "PaddlePaddle/PP-DocLayout-M",
        "sha": "7dbfcce3154a55776dc71ca026a4a2a8388dad8d",
        "license": "apache-2.0",
        "last_modified": "2025-07-22",
    },
    {
        "repo": "PaddlePaddle/PP-DocLayout-L",
        "sha": "ca760c3336d922ee63cdbff85003181222e3911c",
        "license": "apache-2.0",
        "last_modified": "2025-07-22",
    },
    {
        "repo": "PaddlePaddle/PP-DocLayout_plus-L",
        "sha": "aa52b8528c84f9b1a34ac3a88fe0e576edb9d11d",
        "license": "apache-2.0",
        "last_modified": "2025-07-22",
    },
    {
        "repo": "PaddlePaddle/PP-DocLayout_plus-L_onnx",
        "sha": "feb74619326f634e0e883218598096a3733ad9f7",
        "license": "apache-2.0",
        "last_modified": "2026-06-10",
    },
    {
        "repo": "PaddlePaddle/PP-DocLayoutV2_onnx",
        "sha": "7d44592493df02e28110d99de8ca4b1fbc7309bd",
        "license": "apache-2.0",
        "last_modified": "2026-06-10",
    },
    {
        "repo": "PaddlePaddle/PP-DocLayoutV3_onnx",
        "sha": "46bbdf188bb0a772c08aed74882ce7e51a8f1ea6",
        "license": "apache-2.0",
        "last_modified": "2026-06-10",
    },
]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Configuration for one evaluated RTen PP-DocLayout candidate."""

    key: str
    label: str
    rten_rel: str
    unique_ops: int
    threshold: float
    has_order: bool
    labels: tuple[str, ...]


MODEL_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        key="doclayout_s_fp32",
        label="PP-DocLayout-S (FP32, 480x480)",
        rten_rel="models/PP-DocLayout-S-ONNX/model.rten",
        unique_ops=28,
        threshold=0.4,
        has_order=False,
        labels=LABELS_23,
    ),
    ModelSpec(
        key="doclayout_plus_l_fp32",
        label="PP-DocLayout_plus-L (FP32, 800x800)",
        rten_rel="models/PP-DocLayout_plus-L_onnx/model.rten",
        unique_ops=39,
        threshold=0.5,
        has_order=False,
        labels=LABELS_20,
    ),
    ModelSpec(
        key="doclayout_plus_l_int8",
        label="PP-DocLayout_plus-L (INT8 MatMul+Conv, 800x800)",
        rten_rel="models/PP-DocLayout_plus-L_onnx/model_int8.rten",
        unique_ops=41,
        threshold=0.5,
        has_order=False,
        labels=LABELS_20,
    ),
    ModelSpec(
        key="doclayout_v3_fp32",
        label="PP-DocLayoutV3 (FP32, 800x800)",
        rten_rel="models/PP-DocLayoutV3_onnx/model.rten",
        unique_ops=42,
        threshold=0.5,
        has_order=True,
        labels=LABELS_25,
    ),
    ModelSpec(
        key="doclayout_v3_int8_conv",
        label="PP-DocLayoutV3 (INT8 Conv-only, 800x800)",
        rten_rel="models/PP-DocLayoutV3_onnx/model_int8_conv.rten",
        unique_ops=43,
        threshold=0.5,
        has_order=True,
        labels=LABELS_25,
    ),
)

_MIN_TWO_COL_BLOCKS = 4
_HALF_WIDTH_FRAC = 0.52
_BBOX_PAD_PT = 4.0
_SCAN_SCORE_THRESHOLD = 0.5


def capture_pyropdf_heuristic(work_dir: Path) -> None:
    """Render 150 DPI page PNGs and capture pyropdf heuristic metrics for all native labeled pages."""
    import pyropdf  # noqa: PLC0415 - only imported in capture mode.

    pages_dir = work_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, Any] = {}
    for lp in sorted((ROOT / "evidence/pymupdf-comparison/quality").glob("*.json")):
        lab = json.loads(lp.read_text())
        if lab.get("mode", "native") != "native":
            continue
        stem = lp.stem
        pdf_path = ROOT / "tests/assets/real_world" / lab["document"]
        with pyropdf.open(pdf_path) as doc:
            page = doc.pages[lab["page"] - 1]
            rect = [page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1]
            t0 = time.perf_counter()
            pm = page.pixmap(dpi=150)
            render_ms = (time.perf_counter() - t0) * 1e3
            pm.save(pages_dir / f"{stem}.png")

            t0 = time.perf_counter()
            lay = page.layout()
            layout_ms = (time.perf_counter() - t0) * 1e3
            t0 = time.perf_counter()
            text = page.text()
            text_ms = (time.perf_counter() - t0) * 1e3
            t0 = time.perf_counter()
            md = page.markdown()
            md_ms = (time.perf_counter() - t0) * 1e3
            t0 = time.perf_counter()
            tables = page.tables()
            tables_ms = (time.perf_counter() - t0) * 1e3

            blocks = [
                {
                    "bbox": [blk.bbox.x0, blk.bbox.y0, blk.bbox.x1, blk.bbox.y1],
                    "text": "\n".join("".join(s.text for s in ln.spans) for ln in blk.lines),
                }
                for blk in lay.blocks
            ]
            captured[stem] = {
                "document": lab["document"],
                "page": lab["page"],
                "page_rect": rect,
                "img_w": pm.width,
                "img_h": pm.height,
                "render_150dpi_ms": round(render_ms, 2),
                "layout_ms": round(layout_ms, 2),
                "text_ms": round(text_ms, 2),
                "markdown_ms": round(md_ms, 2),
                "tables_ms": round(tables_ms, 2),
                "num_blocks": len(blocks),
                "num_tables": len(tables),
                "table_bboxes": [[t.bbox.x0, t.bbox.y0, t.bbox.x1, t.bbox.y1] for t in tables],
                "text": text,
                "markdown": md,
                "blocks": blocks,
            }
    (work_dir / "pyropdf_heuristic.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.write(f"Captured {len(captured)} native pages to {work_dir / 'pyropdf_heuristic.json'}\n")


def _norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())


def score_anchors(anchors: list[str], text: str) -> dict[str, int]:
    """Count found ground-truth anchors and correctly ordered adjacent anchor pairs."""
    flat = _norm(text)
    found = [(anchor, pos) for anchor in anchors if (pos := flat.find(_norm(anchor))) >= 0]
    pairs = list(itertools.pairwise(found))
    inversions = sum(1 for (_, left), (_, right) in pairs if left > right)
    return {
        "found": len(found),
        "total": len(anchors),
        "in_order": len(pairs) - inversions,
        "pairs": len(pairs),
    }


def px_to_pt(bbox_px: list[float], img_w: int, img_h: int, page_rect: list[float]) -> list[float]:
    """Convert detection pixel coordinates to PDF display-space points."""
    sx = (page_rect[2] - page_rect[0]) / img_w
    sy = (page_rect[3] - page_rect[1]) / img_h
    return [bbox_px[0] * sx, bbox_px[1] * sy, bbox_px[2] * sx, bbox_px[3] * sy]


def xy_column_sort(
    items: list[tuple[str, float, list[float]]],
    page_w: float,
) -> list[tuple[float, str, float, list[float]]]:
    """Order detected regions with a simple two-column XY cut when no pointer head exists."""
    if not items:
        return []
    sorted_y = sorted(items, key=lambda x: (x[2][1], x[2][0]))
    narrow = [x for x in sorted_y if (x[2][2] - x[2][0]) < _HALF_WIDTH_FRAC * page_w]
    has_two_cols = (
        any((x[2][0] + x[2][2]) * 0.5 < 0.45 * page_w for x in narrow)
        and any((x[2][0] + x[2][2]) * 0.5 > 0.55 * page_w for x in narrow)
        and len(narrow) >= _MIN_TWO_COL_BLOCKS
    )
    if not has_two_cols:
        return [(float(i), lbl, sc, box) for i, (lbl, sc, box) in enumerate(sorted_y)]
    mid = page_w * 0.5
    max_y = max(item[2][3] for item in sorted_y)
    top_wide = [x for x in sorted_y if (x[2][2] - x[2][0]) >= _HALF_WIDTH_FRAC * page_w and x[2][3] < 0.35 * max_y]
    bot_wide = [x for x in sorted_y if x not in top_wide and (x[2][2] - x[2][0]) >= _HALF_WIDTH_FRAC * page_w]
    rest = [x for x in sorted_y if x not in top_wide and x not in bot_wide]
    left = [x for x in rest if (x[2][0] + x[2][2]) * 0.5 < mid]
    right = [x for x in rest if (x[2][0] + x[2][2]) * 0.5 >= mid]
    return [(float(i), lbl, sc, box) for i, (lbl, sc, box) in enumerate(top_wide + left + right + bot_wide)]


def reorder_blocks(
    blocks: list[dict[str, Any]],
    regions: list[tuple[float, str, float, list[float]]],
) -> str:
    """Assign native pyropdf text blocks to their smallest enclosing detected region."""
    assigned: list[tuple[float, float, float, str]] = []
    for blk in blocks:
        bx0, by0, bx1, by1 = blk["bbox"]
        cx, cy = (bx0 + bx1) * 0.5, (by0 + by1) * 0.5
        best_rank: float | None = None
        best_area = float("inf")
        for rank, _lbl, _sc, rb in regions:
            in_x = rb[0] - _BBOX_PAD_PT <= cx <= rb[2] + _BBOX_PAD_PT
            in_y = rb[1] - _BBOX_PAD_PT <= cy <= rb[3] + _BBOX_PAD_PT
            if in_x and in_y:
                area = max(1.0, (rb[2] - rb[0]) * (rb[3] - rb[1]))
                if area < best_area:
                    best_area = area
                    best_rank = rank
        assigned.append((best_rank if best_rank is not None else 1e6 + by0, by0, bx0, blk["text"]))
    assigned.sort(key=lambda item: (item[0], item[1], item[2]))
    return "\n".join(item[3] for item in assigned)


def _zip9_size(path: Path) -> int:
    with tempfile.NamedTemporaryFile(suffix=".whl") as tf:
        with zipfile.ZipFile(tf.name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(path, arcname="model.rten")
        return Path(tf.name).stat().st_size


def _empty_agg() -> dict[str, int]:
    return {
        "anchors_found": 0,
        "anchors_total": 0,
        "pairs_in_order": 0,
        "pairs_total": 0,
        "true_tables": 0,
        "fp_tables": 0,
        "jp_headings": 0,
    }


def _accumulate(
    agg: dict[str, int],
    stem: str,
    scores: dict[str, int],
    table_count: int,
    jp_headings: int,
) -> None:
    agg["anchors_found"] += scores["found"]
    agg["anchors_total"] += scores["total"]
    agg["pairs_in_order"] += scores["in_order"]
    agg["pairs_total"] += scores["pairs"]
    if stem in TRUE_TABLE_PAGES and table_count >= 1:
        agg["true_tables"] += 1
    if stem in NON_TABLE_PAGES and table_count >= 1:
        agg["fp_tables"] += 1
    if stem in JP_PAGES:
        agg["jp_headings"] += jp_headings


def _format_benchmark_md(perf_rows: list[dict[str, Any]], mupdf_baseline: dict[str, Any]) -> str:
    lines = [
        "# PP-DocLayout on RTen 0.28.0: Size, Latency, Peak RSS, and ORT Fidelity (`macos-arm64`, 12 labeled pages)",
        "",
        (
            "| Model (`rten 0.28.0`, `rten_format` only) | Classes | ONNX ops | Raw `.rten` | ZIP-9 wheel "
            "| PyPI <100 MiB? | Load (4t) | Mean infer (1t / 4t) | Peak RSS (1t / 4t) |"
        ),
        "| --- | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: |",
        *(
            f"| {r['label']} | {r['classes']} | {r['unique_ops']} | {r['raw_rten_bytes'] / MIB:.2f} MiB | "
            f"{r['zip9_wheel_bytes'] / MIB:.2f} MiB | {'Yes' if r['fits_pypi_100mib'] else '**No**'} | "
            f"{r['load_4t_ms']:.1f} ms | {r['mean_infer_1t_ms']:.0f} / {r['mean_infer_4t_ms']:.0f} ms | "
            f"{r['peak_rss_1t_bytes'] / MIB:.1f} / {r['peak_rss_4t_bytes'] / MIB:.1f} MiB |"
            for r in perf_rows
        ),
        (
            f"| Oracle: `{mupdf_baseline['version']}` (`onnxruntime`) | 11 | ONNX | ~50 MiB (+ORT) | ~68 MiB | Yes | "
            f"186.6 ms | — / {mupdf_baseline['mean_json_ms']:.0f} ms | — / {mupdf_baseline['peak_rss_mib']:.1f} MiB |"
        ),
        "",
        "## Identical-Tensor Numerical Fidelity vs `onnxruntime` (`mhlw-doc-p1`, score >= 0.5)",
        "",
        "| Model | ORT boxes | RTen boxes | Matched | Max score diff | Max box diff (px) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        *(
            f"| {r['label']} | {r['ort_fidelity']['ort']} | {r['ort_fidelity']['rten']} | "
            f"{r['ort_fidelity']['matched']} | {r['ort_fidelity']['max_score_diff']:.5f} | "
            f"{r['ort_fidelity']['max_box_diff_px']:.3f} |"
            for r in perf_rows
        ),
    ]
    return "\n".join(lines) + "\n"


def _format_comparison_md(
    engine_names: list[str],
    agg: dict[str, dict[str, int]],
    per_page: dict[str, Any],
    mupdf_pages: dict[str, Any],
    scans: dict[str, Any],
) -> str:
    n_true = len(TRUE_TABLE_PAGES)
    n_non = len(NON_TABLE_PAGES)
    lines = [
        "# Side-by-Side Layout Accuracy on Labeled Corpus (12 native pages + 2 OCR scans)",
        "",
        "Ground-truth anchors and table labels come from all 14 files in `evidence/pymupdf-comparison/quality/*.json`.",
        (
            "Table counts cover 3 ground-truth table pages (`mhlw-deposit-report-p1`, `nics`, `senate`) and "
            "7 non-table pages; the 2 gridded IRS tax forms (`f1040-p1`, `f1040-p2`) are excluded from both "
            "buckets and reported individually below."
        ),
        "",
        "## Aggregate summary (12 native labeled pages)",
        "",
        (
            f"| Engine | Anchors found | Order pairs OK | True tables (of {n_true}) "
            f"| False-positive table pages (of {n_non}) | Japanese headings (`mhlw-doc p1+p2`) |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in engine_names:
        a = agg[name]
        lines.append(
            f"| {name} | {a['anchors_found']}/{a['anchors_total']} | {a['pairs_in_order']}/{a['pairs_total']} | "
            f"{a['true_tables']}/{n_true} | {a['fp_tables']}/{n_non} | {a['jp_headings']} |"
        )
    lines += [
        "",
        (
            "## Per-page semantic region comparison "
            "(`pymupdf-layout 2.2` vs `PP-DocLayout_plus-L INT8` vs `PP-DocLayoutV3 FP32`)"
        ),
        "",
        (
            "| Page | `pymupdf-layout 2.2` regions (ms) | `PP-DocLayout_plus-L INT8` regions (4t ms) "
            "| `PP-DocLayoutV3 FP32` regions (4t ms) |"
        ),
        "| --- | --- | --- | --- |",
    ]
    for stem, row in per_page.items():
        m_e = row["engines"]["pymupdf-layout 2.2 (md)"]
        l_e = row["engines"]["PP-DocLayout_plus-L (INT8 MatMul+Conv, 800x800)"]
        v_e = row["engines"]["PP-DocLayoutV3 (FP32, 800x800)"]
        m_ms = mupdf_pages[stem]["pymupdf_layout_json_ms"]
        lines.append(
            f"| `{stem}` | `{m_e['regions']}` ({m_ms:.0f} ms) | `{l_e['regions']}` ({l_e['infer_4t_ms']:.0f} ms) "
            f"| `{v_e['regions']}` ({v_e['infer_4t_ms']:.0f} ms) |"
        )
    lines += [
        "",
        "## Image-only scanned documents (2 OCR fixtures, zero embedded PDF text)",
        "",
        "| Scan | `PP-DocLayout_plus-L INT8` regions (4t ms) | `PP-DocLayoutV3 FP32` regions (4t ms) |",
        "| --- | --- | --- |",
    ]
    plus_scans = scans["PP-DocLayout_plus-L (INT8)"]
    v3_scans = scans["PP-DocLayoutV3 (FP32)"]
    for stem in sorted(plus_scans):
        p_entry = plus_scans[stem]
        v_entry = v3_scans[stem]
        lines.append(
            f"| `{stem}` | `{p_entry['regions']}` ({p_entry['infer_4t_ms']:.0f} ms) "
            f"| `{v_entry['regions']}` ({v_entry['infer_4t_ms']:.0f} ms) |"
        )
    return "\n".join(lines) + "\n"


def _collect_perf(scratch: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in MODEL_SPECS:
        rten_path = scratch / spec.rten_rel
        raw_bytes = rten_path.stat().st_size
        whl_bytes = _zip9_size(rten_path)
        d1 = json.loads((scratch / "eval_out" / f"{spec.key}_1t.json").read_text())
        d4 = json.loads((scratch / "eval_out" / f"{spec.key}_4t.json").read_text())
        inf1 = [v["infer_ms"] for v in d1["pages"].values()]
        inf4 = [v["infer_ms"] for v in d4["pages"].values()]
        pre4 = [v["pre_ms"] for v in d4["pages"].values()]
        rows.append(
            {
                "key": spec.key,
                "label": spec.label,
                "classes": len(spec.labels),
                "unique_ops": spec.unique_ops,
                "raw_rten_bytes": raw_bytes,
                "zip9_wheel_bytes": whl_bytes,
                "fits_pypi_100mib": whl_bytes < 100 * MIB,
                "load_1t_ms": d1["load_ms"],
                "load_4t_ms": d4["load_ms"],
                "mean_pre_ms": round(sum(pre4) / len(pre4), 2),
                "mean_infer_1t_ms": round(sum(inf1) / len(inf1), 2),
                "mean_infer_4t_ms": round(sum(inf4) / len(inf4), 2),
                "median_infer_4t_ms": round(sorted(inf4)[len(inf4) // 2], 2),
                "peak_rss_1t_bytes": PEAK_RSS_BYTES[(spec.key, 1)],
                "peak_rss_4t_bytes": PEAK_RSS_BYTES[(spec.key, 4)],
                "ort_fidelity": IDENTICAL_TENSOR_FIDELITY[spec.key],
                "sha256_rten": hashlib.sha256(rten_path.read_bytes()).hexdigest()[:16],
            }
        )
    return rows


def _score_labeled_pages(
    scratch: Path,
    heur: dict[str, Any],
    mupdf: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, int]], dict[str, Any]]:
    label_files = sorted((ROOT / "evidence/pymupdf-comparison/quality").glob("*.json"))
    engine_names = [
        "pyropdf heuristic (text)",
        "pyropdf heuristic (md)",
        "pymupdf-layout 2.2 (md)",
        *[spec.label for spec in MODEL_SPECS],
    ]
    agg = {name: _empty_agg() for name in engine_names}
    per_page: dict[str, Any] = {}
    eval_4t = {spec.key: json.loads((scratch / "eval_out" / f"{spec.key}_4t.json").read_text()) for spec in MODEL_SPECS}

    for lp in label_files:
        lab = json.loads(lp.read_text())
        if lab.get("mode", "native") != "native":
            continue
        stem = lp.stem
        h = heur[stem]
        mp = mupdf["pages"][stem]
        row: dict[str, Any] = {"document": lab["document"], "page": lab["page"], "engines": {}}

        mp_counts = {
            lbl: sum(1 for r in mp["regions"] if r["label"] == lbl)
            for lbl in sorted({r["label"] for r in mp["regions"]})
        }
        baselines = [
            (
                "pyropdf heuristic (text)",
                h["text"],
                h["num_tables"],
                0,
                {"blocks": h["num_blocks"], "tables": h["num_tables"]},
            ),
            (
                "pyropdf heuristic (md)",
                h["markdown"],
                h["num_tables"],
                sum(1 for ln in h["markdown"].splitlines() if ln.startswith("#")),
                {"tables": h["num_tables"]},
            ),
            (
                "pymupdf-layout 2.2 (md)",
                mp["markdown"],
                sum(1 for r in mp["regions"] if r["label"] == "table"),
                sum(1 for r in mp["regions"] if r["label"] in ("section-header", "title")),
                mp_counts,
            ),
        ]
        for ename, text, tbl_n, jp_h, reg_counts in baselines:
            sc = score_anchors(lab["anchors"], text)
            row["engines"][ename] = {"anchors": sc, "regions": reg_counts}
            _accumulate(agg[ename], stem, sc, tbl_n, jp_h)

        page_w = h["page_rect"][2] - h["page_rect"][0]
        for spec in MODEL_SPECS:
            mpage = eval_4t[spec.key]["pages"][stem]
            dets = [d for d in mpage["detections"] if d["score"] >= spec.threshold]
            raw_items = [
                (
                    d.get("order"),
                    spec.labels[d["cls"]] if 0 <= d["cls"] < len(spec.labels) else f"cls{d['cls']}",
                    d["score"],
                    px_to_pt(d["bbox_px"], mpage["img_w"], mpage["img_h"], h["page_rect"]),
                )
                for d in dets
            ]
            if spec.has_order:
                ordered = sorted([(float(o), lbl, sc, box) for o, lbl, sc, box in raw_items], key=lambda item: item[0])
            else:
                ordered = xy_column_sort([(lbl, sc, box) for _, lbl, sc, box in raw_items], page_w)
            reord = reorder_blocks(h["blocks"], ordered)
            sc = score_anchors(lab["anchors"], reord)
            rc: dict[str, int] = {}
            for _, lbl, _, _ in ordered:
                rc[lbl] = rc.get(lbl, 0) + 1
            tbl_n = rc.get("table", 0)
            jp_h = rc.get("doc_title", 0) + rc.get("paragraph_title", 0)
            row["engines"][spec.label] = {"anchors": sc, "infer_4t_ms": mpage["infer_ms"], "regions": rc}
            _accumulate(agg[spec.label], stem, sc, tbl_n, jp_h)

        per_page[stem] = row

    return engine_names, agg, per_page


def _collect_scans(scratch: Path) -> dict[str, Any]:
    scans: dict[str, Any] = {}
    for tag, fn, labels in [
        ("PP-DocLayout_plus-L (INT8)", "scans_plus_l_int8.json", LABELS_20),
        ("PP-DocLayoutV3 (FP32)", "scans_v3_fp32.json", LABELS_25),
    ]:
        data = json.loads((scratch / "eval_out" / fn).read_text())
        scans[tag] = {}
        for stem, entry in data["pages"].items():
            rc: dict[str, int] = {}
            for det in entry["detections"]:
                if det["score"] >= _SCAN_SCORE_THRESHOLD:
                    lbl = labels[det["cls"]]
                    rc[lbl] = rc.get(lbl, 0) + 1
            scans[tag][stem] = {"infer_4t_ms": entry["infer_ms"], "regions": rc}
    return scans


def main() -> None:
    """Capture pyropdf baselines or build benchmark and accuracy reports from evaluation artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="Directory holding models/, eval_out/, and heuristic JSONs.",
    )
    parser.add_argument("--out-dir", type=Path, help="Output directory for results JSON/Markdown.")
    parser.add_argument(
        "--capture-pyropdf",
        action="store_true",
        help="Render 150 DPI page PNGs and capture pyropdf heuristic JSON for all native labeled pages.",
    )
    args = parser.parse_args()

    if args.capture_pyropdf:
        capture_pyropdf_heuristic(args.work_dir)
        return
    if args.out_dir is None:
        parser.error("--out-dir is required unless --capture-pyropdf is specified")

    scratch = args.work_dir
    perf_rows = _collect_perf(scratch)
    heur = json.loads((scratch / "pyropdf_heuristic.json").read_text())
    mupdf = json.loads((scratch / "pymupdf_layout.json").read_text())
    engine_names, agg, per_page = _score_labeled_pages(scratch, heur, mupdf)
    scans = _collect_scans(scratch)

    mp_pages = mupdf["pages"]
    mupdf_baseline = {
        "version": mupdf["version"],
        "mean_json_ms": round(sum(v["pymupdf_layout_json_ms"] for v in mp_pages.values()) / len(mp_pages), 1),
        "mean_md_ms": round(sum(v["pymupdf_layout_md_ms"] for v in mp_pages.values()) / len(mp_pages), 1),
        "peak_rss_mib": mupdf["peak_rss_mib"],
    }
    heuristic_baseline = {
        "mean_layout_ms": round(sum(v["layout_ms"] for v in heur.values()) / len(heur), 2),
        "mean_markdown_ms": round(sum(v["markdown_ms"] for v in heur.values()) / len(heur), 2),
        "mean_render_150dpi_ms": round(sum(v["render_150dpi_ms"] for v in heur.values()) / len(heur), 2),
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "benchmark.json").write_text(
        json.dumps(
            {
                "platform": "Darwin arm64 (Apple Silicon)",
                "rten_version": "0.28.0",
                "rten_features": ["rten_format"],
                "pyropdf_heuristic_baseline": heuristic_baseline,
                "pymupdf_layout_baseline": mupdf_baseline,
                "provenance": MODEL_PROVENANCE,
                "models": perf_rows,
            },
            indent=2,
        )
        + "\n"
    )
    (args.out_dir / "comparison.json").write_text(
        json.dumps({"aggregate": agg, "per_page": per_page, "scans": scans}, ensure_ascii=False, indent=2) + "\n"
    )
    (args.out_dir / "benchmark.md").write_text(_format_benchmark_md(perf_rows, mupdf_baseline))
    comparison_md = _format_comparison_md(engine_names, agg, per_page, mp_pages, scans)
    (args.out_dir / "comparison.md").write_text(comparison_md)
    sys.stdout.write(f"Wrote benchmark & comparison artifacts to {args.out_dir}\n")


if __name__ == "__main__":
    main()
