# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 8.9 | 3.8 | 0.43 |
| Text | 198.1 | 325.7 | 1.64 |
| Words | 240.3 | 478.8 | 1.99 |
| Search | 249.9 | 316.9 | 1.27 |
| Render 72 dpi | 588.5 | 962.2 | 1.63 |
| Render 144 dpi | 1030.5 | 1742.0 | 1.69 |
| Full save | 101.4 | 10.2 | 0.10 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.91 | 1.92 | 2.25 | 1.33 | 2.57 | 2.45 | 0.11 |
| bunka-kokugo-series-019-p4 | 0.59 | 0.50 | 0.61 | 0.57 | 0.34 | 0.39 | 0.62 |
| f1040 | 0.95 | 0.49 | 0.66 | 0.35 | 0.42 | 0.51 | 0.06 |
| mhlw-doc | 0.28 | 0.90 | 0.96 | 0.93 | 0.70 | 0.68 | 0.18 |
| nics-background-checks-2015-11 | 0.39 | 0.83 | 1.30 | 0.91 | 1.16 | 1.19 | 0.19 |
| patent-us223898 | 0.44 | 2.09 | 2.39 | 1.51 | 0.43 | 0.54 | 0.25 |
| pdf20-simple | 0.29 | 0.29 | 0.44 | 0.31 | 0.44 | 0.84 | 0.06 |
| pdfium-jpx-lzw | 0.27 | 0.32 | 0.36 | 0.32 | 0.66 | 0.33 | 0.09 |
| pdfium-links-highlights-annots | 0.31 | 0.27 | 0.32 | 0.22 | 0.45 | 0.68 | 0.07 |
| pdfium-ocg-invisible | 0.30 | 0.34 | 0.42 | 0.49 | 0.50 | 0.69 | 0.08 |
| pdfium-smask-blend | 0.34 | 0.43 | 0.50 | 0.39 | 0.54 | 0.61 | 0.08 |
| pdfium-type3 | 0.33 | 0.38 | 0.54 | 0.47 | 0.50 | 0.75 | 0.08 |
| senate-expenditures | 0.50 | 0.65 | 0.89 | 0.70 | 1.00 | 1.26 | 0.21 |
| usrguide | 0.33 | 2.36 | 2.67 | 1.95 | 1.94 | 1.98 | 0.15 |
| wdl6812-manuscript | 0.52 | 0.20 | 0.21 | 0.17 | 0.69 | 0.49 | 0.68 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `5cf94c7ad4e49d92432bf12ab9f6f97feb24363a82198d22093fb9729b83a16d`.
Built separately in the release profile; rendering uses the public API defaults.
