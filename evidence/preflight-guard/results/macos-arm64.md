# Pre-flight nesting guard benchmark (`macos-arm64`, best of 5 runs)

## Synthetic adversarial verification (guard before hayro)

| Case | PDF bytes | Guard verdict | Guard time (ms) |
| --- | ---: | --- | ---: |
| `type4-braces-50000` | 100656 | `type4_braces: Type 4 PostScript procedure exceeded 256 nested braces` | 0.574 |
| `type3-function-cycle` | 649 | `function_cycle: cyclic function graph at object 6` | 0.033 |
| `type3-function-chain-5000` | 553458 | `function_depth: function nesting exceeded 32 levels` | 0.663 |
| `cs-indexed-cycle` | 519 | `color_space_cycle: cyclic color space at object 5` | 0.023 |
| `cs-icc-alternate-cycle` | 607 | `color_space_cycle: cyclic color space at object 5` | 0.017 |
| `cs-indexed-chain-5000` | 323332 | `color_space_depth: color space nesting exceeded 32 levels` | 0.504 |
| `dag-revisited-deeper-cs` | 2646 | `color_space_depth: color space nesting exceeded 32 levels` | 0.056 |
| `image-smask-cs-chain-40` | 3232 | `color_space_depth: color space nesting exceeded 32 levels` | 0.049 |
| `inline-image-nested-200000` | 4800416 | `inline_image_lookahead: nested inline-image lookahead hazard exceeded 8 stray EI triggers (#740)` | 0.803 |
| `tiling-pattern-cycle` | 684 | `stream_cycle: cyclic nested stream at object 5` | 0.147 |
| `tiling-pattern-chain-2000` | 466029 | `stream_nesting: nested streams exceeded 50 levels` | 6.962 |
| `type3-glyph-cycle` | 798 | `stream_cycle: cyclic nested stream at object 6` | 0.078 |
| `type3-glyph-chain-2000` | 736020 | `stream_nesting: nested streams exceeded 50 levels` | 2.212 |
| `form-xobject-cycle` | 588 | `stream_cycle: cyclic nested stream at object 5` | 0.060 |
| `content-operand-depth-600` | 1614 | `content_operand_depth: content operand nesting exceeded 64 levels` | 0.032 |

## 15-document corpus overhead

All 15 documents under `tests/assets/real_world/` validated under `DocumentLimits::web()` with **0 false positives**.

| Document | Pages | Render 72 dpi (ms) | Text (ms) | Guard cold (ms) | Cold / render | Cold / text | Guard cached (ms) | Cached / render | Streams | Decoded (KiB) | Peak Budget mem (KiB) | Max CS | Max Fn | Max T4 | Max Stream | Max OpDepth |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bill-hr815` | 110 | 421.25 | 66.26 | 3.30 | 0.78% | 4.99% | 3.24 | 0.77% | 115 | 565.9 | 59.2 | 0 | 0 | 0 | 4 | 1 |
| `bunka-kokugo-series-019-p4` | 1 | 9.89 | 0.03 | 0.01 | 0.08% | 31.40% | 0.00 | 0.01% | 1 | 0.0 | 1.2 | 1 | 0 | 0 | 0 | 0 |
| `f1040` | 2 | 16.18 | 4.37 | 0.77 | 4.74% | 17.56% | 0.69 | 4.28% | 78 | 132.8 | 578.2 | 0 | 0 | 0 | 1 | 1 |
| `mhlw-doc` | 2 | 8.62 | 5.14 | 0.18 | 2.06% | 3.45% | 0.16 | 1.80% | 2 | 72.2 | 3.5 | 0 | 0 | 0 | 0 | 2 |
| `nics-background-checks-2015-11` | 1 | 6.15 | 2.48 | 0.27 | 4.39% | 10.90% | 0.26 | 4.16% | 1 | 103.7 | 31.7 | 2 | 0 | 0 | 0 | 1 |
| `patent-us223898` | 4 | 20.82 | 4.95 | 0.17 | 0.81% | 3.41% | 0.15 | 0.71% | 4 | 84.8 | 37.5 | 1 | 0 | 0 | 0 | 0 |
| `pdf20-simple` | 1 | 0.15 | 0.05 | 0.00 | 1.44% | 4.19% | 0.00 | 1.06% | 1 | 0.9 | 2.1 | 0 | 0 | 0 | 0 | 1 |
| `pdfium-jpx-lzw` | 1 | 3.61 | 0.01 | 0.00 | 0.07% | 23.68% | 0.00 | 0.01% | 1 | 0.0 | 1.2 | 0 | 0 | 0 | 0 | 0 |
| `pdfium-links-highlights-annots` | 1 | 0.30 | 0.08 | 0.00 | 0.77% | 2.93% | 0.00 | 0.39% | 1 | 0.2 | 7.1 | 0 | 0 | 0 | 0 | 0 |
| `pdfium-ocg-invisible` | 1 | 0.02 | 0.00 | 0.00 | 4.89% | 20.53% | 0.00 | 3.83% | 1 | 0.0 | 0.9 | 0 | 0 | 0 | 0 | 0 |
| `pdfium-smask-blend` | 1 | 0.08 | 0.01 | 0.00 | 4.68% | 39.55% | 0.00 | 4.15% | 4 | 0.1 | 4.2 | 0 | 0 | 0 | 2 | 0 |
| `pdfium-type3` | 1 | 0.04 | 0.01 | 0.00 | 5.10% | 26.12% | 0.00 | 4.13% | 2 | 0.2 | 3.6 | 0 | 0 | 0 | 1 | 0 |
| `senate-expenditures` | 1 | 5.32 | 2.12 | 0.27 | 5.07% | 12.75% | 0.25 | 4.75% | 1 | 136.6 | 19.2 | 2 | 1 | 0 | 0 | 1 |
| `usrguide` | 27 | 82.39 | 18.52 | 1.05 | 1.28% | 5.68% | 1.01 | 1.23% | 27 | 198.0 | 48.5 | 0 | 0 | 0 | 0 | 1 |
| `wdl6812-manuscript` | 2 | 19.07 | 0.04 | 0.02 | 0.09% | 42.37% | 0.01 | 0.03% | 2 | 0.1 | 3.5 | 1 | 0 | 0 | 0 | 0 |
| **Corpus (15 docs)** | **156** | **593.90** | **104.06** | **6.05** | **1.02%** | **5.81%** | **5.77** | **0.97%** | **241** | **1295.6** | **578.2** | **2** | **1** | **0** | **4** | **2** |

## Per-page distribution (156 pages)

| Metric | Median | 90th percentile | Max |
| --- | ---: | ---: | ---: |
| Guard cold (ms) | 0.029 | 0.042 | 0.514 |
| Guard cached (ms) | 0.029 | 0.041 | 0.451 |
| Guard cold / render | 0.83% | 1.42% | 5.80% (`bill-hr815` p.1) |
| Guard cached / render | 0.82% | 1.37% | 5.26% |

