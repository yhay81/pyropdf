# darwin-arm64

Measured 2026-10-30 on macOS-27.0.1-arm64-arm-64bit-Mach-O (arm),
Python 3.13.16, PyMuPDF 2.0 (MuPDF 1.28.5) against pyropdf 0.1.0,
best of 5 runs per operation, over the 15 documents of
`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.

## Corpus totals

| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |
| --- | ---: | ---: | ---: |
| Open | 5.6 | 1.2 | 0.21 |
| Text | 215.7 | 173.3 | 0.80 |
| Words | 351.2 | 277.1 | 0.79 |
| Search | 145.3 | 173.5 | 1.19 |
| Render 72 dpi | 325.5 | 659.4 | 2.03 |
| Render 144 dpi | 514.5 | 1204.9 | 2.34 |
| Full save | 48.9 | 7.6 | 0.15 |

## Ratio per document

| Document | Open | Text | Words | Search | Render 72 dpi | Render 144 dpi | Full save |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 0.41 | 0.64 | 0.66 | 1.08 | 4.34 | 4.62 | 0.15 |
| bunka-kokugo-series-019-p4 | 0.18 | 0.47 | 0.48 | 0.44 | 0.22 | 0.24 | 1.12 |
| f1040 | 0.84 | 0.33 | 0.62 | 0.28 | 0.47 | 0.53 | 0.09 |
| mhlw-doc | 0.44 | 1.28 | 1.30 | 1.46 | 0.80 | 0.91 | 0.34 |
| nics-background-checks-2015-11 | 0.14 | 0.57 | 0.67 | 0.78 | 1.51 | 1.91 | 0.42 |
| patent-us223898 | 0.22 | 0.88 | 0.76 | 1.24 | 0.40 | 0.41 | 0.52 |
| pdf20-simple | 0.13 | 0.21 | 0.20 | 0.16 | 0.64 | 1.31 | 0.09 |
| pdfium-jpx-lzw | 0.09 | 0.26 | 0.34 | 0.27 | 0.73 | 0.42 | 0.05 |
| pdfium-links-highlights-annots | 0.11 | 0.18 | 0.22 | 0.17 | 0.69 | 1.39 | 0.07 |
| pdfium-ocg-invisible | 0.09 | 0.36 | 0.48 | 0.39 | 0.50 | 1.19 | 0.05 |
| pdfium-smask-blend | 0.05 | 0.28 | 0.37 | 0.32 | 0.51 | 0.53 | 0.06 |
| pdfium-type3 | 0.09 | 0.34 | 0.49 | 0.37 | 0.82 | 1.43 | 0.07 |
| senate-expenditures | 0.21 | 0.51 | 0.51 | 0.64 | 1.50 | 2.23 | 0.32 |
| usrguide | 0.11 | 1.61 | 1.42 | 2.43 | 2.89 | 3.67 | 0.27 |
| wdl6812-manuscript | 0.30 | 0.10 | 0.11 | 0.09 | 0.73 | 0.55 | 1.30 |
