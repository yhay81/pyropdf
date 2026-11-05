## Per document

Totals over the pages, best of 5 runs per page, in milliseconds. The walk tokenizes every stream that interpreting the page runs, once per distinct stream and resources. Operators, shown bytes, and invocations are B's counts: each stream's own times the interpretations of it.

| Document | Pages | Render, 72 dpi | Text | Walk | Walk / render | Walk / text | Page tokens alone | Streams | Decoded content (KiB) | Operators | Shown bytes | Invocations | Most operators on a page |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 110 | 843.9 | 242.2 | 20.70 | 2.5% | 8.5% | 5.62 | 115 | 566 | 25241 | 298609 | 5 | 1322 |
| bunka-kokugo-series-019-p4 | 1 | 14.3 | 0.0 | 0.01 | 0.1% | 43.6% | 0.00 | 1 | 0 | 4 | 0 | 0 | 4 |
| f1040 | 2 | 19.3 | 10.5 | 6.88 | 35.6% | 65.3% | 0.83 | 5 | 128 | 12264 | 9387 | 3 | 7124 |
| mhlw-doc | 2 | 10.4 | 8.8 | 2.59 | 24.8% | 29.3% | 0.28 | 2 | 72 | 7025 | 2423 | 0 | 5375 |
| nics-background-checks-2015-11 | 1 | 12.0 | 5.7 | 7.05 | 58.9% | 124.7% | 0.57 | 1 | 104 | 9205 | 4319 | 0 | 9205 |
| patent-us223898 | 4 | 37.2 | 13.2 | 2.03 | 5.4% | 15.4% | 0.26 | 4 | 85 | 6255 | 11221 | 0 | 2553 |
| pdf20-simple | 1 | 0.3 | 0.1 | 0.02 | 6.0% | 20.0% | 0.00 | 1 | 1 | 26 | 11 | 0 | 26 |
| pdfium-jpx-lzw | 1 | 7.5 | 0.0 | 0.01 | 0.2% | 42.8% | 0.00 | 1 | 0 | 4 | 0 | 0 | 4 |
| pdfium-links-highlights-annots | 1 | 0.7 | 0.1 | 0.02 | 3.1% | 15.6% | 0.00 | 1 | 0 | 11 | 91 | 0 | 11 |
| pdfium-ocg-invisible | 1 | 0.0 | 0.0 | 0.00 | 11.3% | 35.6% | 0.00 | 1 | 0 | 6 | 0 | 0 | 6 |
| pdfium-smask-blend | 1 | 0.2 | 0.0 | 0.02 | 10.1% | 78.8% | 0.00 | 4 | 0 | 24 | 0 | 3 | 24 |
| pdfium-type3 | 1 | 0.1 | 0.0 | 0.03 | 25.1% | 108.9% | 0.00 | 2 | 0 | 21 | 3 | 3 | 21 |
| senate-expenditures | 1 | 8.8 | 3.9 | 4.43 | 50.1% | 112.3% | 0.43 | 1 | 137 | 11562 | 3880 | 0 | 11562 |
| usrguide | 27 | 135.3 | 82.7 | 9.75 | 7.2% | 11.8% | 1.29 | 27 | 198 | 9687 | 46581 | 0 | 754 |
| wdl6812-manuscript | 2 | 26.8 | 0.1 | 0.04 | 0.1% | 71.5% | 0.01 | 2 | 0 | 16 | 0 | 0 | 8 |
| **Corpus** | 156 | **1116.7** | **367.5** | **53.57** | **4.8%** | **14.6%** | **9.30** | | | | | | |

## Per page

| | Median | 90th percentile | Largest |
| --- | ---: | ---: | ---: |
| Walk (ms) | 0.177 | 0.390 | 7.052 |
| Walk / render | 2.4% | 8.3% | 58.9% (nics-background-checks-2015-11 page 1) |
| Operators | 223 | 477 | 11562 |
| Invocations | 0 | 0 | 5 |

Pages with a stream that invokes itself: 0. Type 3 codes that `/Differences` does not map: 0.

## Candidate limits

Pages whose operators exceed a limit of `interpretation_fuel`, counted in operators.

| Limit | Pages refused |
| ---: | ---: |
| 16384 | 0 |
| 65536 | 0 |
| 262144 | 0 |
| 1048576 | 0 |
| 4194304 | 0 |
| 16777216 | 0 |
