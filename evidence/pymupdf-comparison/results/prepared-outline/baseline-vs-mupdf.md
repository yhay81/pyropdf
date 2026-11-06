# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 11.4 | 3.7 | 0.32 |
| Text | 217.0 | 370.6 | 1.71 |
| Words | 257.0 | 520.1 | 2.02 |
| Search | 252.4 | 384.9 | 1.52 |
| Render 72 dpi | 565.0 | 1098.8 | 1.94 |
| Render 144 dpi | 1066.1 | 1814.8 | 1.70 |
| Full save | 108.5 | 10.4 | 0.10 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.99 | 2.29 | 2.83 | 1.80 | 3.49 | 2.44 | 0.08 |
| bunka-kokugo-series-019-p4 | 0.54 | 0.54 | 0.65 | 0.57 | 0.33 | 0.38 | 0.37 |
| f1040 | 0.44 | 0.37 | 0.40 | 0.36 | 0.31 | 0.47 | 0.06 |
| mhlw-doc | 0.28 | 0.73 | 0.90 | 0.82 | 0.61 | 0.62 | 0.20 |
| nics-background-checks-2015-11 | 0.16 | 0.53 | 0.90 | 0.73 | 1.11 | 1.09 | 0.19 |
| patent-us223898 | 0.50 | 2.00 | 2.36 | 1.59 | 0.54 | 0.57 | 0.27 |
| pdf20-simple | 0.36 | 0.45 | 0.44 | 0.25 | 0.27 | 0.56 | 0.08 |
| pdfium-jpx-lzw | 0.35 | 0.32 | 0.37 | 0.17 | 0.68 | 0.33 | 0.08 |
| pdfium-links-highlights-annots | 0.31 | 0.17 | 0.20 | 0.21 | 0.50 | 0.53 | 0.07 |
| pdfium-ocg-invisible | 0.27 | 0.43 | 0.52 | 0.47 | 0.47 | 0.68 | 0.06 |
| pdfium-smask-blend | 0.20 | 0.22 | 0.33 | 0.22 | 0.35 | 0.56 | 0.05 |
| pdfium-type3 | 0.23 | 0.30 | 0.36 | 0.21 | 0.53 | 0.70 | 0.08 |
| senate-expenditures | 0.38 | 0.87 | 0.93 | 0.68 | 0.89 | 1.14 | 0.10 |
| usrguide | 0.20 | 2.48 | 2.30 | 1.96 | 2.01 | 2.26 | 0.18 |
| wdl6812-manuscript | 0.34 | 0.13 | 0.14 | 0.11 | 0.71 | 0.50 | 0.43 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `d9232afcac19d40db410853da4cd9af5979a26fc9064ac8337e522b64b632c63`.
Built separately in the release profile; rendering uses the public API defaults.
