# Damaged-PDF repair coverage (darwin-arm64)

Measured 2026-11-27 on macOS-27.2-arm64-arm-64bit-Mach-O (arm).

## Engines

- **pyropdf**: `pyropdf 0.4.1` (Python 3.15.2)
- **pymupdf**: `PyMuPDF 1.28.2` (Python 3.13.18)
- **qpdf**: `qpdf version 12.4.2` (Python 3.15.2)
- **pdfium**: `pypdfium2 5.15.2` (Python 3.13.18)

Matrix: **316** damaged PDFs (285 corpus-derived across 15 real-world PDFs × 19 damage kinds, plus 31 targeted synthetic malformed fixtures).

Incremental save refusal on repaired pyropdf documents: **256/256** refused.

## Overall recovery rates

| Metric | pyropdf | pymupdf | qpdf | pdfium |
| --- | ---: | ---: | ---: | ---: |
| Open | 314/316 (99.4%) | 309/316 (97.8%) | 260/316 (82.3%) | 214/316 (67.7%) |
| Pages >= 1 | 309/316 (97.8%) | 276/316 (87.3%) | 260/316 (82.3%) | 214/316 (67.7%) |
| Exact pages | 305/316 (96.5%) | 276/316 (87.3%) | 260/316 (82.3%) | 214/316 (67.7%) |
| Text p0 | 291/316 (92.1%) | 257/316 (81.3%) | n/a | 199/316 (63.0%) |
| Render p0 | 308/316 (97.5%) | 276/316 (87.3%) | n/a | 212/316 (67.1%) |
| Full save | 300/316 (94.9%) | 267/316 (84.5%) | 256/316 (81.0%) | 206/316 (65.2%) |
| Native crashes | 0 | 1 | 0 | 0 |

## Corpus-derived suite summary

| Metric | pyropdf | pymupdf | qpdf | pdfium |
| --- | ---: | ---: | ---: | ---: |
| Open | 283/285 (99.3%) | 278/285 (97.5%) | 241/285 (84.6%) | 195/285 (68.4%) |
| Pages >= 1 | 279/285 (97.9%) | 252/285 (88.4%) | 241/285 (84.6%) | 195/285 (68.4%) |
| Exact pages | 275/285 (96.5%) | 252/285 (88.4%) | 241/285 (84.6%) | 195/285 (68.4%) |
| Text p0 | 261/285 (91.6%) | 234/285 (82.1%) | n/a | 182/285 (63.9%) |
| Render p0 | 278/285 (97.5%) | 252/285 (88.4%) | n/a | 194/285 (68.1%) |
| Full save | 271/285 (95.1%) | 243/285 (85.3%) | 237/285 (83.2%) | 188/285 (66.0%) |
| Native crashes | 0 | 1 | 0 | 0 |

### Corpus recovery by category (`Pages >= 1`)

| Category | Cases | pyropdf | pymupdf | qpdf | pdfium |
| --- | ---: | ---: | ---: | ---: | ---: |
| `truncate-95` | 15 | 12/15 (80.0%) | 11/15 (73.3%) | 10/15 (66.7%) | 2/15 (13.3%) |
| `truncate-60` | 15 | 12/15 (80.0%) | 6/15 (40.0%) | 8/15 (53.3%) | 0/15 (0.0%) |
| `junk-prefix-512` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `startxref-keyword` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `xref-section-removed` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 12/15 (80.0%) | 12/15 (80.0%) |
| `length-off-37` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `bit-flips-12` | 15 | 15/15 (100.0%) | 13/15 (86.7%) | 15/15 (100.0%) | 12/15 (80.0%) |
| `endobj-removed-2` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 14/15 (93.3%) | 13/15 (86.7%) |
| `bytes-inserted-late` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `truncated-eof-trailer` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 12/15 (80.0%) | 3/15 (20.0%) |
| `truncated-mid-trailer` | 15 | 15/15 (100.0%) | 12/15 (80.0%) | 12/15 (80.0%) | 3/15 (20.0%) |
| `corrupt-xref-payload` | 15 | 15/15 (100.0%) | 14/15 (93.3%) | 11/15 (73.3%) | 15/15 (100.0%) |
| `shifted-offsets-start` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 14/15 (93.3%) | 15/15 (100.0%) |
| `leading-garbage-2048` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 14/15 (93.3%) | 0/15 (0.0%) |
| `trailing-garbage-4096` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `trailing-stray-object` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `wrong-startxref-offset` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `corrupt-root-ref` | 15 | 15/15 (100.0%) | 1/15 (6.7%) | 0/15 (0.0%) | 0/15 (0.0%) |
| `broken-prev-chain` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 14/15 (93.3%) | 15/15 (100.0%) |

### Corpus full-save recovery by category (`Full save`)

| Category | Cases | pyropdf | pymupdf | qpdf | pdfium |
| --- | ---: | ---: | ---: | ---: | ---: |
| `truncate-95` | 15 | 11/15 (73.3%) | 10/15 (66.7%) | 10/15 (66.7%) | 1/15 (6.7%) |
| `truncate-60` | 15 | 11/15 (73.3%) | 4/15 (26.7%) | 8/15 (53.3%) | 0/15 (0.0%) |
| `junk-prefix-512` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `startxref-keyword` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `xref-section-removed` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 12/15 (80.0%) | 12/15 (80.0%) |
| `length-off-37` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `bit-flips-12` | 15 | 9/15 (60.0%) | 8/15 (53.3%) | 11/15 (73.3%) | 8/15 (53.3%) |
| `endobj-removed-2` | 15 | 15/15 (100.0%) | 14/15 (93.3%) | 14/15 (93.3%) | 11/15 (73.3%) |
| `bytes-inserted-late` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `truncated-eof-trailer` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 12/15 (80.0%) | 3/15 (20.0%) |
| `truncated-mid-trailer` | 15 | 15/15 (100.0%) | 12/15 (80.0%) | 12/15 (80.0%) | 3/15 (20.0%) |
| `corrupt-xref-payload` | 15 | 15/15 (100.0%) | 14/15 (93.3%) | 11/15 (73.3%) | 15/15 (100.0%) |
| `shifted-offsets-start` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 14/15 (93.3%) | 15/15 (100.0%) |
| `leading-garbage-2048` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 14/15 (93.3%) | 0/15 (0.0%) |
| `trailing-garbage-4096` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `trailing-stray-object` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `wrong-startxref-offset` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) | 15/15 (100.0%) |
| `corrupt-root-ref` | 15 | 15/15 (100.0%) | 1/15 (6.7%) | 0/15 (0.0%) | 0/15 (0.0%) |
| `broken-prev-chain` | 15 | 15/15 (100.0%) | 15/15 (100.0%) | 14/15 (93.3%) | 15/15 (100.0%) |

## Targeted malformed fixtures summary

| Metric | pyropdf | pymupdf | qpdf | pdfium |
| --- | ---: | ---: | ---: | ---: |
| Open | 31/31 (100.0%) | 31/31 (100.0%) | 19/31 (61.3%) | 19/31 (61.3%) |
| Pages >= 1 | 30/31 (96.8%) | 24/31 (77.4%) | 19/31 (61.3%) | 19/31 (61.3%) |
| Exact pages | 30/31 (96.8%) | 24/31 (77.4%) | 19/31 (61.3%) | 19/31 (61.3%) |
| Text p0 | 30/31 (96.8%) | 23/31 (74.2%) | n/a | 17/31 (54.8%) |
| Render p0 | 30/31 (96.8%) | 24/31 (77.4%) | n/a | 18/31 (58.1%) |
| Full save | 29/31 (93.5%) | 24/31 (77.4%) | 19/31 (61.3%) | 18/31 (58.1%) |
| Native crashes | 0 | 0 | 0 | 0 |

### Targeted fixtures by category (`Pages >= 1`)

| Category | Cases | pyropdf | pymupdf | qpdf | pdfium |
| --- | ---: | ---: | ---: | ---: | ---: |
| `truncated-eof-trailer` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `truncated-mid-trailer` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) |
| `corrupt-xref-payload` | 4 | 4/4 (100.0%) | 3/4 (75.0%) | 2/4 (50.0%) | 4/4 (100.0%) |
| `shifted-offsets` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `leading-garbage` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) |
| `trailing-garbage` | 2 | 2/2 (100.0%) | 2/2 (100.0%) | 2/2 (100.0%) | 2/2 (100.0%) |
| `missing-startxref` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `broken-prev-chain` | 3 | 3/3 (100.0%) | 3/3 (100.0%) | 3/3 (100.0%) | 3/3 (100.0%) |
| `broken-root` | 3 | 3/3 (100.0%) | 0/3 (0.0%) | 0/3 (0.0%) | 0/3 (0.0%) |
| `broken-root-unreferenced-pages` | 2 | 2/2 (100.0%) | 0/2 (0.0%) | 0/2 (0.0%) | 0/2 (0.0%) |
| `duplicate-generations` | 3 | 3/3 (100.0%) | 3/3 (100.0%) | 1/3 (33.3%) | 3/3 (100.0%) |
| `damaged-objstm` | 2 | 1/2 (50.0%) | 1/2 (50.0%) | 1/2 (50.0%) | 1/2 (50.0%) |
| `missing-size` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `missing-header` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) | 0/1 (0.0%) |
| `line-endings` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `corrupt-stream-length` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `object-zero` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) |
| `missing-endobj` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) | 0/1 (0.0%) |
| `encrypted-broken-root` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) |

### Targeted fixtures full-save recovery by category (`Full save`)

| Category | Cases | pyropdf | pymupdf | qpdf | pdfium |
| --- | ---: | ---: | ---: | ---: | ---: |
| `truncated-eof-trailer` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `truncated-mid-trailer` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) |
| `corrupt-xref-payload` | 4 | 4/4 (100.0%) | 3/4 (75.0%) | 2/4 (50.0%) | 4/4 (100.0%) |
| `shifted-offsets` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `leading-garbage` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) |
| `trailing-garbage` | 2 | 2/2 (100.0%) | 2/2 (100.0%) | 2/2 (100.0%) | 2/2 (100.0%) |
| `missing-startxref` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `broken-prev-chain` | 3 | 3/3 (100.0%) | 3/3 (100.0%) | 3/3 (100.0%) | 3/3 (100.0%) |
| `broken-root` | 3 | 3/3 (100.0%) | 0/3 (0.0%) | 0/3 (0.0%) | 0/3 (0.0%) |
| `broken-root-unreferenced-pages` | 2 | 2/2 (100.0%) | 0/2 (0.0%) | 0/2 (0.0%) | 0/2 (0.0%) |
| `duplicate-generations` | 3 | 3/3 (100.0%) | 3/3 (100.0%) | 1/3 (33.3%) | 2/3 (66.7%) |
| `damaged-objstm` | 2 | 1/2 (50.0%) | 1/2 (50.0%) | 1/2 (50.0%) | 1/2 (50.0%) |
| `missing-size` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `missing-header` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) | 0/1 (0.0%) |
| `line-endings` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `corrupt-stream-length` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) |
| `object-zero` | 1 | 0/1 (0.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) |
| `missing-endobj` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) | 0/1 (0.0%) |
| `encrypted-broken-root` | 1 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) |

## Remaining gaps vs other engines (8)

- `corpus/truncate-95/patent-us223898.pdf`: save (recovered by pymupdf, qpdf) — `save-qpdf: ERROR: page 4: page object 45 0: item index 2 (from 0): ignoring non-stream in an array of streams`
- `corpus/truncate-95/senate-expenditures.pdf`: pages (recovered by pymupdf), render (recovered by pymupdf) — `reopen: is_repaired=False, pages=0; save-qpdf: Pages tree includes non-dictionary object; ignoring`
- `corpus/truncate-60/pdf20-simple.pdf`: save (recovered by qpdf) — `text mismatch: got ''; save-qpdf: ERROR: page 1: page object 4 0: item index 0 (from 0): ignoring non-stream in an array of streams`
- `corpus/truncate-60/senate-expenditures.pdf`: pages (recovered by pymupdf), render (recovered by pymupdf) — `reopen: is_repaired=False, pages=0; save-qpdf: Pages tree includes non-dictionary object; ignoring`
- `corpus/bit-flips-12/f1040.pdf`: render (recovered by pdfium), save (recovered by qpdf, pdfium) — `render: object 823 0: the bytes do not begin with a PDF value; save: saving object 187 0: the object being saved does not begin with a PDF value`
- `corpus/bit-flips-12/pdf20-simple.pdf`: save (recovered by qpdf, pdfium) — `save: saving object 8 0: the object being saved does not begin with a PDF value`
- `targeted/objstm-mismatched-index`: pages (recovered by pymupdf, qpdf, pdfium), text (recovered by pymupdf, pdfium), render (recovered by pymupdf, pdfium), save (recovered by pymupdf, qpdf, pdfium) — `pages: the catalog names no page tree; save: saving object 2 0: an in-use base object has no readable bytes`
- `targeted/object-zero-in-body`: save (recovered by pymupdf, qpdf) — `save: cannot save live object number 0: it is reserved for the free-list head`
