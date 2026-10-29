# darwin-arm64

Measured 2026-10-29 on macOS-27.0.1-arm64-arm-64bit-Mach-O (arm),
Python 3.13.16, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.1.0,
best of 5 runs per operation, over the 14 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 4.5 | 0.8 | 0.19 |
| Text | 211.4 | 167.3 | 0.79 |
| Words | 349.2 | 263.5 | 0.75 |
| Search | 142.7 | 166.0 | 1.16 |
| Render 72 dpi | 320.9 | 851.1 | 2.65 |
| Render 144 dpi | 512.5 | 2032.4 | 3.97 |
| Full save | 47.0 | 7.2 | 0.15 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.40 | 0.63 | 0.65 | 1.09 | 6.01 | 8.23 | 0.14 |
| bunka-kokugo-series-019-p4 | 0.16 | 0.40 | 0.49 | 0.45 | 0.27 | 0.35 | 1.10 |
| f1040 | 0.77 | 0.31 | 0.37 | 0.25 | 0.49 | 0.83 | 0.09 |
| mhlw-doc | 0.13 | 1.25 | 1.24 | 1.34 | 0.98 | 1.46 | 0.38 |
| nics-background-checks-2015-11 | 0.19 | 0.53 | 0.58 | 0.70 | 1.59 | 2.33 | 0.34 |
| patent-us223898 | 0.28 | 0.85 | 0.72 | 1.24 | 0.51 | 0.59 | 0.51 |
| pdf20-simple | 0.09 | 0.19 | 0.24 | 0.16 | 1.44 | 3.34 | 0.10 |
| pdfium-jpx-lzw | 0.08 | 0.26 | 0.36 | 0.29 | 0.82 | 0.58 | 0.06 |
| pdfium-links-highlights-annots | 0.12 | 0.19 | 0.20 | 0.15 | 1.58 | 3.46 | 0.06 |
| pdfium-smask-blend | 0.09 | 0.40 | 0.48 | 0.32 | 0.75 | 1.09 | 0.07 |
| pdfium-type3 | 0.09 | 0.34 | 0.45 | 0.40 | 1.37 | 3.22 | 0.07 |
| senate-expenditures | 0.17 | 0.47 | 0.50 | 0.64 | 1.65 | 3.12 | 0.30 |
| usrguide | 0.09 | 1.71 | 1.41 | 2.26 | 3.16 | 5.19 | 0.24 |
| wdl6812-manuscript | 0.27 | 0.09 | 0.11 | 0.08 | 0.78 | 0.66 | 1.32 |
