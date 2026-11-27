# Text and word extraction performance (#1178)

Profile and optimization of `page.text()`, `doc.text()`, `page.words()`,
`page.layout()`, and `page.search()` across the 15-document real-world corpus
(`tests/assets/real_world/`), closing the Linux x86-64 speed gap against
PyMuPDF 2.1 while keeping the native `abi3` wheel well under the 9.0 MiB
budget (#858) and preserving bit-for-bit identical output.

## Baseline (published v0.4.1 Linux x86-64 vs PyMuPDF 2.1)

From the published `v0.4.1` Linux x86-64 release comparison
(`comparison-v0.4.1.zip`, Linux x86-64, CPython 3.13.18, PyMuPDF 2.1 / MuPDF
1.28.6, best of 5 runs on fresh opens):

| Operation | PyMuPDF 2.1 (ms) | pyropdf v0.4.1 (ms) | Ratio (pyropdf / PyMuPDF) |
| --- | ---: | ---: | ---: |
| `text` | 145.581 | 203.202 | 1.40x |
| `words` | 194.501 | 326.528 | 1.68x |
| `search` | 207.013 | 200.431 | 0.97x |

Two multi-page documents accounted for **85%** of total `text` time and
**88%** of total `words` time:

| Document | Pages | `text` PyMuPDF (ms) | `text` v0.4.1 (ms) | Ratio | `words` PyMuPDF (ms) | `words` v0.4.1 (ms) | Ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bill-hr815.pdf` | 110 | 82.254 | 114.776 | 1.40x | 125.789 | 211.514 | 1.68x |
| `usrguide.pdf` | 27 | 22.751 | 57.914 | 2.55x | 27.104 | 74.982 | 2.77x |
| `patent-us223898.pdf` | 4 | 3.219 | 7.164 | 2.23x | 3.722 | 10.357 | 2.78x |
| `f1040.pdf` | 2 | 21.895 | 9.659 | 0.44x | 22.194 | 13.195 | 0.59x |
| `mhlw-doc.pdf` | 2 | 6.700 | 6.644 | 0.99x | 6.691 | 6.480 | 0.97x |
| `nics-background-checks-2015-11.pdf` | 1 | 3.768 | 3.743 | 0.99x | 4.127 | 5.641 | 1.37x |
| `senate-expenditures.pdf` | 1 | 3.168 | 2.814 | 0.89x | 3.353 | 3.742 | 1.12x |
| Remaining 8 documents | 10 | 1.826 | 0.488 | 0.27x | 1.521 | 0.617 | 0.41x |

## Root-cause CPU profile

Sampling symbol-enabled release builds (`CARGO_PROFILE_RELEASE_STRIP=none
CARGO_PROFILE_RELEASE_DEBUG=line-tables-only`) on `usrguide.pdf` and
`bill-hr815.pdf` identified five dominant bottlenecks:

1. **Per-page font re-parsing (~65% of `usrguide.pdf` `text`, ~11% of
   `bill-hr815.pdf` `text`)**: `collect_page_glyphs` created a brand new
   `hayro_interpret::InterpreterCache` and `Stretches` on every page, so
   shared Type 1 / TrueType / CFF font programs and `ToUnicode` CMaps were
   decompressed and parsed once per page instead of once per bridged document
   version.
2. **Repeated per-glyph Unicode & advance lookups (~16% of `bill-hr815.pdf`
   `text`)**: `OutlineGlyph::as_unicode()` and `advance_width()` ran
   `hayro_font::glyph_name_to_unicode` (`phf::Map` lookup) on every glyph
   occurrence even though both are pure functions of `(font, char_code)`.
3. **Python `Word` construction & unused span conversion (~36% of
   `bill-hr815.pdf` `words`)**: `Page.words()` called `_core.page_layout`,
   cloning the entire `Vec<BlockTuple>` and converting every `Span` (font
   name, style tuple, quad) to Python tuples only to discard them, then
   instantiated frozen `@dataclass` `Word(...)` objects in Python (`__init__`
   kwargs + temporary tuples + CPython GC pressure).
4. **Line clustering, ordering, and per-glyph overhead (~27% of
   `bill-hr815.pdf` `text`)**:
   - `narrow_gap_interval` scanned glyph text (`has_narrow_column_text`)
     before checking whether the geometric gap met `NARROW_COLUMN_GAP`, and
     `narrow_column_boundary` scanned every line twice.
   - `cluster_lines` sorted arena indexes with indirect `&arena[idx]` loads
     into ~112-byte `GlyphRecord`s, and `materialize_lines` allocated an
     intermediate `Vec<Option<GlyphRecord>>` copy.
   - `GlyphRecord` cloned `FontInfo` (`Arc<str>`) per glyph; `may_be_stretched`
     and `collect_glyph` called `hypot()`/`sqrt()` even for axis-aligned
     matrices; `run_font_key` SipHashed every run; and `collect_glyph` called
     `Budget::charge` twice per glyph (hitting `Budget::poll` on every glyph).
5. **`Cargo.toml` `opt-level = "s"` evaluation**: Prior matched builds in
   [`evidence/wheel-size/README.md`](../wheel-size/README.md) measured that
   size-optimizing `pyropdf-engine`, `read-fonts`, `harfrust`, and `rten`
   added only +7.0–7.4% to text extraction while saving 1.26–1.92 MiB of
   compressed wheel size. Keeping `Cargo.toml` at `opt-level = "s"` preserves
   the 9.0 MiB wheel budget while the algorithmic and binding fixes deliver
   over 2x speedup.

## Optimizations implemented

- **Thread-local `ExtractCached` (`crates/pyropdf-engine/src/extract/text.rs`)**:
  Reuses `InterpreterCache`, `Stretches`, and a bounded (`MAX_CACHED_EXTRACT_FONTS
  = 256`) `Vec<(u128, CachedFont)>` per-font `char_code -> (Unicode, bidi_flags,
  advance_width)` table across pages of the same immutable `Arc<Bridged>`
  (mirroring `render::Cached`, verified under Miri by
  `extract::tests::miri_smoke_extract_with_cache_replacement_and_tls_drop`).
  `exact_bboxes=True` continues to use a fresh cache so fallback font
  watchers observe every unembedded font.
- **Compact `GlyphRecord` & fast glyph collection (`text.rs`, `model.rs`,
  `stretch.rs`)**:
  Replaced per-glyph `FontInfo` clones with `font_id: u32` + precomputed
  `(extent_asc, extent_desc)` multipliers and a page-local `Vec<FontInfo>`;
  added an 8-entry `(ptr, len) -> cache_key` cache in `ContentCollector`;
  added `b == 0 && c == 0` fast paths in `may_be_stretched` and
  `collect_glyph`; amortized `Budget::charge` over 64-glyph / 256-byte chunks
  capped to `budget.remaining(...)` with `Drop` release so tight limits refuse
  at the exact offending glyph/byte.
- **Faster clustering, column scan, and layout assembly (`text.rs`,
  `text/layout.rs`)**:
  Sorted compact `(y, x, index)` keys in `cluster_lines`; moved records
  directly out of `arena` in `materialize_lines` with `GlyphRecord::placeholder()`;
  added an `is_sorted()` fast path in `split_overlapping_paint_layers`; checked
  geometry before text in single-pass `single_narrow_gap`; precomputed
  `line_main_horizontal` once per line and combined `glyphs_bbox_and_quad`
  in `layout.rs`; appended directly into `out` via `assemble_text_into`.
- **Direct PyO3 `page_words` and zero-clone `page_text`
  (`crates/pyropdf-python/src/text.rs`, `python/pyropdf/_page.py`, `_text.py`)**:
  `page_text` constructs `PyString` directly from the memoized `Arc<PageResult>`
  without cloning `String`. `_core.page_words` shares `PageKind::Layout(key,
  false)` with `page.layout()` and constructs `pyropdf.Word` and `Rect`
  directly in Rust via Python 3.11+ Limited API (`PyOnceLock`-cached
  constructors/slot setters and one reused 4-element `PyList` per call),
  eliminating `blocks.clone()`, span tuple conversion, and the Python loop.

## Results (macOS arm64 release wheels)

Measured on macOS arm64, Python 3.13.18, best of 5 runs per document on fresh
opens, comparing baseline `main` (`423cddee`) against this branch using
`profile_text.py` (`before-macos-arm64.json`, `after-macos-arm64.json`,
`pymupdf-macos-arm64.json`, `results-macos-arm64.json`):

| Operation | Before (ms) | After (ms) | Speedup | PyMuPDF 2.1 (ms) | Ratio (before -> after) |
| --- | ---: | ---: | ---: | ---: | --- |
| `text` | 242.57 | 109.46 | **2.22x** | 404.31 | 0.60x -> **0.27x** |
| `doc.text()` | 236.76 | 103.35 | **2.29x** | — | — |
| `words` | 325.77 | 148.61 | **2.19x** | 710.84 | 0.46x -> **0.21x** |
| `layout` | 346.86 | 211.10 | **1.64x** | — | — |
| `search` | 222.80 | 108.13 | **2.06x** | 546.52 | 0.41x -> **0.20x** |

Key per-document improvements:

| Document | `text` before -> after | Speedup | `words` before -> after | Speedup |
| --- | --- | ---: | --- | ---: |
| `bill-hr815.pdf` (110p) | 142.13 ms -> 67.70 ms | **2.10x** | 203.32 ms -> 99.19 ms | **2.05x** |
| `usrguide.pdf` (27p) | 67.98 ms -> 18.97 ms | **3.58x** | 81.63 ms -> 23.77 ms | **3.43x** |
| `patent-us223898.pdf` (4p) | 8.27 ms -> 4.91 ms | **1.68x** | 10.93 ms -> 5.86 ms | **1.87x** |
| `f1040.pdf` (2p) | 10.42 ms -> 7.61 ms | **1.37x** | 13.65 ms -> 8.67 ms | **1.57x** |
| `mhlw-doc.pdf` (2p) | 7.42 ms -> 5.28 ms | **1.41x** | 7.59 ms -> 5.18 ms | **1.47x** |
| `nics-background-checks-2015-11.pdf` | 3.31 ms -> 2.48 ms | **1.33x** | 4.91 ms -> 3.15 ms | **1.56x** |
| `senate-expenditures.pdf` | 2.58 ms -> 2.10 ms | **1.23x** | 3.28 ms -> 2.37 ms | **1.38x** |

- **Output parity**: **0 / 15** SHA-256 digest mismatches across `text`,
  `words` (text, exact bbox, block, line, index), and `search("the")`.
- **Wheel size**: macOS arm64 `abi3` wheel grew by **+11,508 bytes
  (+0.011 MiB)** (`8,261,073 B` -> `8,272,581 B`, `7.889 MiB`, well under the
  9.0 MiB limit and 0.25 MiB PR growth allowance).
- **Linux x86-64 expectation**: All changes are platform-independent (cache
  reuse, algorithmic reductions, and PyO3 object-construction avoidance).
  Applying the per-document speedups to the `v0.4.1` Linux x86-64 baseline
  projects `text` from 203.2 ms (`1.40x`) to ~100 ms (`~0.69x` PyMuPDF 2.1)
  and `words` from 326.5 ms (`1.68x`) to ~153 ms (`~0.79x` PyMuPDF 2.1).
  Re-running `profile_text.py` or `evidence/pymupdf-comparison/release.py` on
  the Linux x86-64 release runner verifies the exact Linux numbers directly.

## Remaining follow-up opportunities (out of scope for #1178)

Post-optimization sampling of `bill-hr815.pdf` shows two remaining hotspots
outside `#1178`'s paths:

- `hayro_font::Font::map_code -> read_fonts::Font::glyph_by_name` (~18% of
  remaining `bill-hr815.pdf` `words`): Type 1 fonts resolve glyph names via
  linear string comparison on every character code instead of caching
  `[Option<GlyphId>; 256]` per font (upstream `hayro-font`).
- `pyropdf_store::budget::Budget::poll` (~1–2% after chunking; previously ~4%):
  `self.tightened.contains(&true)` invokes `memchr_aligned` on every poll
  even when no limit was tightened (track with a single `bool` flag in
  `pyropdf-store`).

## Reproduce

```bash
# Measure baseline and candidate pyropdf release wheels in isolated venvs:
python evidence/text-extraction-perf/profile_text.py measure \
    --engine pyropdf --runs 5 --out before.json tests/assets/real_world/*.pdf
python evidence/text-extraction-perf/profile_text.py measure \
    --engine pyropdf --runs 5 --out after.json tests/assets/real_world/*.pdf

# Optionally measure PyMuPDF 2.1 in an isolated venv:
python evidence/text-extraction-perf/profile_text.py measure \
    --engine pymupdf --runs 5 --out pymupdf.json tests/assets/real_world/*.pdf

# Compare timings and verify 0 SHA-256 output parity mismatches:
python evidence/text-extraction-perf/profile_text.py compare \
    --before before.json --after after.json --pymupdf pymupdf.json \
    --out results.json
```
