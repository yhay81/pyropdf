---
title: Getting started
description: Install pyropdf, open a document, read and render a page, edit it in a transaction, and save.
---

# Getting started

## Install

=== "pip"

    ```bash
    pip install pyropdf
    ```

=== "uv"

    ```bash
    uv add pyropdf
    ```

=== "Poetry"

    ```bash
    poetry add pyropdf
    ```

Wheels cover CPython 3.11 to 3.15 on Linux, macOS, and Windows, with
separate wheels for the free-threaded builds. pyropdf has no required
dependencies. Offline OCR needs its models, which an extra installs:

=== "pip"

    ```bash
    pip install "pyropdf[ocr]"
    ```

=== "uv"

    ```bash
    uv add "pyropdf[ocr]"
    ```

=== "Poetry"

    ```bash
    poetry add "pyropdf[ocr]"
    ```

The `jp`, `ko`, `zh-cn`, and `zh-tw` extras install the faces that render
Japanese, Korean, and Chinese fonts that a document does not embed; see
[Rendering](rendering.md#fonts-that-a-document-does-not-embed).

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

```python title="read.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())  # (1)!
    for word in page.words():
        print(word.text, word.bbox)  # (2)!
    print(page.search("total"))  # (3)!
```

1. Text in reading order: columns, tables, and headings come out as a reader
   would read them, not in the order the content stream draws them.
2. Boxes are in display coordinates: points from the top-left of the page as
   a viewer shows it, whatever the page's rotation. See
   [Concepts](concepts.md#display-coordinates).
3. A list of rectangles, one per match, ready to pass to an annotation such
   as `pyropdf.Highlight`.

[Reading content](reading.md) covers layout, characters, tables, Markdown,
and OCR.

## Render a page

```python title="render.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].pixmap(dpi=144).save("page-1.png")
```

## Edit and save

Every edit makes a new version of the document. A transaction groups edits
into one named step that `doc.history` can undo, and leaves the document as
it was if its block raises.

```python title="edit.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("rotate and note"):  # (1)!
        page.rotation = 90
        page.annotations.add(pyropdf.Note((40, 40), contents="Checked"))
    doc.save("output.pdf", incremental=True)  # (2)!
```

1. Both edits become one named step of `doc.history`. If the block raises,
   neither stays, and the document is the version it was before.
2. Appends only the objects that the edits wrote, after the original bytes,
   so signatures over those bytes stay valid.

An incremental save appends only the objects that the edits wrote. A full
save, the default, rewrites the file. [Versions and saving](versions.md)
explains both.

## Next

- [Reading content](reading.md): text, words, layout, tables, Markdown, OCR.
- [Rendering](rendering.md): pixmaps, batches, and SVG.
- [Editing](editing.md): pages, annotations, links, forms, outlines, attachments.
- [Versions and saving](versions.md): transactions, undo, change reports, encryption.
- [Limits and errors](limits.md): bounding the work of each operation.
