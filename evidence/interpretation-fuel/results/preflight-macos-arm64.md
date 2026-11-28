# Pre-flight guard with DAG fuel, EI cap, and function bounds (`preflight-macos-arm64`, best of 5 runs)

## Synthetic adversarial refusals (best of 5 runs)

| Case | PDF bytes | Refusal time (ms) | Verdict |
| --- | ---: | ---: | --- |
| `form-bomb-20` | 3820 | 0.0438 | `page effective operator fuel exceeded limit: interpretation needs too much work: 2097150 units of fuel, over the limit of 1048576` |
| `inline-ei-flood-500` | 2928 | 0.0014 | `inline image candidate EI scan count exceeded limit (#17): interpretation needs too much work: 65 units of fuel, over the limit of 64` |
| `type0-size-2048x2048` | 685 | 0.0014 | `Type 0 sampled function /Size product exceeded limit: interpretation needs too much work: 4194304 units of fuel, over the limit of 1048576` |
| `type4-20000-ops` | 60656 | 0.1063 | `Type 4 PostScript procedure operator count exceeded limit: interpretation needs too much work: 16385 units of fuel, over the limit of 16384` |
| `tiling-10000x10000` | 645 | 0.0030 | `page effective operator fuel exceeded limit: interpretation needs too much work: 200040002 units of fuel, over the limit of 1048576` |

## 15-document real-world corpus under `DocumentLimits::web()`

All 15 documents under `tests/assets/real_world/` passed with **0 false positives**.

| Document | Pages | Render 72 dpi (ms) | Text (ms) | Cold preflight (ms) | Cold / render | Cold / text | Cached preflight (ms) | Max page ops | Total ops | Max EI | Max T0 | Max T4 ops |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bill-hr815` | 110 | 364.43 | 61.46 | 3.67 | 1.01% | 5.97% | 3.59 | 1323 | 25242 | 0 | 0 | 0 |
| `bunka-kokugo-series-019-p4` | 1 | 6.95 | 0.01 | 0.01 | 0.10% | 61.20% | 0.00 | 4 | 4 | 0 | 0 | 0 |
| `f1040` | 2 | 10.41 | 4.02 | 0.93 | 8.89% | 23.04% | 0.86 | 7124 | 12264 | 0 | 0 | 0 |
| `mhlw-doc` | 2 | 5.84 | 4.58 | 0.24 | 4.15% | 5.30% | 0.23 | 5375 | 7025 | 0 | 0 | 0 |
| `nics-background-checks-2015-11` | 1 | 6.13 | 2.36 | 0.36 | 5.90% | 15.31% | 0.35 | 9205 | 9205 | 0 | 0 | 0 |
| `patent-us223898` | 4 | 18.29 | 4.65 | 0.21 | 1.14% | 4.47% | 0.19 | 2553 | 6255 | 0 | 0 | 0 |
| `pdf20-simple` | 1 | 0.14 | 0.05 | 0.00 | 2.35% | 7.12% | 0.00 | 26 | 26 | 0 | 0 | 0 |
| `pdfium-jpx-lzw` | 1 | 3.15 | 0.01 | 0.00 | 0.11% | 57.63% | 0.00 | 4 | 4 | 0 | 0 | 0 |
| `pdfium-links-highlights-annots` | 1 | 0.26 | 0.06 | 0.00 | 1.02% | 4.41% | 0.00 | 11 | 11 | 0 | 0 | 0 |
| `pdfium-ocg-invisible` | 1 | 0.02 | 0.00 | 0.00 | 8.35% | 36.53% | 0.00 | 6 | 6 | 0 | 0 | 0 |
| `pdfium-smask-blend` | 1 | 0.08 | 0.01 | 0.01 | 13.41% | 116.75% | 0.01 | 24 | 24 | 0 | 0 | 0 |
| `pdfium-type3` | 1 | 0.04 | 0.01 | 0.01 | 15.41% | 82.02% | 0.00 | 21 | 21 | 1 | 0 | 0 |
| `senate-expenditures` | 1 | 4.95 | 2.00 | 0.36 | 7.21% | 17.88% | 0.35 | 11562 | 11562 | 0 | 4096 | 0 |
| `usrguide` | 27 | 75.28 | 16.27 | 1.27 | 1.69% | 7.82% | 1.22 | 754 | 9687 | 0 | 0 | 0 |
| `wdl6812-manuscript` | 2 | 16.50 | 0.02 | 0.02 | 0.12% | 94.61% | 0.01 | 8 | 16 | 0 | 0 | 0 |
| **Corpus (15 docs)** | **156** | **512.47** | **95.51** | **7.09** | **1.38%** | **7.43%** | **6.82** | **11562** | **81352** | **1** | **4096** | **0** |

## Per-page distribution (156 pages)

| Metric | Median | 90th percentile | Max |
| --- | ---: | ---: | ---: |
| Cold preflight (ms) | 0.032 | 0.054 | 0.611 |
| Cold / render | 0.99% | 1.84% | 15.41% |

