# pyropdf

pyropdf is the successor of [pylopdf](https://github.com/yhay81/pylopdf):
a Python library, implemented in Rust, for complete control of PDF as
ISO 32000-2 defines it. It keeps pylopdf's concept and replaces its
foundation with one versioned object store that every engine reads, so an
edit costs what it touches, every change can be undone, and a failed
transaction leaves the document as it was.

**Status:** alpha. The API may change between minor versions. Documentation:
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

The 0.1 releases read text, words, layout, tables, and Markdown, run
offline OCR, render pages to pixmaps and SVG, edit pages, annotations,
links, form fields, outlines, and attachments, and save incrementally or
in full, with encryption. Drawing on a page with `page.draw` comes in a
later release.

## Safety claims

No pyropdf release claims safety for untrusted input before hayro's bounded
construction (I2, [#15](https://github.com/yhay81/pyropdf/issues/15)) is
released. `DocumentLimits` bounds the work of an operation, but it is not
that claim.
