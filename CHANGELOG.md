# Changelog

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
