# PDF capabilities: PyMuPDF 2.0 and pyropdf main

This is an inventory of *capabilities*, not a claim of API or output parity.
It compares PyMuPDF 2.0 (released 2026-10-19) with pyropdf main at
[`d06091e27d931fb8f766d2548675a8fdfbcb2677`](https://github.com/yhay81/pyropdf/commit/d06091e27d931fb8f766d2548675a8fdfbcb2677),
inspected 2026-10-30. Main includes changes after the published pyropdf 0.2.0;
this table does **not** describe that release. Open PRs are not included.
#355 tracks the project goal.

The PyMuPDF inventory uses its official [Document](https://pymupdf.readthedocs.io/en/latest/document.html),
[Page](https://pymupdf.readthedocs.io/en/latest/page.html),
[Annot](https://pymupdf.readthedocs.io/en/latest/annot.html),
[Pixmap](https://pymupdf.readthedocs.io/en/latest/pixmap.html) and
[feature comparison](https://pymupdf.readthedocs.io/en/latest/about.html)
references. pyropdf's API, implementation, tests and issue state were inspected
at the commit above. `Available` means an API for the named task exists, not
that every option, file, result or quality level matches. `Partial` names a
known limitation; `Missing` means no equivalent public operation was found.
Rows are task-level, not an exhaustive method-by-method or specification audit;
low-level object access alone is not counted as a high-level feature. Links
name existing work, including broader trackers where appropriate. Text and
rendering accuracy need independent comparisons (#430).

## Documents and pages

| Capability | PyMuPDF 2.0 | pyropdf main | Work |
| --- | --- | --- | --- |
| Open, repair and probe | `open`, `repair`, `is_repaired` | Available: `open`, `probe`, `is_repaired`; recovery coverage has not been shown equal | #252 |
| Insert, move, delete and select pages | `insert_pdf`, `move_page`, `delete_pages`, `select` | Available: `doc.pages` with identity and referential rules | |
| Rotate and flatten | `set_rotation`, `bake` | Available: `page.rotation`, `page.flatten`, `doc.flatten` | |
| Normalize rotation without changing appearance | `remove_rotation` | Missing: setting `rotation` alone changes the displayed page | #429 |
| Page boxes and labels | `set_mediabox`, `get_page_labels`, `set_page_labels` | Available: five page boxes, `doc.page_labels`, `page.label`; labels checked against PyMuPDF on the corpus | #357, #358 |
| Metadata and outline | `metadata`, `set_xml_metadata`, `get_toc`, `set_toc` | Available: `doc.metadata`, `doc.xmp`, `doc.outline` | #356 |
| Layers and visibility | `get_layers`, `set_layer`, `set_oc` | Partial: `doc.layers` reads and changes configuration; rendering and extraction do not yet honor OCMD `/VE` or annotation `/OC` | #360, #372, #373 |
| Embedded files | `embfile_*` | Available: `doc.attachments`; attachment annotations are separate | #162, #227 |
| Low-level objects | `xref_object`, `update_object`, `update_stream` | Available: `doc.objects`, `page.obj`, typed objects | |
| Undo and redo | `journal_*` | Available in memory: `doc.transaction`, `doc.history` | |
| Persist and reload undo history | `journal_save`, `journal_load` | Missing | #428 |

## Reading and understanding

| Capability | PyMuPDF 2.0 | pyropdf main | Work |
| --- | --- | --- | --- |
| Text, words, blocks, characters and formats | `get_text` | Available: `text`, `words`, `chars`, `layout`; dictionary, HTML and XML serialization | #137, #430 |
| Type 3 characters without Unicode mapping | Default text extraction uses a fallback on the corpus fixture | Partial: skipped with a warning; `unmapped="glyph_id"` gives replacements for Type 3, not the original character code. The fixture's intended Unicode is unknown | #437; [comparison](results/text-main.md) |
| Search and selection | `search_for`, `get_text_selection` | Available: `page.search`, `page.selection` | |
| Tables | `find_tables` | Available: `page.tables`; option and quality parity not established | #253, #430 |
| Markdown | `to_markdown` | Available: `page.markdown`, `doc.markdown`; tagged structure and retrieval features remain | #253 |
| Retrieval chunks and structured document JSON | `to_chunks`, `to_json` | Partial: page layout records exist, but no equivalent document-level export and chunking | #253, #427 |
| OCR and searchable OCR layer | `get_textpage_ocr`, `Pixmap.pdfocr_tobytes` | Available: offline `page.ocr` and `page.apply_ocr`; hybrid region selection remains | #145, #146, #253 |
| Vector paths and painted items | `get_drawings`, `get_texttrace` | Available: `page.content`; provenance for all nested content remains | #104 |
| Image placements and data | `get_image_info`, `get_images`, `extract_image` | Available: `page.content`, `page.images`, image records | #359 |
| Page fonts and font programs | `get_fonts`, `extract_font` | Available: `page.fonts`, `doc.font_program`; page font listings are compared with PyMuPDF on the corpus | #399, #418 |
| Annotation appearance and sound | `Annot.get_pixmap`, `get_text`, `get_sound` | Partial: `annotation.pixmap()` and `annotation.text()` render and read one appearance alone; no sound accessors | #423 |

## Rendering and pixmaps

| Capability | PyMuPDF 2.0 | pyropdf main | Work |
| --- | --- | --- | --- |
| Raster and SVG | `get_pixmap`, `get_svg_image` | Available: `page.pixmap`, `page.svg`; gray, RGB and alpha | |
| Selectable text in SVG | `get_svg_image(text_as_path=False)` | Missing: `page.svg` emits text as paths | #435 |
| CMYK raster output | `get_pixmap(colorspace=csCMYK)` | Missing; engine support is tracked separately | #248, #425, #433 |
| Pixmap conversion and basic operations | invert, gamma, tint, crop, scale, Pillow | Available: `Pixmap.invert`, `gamma`, `tint`, `crop`, `scale`, `to_gray`, `to_rgb`, alpha and Pillow conversion | #398 |
| Pixmap transforms, inspection and editing | `flip_rotate`, `warp`, `color_count`, `color_topusage`, `pixel`, `set_pixel`, `set_rect` | Partial: raw bytes and Pillow interop exist; no corresponding public pixmap operations | #424 |
| Pixmap image formats | PNG, JPEG, PAM, PNM family, PS, PSD output; additional input formats | Partial: direct Pixmap input and output support PNG and JPEG; Pillow interop covers some other cases | #431 |
| Rendering speed | MuPDF | Not established for this main. The published 0.1.0 corpus comparison reports 2.03× and 2.34× the time at 72 and 144 dpi respectively, on one machine | #365; [method and limitations](README.md) |

## Writing, annotations and forms

| Capability | PyMuPDF 2.0 | pyropdf main | Work |
| --- | --- | --- | --- |
| Shapes, paths, images and placed pages | `Shape`, `insert_image`, `show_pdf_page` | Available: `page.draw()` canvas, with paths, images and `page` placement | #396 |
| Draw text and use fonts | `insert_text`, `insert_textbox`, `Font`, `insert_font` | Missing from canvas; text and fonts wait on krilla | #153, #323 |
| Images to PDF | `open(image).convert_to_pdf()` | Available: `doc.pages.new_from_image`; supported formats and optional Pillow are documented in its API | #361, #412 |
| Read and edit annotations | `annots`, `Annot` | Available: `page.annotations`; individual appearance and sound access are separate gaps | #160, #422, #423 |
| Create markup annotations | `add_*_annot` | Partial: text markup, notes, shapes, lines, polygons and ink have settled appearances; free text, stamps, attachments and carets remain | #161, #168, #162, #421 |
| Links | `insert_link`, `update_link` | Available: `page.links` | #163 |
| Form fields | `widgets`, `add_widget`, `Widget.update` | Partial: create, read, fill and flatten exist; embedded Unicode form fonts and widget font API remain | #194, #212 |
| HTML layout | `insert_htmlbox`, `Story` | Excluded by #355 | |

## Rewriting and saving

| Capability | PyMuPDF 2.0 | pyropdf main | Work |
| --- | --- | --- | --- |
| Incremental and full save; compression and object streams | `save`, `saveIncr`, `garbage`, `deflate`, `use_objstms` | Available: `save`, `to_bytes`, `prune`, `compress`, `object_streams`, `dedupe` | |
| Linearized save and detection | `save(linear=True)`, `is_fast_webaccess` | Missing | #252, #426 |
| Encryption | standard handler including AES and RC4 | Available: standard handler; exact algorithm and permission behavior is not asserted identical | |
| Redaction | `add_redact_annot`, `apply_redactions` | Missing permanent content removal | #362, #104 |
| Remove content outside a rectangle | `clip_to_rect` removes non-intersecting items; intersecting paths and images remain whole | Missing; crop boxes and rendering clips hide rather than remove content. #420 proposes stronger outside removal | #250, #420 |
| Clean content streams and resources | `Page.clean_contents`, `save(clean=True)` | Missing a high-level content-cleaning operation | #250, #434 |
| Sanitizing | `scrub` | Available: `doc.sanitize`, with a report | #363 |
| Font subsetting | `subset_fonts` | Available: `doc.subset_fonts`; format coverage is not asserted identical | #364 |
| Bulk image rewriting | `rewrite_images` | Partial: `doc.rewrite_images` skips inline, masked and CMYK images, has fewer output choices, and `set_to_gray` changes eligible images only, unlike PyMuPDF's whole-page recoloring | #401, #432; whole-page recoloring #400; [probe](results/text-main.md) |
| Recolor | `Page.recolor`, `Document.recolor` | Missing | #400 |

## Additional pyropdf capabilities and project goals

| Capability | pyropdf main | Work |
| --- | --- | --- |
| Atomic transactions, in-memory undo and change reports | Available | |
| Typed objects and Arlington structural checks | Available; not a claim of full PDF conformance validation | |
| Signing | Available on main: PAdES B-B/B-T with supported signers; verification is separate | #397, #403 |
| Signature verification | Missing | #403 |
| Bounded work on untrusted input | Limits exist, but no safety claim until hayro's bounded construction is released | #15 |
| Conformance-preserving edits and structure-aware reading | Not complete | #255, #253 |
| Free-threaded Python and MIT licence | Available | |

## Boundaries and evidence

#355 excludes non-PDF inputs (such as XPS, EPUB and SVG) and HTML layout;
images to PDF are in scope. API names, coordinate conventions and dependency
choices intentionally differ. This inventory does not establish that every
PyMuPDF PDF method or option has been covered: future findings should be added
with reproducible examples and issues. The [benchmark](README.md) is a
historical 0.1.0 measurement, not a measurement of the main commit above.
