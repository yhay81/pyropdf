# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 8.3 | 4.4 | 0.53 |
| Text | 191.4 | 353.9 | 1.85 |
| Words | 220.1 | 500.5 | 2.27 |
| Search | 242.2 | 334.8 | 1.38 |
| Render 72 dpi | 532.8 | 722.0 | 1.36 |
| Render 144 dpi | 1019.5 | 1414.2 | 1.39 |
| Full save | 100.0 | 10.8 | 0.11 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.95 | 2.18 | 2.57 | 1.43 | 2.11 | 1.93 | 0.13 |
| bunka-kokugo-series-019-p4 | 0.94 | 0.97 | 1.24 | 0.74 | 0.38 | 0.45 | 0.72 |
| f1040 | 1.35 | 0.54 | 0.68 | 0.38 | 0.30 | 0.38 | 0.07 |
| mhlw-doc | 0.28 | 0.95 | 0.99 | 0.95 | 0.54 | 0.55 | 0.21 |
| nics-background-checks-2015-11 | 0.41 | 1.00 | 1.43 | 0.96 | 1.08 | 1.09 | 0.20 |
| patent-us223898 | 0.48 | 2.44 | 2.65 | 1.72 | 0.57 | 0.63 | 0.27 |
| pdf20-simple | 0.42 | 0.43 | 0.48 | 0.36 | 0.48 | 0.91 | 0.08 |
| pdfium-jpx-lzw | 0.31 | 0.32 | 0.35 | 0.30 | 0.86 | 0.40 | 0.09 |
| pdfium-links-highlights-annots | 0.35 | 0.28 | 0.39 | 0.26 | 0.66 | 0.70 | 0.10 |
| pdfium-ocg-invisible | 0.41 | 0.65 | 0.80 | 0.67 | 1.91 | 2.19 | 0.11 |
| pdfium-smask-blend | 0.40 | 0.62 | 0.71 | 0.55 | 1.23 | 1.05 | 0.10 |
| pdfium-type3 | 0.49 | 0.49 | 0.86 | 0.69 | 1.31 | 1.76 | 0.12 |
| senate-expenditures | 0.65 | 0.80 | 1.13 | 0.88 | 1.03 | 1.27 | 0.20 |
| usrguide | 0.26 | 2.58 | 3.25 | 2.15 | 1.42 | 1.56 | 0.16 |
| wdl6812-manuscript | 1.07 | 0.26 | 0.28 | 0.21 | 0.98 | 0.50 | 0.71 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `5cf94c7ad4e49d92432bf12ab9f6f97feb24363a82198d22093fb9729b83a16d`.
Built separately in the release profile; rendering uses the public API defaults.
