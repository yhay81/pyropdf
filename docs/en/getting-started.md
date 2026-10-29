---
title: Getting started
description: Install pyropdf, open a document, read and render a page, edit it in a transaction, and save.
---

# Getting started

## Install

```bash
pip install pyropdf
```

Wheels cover CPython 3.11 to 3.15 on Linux, macOS, and Windows, with
separate wheels for the free-threaded builds. pyropdf has no required
dependencies. Offline OCR needs its models, which an extra installs:

```bash
pip install "pyropdf[ocr]"
```

## Open and close

`pyropdf.open` takes a path or the bytes of a document. A document is a
context manager that closes on exit; `pyropdf.Document()` creates an empty
one.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    print(len(doc.pages), doc.pages[0].rect)

doc = pyropdf.Document()
doc.pages.new((595, 842))
data = doc.to_bytes()
doc.close()
```

`pyropdf.probe` reads the page count, the version, and the metadata without
loading the document:

```python
import pyropdf

info = pyropdf.probe("input.pdf")
print(info.page_count, info.version, info.needs_password)
```

## Read a page

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())
    for word in page.words():
        print(word.text, word.bbox)
    print(page.search("total"))
```

[Reading content](reading.md) covers layout, characters, tables, Markdown,
and OCR.

## Render a page

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].pixmap(dpi=144).save("page-1.png")
```

## Edit and save

Every edit makes a new version of the document. A transaction groups edits
into one named step that `doc.history` can undo, and leaves the document as
it was if its block raises.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("rotate and note"):
        page.rotation = 90
        page.annotations.add(pyropdf.Note((40, 40), contents="Checked"))
    doc.save("output.pdf", incremental=True)
```

An incremental save appends only the objects that the edits wrote. A full
save, the default, rewrites the file. [Versions and saving](versions.md)
explains both.

## Next

- [Reading content](reading.md): text, words, layout, tables, Markdown, OCR.
- [Rendering](rendering.md): pixmaps, batches, and SVG.
- [Editing](editing.md): pages, annotations, links, forms, outlines, attachments.
- [Versions and saving](versions.md): transactions, undo, change reports, encryption.
- [Limits and errors](limits.md): bounding the work of each operation.
