# pyropdf

pyropdf is a Python library, implemented in Rust, for complete control of
PDF as ISO 32000-2 defines it. It keeps a document on one versioned object
store that every engine reads, so an edit costs what it touches, every
change can be undone, and a failed transaction leaves the document as it
was.

**Status:** alpha, 0.2. The API may change between minor versions. Documentation:
<https://pyropdf.haya.works/>. [DESIGN.md](DESIGN.md) describes the
foundation, and [CHANGELOG.md](CHANGELOG.md) what each release holds.

```bash
pip install pyropdf
```

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())
    page.pixmap(dpi=144).save("page-1.png")
    with doc.transaction("rotate"):
        page.rotation = 90
    doc.save("output.pdf", incremental=True)
```

0.2.1 reads text, words, layout, tables, images, fonts, and Markdown,
exports them as JSON or retrieval chunks, runs offline OCR, and renders
pages to pixmaps and SVG. It draws paths, images, pages, and text in
Standard 14 or OpenType fonts with `page.draw`; edits pages and their boxes,
text, images, colors, annotations, links, form fields, outlines,
attachments, metadata, page labels, and layers; signs documents and verifies
signatures; removes hidden or active content and subsets fonts; and saves
incrementally, in full, or linearized, with encryption.

## Safety claims

No pyropdf release claims safety for untrusted input before hayro's bounded
construction (I2, [#15](https://github.com/yhay81/pyropdf/issues/15)) is
released. `DocumentLimits` bounds the work of an operation, but it is not
that claim.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) explains how to build, test, and propose
a change, and [SECURITY.md](SECURITY.md) how to report a vulnerability.
