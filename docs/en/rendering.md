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

## Options

| Option | Effect |
| --- | --- |
| `dpi` | Scales the page uniformly; 72, the default, is one pixel per point. |
| `matrix` | A `Matrix`, such as `Matrix(90)` for a quarter turn, or six numbers that scale, rotate, or skew the page; the raster covers the transformed page, so its translation is dropped. |
| `clip` | A display rectangle `(x0, y0, x1, y1)`; only its pixels are rasterized. |
| `alpha` | Keeps transparency instead of painting the page on white. |
| `background` | An RGB or RGBA tuple, 0 to 255, painted beneath the page; it must be opaque without `alpha`. |
| `annotations` | `False` leaves out annotation and form widget appearances. |
| `colorspace` | `"gray"` converts to gray with ITU-R BT.601 weights. |

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    top = page.pixmap(dpi=200, clip=(0, 0, page.rect.width, 120))
    gray = page.pixmap(dpi=96, colorspace="gray", annotations=False)
    tinted = page.pixmap(background=(255, 250, 235))
    rotated = page.pixmap(matrix=pyropdf.Matrix(90))
    print(top.height, gray.channels, tinted.channels, rotated.width)
```

`clip` takes display coordinates, the space of `page.rect` and of the
boxes that `words()` and `search()` return; see
[Concepts](concepts.md#display-coordinates).

## Encoding

`pixmap.save(path)` writes PNG or JPEG by the file's extension, and
`pixmap.to_bytes("png")` or `to_bytes("jpeg", quality=...)` returns the
encoded bytes. JPEG has no alpha, so encode an opaque pixmap.

## Batches

`doc.render` renders several pages in parallel, on up to `workers` threads
with the GIL released, and returns encoded images in the order of `pages`.
It takes the options of `page.pixmap`, and every page renders from the
version that was current when the call started, even while another thread
edits:

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    images = doc.render(dpi=96, format="png", workers=4)
    print(len(images), "pages rendered")
    first_only = doc.render([0], dpi=150, format="jpeg", quality=85)
```

`max_size` bounds the encoded bytes of the whole batch, 512 MiB by default.

## SVG

`page.svg()` returns the page as an SVG string at 72 dpi, with text drawn as
paths, so it shows the same in every browser but is not searchable.
`annotations=False` leaves out annotations, and `max_size` caps the string,
64 MiB by default:

```python
import pyropdf
from pathlib import Path

with pyropdf.open("input.pdf") as doc:
    Path("page-1.svg").write_text(doc.pages[0].svg(), encoding="utf-8")
```
