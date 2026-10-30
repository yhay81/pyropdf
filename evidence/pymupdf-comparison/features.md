# Features: PyMuPDF 2.0 and pyropdf 0.1.0

Each row names a PDF capability, the PyMuPDF 2.0 API that provides it, and
where pyropdf 0.1.0 stands: **yes**, **partial**, or **no**. A gap links the
issue that closes it; #355 tracks them all. The PyMuPDF column comes from the
classes' public attributes (`dir()`) of the released wheel, and the pyropdf
column from `docs/en/api.md`.

## Documents and pages

| Capability | PyMuPDF 2.0 | pyropdf 0.1.0 | Issue |
| --- | --- | --- | --- |
| Open, repair, and probe | `open`, `repair`, `is_repaired` | yes: `open`, `probe`, `is_repaired` | |
| Insert, move, delete, and select pages | `insert_pdf`, `move_page`, `delete_pages`, `select` | yes, with page identity and referential rules | |
| Rotate and flatten | `set_rotation`, `bake` | yes | |
| Page boxes | `set_mediabox`, `set_cropbox`, and the other boxes | yes | #357 |
| Page labels | `get_page_labels`, `set_page_labels` | yes; every corpus page matches `get_label` | #358 |
| Document metadata | `metadata`, `set_metadata`, `set_xml_metadata` | yes: `doc.metadata` and `doc.xmp`, kept in step | |
| Outline | `get_toc`, `set_toc` | yes: `doc.outline` | |
| Optional content (layers) | `get_layers`, `set_layer`, `set_oc` | yes: `doc.layers`; marking content with `/OC` is the object API | #360 |
| Embedded files | `embfile_*` | yes: `doc.attachments` | |
| Low-level objects | `xref_object`, `update_object`, `update_stream` | yes: `doc.objects`, typed objects | |
| Undo and redo | `journal_*` | yes: transactions and `doc.history` | |

## Reading

| Capability | PyMuPDF 2.0 | pyropdf 0.1.0 | Issue |
| --- | --- | --- | --- |
| Text, words, blocks, and characters | `get_text` in its formats | yes: `text`, `words`, `chars`, `layout` | |
| Search and selection | `search_for`, `get_text_selection` | yes | |
| Tables | `find_tables` | yes | |
| Markdown | `to_markdown` (pymupdf4llm) | yes | |
| OCR | `get_textpage_ocr` (Tesseract) | yes: offline OCR, no external program | |
| Vector paths and content items | `get_drawings`, `get_texttrace` | yes: `page.content()` | |
| Image placements | `get_image_info`, `get_image_rects` | yes: content items | |
| Image data | `extract_image`, `get_images`, `replace_image`, `delete_image` | yes: `page.images()`, `doc.replace_image`, `doc.remove_image` | #359 |
| Fonts of a page | `get_fonts`, `extract_font` | partial: font references on text items | #364 |

## Rendering

| Capability | PyMuPDF 2.0 | pyropdf 0.1.0 | Issue |
| --- | --- | --- | --- |
| Raster and SVG | `get_pixmap`, `get_svg_image` | yes | |
| Speed | MuPDF | 2.65 times slower at 72 dpi, 3.97 at 144 dpi (`results/`) | #365 |
| Pixmap operations | tint, gamma, invert, PIL | partial: save and bytes | |

## Writing

| Capability | PyMuPDF 2.0 | pyropdf 0.1.0 | Issue |
| --- | --- | --- | --- |
| Text, shapes, and images on a page | `insert_text`, `insert_textbox`, `insert_image`, `Shape` | no | #153 |
| Place a page on another | `show_pdf_page` | no | #153 |
| HTML layout | `insert_htmlbox`, `Story` | no; not planned | |
| Fonts | `Font`, `insert_font` | no | #153, #212 |
| Images to PDF | `open(image).convert_to_pdf()` | no | #361 |
| Non-PDF inputs (XPS, EPUB, SVG) | `open` | no; not planned | |

## Annotations and forms

| Capability | PyMuPDF 2.0 | pyropdf 0.1.0 | Issue |
| --- | --- | --- | --- |
| Read and edit annotations | `annots`, `Annot` | yes | |
| Create markup annotations | `add_*_annot` | partial: text markup, notes, ink, lines, shapes, and polygons; appearances drawn by a settler wait | #161 |
| Free text and stamps | `add_freetext_annot`, `add_stamp_annot` | no | #168 |
| File attachment annotations | `add_file_annot` | no | #162 |
| Links | `insert_link`, `update_link` | yes | |
| Form fields: read, fill, flatten | `widgets`, `Widget.update` | yes | |
| Create fields | `add_widget` | yes: `page.widgets.add` | |

## Rewriting and saving

| Capability | PyMuPDF 2.0 | pyropdf 0.1.0 | Issue |
| --- | --- | --- | --- |
| Incremental and full save | `save`, `saveIncr` | yes, with a change report | |
| Pruning and compression | `garbage`, `deflate`, `use_objstms` | yes: `prune`, `compress`, cross-reference streams | |
| Encryption | AES-256, RC4 | yes: the standard handler | |
| Redaction | `add_redact_annot`, `apply_redactions` | no | #362 |
| Sanitizing | `scrub` | yes: `doc.sanitize`, which also reports broken signatures and claims | #363 |
| Font subsetting | `subset_fonts` | yes: `doc.subset_fonts`, which keeps glyph IDs, so text and widths are unchanged | #364 |
| Recolor and rewrite images | `recolor`, `rewrite_images` | partly: `doc.rewrite_images`, not `recolor` | #401 |

## What PyMuPDF does not have

| Capability | pyropdf 0.1.0 | Issue |
| --- | --- | --- |
| Edits that keep dependent structures valid, or refuse | yes | |
| Transactions that leave the document unchanged on failure | yes | |
| Change reports of broken signatures and profile claims | yes | |
| Typed objects and a structural check from the Arlington model | yes | |
| PDF/A, PDF/UA, PDF/X, and PDF/VT claims as values | yes | |
| Free-threaded Python without the GIL | yes | |
| MIT licence (PyMuPDF is AGPL or commercial) | yes | |
| Signing and verifying signatures | no | #366 |
| Bounded work on untrusted input | no | #15 |
| Conformance-preserving edits | no | #255 |
