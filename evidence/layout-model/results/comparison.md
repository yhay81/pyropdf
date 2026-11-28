# Side-by-Side Layout Accuracy on Labeled Corpus (12 native pages + 2 OCR scans)

Ground-truth anchors and table labels come from all 14 files in `evidence/pymupdf-comparison/quality/*.json`.
Table counts cover 3 ground-truth table pages (`mhlw-deposit-report-p1`, `nics`, `senate`) and 7 non-table pages; the 2 gridded IRS tax forms (`f1040-p1`, `f1040-p2`) are excluded from both buckets and reported individually below.

## Aggregate summary (12 native labeled pages)

| Engine | Anchors found | Order pairs OK | True tables (of 3) | False-positive table pages (of 7) | Japanese headings (`mhlw-doc p1+p2`) |
| --- | ---: | ---: | ---: | ---: | ---: |
| pyropdf heuristic (text) | 152/152 | 140/140 | 3/3 | 0/7 | 0 |
| pyropdf heuristic (md) | 147/152 | 134/135 | 3/3 | 0/7 | 0 |
| pymupdf-layout 2.2 (md) | 112/152 | 98/100 | 2/3 | 3/7 | 0 |
| PP-DocLayout-S (FP32, 480x480) | 152/152 | 129/140 | 0/3 | 1/7 | 6 |
| PP-DocLayout_plus-L (FP32, 800x800) | 152/152 | 136/140 | 3/3 | 0/7 | 9 |
| PP-DocLayout_plus-L (INT8 MatMul+Conv, 800x800) | 152/152 | 133/140 | 3/3 | 0/7 | 9 |
| PP-DocLayoutV3 (FP32, 800x800) | 152/152 | 137/140 | 3/3 | 0/7 | 9 |
| PP-DocLayoutV3 (INT8 Conv-only, 800x800) | 152/152 | 135/140 | 3/3 | 0/7 | 9 |

## Per-page semantic region comparison (`pymupdf-layout 2.2` vs `PP-DocLayout_plus-L INT8` vs `PP-DocLayoutV3 FP32`)

| Page | `pymupdf-layout 2.2` regions (ms) | `PP-DocLayout_plus-L INT8` regions (4t ms) | `PP-DocLayoutV3 FP32` regions (4t ms) |
| --- | --- | --- | --- |
| `bill-hr815-p1` | `{'list-item': 15, 'page-header': 1, 'picture': 1, 'section-header': 2, 'text': 14}` (92 ms) | `{'header': 2, 'doc_title': 1, 'text': 21, 'paragraph_title': 3}` (435 ms) | `{'header_image': 1, 'header': 1, 'doc_title': 1, 'text': 23, 'paragraph_title': 3}` (845 ms) |
| `f1040-p1` | `{'table': 3}` (640 ms) | `{'table': 1, 'footer': 1}` (359 ms) | `{'table': 1, 'vision_footnote': 1}` (745 ms) |
| `f1040-p2` | `{'table': 2}` (461 ms) | `{'table': 1, 'number': 1, 'text': 1}` (353 ms) | `{'number': 1, 'table': 1, 'vision_footnote': 2}` (739 ms) |
| `mhlw-deposit-report-p1` | `{'caption': 1, 'page-footer': 1, 'picture': 2}` (207 ms) | `{'text': 2, 'table': 1}` (353 ms) | `{'doc_title': 1, 'text': 2, 'table': 1, 'vision_footnote': 1}` (751 ms) |
| `mhlw-doc-p1` | `{'list-item': 14, 'page-footer': 1, 'page-header': 1, 'text': 3}` (66 ms) | `{'header': 2, 'text': 12, 'paragraph_title': 6, 'number': 1}` (367 ms) | `{'header': 1, 'doc_title': 1, 'paragraph_title': 5, 'text': 12, 'number': 1}` (743 ms) |
| `mhlw-doc-p2` | `{'list-item': 3, 'page-footer': 1, 'text': 4}` (50 ms) | `{'paragraph_title': 3, 'text': 4, 'number': 1}` (346 ms) | `{'paragraph_title': 3, 'text': 4, 'number': 1}` (765 ms) |
| `nics-background-checks-2015-11-p1` | `{'page-footer': 1, 'section-header': 2, 'table': 1, 'text': 6}` (1175 ms) | `{'figure_title': 1, 'table': 1, 'number': 1}` (340 ms) | `{'figure_title': 1, 'table': 1, 'vision_footnote': 2, 'number': 1}` (1097 ms) |
| `patent-us223898-p2` | `{'table': 1, 'title': 1}` (581 ms) | `{'doc_title': 1, 'text': 7, 'header': 1}` (356 ms) | `{'header': 1, 'doc_title': 1, 'text': 14, 'aside_text': 13}` (833 ms) |
| `patent-us223898-p3` | `{'list-item': 1, 'text': 3, 'title': 1}` (384 ms) | `{'header': 2, 'text': 27}` (397 ms) | `{'header': 2, 'aside_text': 18, 'text': 20}` (789 ms) |
| `senate-expenditures-p1` | `{'table': 1, 'text': 1}` (224 ms) | `{'table': 1}` (424 ms) | `{'table': 1, 'number': 1}` (1136 ms) |
| `usrguide-p1` | `{'page-footer': 1, 'section-header': 2, 'table': 1, 'text': 3}` (139 ms) | `{'doc_title': 1, 'text': 2, 'paragraph_title': 1, 'content': 1, 'footnote': 1, 'number': 1}` (352 ms) | `{'doc_title': 1, 'text': 2, 'paragraph_title': 1, 'content': 1, 'footnote': 1, 'number': 1}` (1132 ms) |
| `usrguide-p2` | `{'page-footer': 1, 'section-header': 4, 'table': 1, 'text': 5}` (122 ms) | `{'paragraph_title': 4, 'text': 5, 'number': 1}` (387 ms) | `{'content': 1, 'paragraph_title': 4, 'text': 5, 'inline_formula': 1, 'number': 1}` (1417 ms) |

## Image-only scanned documents (2 OCR fixtures, zero embedded PDF text)

| Scan | `PP-DocLayout_plus-L INT8` regions (4t ms) | `PP-DocLayoutV3 FP32` regions (4t ms) |
| --- | --- | --- |
| `bunka-kokugo-series-019-p4-p1` | `{'text': 10, 'paragraph_title': 1, 'doc_title': 1}` (1151 ms) | `{'text': 7, 'paragraph_title': 1}` (2527 ms) |
| `wdl6812-manuscript-p1` | `{'image': 1}` (1423 ms) | `{'image': 1}` (2123 ms) |
