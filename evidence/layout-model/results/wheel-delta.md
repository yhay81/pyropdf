# Measured Core `abi3` Wheel-Size Delta (`macosx_arm64`, Zopfli-1 repacked)

Baseline built from current `main` with `tools/native-wheel-args.txt` + `tools/repack_wheel.py`.
Both variants wire a live PyO3 `_SpikeLayoutEngine` class in `pyropdf._core` so fat LTO retains
the full model load, 800x800 bilinear preprocessing, RTen inference, and box-extraction pipeline.

| Variant | Delivered wheel (B) | Wheel growth (B / KiB) | Extension growth (B / KiB) | `Cargo.lock` pkgs | `cargo-audit` | `wasm32-emscripten` |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Baseline (`main`, `rten_format`) | 7,755,870 | 0 (0.0 KiB) | 0 (0.0 KiB) | 220 | clean (0 warnings/advisories) | OK |
| **Recommended**: `LayoutEngine` over existing `rten_format` (0 new crates/features) | 7,764,128 | +8,258 (+8.1 KiB) | +8,261 (+8.1 KiB) | 220 | clean (0 warnings/advisories) | OK |
| Alternative: `LayoutEngine` + `rten` `onnx_format` feature (`rten-onnx`) | 7,845,147 | +89,277 (+87.2 KiB) | +89,187 (+87.1 KiB) | 221 | clean (0 warnings/advisories) | OK |

## Standalone `rten 0.28.0` `onnx_format` Feature Overhead (`arm64` & `wasm32`)

| Target | `rten_format` only (raw / zlib-9) | `+onnx_format` delta (raw / zlib-9) |
| --- | ---: | ---: |
| `aarch64-apple-darwin` | 1,852,720 B / 672,308 B | +150,464 B / +71,482 B (+69.8 KiB) |
| `wasm32-unknown-unknown` | 1,596,605 B / 510,469 B | +162,470 B / +45,893 B (+44.8 KiB) |

## 5-Platform 9.0 MiB (`9,437,184` B) Native `abi3` Budget Projection

Anchored to published `v0.4.1` Zopfli-repacked `abi3` wheels (`gh release view v0.4.1`).

| Target (`v0.4.1`) | Current (B) | Headroom (KiB) | +`rten_format` wrapper (proj.) | +`onnx_format` (proj.) |
| --- | ---: | ---: | ---: | ---: |
| `win_amd64` | 8,961,169 | 464.9 KiB | 8.554 MiB (+457 KiB) PASS | 8.631 MiB (+378 KiB) PASS |
| `manylinux_x86_64` | 8,900,191 | 524.4 KiB | 8.496 MiB (+516 KiB) PASS | 8.573 MiB (+437 KiB) PASS |
| `macosx_x86_64` | 8,566,157 | 850.6 KiB | 8.177 MiB (+843 KiB) PASS | 8.254 MiB (+763 KiB) PASS |
| `manylinux_aarch64` | 8,324,603 | 1086.5 KiB | 7.947 MiB (+1078 KiB) PASS | 8.024 MiB (+999 KiB) PASS |
| `macosx_arm64` | 7,735,902 | 1661.4 KiB | 7.385 MiB (+1653 KiB) PASS | 7.463 MiB (+1574 KiB) PASS |
