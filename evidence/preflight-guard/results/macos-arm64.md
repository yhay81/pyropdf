# Pre-flight nesting guard benchmark (`macos-arm64`, best of 5 runs)

## Synthetic adversarial verification (guard before hayro)

| Case | PDF bytes | Guard verdict | Guard time (ms) |
| --- | ---: | --- | ---: |
| `type4-braces-50000` | 100656 | `type4_braces: Type 4 PostScript procedure exceeded 256 nested braces` | 0.492 |
| `type3-function-cycle` | 649 | `function_cycle: cyclic function graph at object 6` | 0.026 |
| `type3-function-chain-5000` | 553458 | `function_depth: function nesting exceeded 32 levels` | 0.604 |
| `cs-indexed-cycle` | 519 | `color_space_cycle: cyclic color space at object 5` | 0.019 |
| `cs-icc-alternate-cycle` | 607 | `color_space_cycle: cyclic color space at object 5` | 0.021 |
| `cs-indexed-chain-5000` | 323332 | `color_space_depth: color space nesting exceeded 32 levels` | 0.473 |
| `inline-image-nested-200000` | 4800416 | `inline_image_lookahead: nested inline-image lookahead hazard exceeded 8 stray EI triggers (#740)` | 0.717 |
| `tiling-pattern-cycle` | 684 | `stream_cycle: cyclic nested stream at object 5` | 0.029 |
| `tiling-pattern-chain-2000` | 466029 | `stream_nesting: nested streams exceeded 50 levels` | 0.452 |
| `type3-glyph-cycle` | 798 | `stream_cycle: cyclic nested stream at object 6` | 0.027 |
| `type3-glyph-chain-2000` | 736020 | `stream_nesting: nested streams exceeded 50 levels` | 0.727 |
| `form-xobject-cycle` | 588 | `stream_cycle: cyclic nested stream at object 5` | 0.018 |
| `content-operand-depth-600` | 1614 | `content_operand_depth: content operand nesting exceeded 64 levels` | 0.011 |

## 15-document corpus overhead

All 15 documents under `tests/assets/real_world/` validated under `DocumentLimits::web()` with **0 false positives**.

| Document | Pages | Render 72 dpi (ms) | Text (ms) | Guard cold (ms) | Cold / render | Cold / text | Guard cached (ms) | Cached / render | Streams | Decoded (KiB) | Peak Budget mem (KiB) | Max CS | Max Fn | Max T4 | Max Stream | Max OpDepth |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bill-hr815` | 110 | 377.47 | 112.50 | 3.29 | 0.87% | 2.93% | 3.12 | 0.83% | 115 | 565.9 | 59.1 | 0 | 0 | 0 | 4 | 1 |
| `bunka-kokugo-series-019-p4` | 1 | 7.80 | 0.01 | 0.01 | 0.07% | 38.77% | 0.00 | 0.01% | 1 | 0.0 | 1.2 | 1 | 0 | 0 | 0 | 0 |
| `f1040` | 2 | 10.72 | 6.31 | 0.72 | 6.76% | 11.48% | 0.67 | 6.27% | 78 | 132.8 | 577.4 | 0 | 0 | 0 | 1 | 1 |
| `mhlw-doc` | 2 | 5.91 | 6.80 | 0.16 | 2.77% | 2.41% | 0.15 | 2.55% | 2 | 72.2 | 3.5 | 0 | 0 | 0 | 0 | 2 |
| `nics-background-checks-2015-11` | 1 | 5.98 | 3.05 | 0.26 | 4.43% | 8.69% | 0.25 | 4.17% | 1 | 103.7 | 31.6 | 2 | 0 | 0 | 0 | 1 |
| `patent-us223898` | 4 | 19.73 | 7.52 | 0.16 | 0.83% | 2.17% | 0.15 | 0.74% | 4 | 84.8 | 37.5 | 1 | 0 | 0 | 0 | 0 |
| `pdf20-simple` | 1 | 0.14 | 0.05 | 0.00 | 1.51% | 4.37% | 0.00 | 1.07% | 1 | 0.9 | 2.1 | 0 | 0 | 0 | 0 | 1 |
| `pdfium-jpx-lzw` | 1 | 3.31 | 0.01 | 0.00 | 0.08% | 29.30% | 0.00 | 0.02% | 1 | 0.0 | 1.2 | 0 | 0 | 0 | 0 | 0 |
| `pdfium-links-highlights-annots` | 1 | 0.27 | 0.07 | 0.00 | 0.67% | 2.48% | 0.00 | 0.47% | 1 | 0.2 | 7.1 | 0 | 0 | 0 | 0 | 0 |
| `pdfium-ocg-invisible` | 1 | 0.02 | 0.00 | 0.00 | 4.89% | 24.16% | 0.00 | 4.22% | 1 | 0.0 | 0.9 | 0 | 0 | 0 | 0 | 0 |
| `pdfium-smask-blend` | 1 | 0.08 | 0.01 | 0.00 | 4.52% | 40.90% | 0.00 | 4.14% | 4 | 0.1 | 4.1 | 0 | 0 | 0 | 2 | 0 |
| `pdfium-type3` | 1 | 0.04 | 0.01 | 0.00 | 5.09% | 27.61% | 0.00 | 4.64% | 2 | 0.2 | 3.6 | 0 | 0 | 0 | 1 | 0 |
| `senate-expenditures` | 1 | 5.02 | 2.37 | 0.28 | 5.52% | 11.67% | 0.25 | 5.07% | 1 | 136.6 | 19.2 | 2 | 1 | 0 | 0 | 1 |
| `usrguide` | 27 | 76.15 | 57.85 | 1.05 | 1.38% | 1.82% | 0.99 | 1.30% | 27 | 198.0 | 48.5 | 0 | 0 | 0 | 0 | 1 |
| `wdl6812-manuscript` | 2 | 17.59 | 0.03 | 0.02 | 0.09% | 48.81% | 0.01 | 0.03% | 2 | 0.1 | 2.8 | 1 | 0 | 0 | 0 | 0 |
| **Corpus (15 docs)** | **156** | **530.22** | **196.59** | **5.97** | **1.13%** | **3.04%** | **5.59** | **1.06%** | **241** | **1295.6** | **577.4** | **2** | **1** | **0** | **4** | **2** |

## Per-page distribution (156 pages)

| Metric | Median | 90th percentile | Max |
| --- | ---: | ---: | ---: |
| Guard cold (ms) | 0.029 | 0.043 | 0.482 |
| Guard cached (ms) | 0.028 | 0.040 | 0.429 |
| Guard cold / render | 0.86% | 1.48% | 7.96% (`f1040` p.1) |
| Guard cached / render | 0.81% | 1.42% | 7.08% |

