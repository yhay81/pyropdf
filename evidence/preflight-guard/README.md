# Iterative pre-flight nesting guard before `hayro` (issue #1197)

Evaluation of the [DESIGN.md §6](../../DESIGN.md#6-interpretation-contract) contingency for
[#15](https://github.com/yhay81/pyropdf/issues/15) (I2 bounded construction) and
[#740](https://github.com/yhay81/pyropdf/issues/740) (nested inline-image lookahead).

## 1. Upstream status (verified 2026-11-27)

Pinned `hayro` revision in `Cargo.toml`: `yhay81/hayro@394e4a0d37baf8af513f6ebda15f4bada85d6f23`
(based on `hayro` 0.9.0 / `4f7d16fa` + `LaurenzV/hayro#1734`). Latest published upstream release is
`hayro-v0.9.0` (2026-11-02).

| Path / Issue | Upstream PR | Status (2026-11-27) | In pinned `394e4a0d`? |
| --- | --- | --- | --- |
| Type 3 stitching functions & Type 4 `{ ... }` braces (#15) | `LaurenzV/hayro#1665` | Merged 2026-11-03 (post-0.9.0, unreleased) | No |
| Color space nesting (#15) | `LaurenzV/hayro#1735` | Merged 2026-11-03 (post-0.9.0, unreleased) | No |
| Content stream truncation signal (#15) | `LaurenzV/hayro#1666` | Open (Draft) | No |
| Object nesting >512 signal (#15) | `LaurenzV/hayro#1736` | Closed (parked pending `#1666`) | No |
| Nested inline-image end lookahead (#740) | `LaurenzV/hayro#1832` | Open | No |
| Quadratic inline-image lookahead (#17) | `LaurenzV/hayro#1667` | Open | No |
| Unbounded `TilingPattern::interpret` depth (new audit finding) | None filed yet | Unfixed upstream | No |
| Unbounded `Type3::render_glyph` depth (new audit finding) | None filed yet | Unfixed upstream | No |
| Cooperative fuel & cancellation (I5, #105) | `LaurenzV/hayro#1052` | Open (maintainer-owned) | No |

## 2. Inventory of crash, DoS, and truncation paths under `DocumentLimits.web()`

Reproduced in isolated child processes on `Darwin arm64`, Python 3.15.2, `pyropdf 0.4.1` via:

```bash
uv run --no-sync python evidence/preflight-guard/reproduce_crashes.py evidence/preflight-guard/results
```

Full raw results: [`results/crashes.md`](results/crashes.md) and [`results/crashes.json`](results/crashes.json).

| Path | Mechanism in pinned `hayro` (`394e4a0d`) | Minimal trigger under `DocumentLimits.web()` | Observed effect |
| --- | --- | --- | --- |
| Type 4 PostScript `{ ... }` (#15) | Recursive `parse_procedure_inner` (`hayro-interpret/src/function/type4.rs:417`); braces are inside stream bytes so `max_object_depth=64` does not apply | 40 KB (20k braces) or 100 KB (50k braces) via Shading `/Function`, Separation tint transform, or ExtGState `/TR` | **`SIGSEGV` (exit 139)** on `text`, `pixmap`, `content`, `svg` |
| Type 3 stitching function chain (#15) | Recursive `Function::new` ↔ `Type3::new` (`function/type3.rs:21`). 1-object cycles are broken by `hayro-syntax`'s `ReaderContext.parent_chain`, but acyclic chains of distinct indirect objects bypass it. (Type 2 is leaf-only.) | 20,000 indirect Type 3 objects (~2.2 MB PDF; 5,000 already crashes `pixmap`) | **`SIGSEGV` (exit 139)** on `text`, `pixmap`, `content`, `svg` |
| ColorSpace nesting chain (#15) | Recursive `ColorSpaceType::new_inner` ↔ `Indexed::new` / `Separation::new` / `DeviceN::new` / `Pattern` / `ICCBased /Alternate` (`color/mod.rs:164`). Acyclic indirect chains bypass `parent_chain` and `max_object_depth=64`. | 20,000 indirect `/Indexed` or `/Separation` (~1.3–1.6 MB), or 10,000 invalid `/ICCBased` `/Alternate` (~1.6 MB) | **`SIGSEGV` (exit 139)** on `text`, `pixmap`, `content`, `svg` |
| Nested inline images (#740) | `read_inline_image` → `starts_with_content` → `TypedIter::new` → `read_inline_image` (`hayro-syntax/src/content/mod.rs:292`) | `BI /W 1 /H 1 ID x EI EI ` × 22 (459 B), × 25 (461 B), × 200,000 (12 KB Flate) | **1.0s** (`n=22`), **7–12s** (`n=25`), **`SIGSEGV` (exit 139)** (`n=200k`) on `text`, `pixmap`, `content`, `operations` |
| **NEW**: Tiling pattern chain | `TilingPattern::new` increments `nesting_depth` (`pattern.rs:194`) and passes it to `Context::new_with` (`pattern.rs:234`), but `TilingPattern::interpret` **never checks** `nesting_depth >= MAX_NESTED_INTERPRETATION_DEPTH (50)` | 2,000 acyclic Type 1 tiling patterns (~466 KB PDF) | **`SIGSEGV` (exit 139)** on `pixmap` and `svg` |
| **NEW**: Type 3 glyph chain | `get_glyph` increments `nesting_depth` (`font/mod.rs:192`) and passes it to `Context::new_with` (`font/type3.rs:64`), but `Type3::render_glyph` **never checks** `nesting_depth >= MAX_NESTED_INTERPRETATION_DEPTH (50)` | 2,000 acyclic Type 3 fonts (~736 KB PDF) | **`SIGSEGV` (exit 139)** on `pixmap`, `content`, and `svg` |
| Content operand nesting >512 (#15/#1666) | `hayro-syntax` caps `[` / `<<` at `MAX_OBJECT_NESTING_DEPTH = 512` and silently stops `UntypedIter` | 600 nested `[` before `(after) Tj` (1.7 KB PDF) | **Silent truncation**: `text() == ""`, `content() == []`, `len(operations) == 0` |
| Controls already bounded | Indirect object nesting (`object-depth-65`); Form XObject & SoftMask cycles | 65 nested `[` in indirect object; self-referencing Form/SoftMask | `object-depth-65` raises `LimitError(code="object_depth")` at open; Form/SoftMask cycles stop at 50 via `begin_nested_interpretation` (silently truncated) |

None of the `SIGSEGV` crashes reach `InternalError` or `catch_unwind`; every one terminates the
Python worker process.

## 3. Iterative, budget-charged pre-flight prototype & 15-document corpus benchmark

`src/guard.rs` implements a zero-recursion pre-flight validator directly on `pyropdf-store`
(`Reads` + `Budget` + `BudgetVec` + `try_insert_set`) so that:

1. Every container and decoded stream is charged to the operation's F6 `Budget` and polls deadline
   and cancellation (`Budget::poll`).
2. `Reads` records the exact object read-set, enabling F4 `Memo` caching per immutable `Version`
   (0.00 ms on subsequent `text()` / `pixmap()` / `content()` calls on an unedited page). Cross-object
   memoization (`CheckedCache`) stores the **maximum entry depth** already proven safe for each
   indirect object (`HashMap<u32, usize>` via `try_insert_map`), not a boolean visited flag, so a
   shared DAG subgraph validated shallowly cannot bypass depth bounds when re-entered deeper.
3. Content streams are scanned by an iterative byte-level scanner *before* `hayro-syntax::content::UntypedIter`
   runs, closing `#740` (with whitespace boundary checks on both sides of `EI`) and content-operand
   silent truncation (`#1666`) without allocating `Value` trees. Image XObjects also walk `/SMask`
   and `/Mask` stream `/ColorSpace` entries iteratively.
4. Bounds match upstream hayro & Acrobat: `MAX_COLOR_SPACE_DEPTH = 32`, `MAX_FUNCTION_DEPTH = 32`,
   `MAX_TYPE4_BRACES = 256`, `MAX_STREAM_DEPTH = 50` (with cycle refusal),
   `MAX_CONTENT_OPERAND_DEPTH = 64`, `MAX_INLINE_LOOKAHEAD_CHAIN = 8`.

Reproduction command:

```bash
cargo run --release --locked --manifest-path evidence/preflight-guard/Cargo.toml -- \
  evidence/preflight-guard/results macos-arm64 tests/assets/real_world/*.pdf
```

Full per-document and per-page measurements: [`results/macos-arm64.md`](results/macos-arm64.md) and
[`results/macos-arm64.json`](results/macos-arm64.json).

### Summary (`macos-arm64`, Apple Silicon, best of 5 runs, 15 documents / 156 pages)

| Metric | Measured |
| --- | ---: |
| Synthetic adversarial cases refused (15 / 15, incl. DAG depth-bypass & Image `/SMask`) | **0.017 – 6.962 ms** |
| False positives on 15-document corpus (156 pages) | **0 / 156** |
| Corpus `render::raster` (72 dpi) total | **593.90 ms** |
| Corpus `extract::page_text` total (post-#1213 perf) | **104.06 ms** |
| Pre-flight guard cold total (empty cache per page) | **6.05 ms** (**1.02%** of render, **5.81%** of text) |
| Pre-flight guard cached total (shared depth-aware `CheckedCache`) | **5.77 ms** (**0.97%** of render) |
| Per-page guard median / p90 / max (cold) | **0.029 ms** / **0.042 ms** / **0.514 ms** (`f1040` p.1, 78 streams) |
| Peak F6 `Budget` memory charged across corpus | **578.2 KiB** (`f1040`; median doc < 32 KiB) |
| Max depths observed on corpus (vs guard bounds) | CS **2**/32, Fn **1**/32, Type4 **0**/256, Stream **4**/50, OpDepth **2**/64 |
| Prototype size (`src/guard.rs`) | **1,256 non-comment LOC** (1,414 total lines; single isolated module vs ~2,500 LOC in pylopdf) |

## 4. Decision options & recommendation

- **Option A (Recommended): Ship the documented temporary pre-flight guard under `DocumentLimits` (per DESIGN.md §6 contingency), while keeping `docs/en/status.md`'s untrusted-input caveat intact until I2, I5, and `vello_cpu` #725 ship.**
  - Run the guard before `hayro` interpretation (`render`, `svg`, `extract`, and `page.operations`) whenever `DocumentLimits` bounds nesting (active by default in `DocumentLimits.web()`, or opt-out via `max_object_depth=None`), memoized per immutable `Version` in `Memo` (F4) so repeated page operations pay **0 ms**.
  - Eliminates all 6 process-killing `SIGSEGV` vectors (including the two newly discovered `TilingPattern` and `Type3` chains that even merged `hayro#1665`/`#1735` do not cover) and `#740`'s exponential hang at **~1.1% render overhead** on first visit and **0%** on memo hits.
  - Strictly follows DESIGN.md §6's explicit contingency clause, F4, and F6, and raises `LimitError` instead of silently dropping objects/content.
- **Option A-fork (Not recommended as sole mitigation): Cherry-pick `#1665`, `#1735`, `#1832`, plus a new `TilingPattern`/`Type3` fix into `[patch.crates-io]` (`yhay81/hayro`).**
  - Prevents `SIGSEGV` with ~0% runtime overhead, but violates I2's refusal contract (`#1665`/`#1735` return `None` silently instead of `LimitError`) and deepens fork divergence against `#603`.
- **Option B: Wait strictly on upstream `hayro` releases (no interim guard).**
  - Zero temporary code to delete, matching the `#678` decision for I5 CPU fuel — **but** unlike `#678` (CPU time / worker timeouts), `#15`, `#740`, and the `TilingPattern`/`Type3` chains cause **uncatchable `SIGSEGV` worker crashes** on 12–100 KB inputs even under `DocumentLimits.web()`, and two of those paths do not yet have upstream PRs.

## 5. Residual gaps (why `docs/en/status.md` safety claim stays withheld)

Even if Option A is approved and shipped, full untrusted-input safety still requires:
1. **I5 fuel & cancellation ([#105](https://github.com/yhay81/pyropdf/issues/105), `LaurenzV/hayro#1052`)**: CPU exhaustion inside hayro interpretation, function evaluation, and tiling fan-out remains unbounded.
2. **`vello_cpu` `alpha_idx` panic ([#725](https://github.com/yhay81/pyropdf/issues/725))**: many long strokes trigger an unwinding Rust panic (caught as `InternalError`, not `SIGSEGV`).
3. **Quadratic inline-image lookahead ([#17](https://github.com/yhay81/pyropdf/issues/17), `LaurenzV/hayro#1667`)**: a single inline image containing tens of thousands of fake `EI ` bytes can still trigger quadratic CPU scanning unless the guard also caps candidate `EI` count per `BI`.
4. **Object nesting >512 when `check_at_open=False` (`LaurenzV/hayro#1736`)**: closed for `DocumentLimits.web()` (`check_at_open=True` + `max_object_depth=64`), still silently dropped by `hayro-syntax` if a caller disables `check_at_open` and `max_object_depth`.

## 6. Exact removal condition

List the pre-flight guard as a temporary engine workaround in [#7](https://github.com/yhay81/pyropdf/issues/7)
(linking `#15`, `#740`, and the new upstream `TilingPattern`/`Type3` depth-check issue). Remove the
guard module once pyropdf adopts a released `hayro` version containing:
1. Bounded CS / Function / Type 4 construction and truncation/nesting error signals (`#15`: `#1665`, `#1735`, `#1666`, `#1736` or equivalent);
2. Non-nested inline-image end lookahead (`#740`: `#1832`); and
3. `MAX_NESTED_INTERPRETATION_DEPTH` enforcement in `TilingPattern::interpret` and `Type3::render_glyph`.

Keep the child-process regression suite (300k Type 4 braces, 20k CS/Function chains, 2k TilingPattern/Type3 chains, `n=200k` inline images) permanently in `tests/`.
