# pyropdf v0.3.1 and PyMuPDF 2.1

Measured on macOS-27.2-arm64-arm-64bit-Mach-O, Python 3.13.18. Corpus and label revision [`4ac61ce4fc41`](https://github.com/yhay81/pyropdf/blob/4ac61ce4fc4164a2ec014d8c3fb2ad34dfcba804/tests/assets/real_world). [Release specification map](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/docs/spec-coverage.md).

| Wheel | SHA-256 |
| --- | --- |
| `pymupdf-2.1-cp311-abi3-macosx_11_0_arm64.whl` | `1bb2284bde6067f05f83961ea678480060870dc966ad520bcf97da0b0eccf40c` |
| `pyropdf-0.3.1-cp311-abi3-macosx_11_0_arm64.whl` | `1944b462a91c9fbc7fdb972eef66491d0d7383814970f4aa625a404940deb9c1` |

Run from the evidence checkout, with these wheels in the current directory:

```sh
python3.13 evidence/pymupdf-comparison/release.py v0.3.1 pyropdf-0.3.1-cp311-abi3-macosx_11_0_arm64.whl pymupdf-2.1-cp311-abi3-macosx_11_0_arm64.whl comparison
```

## Timings

Best of 5 samples (ms), on fresh documents. Operations open a document, read all pages, search for `the`, or save once. Rendering reads raw samples at the named DPI. The ratio is pyropdf / PyMuPDF. Values above 1 are slower. Totals sum the best times only for paired successes and give the denominator. Run-to-run noise matters for short operations.

| Operation | Complete pairs | PyMuPDF ms | pyropdf ms | Ratio |
| --- | ---: | ---: | ---: | ---: |
| open | 15/15 | 7.591 | 0.981 | 0.13 |
| text | 15/15 | 420.630 | 209.611 | 0.50 |
| words | 15/15 | 733.416 | 303.175 | 0.41 |
| search | 15/15 | 561.691 | 209.447 | 0.37 |
| render_72 | 15/15 | 315.994 | 557.826 | 1.77 |
| render_144 | 15/15 | 508.395 | 923.175 | 1.82 |
| save | 15/15 | 48.438 | 7.768 | 0.16 |

### Individual documents

| Document | Operation | PyMuPDF ms | pyropdf ms | Ratio |
| --- | --- | ---: | ---: | ---: |
| bill-hr815.pdf | open | 0.360 | 0.093 | 0.26 |
| bunka-kokugo-series-019-p4.pdf | open | 0.461 | 0.047 | 0.10 |
| f1040.pdf | open | 0.350 | 0.301 | 0.86 |
| mhlw-doc.pdf | open | 0.781 | 0.072 | 0.09 |
| nics-background-checks-2015-11.pdf | open | 0.374 | 0.041 | 0.11 |
| patent-us223898.pdf | open | 0.334 | 0.053 | 0.16 |
| pdf20-simple.pdf | open | 0.521 | 0.030 | 0.06 |
| pdfium-jpx-lzw.pdf | open | 0.539 | 0.028 | 0.05 |
| pdfium-links-highlights-annots.pdf | open | 0.541 | 0.029 | 0.05 |
| pdfium-ocg-invisible.pdf | open | 0.542 | 0.029 | 0.05 |
| pdfium-smask-blend.pdf | open | 0.547 | 0.027 | 0.05 |
| pdfium-type3.pdf | open | 0.535 | 0.028 | 0.05 |
| senate-expenditures.pdf | open | 0.341 | 0.038 | 0.11 |
| usrguide.pdf | open | 1.068 | 0.106 | 0.10 |
| wdl6812-manuscript.pdf | open | 0.296 | 0.058 | 0.20 |
| bill-hr815.pdf | text | 292.791 | 117.334 | 0.40 |
| bunka-kokugo-series-019-p4.pdf | text | 0.095 | 0.053 | 0.56 |
| f1040.pdf | text | 30.468 | 9.462 | 0.31 |
| mhlw-doc.pdf | text | 8.335 | 7.255 | 0.87 |
| nics-background-checks-2015-11.pdf | text | 7.205 | 3.159 | 0.44 |
| patent-us223898.pdf | text | 10.647 | 7.204 | 0.68 |
| pdf20-simple.pdf | text | 0.436 | 0.085 | 0.20 |
| pdfium-jpx-lzw.pdf | text | 0.088 | 0.026 | 0.30 |
| pdfium-links-highlights-annots.pdf | text | 0.567 | 0.118 | 0.21 |
| pdfium-ocg-invisible.pdf | text | 0.076 | 0.033 | 0.43 |
| pdfium-smask-blend.pdf | text | 0.100 | 0.043 | 0.43 |
| pdfium-type3.pdf | text | 0.099 | 0.039 | 0.39 |
| senate-expenditures.pdf | text | 6.479 | 2.409 | 0.37 |
| usrguide.pdf | text | 62.636 | 62.325 | 1.00 |
| wdl6812-manuscript.pdf | text | 0.607 | 0.068 | 0.11 |
| bill-hr815.pdf | words | 537.821 | 187.533 | 0.35 |
| bunka-kokugo-series-019-p4.pdf | words | 0.076 | 0.042 | 0.56 |
| f1040.pdf | words | 38.357 | 12.409 | 0.32 |
| mhlw-doc.pdf | words | 9.227 | 7.401 | 0.80 |
| nics-background-checks-2015-11.pdf | words | 11.332 | 4.833 | 0.43 |
| patent-us223898.pdf | words | 19.384 | 9.807 | 0.51 |
| pdf20-simple.pdf | words | 0.415 | 0.082 | 0.20 |
| pdfium-jpx-lzw.pdf | words | 0.064 | 0.023 | 0.37 |
| pdfium-links-highlights-annots.pdf | words | 0.591 | 0.125 | 0.21 |
| pdfium-ocg-invisible.pdf | words | 0.054 | 0.029 | 0.54 |
| pdfium-smask-blend.pdf | words | 0.080 | 0.037 | 0.47 |
| pdfium-type3.pdf | words | 0.075 | 0.032 | 0.43 |
| senate-expenditures.pdf | words | 9.902 | 3.143 | 0.32 |
| usrguide.pdf | words | 105.489 | 77.612 | 0.74 |
| wdl6812-manuscript.pdf | words | 0.552 | 0.067 | 0.12 |
| bill-hr815.pdf | search | 410.807 | 119.737 | 0.29 |
| bunka-kokugo-series-019-p4.pdf | search | 0.073 | 0.037 | 0.50 |
| f1040.pdf | search | 32.466 | 7.127 | 0.22 |
| mhlw-doc.pdf | search | 7.790 | 7.176 | 0.92 |
| nics-background-checks-2015-11.pdf | search | 7.832 | 3.070 | 0.39 |
| patent-us223898.pdf | search | 14.473 | 7.426 | 0.51 |
| pdf20-simple.pdf | search | 0.388 | 0.060 | 0.16 |
| pdfium-jpx-lzw.pdf | search | 0.063 | 0.018 | 0.28 |
| pdfium-links-highlights-annots.pdf | search | 0.490 | 0.088 | 0.18 |
| pdfium-ocg-invisible.pdf | search | 0.051 | 0.022 | 0.44 |
| pdfium-smask-blend.pdf | search | 0.076 | 0.026 | 0.34 |
| pdfium-type3.pdf | search | 0.066 | 0.023 | 0.35 |
| senate-expenditures.pdf | search | 5.436 | 2.435 | 0.45 |
| usrguide.pdf | search | 81.124 | 62.155 | 0.77 |
| wdl6812-manuscript.pdf | search | 0.556 | 0.046 | 0.08 |
| bill-hr815.pdf | render_72 | 93.712 | 397.842 | 4.25 |
| bunka-kokugo-series-019-p4.pdf | render_72 | 36.804 | 7.155 | 0.19 |
| f1040.pdf | render_72 | 34.984 | 12.476 | 0.36 |
| mhlw-doc.pdf | render_72 | 10.322 | 6.294 | 0.61 |
| nics-background-checks-2015-11.pdf | render_72 | 5.731 | 6.183 | 1.08 |
| patent-us223898.pdf | render_72 | 57.396 | 19.872 | 0.35 |
| pdf20-simple.pdf | render_72 | 0.435 | 0.169 | 0.39 |
| pdfium-jpx-lzw.pdf | render_72 | 5.080 | 3.396 | 0.67 |
| pdfium-links-highlights-annots.pdf | render_72 | 0.789 | 0.347 | 0.44 |
| pdfium-ocg-invisible.pdf | render_72 | 0.083 | 0.042 | 0.50 |
| pdfium-smask-blend.pdf | render_72 | 0.179 | 0.101 | 0.57 |
| pdfium-type3.pdf | render_72 | 0.094 | 0.057 | 0.61 |
| senate-expenditures.pdf | render_72 | 4.940 | 5.100 | 1.03 |
| usrguide.pdf | render_72 | 38.140 | 81.671 | 2.14 |
| wdl6812-manuscript.pdf | render_72 | 27.306 | 17.119 | 0.63 |
| bill-hr815.pdf | render_144 | 178.692 | 689.629 | 3.86 |
| bunka-kokugo-series-019-p4.pdf | render_144 | 52.002 | 11.735 | 0.23 |
| f1040.pdf | render_144 | 38.483 | 15.837 | 0.41 |
| mhlw-doc.pdf | render_144 | 13.187 | 8.718 | 0.66 |
| nics-background-checks-2015-11.pdf | render_144 | 7.534 | 8.602 | 1.14 |
| patent-us223898.pdf | render_144 | 98.930 | 29.352 | 0.30 |
| pdf20-simple.pdf | render_144 | 0.681 | 0.470 | 0.69 |
| pdfium-jpx-lzw.pdf | render_144 | 14.655 | 4.647 | 0.32 |
| pdfium-links-highlights-annots.pdf | render_144 | 1.341 | 0.858 | 0.64 |
| pdfium-ocg-invisible.pdf | render_144 | 0.095 | 0.075 | 0.79 |
| pdfium-smask-blend.pdf | render_144 | 0.405 | 0.251 | 0.62 |
| pdfium-type3.pdf | render_144 | 0.109 | 0.083 | 0.76 |
| senate-expenditures.pdf | render_144 | 5.204 | 7.125 | 1.37 |
| usrguide.pdf | render_144 | 50.679 | 125.281 | 2.47 |
| wdl6812-manuscript.pdf | render_144 | 46.397 | 20.511 | 0.44 |
| bill-hr815.pdf | save | 5.823 | 0.896 | 0.15 |
| bunka-kokugo-series-019-p4.pdf | save | 0.214 | 0.219 | 1.02 |
| f1040.pdf | save | 31.571 | 2.974 | 0.09 |
| mhlw-doc.pdf | save | 1.886 | 0.723 | 0.38 |
| nics-background-checks-2015-11.pdf | save | 0.532 | 0.176 | 0.33 |
| patent-us223898.pdf | save | 1.087 | 0.551 | 0.51 |
| pdf20-simple.pdf | save | 0.196 | 0.020 | 0.10 |
| pdfium-jpx-lzw.pdf | save | 0.148 | 0.010 | 0.07 |
| pdfium-links-highlights-annots.pdf | save | 0.241 | 0.018 | 0.07 |
| pdfium-ocg-invisible.pdf | save | 0.178 | 0.011 | 0.06 |
| pdfium-smask-blend.pdf | save | 0.193 | 0.015 | 0.08 |
| pdfium-type3.pdf | save | 0.160 | 0.011 | 0.07 |
| senate-expenditures.pdf | save | 0.336 | 0.106 | 0.32 |
| usrguide.pdf | save | 5.421 | 1.420 | 0.26 |
| wdl6812-manuscript.pdf | save | 0.451 | 0.617 | 1.37 |

## Reading quality

Both engines use default order. Only human-checked pages score. Adjacent anchor pairs measure order among found anchors. Word placement covers at least 90% of the labeled ink. IoU measures box conventions. The aggregate IoU weights each labeled word once. Table cells come from transcribed rows. Labels and their review are tracked in [#430](https://github.com/yhay81/pyropdf/issues/430) and [#754](https://github.com/yhay81/pyropdf/issues/754).

| Page | Engine | Anchors | Ordered pairs | Placed words | Mean ink IoU | Table cells |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| bill-hr815-p1 | pymupdf | 12/12 | 11/11 | 6/6 | 0.770 | — |
| f1040-p1 | pymupdf | 18/18 | 17/17 | 6/6 | 0.723 | — |
| mhlw-doc-p1 | pymupdf | 11/11 | 9/10 | 6/6 | 0.902 | — |
| mhlw-doc-p2 | pymupdf | 5/5 | 4/4 | 0/0 | — | — |
| nics-background-checks-2015-11-p1 | pymupdf | 11/11 | 9/10 | 6/6 | 0.725 | 14/75 |
| patent-us223898-p2 | pymupdf | 9/9 | 7/8 | 0/0 | — | — |
| usrguide-p2 | pymupdf | 12/12 | 11/11 | 6/6 | 0.712 | — |
| **Total (7/7 pages)** | pymupdf | 78/78 | 68/71 | 30/30 | 0.766 | 14/75 |
| bill-hr815-p1 | pyropdf | 12/12 | 11/11 | 6/6 | 0.764 | — |
| f1040-p1 | pyropdf | 18/18 | 17/17 | 6/6 | 0.838 | — |
| mhlw-doc-p1 | pyropdf | 11/11 | 10/10 | 6/6 | 0.819 | — |
| mhlw-doc-p2 | pyropdf | 5/5 | 4/4 | 0/0 | — | — |
| nics-background-checks-2015-11-p1 | pyropdf | 11/11 | 10/10 | 6/6 | 0.725 | 75/75 |
| patent-us223898-p2 | pyropdf | 9/9 | 8/8 | 0/0 | — | — |
| usrguide-p2 | pyropdf | 12/12 | 11/11 | 6/6 | 0.708 | — |
| **Total (7/7 pages)** | pyropdf | 78/78 | 71/71 | 30/30 | 0.771 | 75/75 |

Peak memory, pixel quality and speed on other platforms were unmeasured in this run. [Rendering investigations](https://github.com/yhay81/pyropdf/issues/365) use other artifacts.

## Oracle delta

Compared with PyMuPDF 2.0, 0 public names, signatures or property kinds changed in the 479 inspected entries of 14 PDF-facing classes. The previous surface and the added, removed or changed entries are machine-readable. These checks cover the audited task matrix and classes, not the entire MuPDF binding.

The [upstream release notes](https://github.com/pymupdf/PyMuPDF/releases/tag/2.1) record two PDF behavior fixes and two other changes: page placement documentation and lazy initialization. Import time was unmeasured. Probes compare decoded label prefixes and scale-dependent glyph heights.

| Baseline or artifact | Escaped and UTF-16 label prefixes | Glyph height follows the Y scale |
| --- | --- | --- |
| PyMuPDF 2.0 | fail | fail |
| pymupdf | pass | pass |
| pyropdf | pass | pass |

## Task-level capabilities

Each linked test describes the release's API contract. Optional model data and fonts are outside the core wheel. The measured workloads above exercise a subset of these tasks.

| Task | PyMuPDF | pyropdf | Release tests and remaining work |
| --- | --- | --- | --- |
| Open, repair and probe | open, is_repaired | open, probe, is_repaired | [document](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_document.py) |
| Insert, move, delete and select pages | insert_pdf, move_page, delete_pages, select | doc.pages | [page_ops](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_page_ops.py), [page_batches](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_page_batches.py) |
| Rotate and flatten | set_rotation, bake | page.rotation, page.flatten, doc.flatten | [flatten](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_flatten.py), [page_boxes](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_page_boxes.py) |
| Normalize rotation | remove_rotation | page.remove_rotation | [remove_rotation](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_remove_rotation.py) |
| Page boxes and labels | set_mediabox, get_page_labels, set_page_labels | page boxes, doc.page_labels, page.label | [page_boxes](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_page_boxes.py), [page_labels](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_page_labels.py) |
| Metadata and outline | metadata, set_xml_metadata, set_toc | doc.metadata, doc.xmp, doc.outline | [metadata](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_metadata.py), [outline](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_outline.py) |
| Layers and visibility | get_layers, set_layer, set_oc | doc.layers | [layers](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_layers.py), [#373](https://github.com/yhay81/pyropdf/issues/373) OCMD visibility expressions and annotation /OC require engine support |
| Embedded files | embfile operations | doc.attachments | [attachments](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_attachments.py) |
| Low-level objects | xref_object, update_object, update_stream | doc.objects, page.obj | [objects](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_objects.py), [typed_views](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_typed_views.py) |
| Undo and redo | journal undo, redo | doc.transaction, doc.history | [history](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_history.py) |
| Persist undo history | journal_save, journal_load | doc.history.save and restore | [history_persistence](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_history_persistence.py) |
| Text, words, blocks, characters and formats | get_text | page.text, words, chars and layout | [layout_serializations](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_layout_serializations.py), [text](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_text.py) |
| Type 3 fallback | get_text | page.text(unmapped="type3_char_code") opts into a character-code heuristic | [text](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_text.py) |
| Search and selection | search_for, get_text_selection | page.search and selection | [search](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_search.py), [text_selection](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_text_selection.py) |
| Tables | find_tables | page.tables | [tables](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_tables.py), [text_quality](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_text_quality.py) |
| Markdown | to_markdown | doc.markdown, page.markdown | [markdown](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_markdown.py) |
| Retrieval chunks and JSON | to_chunks, to_json | doc.to_chunks and to_json | [document_chunks](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_document_chunks.py) |
| OCR and searchable text layer | get_textpage_ocr, pdfocr_tobytes | page.ocr and apply_ocr with optional model data | [ocr_engine](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_ocr_engine.py), [ocr_layer](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_ocr_layer.py) |
| Painted items and paths | get_drawings, get_texttrace | page.content, page.chars | [content_model](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_content_model.py), [text_provenance](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_text_provenance.py), [#104](https://github.com/yhay81/pyropdf/issues/104) provenance coverage for nested painted content |
| Image placements and data | get_image_info, get_images, extract_image | page.content and page.images | [images](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_images.py), [content_model](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_content_model.py) |
| Page fonts and programs | get_fonts, extract_font | page.fonts and doc.font_program | [page_fonts](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_page_fonts.py) |
| Annotation appearance and sound | get_pixmap, get_text, get_sound | annotation.pixmap, text and sound | [annotation_appearances](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_annotation_appearances.py), [annotation_sound](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_annotation_sound.py) |
| Raster and SVG | get_pixmap, get_svg_image | page.pixmap and page.svg | [pixmap](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_pixmap.py), [rendering_svg](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_rendering_svg.py) |
| Selectable text in SVG | get_svg_image(text_as_path=False) | page.svg emits paths | [rendering_svg](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_rendering_svg.py), [#435](https://github.com/yhay81/pyropdf/issues/435) selectable text needs hayro-svg support |
| CMYK raster output | get_pixmap(colorspace=csCMYK) | page.pixmap supports RGB and gray layouts | [rendering_options](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_rendering_options.py), [#425](https://github.com/yhay81/pyropdf/issues/425) engine cannot supply CMYK raster samples |
| Pixmap conversion and operations | invert, gamma, tint, scale | Pixmap.invert, gamma, tint, crop, scale, to_rgb and to_gray | [pixmap_ops](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_pixmap_ops.py) |
| Pixmap transforms and pixels | flip_rotate, warp, pixel, set_pixel, set_rect | Pixmap.rotate, flip, warp, pixel, with_pixel and fill | [pixmap_extra](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_pixmap_extra.py) |
| Pixmap image formats | Pixmap image input and output | Pixmap.from_bytes and to_bytes for documented formats | [pixmap_formats](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_pixmap_formats.py), [#355](https://github.com/yhay81/pyropdf/issues/355) JPEG XR decoding has no maintained Rust implementation |
| Rendering speed | get_pixmap | page.pixmap | [pixmap](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_pixmap.py), [#365](https://github.com/yhay81/pyropdf/issues/365) see the measured release ratios below |
| Shapes, images and placed pages | Shape, insert_image, show_pdf_page | page.draw with paths, images and page placement | [canvas](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_canvas.py), [canvas_page_forms](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_canvas_page_forms.py) |
| Draw text and use fonts | insert_text, insert_textbox, Font | Canvas.text with standard and embedded fonts | [canvas_text](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_canvas_text.py), [canvas_embedded](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_canvas_embedded.py) |
| Images to PDF | open(image).convert_to_pdf | doc.pages.new_from_image | [image_pages](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_image_pages.py) |
| Read and edit annotations | annots, Annot | page.annotations | [annotation_edits](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_annotation_edits.py), [annotation_reading](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_annotation_reading.py) |
| Create markup annotations | add_*_annot | page.annotations with markup, free text, stamps and attachments | [annotation_markup](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_annotation_markup.py), [annotation_free_text](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_annotation_free_text.py), [annotation_stamps](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_annotation_stamps.py), [annotation_files](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_annotation_files.py) |
| Links | insert_link, update_link | page.links | [links](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_links.py) |
| Form fields | widgets, add_widget, Widget.update | doc.form and widgets | [acroform](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_acroform.py), [field_creation](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_field_creation.py), [field_properties](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_field_properties.py) |
| HTML layout | insert_htmlbox, Story | Excluded from PDF task scope by #355 |  |
| Incremental and full saves | save, saveIncr, deflate, use_objstms | doc.save and to_bytes with compression, object streams and deduplication | [save_options](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_save_options.py) |
| Linearized files | save(linear=True), is_fast_webaccess | doc.save(linearize=True), doc.is_linearized | [linearization](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_linearization.py) |
| Encryption | standard PDF security handlers | open(password=...) and save encryption options | [encrypted](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_encrypted.py) |
| Permanent redaction | add_redact_annot, apply_redactions | Content removal unavailable | [#362](https://github.com/yhay81/pyropdf/issues/362) permanent removal of text, images and vector paths |
| Remove content outside a rectangle | clip_to_rect | Page cropping can hide content | [#420](https://github.com/yhay81/pyropdf/issues/420) permanent clipping of page content |
| Clean streams and resources | clean_contents, save(clean=True) | page.clean_contents | [clean_content](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_clean_content.py) |
| Sanitize | scrub | doc.sanitize | [sanitize](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_sanitize.py) |
| Subset fonts | subset_fonts | doc.subset_fonts | [font_subsetting](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_font_subsetting.py) |
| Rewrite images | rewrite_images | doc.rewrite_images returns an unsupported-image report | [rewrite_images](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_rewrite_images.py), [#900](https://github.com/yhay81/pyropdf/issues/900) alternate images and mask cases need further coverage |
| Recolor | Page.recolor, Document.recolor | page.recolor and doc.recolor | [recolor](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_recolor.py) |
| Atomic transactions and changes | — | doc.transaction, doc.changes | [changes](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_changes.py), [history](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_history.py) |
| Typed objects and structural checks | — | doc.objects and doc.check | [typed_views](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_typed_views.py), [structural_check](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_structural_check.py) |
| Sign | — | doc.sign | [signing](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_signing.py) |
| Verify signatures | — | doc.signatures and signature.verify | [verify](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_verify.py) |
| Bounded operations | — | Limits and typed refusals | [limit_codes](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_limit_codes.py), [#15](https://github.com/yhay81/pyropdf/issues/15) full construction bounds await the upstream parser |
| Conformance and structure-aware reading | — | Profile validation and structured reading are separate tasks | [profiles](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_profiles.py), [markdown](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_markdown.py), [#255](https://github.com/yhay81/pyropdf/issues/255) complete conformance-preserving editing |
| Free-threaded Python and licensing | — | Free-threaded wheels and MIT license | [threads](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_threads.py), [release](https://github.com/yhay81/pyropdf/blob/384023dc1e13521b677dbb42f3f22ab6aa590ce2/tests/test_release.py) |

The accompanying `observations.json` holds every timing sample, exception, corpus/label hash, installed version, API delta and quality score. The two `*-pages.json.gz` files contain the original extracted text, word boxes and table cells. A failed extraction retains its diagnostic in the observations.
