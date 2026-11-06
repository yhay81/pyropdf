# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 9.7 | 4.6 | 0.47 |
| Text | 216.6 | 386.2 | 1.78 |
| Words | 268.5 | 550.4 | 2.05 |
| Search | 278.6 | 444.5 | 1.60 |
| Render 72 dpi | 619.7 | 1280.1 | 2.07 |
| Render 144 dpi | 1308.4 | 2283.9 | 1.75 |
| Full save | 134.2 | 12.4 | 0.09 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.91 | 2.07 | 2.42 | 1.94 | 3.58 | 2.34 | 0.09 |
| bunka-kokugo-series-019-p4 | 0.68 | 0.48 | 0.64 | 0.57 | 0.38 | 0.45 | 0.59 |
| f1040 | 1.25 | 0.63 | 0.81 | 0.36 | 0.50 | 0.52 | 0.06 |
| mhlw-doc | 0.37 | 1.02 | 1.22 | 1.05 | 0.86 | 0.83 | 0.23 |
| nics-background-checks-2015-11 | 0.42 | 0.69 | 1.09 | 0.95 | 1.05 | 1.11 | 0.19 |
| patent-us223898 | 0.34 | 2.12 | 2.74 | 2.10 | 0.59 | 0.60 | 0.31 |
| pdf20-simple | 0.46 | 0.51 | 0.60 | 0.46 | 0.58 | 0.87 | 0.12 |
| pdfium-jpx-lzw | 0.44 | 0.41 | 0.49 | 0.42 | 0.67 | 0.32 | 0.08 |
| pdfium-links-highlights-annots | 0.26 | 0.31 | 0.36 | 0.27 | 0.49 | 0.73 | 0.09 |
| pdfium-ocg-invisible | 0.30 | 0.56 | 0.58 | 0.56 | 0.69 | 0.71 | 0.05 |
| pdfium-smask-blend | 0.31 | 0.49 | 0.62 | 0.34 | 0.64 | 0.76 | 0.09 |
| pdfium-type3 | 0.37 | 0.40 | 0.77 | 0.58 | 0.56 | 0.74 | 0.07 |
| senate-expenditures | 0.38 | 0.68 | 1.00 | 0.77 | 1.13 | 1.46 | 0.21 |
| usrguide | 0.31 | 2.80 | 2.39 | 1.95 | 1.88 | 2.11 | 0.13 |
| wdl6812-manuscript | 0.65 | 0.15 | 0.22 | 0.20 | 0.74 | 0.59 | 0.60 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `223aca8df38272c4ce05e7d1d89bf3200b2c9fd50f5fefc6b26184ae226d12c6`.
Built separately in the release profile; rendering uses the public API defaults.
