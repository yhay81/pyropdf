# Measured Native `abi3` Wheel-Size Delta (`macosx_arm64`, Zopfli-1 repacked)

Baseline built from current `main` with `tools/native-wheel-args.txt` + `tools/repack_wheel.py`.
Every variant wires a live PyO3 export so fat LTO retains the full layout pipeline.

| Variant | Delivered wheel (B) | Wheel growth (B / KiB) | Extension growth (B / KiB) | `Cargo.lock` pkgs | `cargo-audit` | `wasm32-emscripten` |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Baseline (`main`) | 7,738,445 | 0 (0.0 KiB) | 0 (0.0 KiB) | 220 | clean (0 warnings/advisories) | OK |
| Option B (`harfrust 0.16` + `unicode-bidi`, 0 new crates) | 7,755,922 | +17,477 (+17.1 KiB) | +17,476 (+17.1 KiB) | 220 | clean (0 warnings/advisories) | OK |
| Option A1 (`parley 0.13` `std`, `opt-level="z"`) | 8,106,108 | +367,663 (+359.0 KiB) | +363,709 (+355.2 KiB) | 252 | clean (0 warnings/advisories) | OK |
| Option A2 (`parley 0.13` `std+complex-scripts`, `opt-level="z"`) | 10,506,553 | +2,768,108 (+2703.2 KiB) | +2,764,155 (+2699.4 KiB) | 252 | clean (0 warnings/advisories) | OK |

## 5-Platform 9.0 MiB (`9,437,184` B) Native `abi3` Budget Projection

Anchored to published `v0.4.1` Zopfli-repacked `abi3` wheels (`gh release view v0.4.1`).
Because `x86_64` wheels compress ~5–15% larger than `arm64` for identical code/data, applying the
`macosx_arm64` byte delta directly is a **conservative lower bound** for `win_amd64` and `manylinux_x86_64`.

| Target (`v0.4.1`) | Current (B) | Headroom (KiB) | +Option B (proj.) | +Option A1 `parley std` (proj.) | +Option A2 `+complex-scripts` (proj.) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `win_amd64` | 8,961,169 | 464.9 KiB | 8.563 MiB (+448 KiB) PASS | 8.897 MiB (+106 KiB) PASS | 11.186 MiB (-2238 KiB) **OVER** |
| `manylinux_x86_64` | 8,900,191 | 524.4 KiB | 8.505 MiB (+507 KiB) PASS | 8.839 MiB (+165 KiB) PASS | 11.128 MiB (-2179 KiB) **OVER** |
| `macosx_x86_64` | 8,566,157 | 850.6 KiB | 8.186 MiB (+834 KiB) PASS | 8.520 MiB (+492 KiB) PASS | 10.809 MiB (-1853 KiB) **OVER** |
| `manylinux_aarch64` | 8,324,603 | 1086.5 KiB | 7.956 MiB (+1069 KiB) PASS | 8.290 MiB (+727 KiB) PASS | 10.579 MiB (-1617 KiB) **OVER** |
| `macosx_arm64` | 7,735,902 | 1661.4 KiB | 7.394 MiB (+1644 KiB) PASS | 7.728 MiB (+1302 KiB) PASS | 10.017 MiB (-1042 KiB) **OVER** |
