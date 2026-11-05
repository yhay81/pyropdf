# Changelog

## Unreleased

- `page.pixmap(glyph_cache=True)` and `Document.render(glyph_cache=True)`
  render text through hayro's glyph cache, which rasterizes each glyph once
  at each size and reuses it: on the corpus, about 1.5 times as fast at
  72 dpi and 1.4 times at 144 dpi, but text moves by up to half a pixel
  vertically, and the cache's memory is outside `DocumentLimits`.
  `DocumentLimits.web()` accepts the option. Without it, renders are
  unchanged (#640, #685).

## 0.2.2 (2026-11-04)

- pyropdf moves to hayro 0.9.0 with LaurenzV/hayro#1734 applied, which
  hayro has not released: every hayro crate comes from yhay81/hayro commit
  394e4a0d, which also holds two fixes after 0.9.0 (ToUnicode maps written
  with `cidrange`, LaurenzV/hayro#1730; fallible allocation in CCITT
  decoding, LaurenzV/hayro#1733). A build from the sdist fetches that
  commit from GitHub and needs maturin 1.16 or later (#18, #617).
- hayro 0.9.0's fixes and performance work reach rendering and extraction,
  as its changelogs list them: patterns, soft masks, clipping, embedded
  fonts, the Unicode of glyph names, and malformed files (#18).
- Text renders as it did in 0.2.1: unhinted, and without hayro 0.9's glyph
  cache, which would move text by less than a pixel on most pages. Of 163
  corpus pages at 72 dpi, 162 render byte for byte as on hayro 0.8, and one
  differs in 495 pixels by at most 5 of 255 (#18, #622).
- `page.pixmap(hinting=True)` and `Document.render(hinting=True)` hint text
  as FreeType's light hinting does, which sharpens small text, with hayro's
  default settings but without its glyph cache. Hinted renders of a
  document may differ before and after `doc.subset_fonts` or a save that
  compacts its fonts (#612, #622).
- `SoftMask.xref` is deprecated: reading it warns with `DeprecationWarning`,
  and it is 0, since hayro 0.9 no longer identifies the group of a soft
  mask. `SoftMask(subtype=...)` builds one without it, and building,
  comparing, hashing, and pickling a mask do not warn. The next minor
  release removes it (#18).
- `TilingPattern.opacity` reports the opacity at which a tiling pattern
  paints, as `ShadingPattern.opacity` does, in `page.content()` and
  `page.paths()`, since hayro 0.9 leaves it to the device (#608).
- Objects numbered above 2,147,483,647 resolve in rendering and text
  extraction (#18).
- Content operators with up to 33 operands, such as `scn` with up to 32
  DeviceN components, are read, rendered, and extracted; reading stopped at
  the 11th operand, which also left the rest of the page unrendered (#18).
- An item of a page's `/Contents` array that is not a stream is skipped,
  and the streams after it are read; reading stopped at it (#18).
- Text in render mode 7, which only clips, is extracted, and
  `page.content()` reports it as an `INVISIBLE` text run (#18).
- A name whose `#` is not followed by two hexadecimal digits reads as
  written instead of raising `MalformedPdfError` (#18).
- `Operations.parse` and `page.operations` raise `MalformedPdfError` for an
  operand nested deeper than 64 levels, where reading stopped before it
  (#18).
- A stream whose `/F` names an external file reads, with its dictionary and
  its bytes, and decoding its data raises `MalformedPdfError` instead of
  returning the bytes to ignore (#224).
- Text in a font that a document does not embed, or whose program does not
  load, still reads by the PDF's widths, though hayro 0.9 stretches the
  substitute glyph in its transform: the boxes of `words`, `layout`,
  `search`, `selection`, and `tables`, the word gaps of `text`, and the
  advances of `page.content()` do not follow the stretch, while ink boxes
  do, as a rendering draws it (#656).
- Text in a CJK font that a document does not embed renders in fallback
  faces instead of blank. `doc.fallback_fonts` maps `"ja"`, `"ko"`,
  `"zh-CN"`, and `"zh-TW"` to `FallbackFont` records of a sans and a serif
  face, as a `FallbackFonts` mapping that holds the faces of the installed
  extras `pyropdf[jp]`, `[ko]`, `[zh-cn]`, and `[zh-tw]` until it is
  edited; a serif name such as a Mincho takes the serif face.
  `page.pixmap`, `Document.render`, `page.svg`, and `Annotation.pixmap`
  draw with them, and they are not saved (#600).
- `page.words`, `page.layout`, `page.chars`, and `page.search` with
  `exact_bboxes=True` measure such a font on the face of
  `doc.fallback_fonts` that draws it, and the text runs of `page.content()`
  describe it as that face: glyph IDs, glyph and run boxes, and the font's
  weight, where its glyphs had no box and a run's box lay on its baseline.
  These results now depend on `doc.fallback_fonts`; text and the other
  boxes do not (#624, #658).
- `page.paths()` returns the paths of `page.content()` as `Paths` columns,
  which NumPy reads without a copy through `Paths.column` and Arrow through
  the PyCapsule interface, and which build a `Path` only for a row that is
  read. On a page of 200,000 lines it takes 0.21 s, where the paths of
  `page.content()` take 4.5 s (#606, #615, #618).
- `Canvas` fills and strokes paths and shapes with the new `LinearGradient`
  and `RadialGradient`, built from `ColorStop`s, with `GradientExtend`
  choosing `PAD`, `NONE`, `REPEAT`, or `REFLECT`. Stops with alpha paint
  through a soft mask, equal function and shading objects are written once
  in a canvas, and a repeating gradient that needs more than 4,096 cycles
  raises `LimitError` with code `content_element_count`. Text still paints
  with a `Color` only, and refuses a gradient with `TypeError` (#632).
- The type alias `Paint` also includes the two gradients, and `Gradient`
  names them both (#632).
- `Font.from_file(path, weight=...)` and `Font.from_bytes(data, weight=...)`
  draw any weight on a variable TrueType face's axis, from a static program
  written at load, so that it draws, saves, and subsets as a static face,
  also in `doc.form.font` and `FallbackFont`; `Font.weight` and `Font.name`
  describe the instance. A weight outside the axis raises `PdfError`. The
  program counts against `max_size` (`font_input_size`), and a coordinate
  that the variations push outside 16 bits refuses. `weight=None` still
  draws the default instance, and CFF2 stays refused (#629).
- Canvas text paints a face's COLR v0 and v1 glyphs, and the PNG strikes of
  its `sbix` and CBDT tables, in their own colours, as Type 3 fonts that
  read back as text, where 0.2.1 drew their monochrome fallback outlines.
  COLR paints in the face's default palette, at the instance that the face
  loaded: solid colours, linear and radial gradients, transforms, clips,
  shared layers, and the composite modes that PDF's blend modes and soft
  masks express. These glyphs keep their colours whatever the fill, except
  a COLR layer in the text's colour, which takes it. A face with `sbix` or
  CBDT bitmaps and no outlines loads, and `Font.char_bbox` gives the box of
  a bitmap glyph's image (#631, #633).
- A COLR glyph that needs a sweep gradient, a gradient stop in the text's
  colour, the modes `Plus`, `Xor`, `SrcAtop`, or `DestAtop`, or a palette
  entry that the default palette lacks draws its fallback outline, and one
  whose paints form a cycle, or nest or number past skrifa's bounds, raises
  `PdfError` whatever its outline (#633).
- A colour glyph that has no outline and that the text cannot draw in
  colour raises `PdfError` naming the character, instead of drawing nothing
  under text that reads back. `Canvas.text` and `Canvas.textbox` refuse an
  `SVG ` glyph, a JPEG, TIFF, or raw strike, or a COLR glyph that they
  cannot paint. A text or choice field filled in `doc.form.font`, and a
  free text annotation in an OpenType face (`FreeText(..., font=face)`)
  when it is added or its text changes, refuse any COLR, `sbix`, CBDT, or
  `SVG ` glyph without an outline. A colour glyph with a monochrome
  fallback outline still draws that outline, and `Font` measures such
  glyphs as before (#630, #639, #651).
- Canvas text in an embedded face draws a multi-glyph cluster whose glyphs
  do not read as its text inside one `/ActualText`, and maps a glyph
  without text of its own to U+2060, so MuPDF, PDFium, and Xpdf read the
  cluster exactly and a glyph such as a dot that a face draws apart from its
  letter no longer reads as a stray character. Form field values and free
  text annotations in an embedded face draw clusters the same way. pdf.js,
  which ends a text chunk at each marked-content sequence, scrambles a
  right-to-left word that holds such a cluster (#637, #648).
- `page.text()` and the other text readers honour `/ActualText` over the
  glyphs inside it, as Xpdf, MuPDF, and PDFium do; `page.content()` still
  reports each glyph's CMap text (#637).
- `page.text()` measures Type 3 glyphs by the widths of their font, so
  adjacent glyphs no longer read with a space between them (#654).
- A save that compacts a loaded CFF face keeps the stem hints and masks of
  the glyphs that it keeps and the hint values of each Private DICT, so a
  viewer that hints CFF text from the program, and
  `page.pixmap(hinting=True)`, draw the saved text as they draw the face
  (#611).
- Saving a document that holds a loaded OpenType face again writes the same
  bytes when nothing between the saves changed it, also after a dropped
  transaction or an undone edit, and inside an open transaction: a TrueType
  face's `/CIDToGIDMap` took a new object number at each save, and a
  discarded object number raised `/Size` (#652, #665).
- `page.pixmap(clip=...)` applies the size limits to the clipped raster
  instead of the whole page, as PyMuPDF does, so a small region of a large
  page renders at scales at which the whole page would not, and
  `page.ocr(clip=...)` recognizes it at the default 300 dpi. With a clip,
  the page under the matrix may measure up to 524,288 pixels a side, and a
  larger one raises `ValueError` (#605).
- Text is shaped by harfrust 0.15: a run that a face's own tables make
  outgrow the shaper's allowance raises `PdfError` instead of using a
  partial result (#617).
- The GitHub release is immutable from 0.2.2 on, and the Release workflow
  fails unless it is; the releases of 0.1.0 to 0.2.1 stay mutable (#604).

No release claims safety for untrusted input before hayro's bounded
construction (I2, [#15](https://github.com/yhay81/pyropdf/issues/15)) is
released.

## 0.2.1 (2026-11-03)

- `page.draw()` returns a `Canvas` that draws on the page when its `with`
  block ends: paths, built with `PathBuilder` or from segments; JPEG, PNG,
  and JPEG 2000 images, pixmaps, and images of the document; and pages of
  any open document, with clipping, transforms, opacity, and blend modes
  (#396).
- `Canvas.text` and `Canvas.textbox` draw text in the Standard 14 fonts,
  and `textbox` wraps text inside a rectangle and reports with `TextFit` how
  it fits (#467).
- `Font.standard` returns a Standard 14 font with its metrics, and
  `Font.text_width`, `text_height`, `char_widths`, and `char_bbox` measure
  text (#496).
- `Font.from_file` and `Font.from_bytes` load OpenType faces with TrueType
  or CFF outlines (#479), which `Canvas.text` and `Canvas.textbox` draw
  text in as Type0 fonts (#481).
- Text in a loaded face is shaped by harfrust, in bidirectional and script
  runs (#480).
- A save subsets each loaded face to the glyphs that the document draws
  (#482).
- `Canvas.layer` draws a block as optional content of a `Layer` or a
  `LayerMembership`, `Canvas.tag` makes it a structure element of the
  document's structure tree, and `doc.layers.add` adds a layer (#476).
- `Canvas.draw` paints again the `Path` and `Image` items that
  `Page.content` returns (#581).
- A page placed more than once shares one Form XObject while its source is
  unchanged, and a page placed from the same document keeps its own copy of
  the resources, which later edits of the source do not change (#503).
- `doc.pages.new_from_image` adds one page per frame of an image, each the
  size of its frame at the resolution that its JFIF, Exif, or PNG data
  gives (#361, #412).
- `page.replace_text` replaces text that simple fonts draw, in the page and
  in the Form XObjects that it paints, in drawing order (#402, #413).
- `doc.rewrite_images` downsamples the images that the pages place, by
  their resolution on the page, and recompresses them, each image object
  once, including masked, CMYK, ICC-based, Indexed, inline, and bitonal
  images, and returns an `ImageRewriteReport` (#401, #432, #451, #452).
- `page.recolor` and `doc.recolor` convert the colors that pages paint to
  gray, RGB, or CMYK, and report with `RecolorReport` the colors that they
  leave (#400).
- `page.fonts()` lists the font dictionaries that a page and its Form
  XObjects name, and `doc.font_program` returns the embedded program of a
  font (#399).
- `page.remove_rotation()` sets the rotation to 0 and keeps what the page
  displays, with its annotations and the destinations into it (#429, #442).
- `page.clean_contents()` normalizes the page's content and its annotation
  appearances, and drops the resources that the content never names
  (#434).
- `Pixmap` gains `invert`, `gamma`, `tint`, `to_gray`, `to_rgb`,
  `with_alpha`, `without_alpha`, `crop`, `scale`, and `to_pil`, and
  `Pixmap.from_bytes` and `Pixmap.open` read images (#398).
- `Pixmap` gains `rotate`, `flip`, `warp`, `pixel`, `with_pixel`, `fill`,
  `color_count`, `colors`, and `top_color` (#424).
- `Pixmap` reads PBM, PGM, PPM, PAM, BMP, GIF, TIFF, and JPEG 2000, and
  writes PAM, PBM, PGM, PPM, PNM, PSD, and PostScript (#431, #446).
- RGB pixmaps without alpha skip a pixel conversion, which renders two
  corpus documents about 8% faster at 72 dpi and 17% at 144 dpi (#393).
- Rendering refuses with `ValueError` a raster wider than 65,408 or taller
  than 65,532 pixels, where vello_cpu would panic (#392).
- `Annotation.pixmap` renders one annotation's appearance, and
  `Annotation.text` extracts the text that it draws (#422).
- `Annotation.sound` reads the audio of a sound annotation and its format,
  as `Sound` and `SoundEncoding` (#423).
- `page.annotations.add` creates `Caret` annotations and settles their
  appearances (#421).
- `page.annotations.add` creates `FileAttachment` annotations, and
  `Annotation.file` reads and writes the embedded file, its name,
  description, and dates, as `AnnotationFile` (#162, #468).
- The file name, description, and dates of `doc.attachments` entries can be
  assigned and removed (#227).
- `doc.attachments.add` keeps an attachment that another thread adds or
  removes at the same time, and stores the text and data that it
  validated, even from `str` and `bytes` subclasses (#539).
- `page.annotations.add` creates and draws `FreeText` and `Stamp`
  annotations, and free text can draw in a loaded face (#168, #540).
- `doc.form.font` sets the OpenType face that text and choice fields draw
  their values in, embedded once and subset at save (#194).
- `Widget.font` reads and sets the Standard 14 font of a widget, resolved
  through the form's resources (#212, #545).
- `TextField`, `ComboBox`, `ListBox`, and `PushButton` take a Standard 14
  `font`, which the form's resources name without colliding with existing
  names (#156, #534).
- A redrawn widget keeps its Standard 14 font when the font dictionary has
  no `/Encoding` (#544).
- `doc.sign` signs a document as PAdES B-B, or B-T with a time-stamp, in a
  new revision, with a `Signer` from PEM, PKCS#12, or a callback (#397).
- `doc.signatures` lists the signatures of the opened file, and
  `Signature.verify` checks their digest, CMS signature, time-stamp, trust,
  and later modifications (#403).
- `doc.to_json` and `doc.to_chunks` export pages' elements, with their
  kinds, pages, and boxes, as JSON and as retrieval chunks, `Element` and
  `Chunk` (#427).
- `doc.save(path, linearize=True)` writes a linearized file, and
  `doc.is_linearized` detects one (#426).
- `doc.history.save()` returns the undo history as bytes, and
  `doc.history.restore()` restores it on the document that it was saved
  from (#428).
- `doc.subset_fonts` keeps a font whole when the content that uses it
  cannot be decoded (#486).
- `page.text()` keeps a heading above two columns whole (#460), keeps
  right-to-left marks with their letters (#469), and reads a line that
  mixes text directions in logical order (#488).
- `DocumentLimits.max_memory_size` sets the memory budget of each
  operation (#553).
- The limit code `object_parse_size` refuses an object larger than 256 MiB
  before hayro-syntax parses it (#509).
- New limit codes bound the new operations: `export_output_size` (#427),
  `font_input_size` (#479), `image_input_size` (#412), `shaped_glyph_count`
  (#480), `text_edit_forms` (#413), and `text_input_size` (#496).
- A canvas, and a form fill in an embedded face, is one operation of the
  limits from preparing its content to writing it, and rolls back whole
  when it is refused (#500, #518).
- Annotation and form field edits check their input against the limits
  before copying it (#542, #550).
- Cancellation and deadlines are checked while objects parse (#514), while
  values are copied for editing (#502) and written (#508, #511, #515),
  while text lays out (#541, #552), and while streams compress and PNG
  images encode and decode (#501, #517).

No release claims safety for untrusted input before hayro's bounded
construction (I2, [#15](https://github.com/yhay81/pyropdf/issues/15)) is
released.

## 0.2.0 (2026-10-30)

- `doc.metadata` reads and writes the Info dictionary and writes the matching
  properties of an existing XMP packet in the same transaction; `doc.xmp`
  reads, replaces, and removes the packet; `pdf_date` and `parse_pdf_date`
  convert PDF dates (#356).
- `Page.media_box`, `crop_box`, `bleed_box`, `trim_box`, and `art_box` read
  the page boxes in display space and set them on one page (#357).
- `doc.page_labels` reads and replaces the page label ranges, and
  `page.label` and `doc.pages.labeled` read the labels (#358).
- `page.images()` decodes each image that a page paints and returns its
  data as stored; `doc.replace_image` and `doc.remove_image` rewrite an
  image object for every placement (#359).
- `doc.layers` lists the optional content groups and configurations,
  switches groups and applies configurations, which rendering and text
  follow, and evaluates visibility expressions with a bounded depth (#360).
- `doc.sanitize` removes JavaScript, actions, attachments, metadata,
  comments, hidden text, and unused objects, saves one revision, and
  reports the signatures and profile claims that break (#363).
- `doc.sanitize(hidden_text=True)` also removes invisible text inside the
  form XObjects that pages and appearances paint, with the rendering mode
  each form inherits (#378).
- `doc.subset_fonts` subsets each embedded TrueType, OpenType, and CFF
  program to the glyphs that the pages draw, keeping glyph IDs, so text and
  widths are unchanged (#364).
- The API reference gives every method and property its full description,
  one page per topic (#382), and the guides add recipes, concepts, and a
  comparison with other libraries (#383).

## 0.1.0 (2026-10-29)

The first alpha release, on the versioned object store of DESIGN.md.

- Open, probe, repair, and save documents, incrementally or in full, with
  the standard security handler.
- Transactions, undo and redo through `Document.history`, and change
  reports of what a save writes.
- Pages with identity: insert, move, select, import from another document,
  rotate, and flatten.
- Text, words, characters, layout, search, selection, tables, Markdown, and
  offline OCR.
- Rendering to pixmaps and SVG through hayro.
- Annotations, links, destinations, outlines, attachments, and forms with
  widgets.
- Typed objects from the Arlington model and a structural check.

No release claims safety for untrusted input before hayro's bounded
construction (I2, [#15](https://github.com/yhay81/pyropdf/issues/15)) is
released.
