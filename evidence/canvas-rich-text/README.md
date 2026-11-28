# Styled Rich Text Runs & BiDi Layout on `Canvas` (issue #1198)

Evaluation of **Option A** (adopting `parley 0.13.0`) vs **Option B** (building bounded styled-run +
UAX #9 BiDi layout directly on pyropdf's existing `harfrust 0.16` + `skrifa 0.50` + `read-fonts 0.46`
+ `unicode-bidi 0.3.18` + `unicode-linebreak 0.1.5` + `unicode-segmentation 1.13.3` +
`unicode-script 0.5.9` + `pdf-base14-metrics` stack), per [DESIGN.md §7](../../DESIGN.md#7-writing-and-editing)
and [§11](../../DESIGN.md#11-layout), Writing tracker [#249](https://github.com/yhay81/pyropdf/issues/249),
and Continuous tracks [#258](https://github.com/yhay81/pyropdf/issues/258).

## Executive summary & recommendation

**Recommend Option B (`harfrust 0.16` + `unicode-bidi 0.3.18` on pyropdf's existing crates).**

| Dimension | Option A (`parley 0.13.0`) | Option B (existing `harfrust` + `unicode-bidi` stack) |
| --- | --- | --- |
| Native `abi3` wheel delta (`macosx_arm64`, Zopfli-1) | **+367,663 B (+359.0 KiB)** (`std`, `opt-level="z"`); **+2,768,108 B (+2.640 MiB)** (`+complex-scripts`) | **+17,477 B (+17.1 KiB)** |
| 9.0 MiB native `abi3` wheel budget (#858) | Exceeds 256 KiB PR growth gate in `std`-only and consumes ~77–92% of `win_amd64`'s 464.9 KiB headroom; **`+complex-scripts` blows 9.0 MiB on all 5 targets** | Passes with **448 KiB** headroom on `win_amd64` (15× under PR growth gate) |
| `wasm32-unknown-emscripten` build & size | Compiles cleanly; standalone `wasm32` `parley std` = **467.8 KiB** zlib-9 (`+complex-scripts` = **2.761 MiB**) | Compiles cleanly; **~17 KiB** delta (0 new crates) |
| `Cargo.lock` growth & crate duplication | **+32 packages** (`std`) / **+56** (`system`); **duplicates `harfrust` (`0.15.0` alongside `0.16.1`)**, adds `fontique 0.13`, `memmap2`, 5 ICU4X `compiled_data` crates | **+0 packages**, **0 new duplicates** |
| `cargo-audit --deny warnings` | Clean (both `std` and `system`) | Clean |
| DESIGN.md F6 budget & `crates/clippy.toml` | **Not budgetable**: 0 `try_reserve` calls and **463 infallible `Vec`/`String`/`HashMap` growths** across 75 `.rs` files; no `Budget::poll()` / cancellation / glyph-cap hook | **100% F6 compliant**: `BudgetVec`, `Budget::poll()`, `ShapedGlyphCount`, `TextLineCount`, `MemorySize` |
| DESIGN.md §10.1 Concurrency (no font registry) | `default` (`system`) pulls OS font backends (`yeslogic-fontconfig-sys` C lib on Linux, CoreText, DirectWrite); requires `default-features = false` and per-call `FontContext::new()` | Zero OS font discovery; pure function of passed `Font` objects |
| Standard 14 PDF fonts (Helvetica/Times/Courier) | **Unsupported** (`fontique` requires OpenType bytes -> forces two separate layout engines) | **Unified**: Standard 14 AFM and OpenType spans mix freely on one line |
| Multilingual BiDi + line wrapping quality | Excellent (UAX #9 + UAX #14 + ICU4X) | Identical line counts & visible glyphs across all 5 test cases; direct `ToUnicode`/`/ActualText` cluster preservation |

## 1. Native `abi3` wheel-size & `wasm32` measurements

Measured on `macosx_arm64` (`Darwin arm64`, Rust `1.100.0`, `maturin 1.16.0`) using the exact release
flags (`tools/native-wheel-args.txt`: `--release --compression-level 9`, fat LTO, `strip = "symbols"`)
plus Zopfli-1 repacking (`tools/repack_wheel.py`) and `tools/wheel_size.py --abi abi3 --base`. Every
variant wired a live PyO3 function in `pyropdf._core` so fat LTO retained the full layout pipeline.
Even under the most favorable size profile (`opt-level = "z"` for every `parley`, `fontique`,
`parlance`, and `icu_*` crate), `parley` costs **21× more wheel bytes** than Option B.

Full raw measurements & SHA-256 digests: [`results/wheel-delta.md`](results/wheel-delta.md) and
[`results/wheel-delta.json`](results/wheel-delta.json).

### Measured `macosx_arm64` `abi3` delta

| Variant | Delivered wheel (B) | Wheel growth | Extension growth | `Cargo.lock` pkgs | `cargo-audit` | `wasm32-emscripten` |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Baseline (`main`) | 7,738,445 | 0 B | 0 B | 220 | clean | OK |
| **Option B** (`harfrust 0.16` + `unicode-bidi`) | **7,755,922** | **+17,477 B (+17.1 KiB)** | +17,476 B | **220 (+0)** | clean | OK |
| **Option A1** (`parley 0.13` `std`, `opt-level="z"`) | 8,106,108 | **+367,663 B (+359.0 KiB)** | +363,709 B | 252 (+32) | clean | OK |
| **Option A2** (`parley 0.13` `std+complex-scripts`, `opt-level="z"`) | 10,506,553 | **+2,768,108 B (+2,703.2 KiB)** | +2,764,155 B | 252 (+32) | clean | OK |

### 5-Platform 9.0 MiB (`9,437,184` B) `abi3` headroom projection

Anchored to the published `v0.4.1` Zopfli-repacked `abi3` wheels (`gh release view v0.4.1`). Because
`x86_64` code compresses ~5–15% larger than `arm64` (note `win_amd64` / `macosx_arm64` = `1.158×`),
adding the raw `macosx_arm64` byte delta is a **conservative lower bound** for `win_amd64` and
`manylinux_x86_64`:

| Target (`v0.4.1`) | Current (B) | Headroom | +Option B (proj.) | +Option A1 `parley std` (proj.) | +Option A2 `+complex-scripts` (proj.) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `win_amd64` | 8,961,169 | 464.9 KiB | 8.563 MiB (+448 KiB) PASS | 8.897 MiB (+106 KiB) PASS* | 11.186 MiB (**-2,238 KiB OVER**) |
| `manylinux_x86_64` | 8,900,191 | 524.4 KiB | 8.505 MiB (+507 KiB) PASS | 8.839 MiB (+165 KiB) PASS* | 11.128 MiB (**-2,179 KiB OVER**) |
| `macosx_x86_64` | 8,566,157 | 850.6 KiB | 8.186 MiB (+834 KiB) PASS | 8.520 MiB (+492 KiB) PASS | 10.809 MiB (**-1,853 KiB OVER**) |
| `manylinux_aarch64` | 8,324,603 | 1,086.5 KiB | 7.956 MiB (+1,069 KiB) PASS | 8.290 MiB (+727 KiB) PASS | 10.579 MiB (**-1,617 KiB OVER**) |
| `macosx_arm64` | 7,735,902 | 1,661.4 KiB | 7.394 MiB (+1,644 KiB) PASS | 7.728 MiB (+1,302 KiB) PASS | 10.017 MiB (**-1,042 KiB OVER**) |

\* At `win_amd64`'s `1.158×` compression ratio, Option A1 projects to ~+416 KiB, leaving < 50 KiB
headroom below the hard 9.0 MiB ceiling — and immediately failing the 256 KiB PR growth gate.

### Standalone `wasm32-unknown-unknown` & `system` feature probes

- `parley 0.13.0` (`default-features = false, features = ["std"]`, `opt-level="s"`, fat LTO, stripped):
  - `wasm32-unknown-unknown`: raw `1,249,787` B (`1.192` MiB), zlib-9 **`467,750` B (`0.446` MiB)**
  - `aarch64-apple-darwin`: raw `1,460,816` B (`1.393` MiB), zlib-9 **`699,097` B (`0.667` MiB)**
- `parley 0.13.0` (`+complex-scripts`):
  - `wasm32-unknown-unknown`: raw `5,041,278` B (`4.808` MiB), zlib-9 **`2,895,027` B (`2.761` MiB)**
  - `aarch64-apple-darwin`: raw `5,275,120` B (`5.031` MiB), zlib-9 **`3,157,060` B (`3.011` MiB)**
- `parley 0.13.0` (`default` / `system` feature):
  - `aarch64-apple-darwin`: raw `1,512,624` B (`1.443` MiB), zlib-9 **`726,606` B (`0.693` MiB)** (+27.5 KiB on macOS; pulls `yeslogic-fontconfig-sys` C library on Linux and `windows` DirectWrite on Windows).

Reproduce wheel-size measurements:

```bash
python3 evidence/canvas-rich-text/measure_wheels.py \
  --baseline /tmp/baseline-wheel --work-dir /tmp/work-wheels --out-dir evidence/canvas-rich-text/results
```

## 2. Dependency overlap, F6 budgetability & §10.1 Concurrency audit

1. **Duplicate shaper in `parley 0.13.0`**: `parley_engine 0.13.0` pins `harfrust = "0.15.0"` and
   `skrifa = "0.49.0"`, while `pyropdf-engine` uses `harfrust = "0.16.0"` and `skrifa = "0.50.0"`.
   Adding `parley` compiles and links **two major-incompatible copies of `harfrust`** into
   `pyropdf._core`, plus `fontique 0.13`, `parlance 0.1.1`, `parley_data 0.13`, `parley_emoji 0.13`,
   `memmap2`, and five ICU4X `compiled_data` crates (`icu_properties`, `icu_normalizer`,
   `icu_segmenter`, `icu_locale_fallback`, `icu_locale_core`).
2. **F6 fallible/budgeted allocations (`crates/clippy.toml`)**: Auditing the 75 source files of
   `parley 0.13.0`, `parley_engine 0.13.0`, and `fontique 0.13.0` found **0 `try_reserve` calls**
   and **463 infallible `Vec`/`String`/`HashMap` growth calls**, with no cooperative cancellation,
   deadline, memory-budget, or glyph-count hooks. By contrast, Option B (`src/bidi_rich.rs`) builds
   every input-derived buffer on `BudgetVec`, polls `Budget::poll()`, and enforces
   `LimitCode::ShapedGlyphCount`, `LimitCode::TextLineCount`, and `LimitCode::MemorySize` directly.
3. **DESIGN.md §10.1 Concurrency**: `parley`'s default `system` feature enumerates OS fonts via
   Fontconfig / CoreText / DirectWrite, violating pyropdf's "no process-global font registry" rule.
   Disabling `system` works (`default-features = false, features = ["std"]`), but requires creating a
   fresh `FontContext` per call or managing a custom per-canvas cache, losing `parley`'s main
   ergonomic advantage while still carrying `fontique` + `memmap2` + duplicate `harfrust`.
4. **Standard 14 PDF fonts**: `Canvas.text()` and `Canvas.textbox()` support unembedded Standard 14
   fonts via `pdf-base14-metrics`. `parley` cannot measure or lay out AFM-only fonts, so adopting
   `parley` would force pyropdf to maintain two separate rich-text layout implementations (or forbid
   mixing Standard 14 and OpenType spans). Option B handles both in one unified pipeline.
5. **PDF `ToUnicode` / `/ActualText` preservation**: `crates/pyropdf-engine/src/fonts/shape.rs` and
   `python/pyropdf/_canvas/_text.py` (`_GlyphRun`) already implement cluster-to-Unicode mapping and
   RTL `/ActualText` emission tested across MuPDF/poppler/Xpdf (`evidence/cluster-text`). Option B
   reuses that exact machinery directly.

## 3. Prototype correctness & performance benchmark

Standalone benchmark crate: `evidence/canvas-rich-text/` (using the repository's committed test fonts
`NotoSansJP-subset.otf`, `NotoSansHebrew-subset.ttf`, `NotoSansArabic-Variable-subset.ttf`, and
`variable-latin.ttf`, zero system fonts required).

Reproduce:

```bash
cargo run --release --locked --manifest-path evidence/canvas-rich-text/Cargo.toml -- \
  evidence/canvas-rich-text/results
```

Full results: [`results/benchmark.md`](results/benchmark.md) and [`results/benchmark.json`](results/benchmark.json).

| Case | Option A `parley` (ms) | A lines / glyphs | Option B `harfrust+bidi` (ms) | B lines / glyphs | B peak F6 Budget (KiB) | B rules / links |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `styled-latin-justified` | 0.021 | 2 / 36 | 0.049 | 2 / 35 | 6.1 | 3 / 1 |
| `bidi-ltr-with-hebrew-and-digits` | 0.018 | 1 / 25 | 0.057 | 1 / 25 | 3.8 | 1 / 1 |
| `bidi-rtl-arabic-wrapped` | 0.020 | 2 / 35 | 0.060 | 2 / 34 | 4.4 | 1 / 1 |
| `multilingual-cjk-hebrew-arabic` | 0.021 | 3 / 35 | 0.072 | 3 / 33 | 6.5 | 2 / 1 |
| `long-bidi-500-tokens` | 0.361 | 33 / 1,760 | 0.866 | 33 / 1,727 | 210.7 | 0 / 0 |

Notes:
- Line counts match **5 / 5** identically. The 1-glyph-per-line difference (`1,760 - 1,727 = 33` over
  33 lines) is expected: Option B strips trailing line-end whitespace per PDF textbox convention,
  whereas `parley` retains trailing whitespace glyphs for interactive cursor editing.
- The Option B prototype shapes once for wrapping and once for visual run emission (~0.05–0.87 ms);
  caching shaped cluster slices during production implementation removes the second pass.
- Option B verified deterministic F6 refusals (`ShapedGlyphCount`, `TextLineCount`, `MemorySize`)
  and mixed Standard-14 Helvetica + OpenType RTL Hebrew spans on a single line with link hit-box.

## 4. Draft Python API specification

See [`api-spec.md`](api-spec.md) for the complete DESIGN.md §10 specification (`TextSpan` with
`render_mode`, `TextAlign`, `TextDirection`, `base_direction` preserving `Canvas.text()`/`textbox()`'s
existing `direction: Literal["horizontal", "vertical"]`, `fallback_fonts`, and atomic `/Link`
annotation emission inside the `Canvas` transaction).

## 5. Proposed follow-up implementation issues (once Option B is approved)

1. **`pyropdf-engine`: bounded multi-style & UAX #9 BiDi paragraph layout** — Add
   `crates/pyropdf-engine/src/fonts/rich_layout.rs` (extending `ShapingFace`, `standard::scan`, and
   `pdf-base14-metrics`) with UAX #9 P2/P3 base direction (`auto`/`ltr`/`rtl`), UAX #14 wrapping +
   UAX #29 emergency breaks, per-line UAX #9 L1/L2 reordering, mixed font-size line metrics,
   `start`/`end`/`justify` alignment, decoration metrics (`post`/`OS/2`), link hit-boxes, and full
   F6 `BudgetVec`/`Budget::poll()` compliance + property tests (F9).
2. **`pyropdf-python` + `Canvas`: `TextSpan`, styled `Canvas.text()`/`textbox()`, decorations, and
   atomic `/Link` annotations** — Expose `TextSpan`, `TextAlign`, `TextDirection`; extend
   `Canvas.text()` and `Canvas.textbox()` to accept `str | Sequence[TextSpan]`; emit per-run paint,
   underline/strikethrough paths (verified round-trip with `Page.layout(styles=True)`), and `/Link`
   annotations in the same `Canvas` transaction and undo step; full multilingual Arabic/Hebrew/CJK/Latin
   round-trip tests (extraction, rendering, link hit-boxes, limits).
3. **Update `#258` continuous track entry for `parley`** — Record that `parley 0.13.0` was evaluated
   in `#1198` and declined for core `Canvas` rich text due to the 9.0 MiB wheel budget (+359 KiB
   minimum / +2.64 MiB with complex scripts), duplicate `harfrust`, and non-F6 allocations; keep
   `fulgur`/`Blitz` HTML-to-PDF reassessment separate on `#258`'s 2027-01 watchlist.
