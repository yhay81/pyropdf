# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 9.3 | 3.6 | 0.39 |
| Text | 245.0 | 342.8 | 1.40 |
| Words | 283.9 | 499.1 | 1.76 |
| Search | 264.6 | 319.6 | 1.21 |
| Render 72 dpi | 558.1 | 628.2 | 1.13 |
| Render 144 dpi | 1104.1 | 1196.8 | 1.08 |
| Full save | 107.2 | 11.0 | 0.10 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.71 | 1.48 | 1.82 | 1.27 | 1.68 | 1.42 | 0.11 |
| bunka-kokugo-series-019-p4 | 0.45 | 0.44 | 0.59 | 0.54 | 0.34 | 0.38 | 0.48 |
| f1040 | 0.89 | 0.49 | 0.65 | 0.37 | 0.42 | 0.50 | 0.06 |
| mhlw-doc | 0.26 | 0.86 | 0.97 | 0.90 | 0.70 | 0.71 | 0.20 |
| nics-background-checks-2015-11 | 0.37 | 0.91 | 1.36 | 0.95 | 1.18 | 1.21 | 0.13 |
| patent-us223898 | 0.35 | 1.47 | 2.44 | 1.67 | 0.54 | 0.53 | 0.26 |
| pdf20-simple | 0.45 | 0.36 | 0.44 | 0.34 | 0.94 | 0.83 | 0.09 |
| pdfium-jpx-lzw | 0.32 | 0.32 | 0.37 | 0.32 | 0.67 | 0.35 | 0.09 |
| pdfium-links-highlights-annots | 0.31 | 0.28 | 0.34 | 0.23 | 0.52 | 0.66 | 0.07 |
| pdfium-ocg-invisible | 0.32 | 0.50 | 0.62 | 0.78 | 0.56 | 0.71 | 0.08 |
| pdfium-smask-blend | 0.33 | 0.43 | 0.51 | 0.38 | 0.54 | 0.57 | 0.08 |
| pdfium-type3 | 0.31 | 0.64 | 0.57 | 0.45 | 0.54 | 0.74 | 0.09 |
| senate-expenditures | 0.51 | 0.74 | 0.97 | 0.75 | 0.90 | 1.23 | 0.16 |
| usrguide | 0.24 | 2.23 | 2.69 | 1.68 | 1.11 | 1.20 | 0.17 |
| wdl6812-manuscript | 0.35 | 0.27 | 0.33 | 0.26 | 0.68 | 0.49 | 0.65 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `3378caedd7533aea6eba04ba6518f30c848fca352385735da1ac99d4416da856`.
Built separately in the release profile; rendering uses the public API defaults.
