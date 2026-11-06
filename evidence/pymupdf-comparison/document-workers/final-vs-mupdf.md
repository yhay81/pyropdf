# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 18.7 | 5.7 | 0.31 |
| Text | 269.6 | 378.7 | 1.40 |
| Words | 298.8 | 586.6 | 1.96 |
| Search | 383.2 | 423.7 | 1.11 |
| Render 72 dpi | 720.3 | 741.8 | 1.03 |
| Render 144 dpi | 1303.1 | 1417.1 | 1.09 |
| Full save | 116.3 | 13.1 | 0.11 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 1.43 | 1.59 | 2.50 | 1.25 | 1.19 | 1.36 | 0.12 |
| bunka-kokugo-series-019-p4 | 0.38 | 0.49 | 0.76 | 0.43 | 0.44 | 0.49 | 0.42 |
| f1040 | 0.22 | 0.35 | 0.66 | 0.18 | 0.55 | 0.65 | 0.08 |
| mhlw-doc | 0.54 | 1.13 | 1.30 | 1.16 | 0.80 | 0.85 | 0.20 |
| nics-background-checks-2015-11 | 0.28 | 0.99 | 1.52 | 1.12 | 1.25 | 1.22 | 0.23 |
| patent-us223898 | 0.58 | 3.02 | 3.08 | 1.57 | 0.66 | 0.54 | 0.21 |
| pdf20-simple | 0.33 | 0.42 | 0.61 | 0.35 | 0.43 | 0.85 | 0.09 |
| pdfium-jpx-lzw | 0.30 | 0.31 | 0.32 | 0.45 | 0.65 | 0.30 | 0.08 |
| pdfium-links-highlights-annots | 0.36 | 0.19 | 0.24 | 0.17 | 0.58 | 0.63 | 0.06 |
| pdfium-ocg-invisible | 0.36 | 0.52 | 0.83 | 0.51 | 0.48 | 0.68 | 0.05 |
| pdfium-smask-blend | 0.23 | 0.33 | 0.61 | 0.44 | 0.47 | 0.72 | 0.05 |
| pdfium-type3 | 0.27 | 0.32 | 0.44 | 0.61 | 0.65 | 0.51 | 0.06 |
| senate-expenditures | 0.32 | 0.68 | 0.86 | 0.67 | 0.86 | 1.33 | 0.19 |
| usrguide | 0.24 | 2.77 | 3.01 | 2.59 | 1.63 | 1.31 | 0.14 |
| wdl6812-manuscript | 0.48 | 0.30 | 0.33 | 0.31 | 0.56 | 0.39 | 0.51 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `adf07f6b83c6f61dcebbf6bee8859b252e036d52c305259f6115147ac6e59dbd`.
Built separately in the release profile; rendering uses the public API defaults.
