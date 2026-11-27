# Side-by-Side Layout Accuracy on Labeled Corpus (11 pages + 2 scans)

Ground-truth anchors and table labels come from `evidence/pymupdf-comparison/quality/*.json`.

## Aggregate summary (11 labeled pages)

| Engine | Anchors found | Order pairs OK | True tables (`nics`, `senate`) | False-positive table pages (of 7) | Japanese headings (`mhlw p1+p2`) |
| --- | ---: | ---: | ---: | ---: | ---: |
| pyropdf heuristic (text) | 140/140 | 129/129 | 2/2 | 0/7 | 0 |
| pyropdf heuristic (md) | 137/140 | 126/126 | 2/2 | 0/7 | 0 |
| pymupdf-layout 2.2 (md) | 103/140 | 90/92 | 2/2 | 3/7 | 0 |
| PP-DocLayout-S (FP32, 480x480) | 140/140 | 118/129 | 0/2 | 1/7 | 6 |
| PP-DocLayout_plus-L (FP32, 800x800) | 140/140 | 125/129 | 2/2 | 0/7 | 9 |
| PP-DocLayout_plus-L (INT8 MatMul+Conv, 800x800) | 140/140 | 122/129 | 2/2 | 0/7 | 9 |
| PP-DocLayoutV3 (FP32, 800x800) | 140/140 | 126/129 | 2/2 | 0/7 | 9 |
| PP-DocLayoutV3 (INT8 Conv-only, 800x800) | 140/140 | 124/129 | 2/2 | 0/7 | 9 |

## Per-page semantic region comparison (`pymupdf-layout 2.2` vs `PP-DocLayout_plus-L INT8` vs `PP-DocLayoutV3 FP32`)

| Page | `pymupdf-layout 2.2` regions (ms) | `PP-DocLayout_plus-L INT8` regions (4t ms) | `PP-DocLayoutV3 FP32` regions (4t ms) |
| --- | --- | --- | --- |
| `bill-hr815-p1` | `{'list-item': 15, 'page-header': 1, 'picture': 1, 'section-header': 2, 'text': 14}` (92 ms) | `{'header': 2, 'doc_title': 1, 'text': 21, 'paragraph_title': 3}` (391 ms) | `{'header_image': 1, 'header': 1, 'doc_title': 1, 'text': 23, 'paragraph_title': 3}` (769 ms) |
| `f1040-p1` | `{'table': 3}` (640 ms) | `{'table': 1, 'footer': 1}` (397 ms) | `{'table': 1, 'vision_footnote': 1}` (706 ms) |
| `f1040-p2` | `{'table': 2}` (461 ms) | `{'table': 1, 'number': 1, 'text': 1}` (379 ms) | `{'number': 1, 'table': 1, 'vision_footnote': 2}` (702 ms) |
| `mhlw-doc-p1` | `{'list-item': 14, 'page-footer': 1, 'page-header': 1, 'text': 3}` (66 ms) | `{'header': 2, 'text': 12, 'paragraph_title': 6, 'number': 1}` (362 ms) | `{'header': 1, 'doc_title': 1, 'paragraph_title': 5, 'text': 12, 'number': 1}` (725 ms) |
| `mhlw-doc-p2` | `{'list-item': 3, 'page-footer': 1, 'text': 4}` (50 ms) | `{'paragraph_title': 3, 'text': 4, 'number': 1}` (349 ms) | `{'paragraph_title': 3, 'text': 4, 'number': 1}` (700 ms) |
| `nics-background-checks-2015-11-p1` | `{'page-footer': 1, 'section-header': 2, 'table': 1, 'text': 6}` (1175 ms) | `{'figure_title': 1, 'table': 1, 'number': 1}` (351 ms) | `{'figure_title': 1, 'table': 1, 'vision_footnote': 2, 'number': 1}` (702 ms) |
| `patent-us223898-p2` | `{'table': 1, 'title': 1}` (581 ms) | `{'doc_title': 1, 'text': 7, 'header': 1}` (421 ms) | `{'header': 1, 'doc_title': 1, 'text': 14, 'aside_text': 13}` (692 ms) |
| `patent-us223898-p3` | `{'list-item': 1, 'text': 3, 'title': 1}` (384 ms) | `{'header': 2, 'text': 27}` (365 ms) | `{'header': 2, 'aside_text': 18, 'text': 20}` (692 ms) |
| `senate-expenditures-p1` | `{'table': 1, 'text': 1}` (224 ms) | `{'table': 1}` (355 ms) | `{'table': 1, 'number': 1}` (727 ms) |
| `usrguide-p1` | `{'page-footer': 1, 'section-header': 2, 'table': 1, 'text': 3}` (139 ms) | `{'doc_title': 1, 'text': 2, 'paragraph_title': 1, 'content': 1, 'footnote': 1, 'number': 1}` (353 ms) | `{'doc_title': 1, 'text': 2, 'paragraph_title': 1, 'content': 1, 'footnote': 1, 'number': 1}` (1280 ms) |
| `usrguide-p2` | `{'page-footer': 1, 'section-header': 4, 'table': 1, 'text': 5}` (122 ms) | `{'paragraph_title': 4, 'text': 5, 'number': 1}` (346 ms) | `{'content': 1, 'paragraph_title': 4, 'text': 5, 'inline_formula': 1, 'number': 1}` (1729 ms) |

## Image-only scanned documents (zero embedded PDF text)

| Scan | `PP-DocLayout_plus-L INT8` regions (4t ms) | `PP-DocLayoutV3 FP32` regions (4t ms) |
| --- | --- | --- |
| `bunka-kokugo-series-019-p4-p1` | `{'text': 10, 'paragraph_title': 1, 'doc_title': 1}` (1151 ms) | `{'text': 7, 'paragraph_title': 1}` (2527 ms) |
| `wdl6812-manuscript-p1` | `{'image': 1}` (1423 ms) | `{'image': 1}` (2123 ms) |
