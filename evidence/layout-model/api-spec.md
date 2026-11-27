# Draft Packaging & Public API Specification for `pyropdf[layout]` (issue #1199)

Follows [DESIGN.md §10](../../DESIGN.md#10-python-api) (Python API),
[§10.1](../../DESIGN.md#101-concurrency) (Concurrency), [F6](../../DESIGN.md#3-principles) (Bounded
resources), and mirrors the existing `pyropdf[ocr]` (`OcrEngine` / `pylopdf-ocr-models`) and
`use_structure` (`#1181`) conventions on `main`.

> Status: Draft for owner decision in #1199. Nothing in this document is implemented on `main` yet.

---

## 1. Optional Extra & Model Package (`pyropdf[layout]`)

### `pyproject.toml`

```toml
[project.optional-dependencies]
layout = ["pyropdf-layout-models>=0.1.0,<0.2"]
```

### Separate Data Distribution: `pyropdf-layout-models`

Mirrors `pylopdf-ocr-models` (`0.1.0`, `25.4 MiB`, Apache-2.0):

- **Wheel tag**: `py3-none-any` (pure data, zero native code, installs identically on Linux/macOS/Windows/Pyodide).
- **License**: `Apache-2.0` (compatible with MIT-licensed `pyropdf` as an optional dependency; core `pyropdf` remains 100% MIT and carries zero model weights).
- **Contents (~23.10 MiB compressed ZIP-9; well below PyPI's 100 MiB ceiling)**:
  - `pyropdf_layout_models/data/pp_doclayout_plus_l_int8.rten` (`32.83 MiB` raw, `23.10 MiB` compressed; converted from `PaddlePaddle/PP-DocLayout_plus-L_onnx@feb74619326f` via dynamic INT8 `MatMul`+`Conv` quantization + `rten-convert 0.22.0`).
  - `pyropdf_layout_models/data/model.json` (model name, input size `800`, normalization `scale=1/255, mean=[0,0,0], std=[1,1,1]`, ordered 20-entry `labels`, SHA-256 digest).
  - `LICENSE` (Apache-2.0) and `NOTICE` (attribution to PaddlePaddle / PP-DocLayout_plus-L_onnx commit SHA + quantization notice).
- **Discovery contract** (mirrors `pylopdf_ocr_models.model_paths()`):

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class LayoutModelPaths:
    model: Path
    metadata: Path
    version: str

def model_paths() -> LayoutModelPaths: ...
```

---

## 2. Public Types & Exceptions (`pyropdf`)

```python
class LayoutError(PdfError):
    """Model loading, validation, or inference failure in ``LayoutEngine``."""
```

Resource-limit refusals (oversized model file, oversized raster, excess detections, cancellation,
or deadline) raise `LimitError` (same separation as `OcrError` vs `LimitError`).

```python
LayoutKind: TypeAlias = Literal[
    "title",        # doc_title
    "heading",      # paragraph_title
    "text",         # text, vertical_text
    "table",        # table
    "figure",       # image, chart, seal, header_image, footer_image
    "caption",      # figure_title, table_title, chart_title
    "header",       # header
    "footer",       # footer
    "footnote",     # footnote, vision_footnote
    "formula",      # formula, display_formula, inline_formula, formula_number
    "code",         # algorithm
    "toc",          # content
    "reference",    # reference, reference_content
    "page_number",  # number
    "abstract",     # abstract
    "aside",        # aside_text
]


@dataclass(frozen=True, slots=True, kw_only=True)
class LayoutRegion:
    """One detected semantic layout region on a page or pixmap.

    ``Page.regions`` returns ``bbox`` in the page's display space (points,
    top-left origin, matching ``Page.layout`` and ``Page.ocr``).
    ``LayoutEngine.detect`` returns ``bbox`` in pixmap pixels (top-left origin).
    """

    kind: LayoutKind
    label: str
    bbox: Rect
    confidence: float
    reading_order: int | None = None
```

Providing both normalized `kind` and raw model `label` isolates downstream callers from class-table
renames if a future `pyropdf-layout-models` release upgrades from `PP-DocLayout_plus-L` (20 classes)
to `PP-DocLayoutV3` (25 classes, which also populates `reading_order`) or when users pass a custom
`.rten` model.

---

## 3. `LayoutEngine` (DESIGN.md §10.1 Concurrency & F6 Bounds)

```python
class LayoutEngine:
    """Pure-Rust RTen document layout detector.

    Loads a pre-converted ``.rten`` model (by default discovered from
    ``pyropdf-layout-models`` via ``pyropdf[layout]``). Thread-safe and
    reusable across documents and threads.
    """

    def __init__(
        self,
        model_path: os.PathLike[str] | str | None = None,
        *,
        labels: Sequence[str] | None = None,
        threads: int | None = None,
        max_concurrent: int = 1,
        max_model_size: int = 64 * 1024 * 1024,
    ) -> None: ...

    @property
    def threads(self) -> int: ...

    @property
    def max_concurrent(self) -> int: ...

    @property
    def max_model_size(self) -> int: ...

    def detect(
        self,
        pixmap: Pixmap,
        *,
        min_confidence: float = 0.5,
    ) -> list[LayoutRegion]: ...
```

### Resource-bound & concurrency design

| Control | Default | Behavior |
| --- | --- | --- |
| `max_model_size` | `64 MiB` | Checked via `metadata().len()` before reading into memory; refuses larger files with `LimitError` (accommodates `PP-DocLayout_plus-L` INT8 `32.8 MiB` and future V3 INT8 `54.2 MiB`). |
| `max_concurrent` | `1` (range `1..=16`) | `threading.BoundedSemaphore(max_concurrent)` guarding render + inference. Default `1` (vs `OcrEngine`'s `2`) bounds peak RSS to ~`415 MiB` per engine even under free-threaded Python 3.15t. |
| `threads` | `min(4, cpu_count)` (range `1..=16`) | Dedicated `rten::ThreadPool` per engine (no process-global thread pool mutation, §10.1). |
| Input tensor bound | Fixed `1 × 3 × 800 × 800` `f32` (`7.32 MiB`) | Input raster capped at `512 MiB` RGBA (`LimitError`), then bilinearly resized into a fixed `7.32 MiB` tensor with `try_reserve_exact`. RTen scratch buffers therefore do not scale with input page size. |
| Output detection cap | `1_024` regions | Model outputs at most `300` queries (RT-DETR fixed query count); custom `.rten` models exceeding `1_024` retained detections raise `LimitError`. |
| GIL & cancellation | `py.detach()` | Releases the GIL during preprocessing and `Model::run`; `Page.regions` checks document cancellation/deadline before rendering and before inference (an individual `Model::run` call runs to completion, matching `OcrEngine`). |

---

## 4. `Page` and `Document` Integration

### Low-level region detection

```python
class Page:
    def regions(
        self,
        *,
        engine: LayoutEngine | None = None,
        dpi: float = 150,
        min_confidence: float = 0.5,
        clip: Sequence[float] | None = None,
    ) -> list[LayoutRegion]:
        """Render the page at ``dpi`` and detect semantic layout regions in display space."""
```

- Default `dpi=150` renders A4/Letter to ~`1240×1754` in `~10 ms` (well above the detector's
  `800×800` input resolution without paying 300 DPI's 4× raster cost).
- When `engine is None`, lazily loads and caches a default `LayoutEngine()` from
  `pyropdf-layout-models` (`@functools.cache`), raising `LayoutError("Layout models are not
  installed; install pyropdf[layout] or pass an explicit model_path")` when missing.

### Opt-in enrichment of `markdown()`, `to_json()`, and `to_chunks()`

Existing methods remain **fast and deterministic by default** (`layout_engine=None` / `use_layout=False`):

```python
# Page.markdown / Document.markdown / Document.to_json / Document.to_chunks
...,
use_structure: Literal["auto", "always", "never"] = "auto",
use_layout: bool = False,
layout_engine: LayoutEngine | None = None,
```

#### Composition rules with `use_structure` (#1181) and geometric layout

1. **Tagged PDFs take priority**: When `use_structure="always"` or (`use_structure="auto"` and the
   structure tree passes trustworthiness checks), structure-tree roles drive export and the ML
   detector is **not** invoked (saving ~370 ms/page and ~415 MiB RSS).
2. **Untagged text PDFs (semantic enrichment, not blind replacement)**: When `use_layout=True` (or
   a `LayoutEngine` is passed) on an untagged page:
   - **Preserve pyropdf's native geometric reading order** (`129/129` pairs on the 11-page corpus)
     instead of overwriting it with coarse detector boxes (`122/129`).
   - Use `doc_title` / `paragraph_title` regions to emit Markdown headings even when font sizes are
     uniform (fixes 0 -> 9 Japanese headings on `mhlw-doc`).
   - Tag `header` / `footer` / `page_number` / `footnote` / `toc` regions cleanly in `Element`
     metadata (`to_json()` / `to_chunks()`) and suppress or demote running headers/footers in
     Markdown when requested.
   - Cross-check ML `table` boxes with geometric line/text table extraction (avoiding `pymupdf-layout`'s
     3/7 false-positive table regressions and preventing dense forms like `f1040` from collapsing
     into broken Markdown tables).
3. **Image-only scanned PDFs (paired with `Page.ocr`)**: When a page has zero embedded text,
   `Page.regions()` supplies page segmentation (e.g., 12 regions on vertical Japanese scan
   `bunka-kokugo-series-019-p4.pdf`) so callers can run region-aware OCR or reading-order sorting.

### Open Ergonomic Choice for the Owner

Should `markdown()` / `to_json()` / `to_chunks()` expose:
- **Choice 1 (recommended)**: `layout_engine: LayoutEngine | None = None` plus `use_layout: bool = False` (passing `use_layout=True` uses the cached default `LayoutEngine`, while passing `layout_engine=eng` enables layout using the caller's configured engine without needing both flags — mirroring `use_structure` naming requested in #1199).
- **Choice 2 (minimal)**: only `layout_engine: LayoutEngine | Literal["auto"] | None = None` (single parameter, avoids two interacting knobs).
