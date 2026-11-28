# PDF capabilities: PyMuPDF 2.1 and pyropdf main

The [release comparisons](README.md#release-comparisons) identify published wheels,
updated task coverage, and measured results, including the Linux/x86-64 release
archives attached to [v0.4.1](https://github.com/yhay81/pyropdf/releases/tag/v0.4.1)
(`comparison-v0.4.1.zip`) and [v0.4.0](https://github.com/yhay81/pyropdf/releases/tag/v0.4.0)
(`comparison-v0.4.0.zip`), the [v0.3.1 macOS/arm64 report](releases/v0.3.1-darwin-arm64/comparison.md),
and the [labeled text/table quality results](results/quality.md).

This is an inventory of *capabilities*, not a claim of API or output parity.
It compares PyMuPDF 2.1 (and its audited 2.0 baseline) with pyropdf `main` at
[`b18631bb`](https://github.com/yhay81/pyropdf/commit/b18631bb),
inspected 2026-11-28 (after the published `v0.4.1` release). Open PRs are not
included. #355 tracks the project goal, and [capabilities.tsv](capabilities.tsv)
holds the machine-readable release task map aligned with this inventory.

The PyMuPDF inventory uses its official [Document](https://pymupdf.readthedocs.io/en/latest/document.html),
[Page](https://pymupdf.readthedocs.io/en/latest/page.html),
[Annot](https://pymupdf.readthedocs.io/en/latest/annot.html),
[Pixmap](https://pymupdf.readthedocs.io/en/latest/pixmap.html) and
[feature comparison](https://pymupdf.readthedocs.io/en/latest/about.html)
references. pyropdf's API, implementation, tests and issue state were inspected
at the commit above. `Available` means an API for the named task exists and is
covered by tests on `main`, not that every option, file, result or quality level
matches. `Partial` names a known limitation; `Missing` means no equivalent
public operation is shipped. Rows are task-level, not an exhaustive
method-by-method or specification audit; low-level object access alone is not
counted as a high-level feature. Links name open work, including broader
trackers where appropriate. Text and layout accuracy are scored separately in
[quality.md](results/quality.md) (#1192).

## Documents and pages

| Capability | PyMuPDF 2.1 | pyropdf main | Work |
| --- | --- | --- | --- |
| Open, repair and probe | `open`, `repair`, `is_repaired` | Available: `open`, `probe`, `is_repaired`; deterministic repair benchmarked and hardened across 316 damaged PDFs against PyMuPDF, qpdf, and pdfium ([report](../repair-coverage/README.md)) | #252 |
| Insert, move, delete and select pages | `insert_pdf`, `move_page`, `delete_pages`, `select` | Available: `doc.pages` with identity and referential rules | |
| Rotate and flatten | `set_rotation`, `bake` | Available: `page.rotation`, `page.flatten`, `doc.flatten` | |
| Normalize rotation without changing appearance | `remove_rotation` | Available: `page.remove_rotation()` keeps content, boxes, annotations, links, and widgets as displayed | |
| Page boxes and labels | `set_mediabox`, `get_page_labels`, `set_page_labels` | Available: five page boxes, `doc.page_labels`, `page.label`; labels checked against PyMuPDF on the corpus | |
| Metadata and outline | `metadata`, `set_xml_metadata`, `get_toc`, `set_toc` | Available: `doc.metadata`, `doc.xmp`, `doc.outline` | |
| Layers and visibility | `get_layers`, `set_layer`, `set_oc` | Partial: `doc.layers` reads and changes configuration; rendering and extraction do not yet honor OCMD `/VE` or annotation `/OC` | #372, #373 |
| Embedded files | `embfile_*` | Available: `doc.attachments`; file attachment annotations are separate | |
| Low-level objects | `xref_object`, `update_object`, `update_stream` | Available: `doc.objects`, `page.obj`, typed objects | |
| Undo and redo | `journal_*` | Available in memory: `doc.transaction`, `doc.history` | |
| Persist and reload undo history | `journal_save`, `journal_load` | Available: `doc.history.save()`, `doc.history.restore()`; the format is pyropdf's own | |

## Reading and understanding

| Capability | PyMuPDF 2.1 | pyropdf main | Work |
| --- | --- | --- | --- |
| Text, words, blocks, characters and formats | `get_text` | Available: `page.text`, `words`, `chars`, `layout`; dictionary, HTML and XML serialization; `/ActualText` opt-out (`actual_text=False`, #1177), mixed horizontal/vertical & Japanese ruby handling (`ruby=`, #1184), and direct PyO3/thread-local extraction optimization (#1178) | |
| Type 3 characters without Unicode mapping | Default text extraction uses a fallback on the corpus fixture | Available (opt-in): default skips with a warning; `unmapped="type3_char_code"` recovers the Type 3 character code (#437) and `unmapped="glyph_id"` returns glyph IDs | |
| Search and selection | `search_for`, `get_text_selection` | Available: `page.search`, `page.selection` | |
| Tables | `find_tables` | Available: `page.tables` (including borderless multi-line rows under bordered double-rule headers, #1153; explicit line guides, tolerances, `header_rows`/`headers`, `to_dicts()`, and `to_dataframe()`, #1186) | #253, #1192 |
| Markdown | `to_markdown` | Available: `page.markdown`, `doc.markdown`; structure-tree-driven output (`use_structure="auto"`, #1181), retrieval export controls (`margins`, `ignore_headers_footers`, `images`, `max_graphics`, #1182), and mixed orientation/ruby (#1184) | #253, #1192 |
| Retrieval chunks and structured document JSON | `to_chunks`, `to_json` | Available: `doc.to_json` and `doc.to_chunks` with structure-tree support (#1181), retrieval export controls (#1182), and character budget instead of tokens; see `evidence/document-chunks/`; retrieval provenance tracing remains | #253, #853 |
| OCR and searchable OCR layer | `get_textpage_ocr`, `Pixmap.pdfocr_tobytes` | Available: offline `page.ocr` and `page.apply_ocr` with optional `[ocr]` model data and `mode="full" \| "skip_existing" \| "hybrid"` region-selective OCR (#1185) | #253, #1192 |
| Vector paths and painted items | `get_drawings`, `get_texttrace` | Available: `page.content`, `page.chars`, `page.paths().clusters()` / `path_clusters()` (#1179), stored `Color.colorspace`/`components`, `TextRun.writing_mode`, and `FontRef` identity (#1176); provenance for nested painted content remains | #104 |
| Image placements and data | `get_image_info`, `get_images`, `extract_image` | Available: `page.content`, `page.images`, image records, `replace`, and `remove` | |
| Page fonts and font programs | `get_fonts`, `extract_font` | Available: `page.fonts`, `doc.font_program`; page font listings are compared with PyMuPDF on the corpus | |
| Annotation appearance and sound | `Annot.get_pixmap`, `get_text`, `get_sound` | Available: `annotation.pixmap()` and `annotation.text()` render and read one appearance alone; `annotation.sound()` reads a sound annotation's audio and format | |

## Rendering and pixmaps

| Capability | PyMuPDF 2.1 | pyropdf main | Work |
| --- | --- | --- | --- |
| Raster and SVG | `get_pixmap`, `get_svg_image` | Available: `page.pixmap`, `page.svg`; gray, RGB, alpha, `antialias` (#1175), hinting, and opt-in glyph cache | |
| Selectable text in SVG | `get_svg_image(text_as_path=False)` | Missing: `page.svg` emits text as paths | #435 |
| CMYK raster output | `get_pixmap(colorspace=csCMYK)` | Missing: `page.pixmap` supports RGB and gray layouts; engine CMYK rasterization is tracked separately | #248, #425, #433 |
| Pixmap conversion and basic operations | invert, gamma, tint, crop, scale, Pillow | Available: `Pixmap.invert`, `gamma`, `tint`, `crop`, `scale`, `to_gray`, `to_rgb`, alpha and Pillow conversion | |
| Pixmap transforms, inspection and editing | `flip_rotate`, `warp`, `color_count`, `color_topusage`, `pixel`, `set_pixel`, `set_rect` | Available: `Pixmap.rotate`, `flip`, `warp`, `color_count`, `colors`, `top_color`, `pixel`, `with_pixel`, `fill`; edits return new pixmaps | |
| Pixmap image formats | PNG, JPEG, PAM, PNM family, PS, PSD output; additional input formats | Available: `Pixmap` reads PNG, JPEG, PBM, PGM, PPM, PAM, BMP (uncompressed and run-length), GIF, TIFF, and JPEG 2000 and writes PNG, JPEG, PAM, PBM, PGM, PPM, PNM, PSD, and PostScript; JPEG XR is refused (no maintained Rust decoder), and CMYK waits for #425 | #355, #425 |
| Rendering speed | MuPDF | Measured in `v0.4.1` on Linux x86-64 vs PyMuPDF 2.1 (`comparison-v0.4.1.zip`): 1.81× at 72 dpi and 1.54× at 144 dpi (`open` 0.26×, `save` 0.12×, `search` 0.97×, `text` 1.40×, `words` 1.68×; post-v0.4.1 text/word optimization in #1178); `v0.3.1` macOS/arm64 was 1.77× / 1.82× | #365, #715; [v0.4.1](https://github.com/yhay81/pyropdf/releases/tag/v0.4.1), [v0.3.1 macOS/arm64](releases/v0.3.1-darwin-arm64/comparison.md) |

## Writing, annotations and forms

| Capability | PyMuPDF 2.1 | pyropdf main | Work |
| --- | --- | --- | --- |
| Shapes, paths, images and placed pages | `Shape`, `insert_image`, `show_pdf_page` | Available: `page.draw()` canvas with paths, images, optional-content layers, tags, `page` placement, and `Canvas.draw(item)` round-trip for axial/radial shadings, tiling patterns, inline images, and stencil image masks (#1188); soft masks and function/mesh shadings remain | #1200 |
| Draw text and use fonts | `insert_text`, `insert_textbox`, `Font`, `insert_font` | Available: `Canvas.text` with Standard 14 fonts (#467) and embedded TrueType/OpenType/CFF faces shaped with `harfrust` and subset at save (#153, #479–#482), invisible/clip render modes, vertical writing, and per-glyph font fallback (#1187); rich BiDi runs remain | #249, #1198 |
| Images to PDF | `open(image).convert_to_pdf()` | Available: `doc.pages.new_from_image`, `new_from_images`, and `new_from_cbz` (#1189) | |
| Read and edit annotations | `annots`, `Annot` | Available: `page.annotations`, including appearances, sound, and multimedia/3D/RichMedia data | |
| Create markup annotations | `add_*_annot` | Available: `page.annotations` creates text markup, notes, shapes, lines, polygons, ink, carets, free text, stamps, and file attachments with settled appearances | |
| Links | `insert_link`, `update_link` | Available: `page.links` | |
| Form fields | `widgets`, `add_widget`, `Widget.update` | Available: `doc.form` and widgets support create, read, fill, flatten, embedded Unicode fonts, rich text, password appearances, and legacy XFA packet preservation | |
| HTML layout | `insert_htmlbox`, `Story` | Excluded from PDF task scope by #355 | |

## Rewriting and saving

| Capability | PyMuPDF 2.1 | pyropdf main | Work |
| --- | --- | --- | --- |
| Incremental and full save; compression and object streams | `save`, `saveIncr`, `garbage`, `deflate`, `use_objstms` | Available: `save`, `to_bytes`, `prune`, `compress`, `object_streams`, `dedupe`, `hybrid`, `clean`, `ascii`, `pretty`, and `stream_filter` | |
| Linearized save and detection | `save(linear=True)`, `is_fast_webaccess` | Available: `doc.save(linearize=True)`, `doc.is_linearized` | |
| Encryption | standard handler including AES and RC4 | Available: standard handler (RC4/AES), public-key recipients (#864), AES-GCM ISO/TS 32003 (#865), and document MAC ISO/TS 32004 (#866); direct rendering of some legacy recipient, crypt-filter, and GCM streams awaits store-source integration | #937, #968, #993, #1043 |
| Redaction | `add_redact_annot`, `apply_redactions` | Partial: redaction marks and independent verification exist (#1076); permanent removal of text, images, and vector content awaits nested provenance | #362, #104 |
| Remove content outside a rectangle | `clip_to_rect` removes non-intersecting items; intersecting paths and images remain whole | Missing: crop boxes and rendering clips hide rather than remove content. #420 proposes stronger outside removal | #250, #420 |
| Clean content streams and resources | `Page.clean_contents`, `save(clean=True)` | Available: `page.clean_contents()` and `save(clean=True)` / `to_bytes(clean=True)` normalize page content and appearance streams and prune unnamed resources without mutating the live document | |
| Sanitizing | `scrub` | Available: `doc.sanitize`, with a report | |
| Font subsetting | `subset_fonts` | Available: `doc.subset_fonts` (TrueType and CFF) | |
| Bulk image rewriting | `rewrite_images` | Available: `doc.rewrite_images` rewrites masked, alternate, CMYK, ICC-based, stencil mask, and inline images across Form XObjects and appearances (lossless, grayscale, and bitonal CCITT Group 4) and returns an unsupported-image report for images it leaves untouched | |
| Recolor | `Page.recolor`, `Document.recolor` | Available: `page.recolor` and `doc.recolor` (gray, RGB, CMYK); filtered and nested CMYK function curves remain | #1007 |
| Text replacement | — | Partial: `page.replace_text` edits simple, composite (CID/Type 0), Type 3, and vertical-writing fonts with fallback switching and `/ActualText`/`/MCID` preservation; item-level editing over provenance remains | #250, #1201 |

## Additional pyropdf capabilities and project goals

| Capability | pyropdf main | Work |
| --- | --- | --- |
| Atomic transactions, undo/redo, persisted history, and change reports | Available: `doc.transaction`, `doc.history`, `doc.changes`, and incremental `doc.revisions` | |
| Typed objects and Arlington structural checks | Available: `doc.objects`, `doc.check`, and 838 mapped ISO 32000-2 features (`docs/spec-coverage.md`); not a claim of standalone conformance certification | #254, #256 |
| Signing | Available: `doc.sign` (PAdES B-B/B-T with ECDSA, signer and timestamp callbacks, DocMDP/FieldMDP, DSS, ISO/TS 32001/32002) | |
| Signature verification | Available: `doc.signatures` and `signature.verify` (integrity, byte coverage, signer, timestamps, later modifications) | |
| Conformance-preserving edits and authoring | Available (#255 closed): PDF/A-1 through PDF/A-4 and PDF/UA-1/2 authoring, editing, and pre-save claim reports validated with veraPDF | |
| Bounded work on untrusted input | Partial: `DocumentLimits` and `DocumentLimits.web()` enforce store and API budgets, but no untrusted-input safety claim until hayro's bounded construction ships | #15, #1197 |
| Structure-aware reading and retrieval | Available: structure tree read/edit (#822, #907) and structure-driven Markdown/JSON/chunks (#1181) with retrieval export controls (#1182); retrieval provenance tracing remains | #253, #853 |
| Free-threaded Python, WebAssembly, and MIT licence | Available: native `abi3-py311`, free-threaded `cp314t`/`cp315t`, and PyEmscripten wheels under MIT | #257, #258 |

## Boundaries and evidence

#355 excludes non-PDF inputs (such as XPS, EPUB and SVG) and HTML layout;
images to PDF stay in scope. API names, coordinate conventions and dependency
choices intentionally differ. This inventory does not establish that every
PyMuPDF PDF method or option has been covered: future findings should be added
with reproducible examples and issues. Release-wheel timings and reading-quality
scores are published with each release (`comparison-v0.4.1.zip`,
`comparison-v0.4.0.zip`, [v0.3.1 macOS/arm64](releases/v0.3.1-darwin-arm64/comparison.md))
and in [results/quality.md](results/quality.md); [README.md](README.md) keeps
the historical 0.1.0 baseline alongside them.
