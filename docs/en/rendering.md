---
title: Rendering
description: Render pages to pixmaps, PNG, JPEG, and SVG through hayro, one at a time or in parallel batches.
---

# Rendering

pyropdf renders through [hayro](https://github.com/LaurenzV/hayro), reading
the current version straight from the object store.

## Pixmaps

`page.pixmap()` returns a `Pixmap`: immutable 8-bit pixels, RGB or gray,
with or without alpha. `dpi` scales the page, 72 by default.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmap = doc.pages[0].pixmap(dpi=144, alpha=True)
    print(pixmap.width, pixmap.height, pixmap.channels)
    pixmap.save("page-1.png")
    jpeg = doc.pages[0].pixmap(dpi=144).to_bytes("jpeg", quality=85)  # JPEG has no alpha
```

A pixmap supports the buffer protocol, so NumPy and Pillow read its pixels
without a copy: `numpy.asarray(pixmap)` has the shape
`(height, width, channels)`.

`clip` renders part of a page, `matrix` a transformed page, `colorspace="gray"`
gray pixels, and `annotations=False` the page without its annotations.

## Batches

`doc.render` renders several pages in parallel and returns encoded images in
the order of `pages`:

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    images = doc.render(dpi=96, format="png", workers=4)
    print(len(images), "pages rendered")
```

## SVG

`page.svg()` returns the page as an SVG string at 72 dpi, with text drawn as
paths:

```python
import pyropdf
from pathlib import Path

with pyropdf.open("input.pdf") as doc:
    Path("page-1.svg").write_text(doc.pages[0].svg(), encoding="utf-8")
```
