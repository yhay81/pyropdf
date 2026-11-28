# Apache-2.0 PP-DocLayout on RTen for `pyropdf[layout]` (issue #1199)

Evaluation of running Apache-2.0 PP-DocLayout models on pyropdf's existing pure-Rust `rten 0.28.0`
runtime (`default-features = false, features = ["rten_format"]`) for an optional `pyropdf[layout]`
extra, per [DESIGN.md §11](../../DESIGN.md#11-layout) and [§14](../../DESIGN.md#14-moving-from-pylopdf),
Understanding tracker [#253](https://github.com/yhay81/pyropdf/issues/253), and Continuous tracks
[#258](https://github.com/yhay81/pyropdf/issues/258).

## Executive summary & recommendation

**Recommend Option A: ship `PP-DocLayout_plus-L` (dynamic INT8 `MatMul+Conv`, `.rten` format,
23.10 MiB compressed) in a separately versioned Apache-2.0 data wheel `pyropdf-layout-models`
(`pyropdf[layout]`), reusing `pyropdf-engine`'s existing `rten_format` runtime with zero new crates
and `+8.1 KiB` core wheel growth.**

Keep pyropdf's fast geometric `Page.layout()` (`4.5 ms`/page) and `use_structure="auto"` as the
default; expose `LayoutEngine` / `Page.regions()` / opt-in `use_layout=True` as semantic
enrichment (headings, headers/footers, footnotes, TOC, scanned-page segmentation) rather than
overwriting native text reading order.

| Candidate | Model wheel (ZIP-9) | PyPI <100 MiB? | Core `abi3` delta | Load / Infer (4t) | Peak RSS (4t) | True tables (of 3) | FP tables (of 7) | JP headings (`mhlw-doc`) | Verdict |
| --- | ---: | :---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `PP-DocLayout-S` (FP32, 480×480) | 4.26 MiB | Yes | +8.1 KiB | 4.5 / 26 ms | 57.5 MiB | **0/3** | 1/7 | 6 | **Reject**: misses all 3 real tables |
| **`PP-DocLayout_plus-L` (INT8, 800×800)** | **23.10 MiB** | **Yes** | **+8.1 KiB** | **21.5 / 372 ms** | **412.2 MiB** | **3/3** | **0/7** | **9** | **Recommend**: matches FP32 quality, 4.3× under PyPI cap, 3× less RSS than `pymupdf-layout` |
| `PP-DocLayout_plus-L` (FP32, 800×800) | 112.27 MiB | **No** | +8.1 KiB | 43.4 / 700 ms | 550.5 MiB | 3/3 | 0/7 | 9 | **Reject**: exceeds PyPI 100 MiB file cap |
| `PP-DocLayoutV3` (INT8 Conv-only, 800×800) | 42.91 MiB | Yes | +8.1 KiB | 25.4 / 718 ms | **940.6 MiB** | 3/3 | 0/7 | 9 | **Defer**: 941 MiB RSS (polygon mask head) + INT8 drift (`17/18` vs ORT, `5.26 px`) |
| `PP-DocLayoutV3` (FP32, 800×800) | 112.46 MiB | **No** | +8.1 KiB | 48.5 / 916 ms | **977.3 MiB** | 3/3 | 0/7 | 9 | **Defer**: >100 MiB wheel + 977 MiB RSS; strong future upgrade if mask head pruned |
| Oracle: `pymupdf-layout 2.2` (`onnxruntime`) | ~68 MiB (incl. ORT) | Yes | N/A | 186.6 / 345 ms | 1,224.8 MiB | 2/3 | **3/7** | 0 | Reference baseline (AGPL/commercial, 3× RSS, misses `mhlw-deposit-report` table, 3/7 FP tables) |
| Built-in `pyropdf` heuristic (default) | 0 B | N/A | 0 B | 0 / 4.5 ms | ~40 MiB | **3/3** | **0/7** | 0 | Keep as default (`152/152` anchors, `140/140` order pairs) |

---

## 1. License & Model Provenance

Every evaluated PP-DocLayout release is officially published by PaddlePaddle on Hugging Face under
**Apache-2.0**, which is redistribution-compatible with MIT-licensed `pyropdf` when shipped in a
separate optional data package (`pyropdf-layout-models`, licensed `Apache-2.0` with upstream
`LICENSE` and `NOTICE`), identical to how `pyropdf[ocr]` depends on `pylopdf-ocr-models`
(`0.1.0`, `25.4 MiB`, `Apache-2.0`).

| Hugging Face repository | Pinned commit SHA | License | Last modified | Format / Size |
| --- | --- | --- | --- | --- |
| `PaddlePaddle/PP-DocLayout_plus-L_onnx` (**recommended source**) | `feb74619326f` (`feb74619326f634e0e883218598096a3733ad9f7`) | `apache-2.0` | `2026-06-10` | Official ONNX opset 16 (`129.7 MB`, 20 classes) |
| `PaddlePaddle/PP-DocLayoutV3_onnx` | `46bbdf188bb0` (`46bbdf188bb0a772c08aed74882ce7e51a8f1ea6`) | `apache-2.0` | `2026-06-10` | Official ONNX opset 17 (`130.5 MB`, 25 classes + order + mask head) |
| `PaddlePaddle/PP-DocLayoutV2_onnx` | `7d44592493df` (`7d44592493df02e28110d99de8ca4b1fbc7309bd`) | `apache-2.0` | `2026-06-10` | Official ONNX opset 17 (`214.0 MB`, 25 classes) |
| `PaddlePaddle/PP-DocLayout-S` | `8ac289e66575` (`8ac289e66575bb9bba6e15c53719d8b15cc9b3b2`) | `apache-2.0` | `2025-07-22` | Official Paddle (`4.8 MB`, 23 classes) |
| `stefanj0/PP-DocLayout-S-ONNX` | `7e851b9e5ad6` (`7e851b9e5ad66613d7318a3213ab3336ebe0a514`) | `apache-2.0` | `2026-05-26` | Community ONNX export (`4.9 MB`, opset 17) |
| `PaddlePaddle/PP-DocLayout-M` / `-L` | `7dbfcce3154a` / `ca760c3336d9` | `apache-2.0` | `2025-07-22` | Official Paddle (`23.4 MB` / `129.0 MB`) |

Note: `PaddlePaddle/PP-DocLayout_plus-L_onnx` and `PP-DocLayoutV3_onnx` are **official PaddlePaddle
ONNX exports** (no third-party conversion provenance risk), and their `inference.yml` specifies
`scale=1/255, mean=[0,0,0], std=[1,1,1]` RGB normalization (whereas `PP-DocLayout-S` uses ImageNet
mean/std).

---

## 2. RTen 0.28.0 Operator Compatibility, Conversion Pipeline & Core Wheel Impact

### Zero new crates, zero new `rten` features, zero extra operators

All five evaluated models (`PP-DocLayout-S` FP32, `PP-DocLayout_plus-L` FP32 & INT8, `PP-DocLayoutV3`
FP32 & INT8) convert cleanly with `rten-convert 0.22.0` and execute end-to-end on **`rten 0.28.0`**
configured with **`default-features = false, features = ["rten_format"]`** — the exact feature set
already compiled into `crates/pyropdf-engine/Cargo.toml` for OCR.

- **Conversion pipeline for recommended model**:
  1. Download `inference.onnx` (`129,736,329 B`) from `PaddlePaddle/PP-DocLayout_plus-L_onnx@feb74619326f`.
  2. Dynamic INT8 quantization via `onnxruntime.quantization.quantize_dynamic(..., op_types_to_quantize=["MatMul", "Conv"], weight_type=QuantType.QUInt8)` -> `34,129,714 B` (`41` unique ONNX ops; adds `ConvInteger`, `DynamicQuantizeLinear`, `MatMulInteger` — all natively supported by `rten 0.28.0`).
  3. Convert via `rten-convert 0.22.0` -> `34,425,280 B` (`32.83 MiB` raw, **`23.10 MiB`** ZIP-9 wheel).
- **Identical-tensor numerical fidelity vs `onnxruntime`** (`mhlw-doc-p1`, score ≥ 0.5, identical
  bilinear NCHW float32 input tensor fed to both ORT and RTen; full table in
  [`results/benchmark.md`](results/benchmark.md)):
  - FP32 models (`S`, `plus-L`, `V3`): **100% box match** (`17/17`, `20/20`, `20/20`) within
    `max_score_diff <= 0.00007` and `max_box_diff <= 0.005 px` (confirming exact RTen op parity).
  - Recommended `PP-DocLayout_plus-L` INT8: **21/21** exact detection match within
    `max_score_diff = 0.01489` and `max_box_diff = 0.726 px`.
  - `PP-DocLayoutV3` INT8 Conv-only: `17/18` match (`max_score_diff = 0.08053`, `max_box_diff = 5.263 px`),
    further confirming `plus-L` INT8's superior quantization stability.

### Measured core `abi3` wheel-size delta, `cargo-audit`, & `wasm32`

Measured on `macosx_arm64` using `tools/native-wheel-args.txt` + `tools/repack_wheel.py` (Zopfli-1)
+ `tools/wheel_size.py --abi abi3 --base`, wiring a live PyO3 `_SpikeLayoutEngine` class in
`pyropdf._core` so fat LTO retains model loading, 800×800 bilinear preprocessing, RTen execution,
and box extraction. Full data: [`results/wheel-delta.md`](results/wheel-delta.md) and
[`results/wheel-delta.json`](results/wheel-delta.json).

| Variant | Delivered wheel (B) | Wheel growth | `Cargo.lock` pkgs | `cargo-audit` | `wasm32-emscripten` |
| --- | ---: | ---: | ---: | --- | --- |
| Baseline (`main`, `rten_format`) | 7,755,870 | 0 B | 220 | clean | OK |
| **Recommended**: `LayoutEngine` over existing `rten_format` | **7,764,128** | **+8,258 B (+8.1 KiB)** | **220 (+0)** | clean | OK |
| Alternative: `+onnx_format` feature (`rten-onnx`) | 7,845,147 | +89,277 B (+87.2 KiB) | 221 (+1) | clean | OK |

- **9.0 MiB native `abi3` wheel ceiling (#858)**: Recommended `rten_format` wrapper keeps **all 5
  native targets** well under 9.0 MiB (`win_amd64` stays at `8.554 MiB` with **+457 KiB** headroom,
  31× under the 256 KiB PR growth gate).
- **Why `.rten` over `.onnx` in `pyropdf-layout-models`**: Pre-converting to `.rten` saves
  **81.0 KiB** of native `abi3` wheel size (`+8.1 KiB` vs `+87.2 KiB`), avoids adding `rten-onnx`
  (`+44.8 KiB` zlib-9 on standalone `wasm32-unknown-unknown` / `+69.8 KiB` on `arm64`), and cuts
  model load latency (`21.5 ms` vs ONNX protobuf deserialization).

---

## 3. Inference Latency, Peak RSS, and F6 / `DocumentLimits` Compliance

Full numbers: [`results/benchmark.md`](results/benchmark.md) and
[`results/benchmark.json`](results/benchmark.json) (all 12 native labeled corpus pages on Apple
Silicon `macos-arm64`, uncontended run, Peak RSS via `/usr/bin/time -l`).

- **Baseline `pyropdf` heuristic (`main` `b18631bb`)**: `4.49 ms` mean `page.layout()`, `9.13 ms`
  mean `page.pixmap(dpi=150)`, ~40 MiB RSS.
- **`PP-DocLayout_plus-L` INT8 (4t)**: `21.5 ms` model load, `5.0 ms` Rust bilinear preprocessing,
  **`372 ms`** mean inference per page, **`412.2 MiB`** peak RSS (nearly **3× lower** than
  `pymupdf-layout 2.2`'s `1,224.8 MiB` peak RSS and close to `pymupdf-layout`'s `345 ms` mean
  latency).
- **`PP-DocLayoutV3` Peak RSS caveat**: Even after Conv-only INT8 quantization, `PP-DocLayoutV3`
  peaks at **`940.6 MiB`** RSS (`977.3 MiB` FP32) because its graph always materializes a
  `[300, 200, 200]` polygon mask head tensor (`48 MB` per intermediate stage). Quantizing `MatMul`
  in V3 degraded recall on `usrguide-p2` (dropping 3/4 `paragraph_title`s), forcing Conv-only INT8
  (`42.91 MiB` wheel).
- **F6 budgetability**: Like `OcrEngine`, RTen allocates operator scratch tensors internally outside
  `pyropdf_store::budget::Budget`. Because PP-DocLayout resizes every page to a **fixed
  `1 × 3 × 800 × 800` tensor (`7.32 MiB`)**, RTen's working set is bounded by the model graph rather
  than untrusted PDF input size. `LayoutEngine` enforces explicit caps matching `OcrEngine`:
  `max_model_size=64 MiB`, `512 MiB` max RGBA raster, fallible `try_reserve_exact` on the `7.32 MiB`
  input buffer, `max_concurrent=1` default (bounding RSS to ~`412 MiB` per engine even under
  free-threaded Python 3.15t), `threads` `1..=16`, `<= 1_024` detections, and document cancellation /
  deadline polling before render and before inference.

---

## 4. Side-by-Side Accuracy on the Labeled Corpus (Honest Wins & Losses)

Evaluated on **all 14 files** in `evidence/pymupdf-comparison/quality/*.json`: **12 native labeled
pages** (Japanese government documents `mhlw-doc` p1/p2, rotated Japanese statutory business form
`forms/mhlw-deposit-report.pdf` p1, multi-column `patent-us223898` p2/p3, rotated table
`senate-expenditures`, dense tax form `f1040` p1/p2, legislation `bill-hr815`, wide table `nics`,
and LaTeX manual `usrguide` p1/p2) plus **2 OCR scan fixtures** (`bunka-kokugo-series-019-p4.pdf`
vertical Japanese scan and `wdl6812-manuscript.pdf`).

Table-scoring taxonomy: **3** ground-truth table pages (`mhlw-deposit-report-p1`, `nics`, `senate`)
and **7** non-table pages; the **2** gridded IRS tax forms (`f1040-p1`, `f1040-p2`) are excluded
from both buckets and reported individually in [`results/comparison.md`](results/comparison.md) and
[`results/comparison.json`](results/comparison.json).

### Aggregate metrics (12 native labeled pages)

| Engine | Anchors found | Order pairs OK | True tables (of 3) | False-positive table pages (of 7) | Japanese headings (`mhlw-doc p1+p2`) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `pyropdf` heuristic (text) | **152/152** | **140/140** | **3/3** | **0/7** | 0 |
| `pyropdf` heuristic (md) | 147/152 | 134/135 | **3/3** | **0/7** | 0 |
| `pymupdf-layout 2.2` (md) | 112/152 | 98/100 | 2/3 | **3/7** | 0 |
| `PP-DocLayout-S` (FP32) | 152/152 | 129/140 | **0/3** | 1/7 | 6 |
| `PP-DocLayout_plus-L` (FP32) | 152/152 | 136/140 | **3/3** | **0/7** | **9** |
| **`PP-DocLayout_plus-L` (INT8)** | **152/152** | **133/140** | **3/3** | **0/7** | **9** |
| `PP-DocLayoutV3` (FP32) | 152/152 | 137/140 | **3/3** | **0/7** | **9** |
| `PP-DocLayoutV3` (INT8 Conv-only) | 152/152 | 135/140 | **3/3** | **0/7** | **9** |

### Where `PP-DocLayout_plus-L` (INT8) WINS

1. **Japanese uniform-font headings (`mhlw-doc` p1/p2)**: Both `pyropdf` heuristic (`0`) and
   `pymupdf-layout 2.2` (`0`, misclassifies 14 lines as `list-item`) miss Japanese headings set in
   the same 10.5 pt font size as body text. `PP-DocLayout_plus-L` INT8 recovers **9 headings**
   (6 on p1, 3 on p2) plus running `header` and page `number`.
2. **3/3 real tables and 0/7 false-positive tables (vs `pymupdf-layout`'s 2/3 and 3/7)**:
   `pymupdf-layout 2.2` **misses** the rotated Japanese statutory accounting form
   `mhlw-deposit-report-p1` (classifying it as `picture` + `caption`), turns `usrguide-p1` TOC,
   `usrguide-p2` code listing, and `patent-us223898-p2` two-column text into broken Markdown tables,
   and drops `f1040` down to 8/18 anchors. `PP-DocLayout_plus-L` INT8 detects all 3 real tables
   (`mhlw-deposit-report`, `nics`, rotated `senate-expenditures`) with **0/7** false positives and
   classifies `usrguide-p1` accurately (`doc_title`, `paragraph_title`, `content` TOC, `footnote`,
   `number`).
3. **Image-only scanned documents**: On scanned vertical Japanese `bunka-kokugo-series-019-p4.pdf`
   (zero embedded text), `PP-DocLayout_plus-L` INT8 segments `1 doc_title`, `1 paragraph_title`,
   and `10 text` columns cleanly — complementing `Page.ocr(mode="hybrid")` (#1185) for full-page
   scans.
4. **3× lower peak RSS and MIT-compatible optional Apache-2.0 stack** vs `pymupdf-layout`'s
   `1,224.8 MiB` RSS and AGPL/commercial license + `onnxruntime` binary dependency.

### Where `PP-DocLayout_plus-L` (INT8) LOSES (honest trade-offs)

1. **Speed & memory vs `pyropdf` heuristic**: `~372 ms`/page and `412 MiB` RSS vs `4.5 ms`/page and
   `~40 MiB` RSS (~80× slower, 10× more memory). It must remain strictly opt-in.
2. **Reading order on text-bearing PDFs**: Naively sorting native text blocks by `PP-DocLayout_plus-L`
   boxes + XY-cut drops reading-order pair accuracy from `140/140` (heuristic) to `133/140` (and
   even V3's learned pointer head reaches `137/140`). Therefore, for text-bearing PDFs the model
   should **enrich semantic roles** while preserving pyropdf's native geometric reading order.
3. **No `list_item` class**: `pymupdf-layout 2.2` detects 15 `list-item` regions on `bill-hr815-p1`
   at `92 ms`; PP-DocLayout's 20-class taxonomy emits `text` for list items (pyropdf's existing
   textual list-marker heuristics remain necessary).
4. **Dense tax forms & small footnotes**: Like `pymupdf-layout`, `PP-DocLayout_plus-L` labels the
   entire gridded IRS Form 1040 (`f1040 p1/p2`) as one `table` region, and misses the small bottom
   footnote on `nics-p1` (which `PP-DocLayoutV3` catches as `vision_footnote`).
5. **Cold single-page scan latency**: `bunka-kokugo-series-019-p4` (`1,151 ms`) and
   `wdl6812-manuscript` (`1,423 ms`) ran slower during cold single-page invocations than amortized
   multi-page runs (`~372 ms`/page at 150 DPI).

---

## 5. Proposed Packaging & Public API

See [`api-spec.md`](api-spec.md) for the complete DESIGN.md §10 specification (`pyropdf[layout]` ->
`pyropdf-layout-models`, `LayoutEngine`, `LayoutRegion`, `LayoutKind`, `LayoutError`,
`Page.regions()`, and composition with `use_structure` (#1181), `ignore_headers_footers` (#1212),
`ruby` (#1184), and `Page.ocr(mode="hybrid")` (#1185)).

## 6. Reproducing Measurements

```bash
# 1. Verify standalone RTen 0.28.0 evaluator crate (fmt, clippy, wasm32, cargo-audit):
cargo clippy --manifest-path evidence/layout-model/Cargo.toml --locked --all-targets -- -D warnings
cargo check --manifest-path evidence/layout-model/Cargo.toml --locked --target wasm32-unknown-emscripten

# 2. Measure core native abi3 wheel-size delta (Zopfli-1 repacked):
python3 evidence/layout-model/measure_wheels.py \
  --baseline /tmp/layout-wheels/baseline \
  --work-dir /tmp/layout-wheels/work \
  --out-dir evidence/layout-model/results

# 3. Capture 150 DPI page PNGs & pyropdf heuristic baselines for all native quality labels:
uv run python evidence/layout-model/evaluate.py --capture-pyropdf --work-dir /tmp/layout-work

# 4. Run RTen inference on the rendered PNGs & regenerate benchmark/comparison tables:
cargo run --release --locked --manifest-path evidence/layout-model/Cargo.toml -- \
  /path/to/model_int8.rten plus_l 4 /tmp/layout-work/eval_out/doclayout_plus_l_int8_4t.json \
  /tmp/layout-work/pages/*.png
python3 evidence/layout-model/evaluate.py \
  --work-dir /tmp/layout-work \
  --out-dir evidence/layout-model/results
```

## 7. Proposed Follow-up Implementation Issues (Once Approved)

1. **Publish `pyropdf-layout-models` (`0.1.0`, Apache-2.0, `py3-none-any`)**: reproducible build
   script converting `PaddlePaddle/PP-DocLayout_plus-L_onnx@feb74619326f` -> dynamic INT8 ->
   `.rten` (`23.10 MiB` ZIP-9 wheel) with `model.json`, `LICENSE`, `NOTICE`, and `model_paths()`.
2. **`pyropdf-engine` + `pyropdf-python`: `LayoutEngine`, `LayoutRegion`, and `Page.regions()`**:
   add bounded RTen layout inference over the existing `rten_format` dependency (`+8.1 KiB` wheel
   delta, `max_concurrent=1`, `max_model_size=64 MiB`, cancellation/deadline polling, `py.detach()`),
   wire `pyropdf[layout]` optional extra in `pyproject.toml`, and add unit/limit tests.
3. **Semantic enrichment of `markdown()`, `to_json()`, and `to_chunks()` (`use_layout` / `layout_engine`)**:
   compose opt-in ML layout regions with `use_structure` (#1181), `ignore_headers_footers` (#1212),
   `ruby` (#1184), and geometric layout (preserving native `140/140` reading order while recovering
   uniform-font headings, single-page headers/footers, footnotes, and scanned-page segmentation),
   plus update `#258` continuous track to monitor `PP-DocLayoutV3` mask-head pruning / `rten` memory
   improvements.
