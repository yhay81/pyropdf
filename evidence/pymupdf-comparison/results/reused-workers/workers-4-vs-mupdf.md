# windows-amd64-checkout

Measured 2026-11-06 on Windows-11-10.0.26300-SP0 (AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD),
Python 3.13.17, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.2.2,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 8.8 | 3.6 | 0.41 |
| Text | 192.1 | 323.4 | 1.68 |
| Words | 219.9 | 477.4 | 2.17 |
| Search | 240.3 | 314.3 | 1.31 |
| Render 72 dpi | 529.5 | 550.4 | 1.04 |
| Render 144 dpi | 1025.5 | 1074.9 | 1.05 |
| Full save | 99.1 | 10.3 | 0.10 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.85 | 2.03 | 2.56 | 1.38 | 1.54 | 1.42 | 0.12 |
| bunka-kokugo-series-019-p4 | 0.50 | 0.55 | 0.64 | 0.54 | 0.34 | 0.39 | 0.59 |
| f1040 | 0.86 | 0.52 | 0.70 | 0.36 | 0.31 | 0.36 | 0.07 |
| mhlw-doc | 0.30 | 0.93 | 0.97 | 1.11 | 0.67 | 0.55 | 0.21 |
| nics-background-checks-2015-11 | 0.46 | 0.94 | 1.78 | 1.12 | 1.14 | 1.15 | 0.20 |
| patent-us223898 | 0.52 | 2.14 | 2.55 | 1.68 | 0.55 | 0.53 | 0.26 |
| pdf20-simple | 0.35 | 0.41 | 0.47 | 0.36 | 0.71 | 1.00 | 0.09 |
| pdfium-jpx-lzw | 0.43 | 0.32 | 0.41 | 0.37 | 0.68 | 0.30 | 0.06 |
| pdfium-links-highlights-annots | 0.24 | 0.18 | 0.22 | 0.19 | 0.58 | 0.64 | 0.08 |
| pdfium-ocg-invisible | 0.32 | 0.42 | 0.59 | 0.50 | 1.86 | 1.86 | 0.08 |
| pdfium-smask-blend | 0.32 | 0.43 | 0.52 | 0.39 | 1.00 | 0.65 | 0.07 |
| pdfium-type3 | 0.30 | 0.46 | 0.55 | 0.60 | 1.86 | 1.53 | 0.08 |
| senate-expenditures | 0.37 | 0.70 | 0.90 | 0.69 | 1.00 | 1.16 | 0.19 |
| usrguide | 0.28 | 2.21 | 2.69 | 1.88 | 1.01 | 1.12 | 0.17 |
| wdl6812-manuscript | 0.50 | 0.31 | 0.32 | 0.26 | 0.70 | 0.45 | 0.64 |

## Local artifact

Wheel: `pyropdf-0.2.2-cp311-abi3-win_amd64.whl`. SHA-256: `5cf94c7ad4e49d92432bf12ab9f6f97feb24363a82198d22093fb9729b83a16d`.
Built separately in the release profile; rendering uses the public API defaults.
