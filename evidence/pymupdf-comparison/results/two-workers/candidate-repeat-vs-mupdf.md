# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 9.0 | 4.6 | 0.51 |
| Text | 222.7 | 419.1 | 1.88 |
| Words | 234.7 | 588.8 | 2.51 |
| Search | 246.2 | 401.7 | 1.63 |
| Render 72 dpi | 543.4 | 840.3 | 1.55 |
| Render 144 dpi | 1050.0 | 1713.4 | 1.63 |
| Full save | 108.8 | 12.9 | 0.12 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.84 | 2.08 | 2.62 | 1.56 | 2.47 | 2.34 | 0.07 |
| bunka-kokugo-series-019-p4 | 0.38 | 0.40 | 0.45 | 0.52 | 0.41 | 0.41 | 0.52 |
| f1040 | 0.71 | 0.51 | 0.84 | 0.43 | 0.36 | 0.42 | 0.10 |
| mhlw-doc | 0.40 | 1.23 | 1.30 | 1.21 | 0.69 | 0.66 | 0.27 |
| nics-background-checks-2015-11 | 0.54 | 1.07 | 1.97 | 1.24 | 1.20 | 1.32 | 0.20 |
| patent-us223898 | 0.73 | 2.72 | 2.72 | 1.78 | 0.65 | 0.71 | 0.31 |
| pdf20-simple | 0.66 | 0.59 | 0.62 | 0.50 | 0.91 | 1.47 | 0.12 |
| pdfium-jpx-lzw | 0.40 | 0.46 | 0.55 | 0.46 | 1.06 | 0.52 | 0.12 |
| pdfium-links-highlights-annots | 0.48 | 0.40 | 0.51 | 0.37 | 1.04 | 1.29 | 0.11 |
| pdfium-ocg-invisible | 0.49 | 0.73 | 0.78 | 0.66 | 2.93 | 2.55 | 0.11 |
| pdfium-smask-blend | 0.41 | 0.64 | 0.75 | 0.58 | 1.43 | 0.79 | 0.08 |
| pdfium-type3 | 0.50 | 0.82 | 0.99 | 0.77 | 1.44 | 2.31 | 0.12 |
| senate-expenditures | 0.74 | 1.02 | 1.44 | 1.09 | 1.06 | 1.47 | 0.20 |
| usrguide | 0.28 | 2.91 | 4.07 | 2.97 | 1.54 | 1.70 | 0.16 |
| wdl6812-manuscript | 0.67 | 0.23 | 0.25 | 0.18 | 0.82 | 0.55 | 0.66 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `266690277a5bddb1c59511af0c8a978d98121b6b9d548c36f6b57b167bdd27dd`.
Built separately in the release profile; rendering uses the public API defaults.
