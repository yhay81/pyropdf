---
title: Choosing pyropdf
description: What pyropdf is good at, what it does not do yet, and how it compares with PyMuPDF, pypdf, and pikepdf.
---

# Choosing pyropdf

## When pyropdf fits

- **Edits you can trust.** Every edit is a version: transactions undo
  themselves when they fail, `doc.history` undoes and redoes, and
  `doc.changes` says which signatures and PDF/A or PDF/UA claims a save
  would break before you save.
- **Saves that keep what you did not touch.** An incremental save appends
  only the changed objects, so signatures over the original bytes stay
  valid, and a save without edits reproduces its input.
- **Reading and rendering in one package.** Text in reading order, words,
  layout, tables, Markdown, offline OCR, and rendering to pixmaps and SVG,
  with no required dependencies.
- **A permissive license.** pyropdf is MIT licensed.
- **Modern Python.** CPython 3.11 to 3.15, free-threaded builds included,
  with parallel reads of one document.

## What it does not do yet

- Drawing new content on a page (`page.draw`), creating fonts, and free text
  and stamp annotations.
- Digital signatures: pyropdf keeps existing signatures valid, but does
  not sign yet.
- Safety for untrusted input is not claimed yet; see
  [Status and safety](status.md).
- Rendering is slower than MuPDF's (below).
- The API is alpha and may change between minor versions.

## Compared with other libraries

| | pyropdf | PyMuPDF | pypdf | pikepdf |
| --- | --- | --- | --- | --- |
| License | MIT | AGPL-3.0 or commercial | BSD-3-Clause | MPL-2.0 |
| Implementation | Rust (hayro renders) | C (MuPDF) | Pure Python | C++ (qpdf) |
| Render pages | yes | yes | no | no |
| Text, words, tables | yes | yes | text | no |
| Undo and atomic transactions | yes | journalling | no | no |

PyMuPDF is the most complete of these and the one pyropdf measures itself
against. On a corpus of 14 real-world documents, as the ratio of pyropdf's
time to PyMuPDF's (lower is faster):

| Operation | pyropdf / PyMuPDF |
| --- | ---: |
| Open | 0.19 |
| Text | 0.79 |
| Words | 0.75 |
| Search | 1.16 |
| Render 72 dpi | 2.65 |
| Render 144 dpi | 3.97 |
| Full save | 0.15 |

The method, the raw timings, and the feature-by-feature comparison are in
[evidence/pymupdf-comparison](https://github.com/yhay81/pyropdf/tree/main/evidence/pymupdf-comparison).
