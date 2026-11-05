# windows-amd64-checkout

Measured 2026-11-05 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 9.2 | 9.8 | 1.06 |
| Text | 216.5 | 685.2 | 3.16 |
| Words | 256.7 | 975.4 | 3.80 |
| Search | 321.9 | 591.3 | 1.84 |
| Render 72 dpi | 744.4 | 1901.1 | 2.55 |
| Render 144 dpi | 1229.8 | 3661.1 | 2.98 |
| Full save | 135.3 | 15.6 | 0.12 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.87 | 4.40 | 5.42 | 2.28 | 5.53 | 5.00 | 0.11 |
| bunka-kokugo-series-019-p4 | 0.48 | 0.53 | 0.63 | 0.76 | 0.55 | 0.69 | 0.53 |
| f1040 | 1.00 | 0.69 | 1.28 | 0.66 | 0.66 | 0.83 | 0.08 |
| mhlw-doc | 0.68 | 1.43 | 1.29 | 1.62 | 0.82 | 0.97 | 0.35 |
| nics-background-checks-2015-11 | 0.73 | 2.09 | 1.98 | 1.56 | 1.45 | 2.42 | 0.24 |
| patent-us223898 | 0.75 | 3.46 | 4.72 | 2.97 | 0.72 | 0.86 | 0.33 |
| pdf20-simple | 0.67 | 1.02 | 0.65 | 0.38 | 0.71 | 1.52 | 0.09 |
| pdfium-jpx-lzw | 1.73 | 0.44 | 0.77 | 0.45 | 0.90 | 0.64 | 0.12 |
| pdfium-links-highlights-annots | 0.49 | 0.42 | 0.49 | 0.27 | 0.73 | 1.10 | 0.11 |
| pdfium-ocg-invisible | 0.32 | 0.64 | 0.70 | 0.96 | 1.60 | 2.41 | 0.11 |
| pdfium-smask-blend | 0.37 | 0.51 | 0.53 | 0.51 | 0.57 | 0.64 | 0.10 |
| pdfium-type3 | 0.57 | 0.58 | 0.76 | 0.60 | 0.79 | 1.14 | 0.11 |
| senate-expenditures | 0.60 | 1.17 | 1.49 | 1.23 | 2.15 | 2.82 | 0.20 |
| usrguide | 0.63 | 3.46 | 2.87 | 1.55 | 1.56 | 2.10 | 0.10 |
| wdl6812-manuscript | 6.36 | 0.22 | 0.25 | 0.19 | 0.76 | 0.59 | 0.91 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `d9232afcac19d40db410853da4cd9af5979a26fc9064ac8337e522b64b632c63`.
Built separately in the release profile; rendering uses the public API defaults.
