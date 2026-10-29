# Changelog

## Unreleased

- `doc.metadata` reads and writes the Info dictionary and writes the matching
  properties of an existing XMP packet in the same transaction; `doc.xmp`
  reads, replaces, and removes the packet; `pdf_date` and `parse_pdf_date`
  convert PDF dates (#356).
- `Page.media_box`, `crop_box`, `bleed_box`, `trim_box`, and `art_box` read
  the page boxes in display space and set them on one page (#357).

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
