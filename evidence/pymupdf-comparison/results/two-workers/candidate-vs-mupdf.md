# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 12.6 | 5.0 | 0.40 |
| Text | 252.4 | 408.4 | 1.62 |
| Words | 310.1 | 576.0 | 1.86 |
| Search | 329.0 | 406.8 | 1.24 |
| Render 72 dpi | 740.7 | 880.1 | 1.19 |
| Render 144 dpi | 1730.5 | 1732.7 | 1.00 |
| Full save | 132.7 | 12.6 | 0.09 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 1.00 | 2.05 | 2.05 | 1.31 | 1.72 | 1.19 | 0.09 |
| bunka-kokugo-series-019-p4 | 0.38 | 0.44 | 0.64 | 0.48 | 0.34 | 0.37 | 0.43 |
| f1040 | 1.05 | 0.47 | 0.52 | 0.31 | 0.25 | 0.31 | 0.07 |
| mhlw-doc | 0.27 | 1.02 | 0.89 | 0.96 | 0.48 | 0.47 | 0.12 |
| nics-background-checks-2015-11 | 0.38 | 0.91 | 1.40 | 0.87 | 1.03 | 0.82 | 0.15 |
| patent-us223898 | 0.48 | 1.86 | 2.55 | 2.14 | 0.54 | 0.57 | 0.26 |
| pdf20-simple | 0.38 | 0.49 | 0.52 | 0.40 | 0.57 | 0.88 | 0.08 |
| pdfium-jpx-lzw | 0.31 | 0.30 | 0.37 | 0.38 | 0.67 | 0.37 | 0.07 |
| pdfium-links-highlights-annots | 0.27 | 0.18 | 0.45 | 0.30 | 0.55 | 0.63 | 0.07 |
| pdfium-ocg-invisible | 0.31 | 0.57 | 0.65 | 0.53 | 1.44 | 1.64 | 0.06 |
| pdfium-smask-blend | 0.34 | 0.45 | 0.71 | 0.41 | 1.16 | 0.84 | 0.10 |
| pdfium-type3 | 0.27 | 0.66 | 0.87 | 0.64 | 1.35 | 1.69 | 0.10 |
| senate-expenditures | 0.44 | 0.93 | 1.53 | 1.02 | 1.03 | 1.09 | 0.14 |
| usrguide | 0.19 | 2.20 | 2.87 | 1.89 | 1.38 | 1.39 | 0.13 |
| wdl6812-manuscript | 0.44 | 0.19 | 0.24 | 0.15 | 0.84 | 0.50 | 0.47 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `266690277a5bddb1c59511af0c8a978d98121b6b9d548c36f6b57b167bdd27dd`.
Built separately in the release profile; rendering uses the public API defaults.
