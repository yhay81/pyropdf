# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 13.1 | 4.5 | 0.34 |
| Text | 328.9 | 406.6 | 1.24 |
| Words | 350.6 | 611.8 | 1.75 |
| Search | 377.6 | 424.0 | 1.12 |
| Render 72 dpi | 730.9 | 807.5 | 1.10 |
| Render 144 dpi | 1391.0 | 1352.0 | 0.97 |
| Full save | 151.0 | 13.6 | 0.09 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.73 | 1.20 | 1.84 | 0.98 | 1.28 | 1.17 | 0.07 |
| bunka-kokugo-series-019-p4 | 0.20 | 0.28 | 0.24 | 0.19 | 0.30 | 0.30 | 0.58 |
| f1040 | 0.84 | 0.62 | 0.48 | 0.34 | 0.36 | 0.40 | 0.07 |
| mhlw-doc | 0.25 | 0.85 | 0.82 | 0.95 | 0.71 | 0.54 | 0.15 |
| nics-background-checks-2015-11 | 0.41 | 1.00 | 1.19 | 0.92 | 1.04 | 0.98 | 0.17 |
| patent-us223898 | 0.45 | 2.11 | 1.89 | 1.32 | 0.45 | 0.51 | 0.23 |
| pdf20-simple | 0.34 | 0.35 | 0.41 | 0.25 | 0.43 | 0.95 | 0.09 |
| pdfium-jpx-lzw | 0.32 | 0.30 | 0.36 | 0.35 | 0.66 | 0.34 | 0.11 |
| pdfium-links-highlights-annots | 0.31 | 0.26 | 0.32 | 0.23 | 0.36 | 0.44 | 0.08 |
| pdfium-ocg-invisible | 0.22 | 0.26 | 0.54 | 0.43 | 0.40 | 0.52 | 0.07 |
| pdfium-smask-blend | 0.27 | 0.37 | 0.54 | 0.31 | 0.49 | 0.50 | 0.06 |
| pdfium-type3 | 0.24 | 0.43 | 0.45 | 0.49 | 0.64 | 0.39 | 0.07 |
| senate-expenditures | 0.45 | 0.72 | 0.85 | 0.66 | 1.29 | 1.54 | 0.16 |
| usrguide | 0.18 | 1.96 | 2.96 | 2.75 | 2.58 | 1.54 | 0.15 |
| wdl6812-manuscript | 0.64 | 0.30 | 0.29 | 0.21 | 0.74 | 0.60 | 0.61 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `3378caedd7533aea6eba04ba6518f30c848fca352385735da1ac99d4416da856`.
Built separately in the release profile; rendering uses the public API defaults.
