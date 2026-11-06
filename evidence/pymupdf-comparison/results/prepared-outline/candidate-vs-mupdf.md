# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 12.0 | 3.9 | 0.32 |
| Text | 292.5 | 430.9 | 1.47 |
| Words | 273.4 | 561.3 | 2.05 |
| Search | 289.7 | 356.0 | 1.23 |
| Render 72 dpi | 596.3 | 1096.8 | 1.84 |
| Render 144 dpi | 1237.6 | 1894.6 | 1.53 |
| Full save | 132.2 | 12.3 | 0.09 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.43 | 1.69 | 2.54 | 1.38 | 3.16 | 2.05 | 0.08 |
| bunka-kokugo-series-019-p4 | 0.34 | 0.53 | 0.46 | 0.46 | 0.34 | 0.40 | 0.35 |
| f1040 | 0.58 | 0.37 | 0.55 | 0.38 | 0.47 | 0.67 | 0.07 |
| mhlw-doc | 0.29 | 0.76 | 1.03 | 0.76 | 0.61 | 0.87 | 0.17 |
| nics-background-checks-2015-11 | 0.36 | 0.70 | 1.33 | 0.72 | 1.16 | 1.52 | 0.15 |
| patent-us223898 | 0.39 | 1.93 | 2.58 | 1.89 | 0.57 | 0.59 | 0.25 |
| pdf20-simple | 0.40 | 0.41 | 0.47 | 0.36 | 0.52 | 0.66 | 0.08 |
| pdfium-jpx-lzw | 0.28 | 0.25 | 0.36 | 0.33 | 0.69 | 0.35 | 0.08 |
| pdfium-links-highlights-annots | 0.27 | 0.20 | 0.28 | 0.26 | 0.45 | 0.55 | 0.05 |
| pdfium-ocg-invisible | 0.28 | 0.41 | 0.64 | 0.51 | 0.49 | 0.81 | 0.06 |
| pdfium-smask-blend | 0.27 | 0.33 | 0.52 | 0.39 | 0.40 | 0.57 | 0.06 |
| pdfium-type3 | 0.25 | 0.48 | 0.60 | 0.33 | 0.46 | 0.67 | 0.08 |
| senate-expenditures | 0.43 | 0.76 | 0.78 | 0.57 | 1.01 | 1.14 | 0.18 |
| usrguide | 0.17 | 2.64 | 2.68 | 1.67 | 1.87 | 1.81 | 0.13 |
| wdl6812-manuscript | 0.35 | 0.15 | 0.15 | 0.11 | 0.65 | 0.45 | 0.47 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `6b655a3f0b2575a026d153712b4f1b25fc19da4ede49c52fb7b08f3295adb10a`.
Built separately in the release profile; rendering uses the public API defaults.
