# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 10.5 | 4.4 | 0.42 |
| Text | 205.6 | 348.1 | 1.69 |
| Words | 233.4 | 490.9 | 2.10 |
| Search | 263.2 | 338.7 | 1.29 |
| Render 72 dpi | 554.3 | 1049.1 | 1.89 |
| Render 144 dpi | 1073.2 | 1942.5 | 1.81 |
| Full save | 106.8 | 11.1 | 0.10 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.94 | 1.84 | 2.48 | 1.36 | 3.26 | 2.66 | 0.11 |
| bunka-kokugo-series-019-p4 | 0.67 | 0.36 | 0.45 | 0.39 | 0.33 | 0.40 | 0.60 |
| f1040 | 1.16 | 0.47 | 0.68 | 0.36 | 0.44 | 0.53 | 0.06 |
| mhlw-doc | 0.15 | 0.84 | 0.90 | 0.89 | 0.65 | 0.60 | 0.19 |
| nics-background-checks-2015-11 | 0.34 | 0.81 | 1.16 | 0.71 | 1.09 | 1.17 | 0.20 |
| patent-us223898 | 0.42 | 1.97 | 2.61 | 1.69 | 0.62 | 0.69 | 0.34 |
| pdf20-simple | 0.54 | 1.00 | 0.69 | 0.47 | 0.93 | 1.45 | 0.12 |
| pdfium-jpx-lzw | 0.51 | 0.68 | 0.67 | 0.57 | 0.88 | 0.42 | 0.09 |
| pdfium-links-highlights-annots | 0.35 | 0.27 | 0.28 | 0.25 | 0.57 | 0.66 | 0.07 |
| pdfium-ocg-invisible | 0.33 | 0.46 | 0.53 | 0.47 | 0.51 | 0.36 | 0.07 |
| pdfium-smask-blend | 0.23 | 0.24 | 0.31 | 0.21 | 0.35 | 0.40 | 0.07 |
| pdfium-type3 | 0.40 | 0.67 | 0.80 | 0.66 | 0.90 | 1.12 | 0.12 |
| senate-expenditures | 0.57 | 0.77 | 0.91 | 0.89 | 1.55 | 1.69 | 0.22 |
| usrguide | 0.26 | 2.89 | 2.72 | 1.86 | 1.67 | 1.77 | 0.19 |
| wdl6812-manuscript | 0.64 | 0.32 | 0.19 | 0.22 | 0.89 | 0.61 | 0.71 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `d9232afcac19d40db410853da4cd9af5979a26fc9064ac8337e522b64b632c63`.
Built separately in the release profile; rendering uses the public API defaults.
