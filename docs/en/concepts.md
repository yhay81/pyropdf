---
title: Concepts
description: The model behind pyropdf's API — display coordinates, versions, handles, threads, and the cost of operations.
---

# Concepts

## Display coordinates

Every rectangle and point in the API, the boxes of words and search hits,
annotation rectangles, `clip`, and the page boxes, is in **display
coordinates**: points of 1/72 inch, with the origin at the top-left corner
of the visible page and `y` growing downward. The visible page is the
CropBox clipped to the MediaBox, with the page's rotation applied, so
`page.rect` is `(0, 0, width, height)` as a viewer shows it.

PDF's own space has its origin at the bottom-left of the MediaBox and does
not rotate; pyropdf converts for you, so a rectangle found by `search` can go
straight into an annotation. To map a rectangle to the pixels of a render,
scale by `dpi / 72`:

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    dpi = 144
    pixmap = page.pixmap(dpi=dpi)
    x0, y0, x1, y1 = (value * dpi / 72 for value in page.words()[0].bbox)
    print(pixmap.width, round(x0), round(y0), round(x1), round(y1))
```

## Versions

A document is the file it opened, the base, and a chain of versions above
it. An edit writes only the objects it changes into a new version; the rest
stay shared with the versions below. Readers, renderers, and saves all work
on the current version, so an edit is visible to the next `text()` or
`pixmap()` without saving, and a result that depends only on a version is
computed once for it.

`doc.history` keeps the steps that undo and redo walk. Each step holds the
objects that it replaced, and `doc.history.size` counts their bytes:
`doc.history.max_size` bounds them, and `doc.history.clear()` forgets the
steps while keeping the document as it is.

## Handles

A `Page`, `Annotation`, `Field`, or object handle names a thing, not a
position. A page handle follows its page when pages move, and reads
whatever the current version says about it, also after an undo. Once its
page is gone, using it raises `StalePageError`; objects raise
`StaleObjectError` the same way.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    first = doc.pages[0]
    doc.pages.move(0, 1)
    print(first.index)           # 1: the handle followed its page
    del doc.pages[1]
    try:
        first.text()
    except pyropdf.StalePageError:
        print("the page is gone")
```

## Transactions

`doc.transaction(name)` makes the edits of its block one step. If the block
raises, the document returns to the version before it; nothing is copied to
make that possible. Edits outside a transaction are a step each.
[Versions and saving](versions.md) shows both.

## Threads

Several threads may read one document at once: a read holds a lock only
to take the current version, then works without it. Writes are serialized
and publish complete versions, so a long render keeps reading the version
that it started with while another thread edits. Rendering, text, and
other heavy reads release the GIL, and pyropdf ships wheels for the
free-threaded builds of CPython.

```python
from concurrent.futures import ThreadPoolExecutor

import pyropdf

with pyropdf.open("input.pdf") as doc, ThreadPoolExecutor() as pool:
    texts = list(pool.map(lambda page: page.text(), doc.pages))
    print(len(texts))
```

`doc.render` already renders pages on several threads.

## Cost

- Opening reads the cross-reference data, not every object; `probe` reads
  less still.
- An edit costs what it touches: rotating a page writes one object.
- An incremental save appends only the objects that the edits wrote; a full
  save rewrites the file.
- `DocumentLimits` bounds the work of each operation; see
  [Limits and errors](limits.md).
