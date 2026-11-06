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
| `hinting` | `True` hints text as FreeType's light hinting does, which sharpens small text; text renders unhinted by default. |
| `glyph_cache` | `True` rasterizes each glyph once at each size and reuses it, which renders text faster but moves it by up to half a pixel vertically. The cache's memory is outside `DocumentLimits` and lasts as long as the thread's render cache for the document's version. Off by default. |

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

A raster is at most 65,408 pixels wide, 65,532 high, and 64,000,000 in
all, and a larger one raises `ValueError`. With `clip`, these limits apply
to the clipped raster, so a small region renders at scales at which the
whole page would not: the 100 by 100 point corner of an A1 page renders
2,000 pixels square at 20 times, where the whole page would take 1.6
billion pixels. The page under the matrix may measure up to 524,288 pixels
a side; within that, content that reaches the clip from the far side of the
page lands within 1/16 of a pixel of where it belongs.

## Encoding

`pixmap.save(path)` writes the format that the file's extension names, and
`pixmap.to_bytes("png")` or `to_bytes("jpeg", quality=...)` returns the
encoded bytes. Besides PNG and JPEG, both write PAM, PBM, PGM, PPM, PNM,
PSD, and PostScript. Only PNG, PAM, and PSD keep alpha, and the other
formats refuse it, so encode an opaque pixmap.

## Pixel operations

Each operation returns a new pixmap: `invert(rect=None)`, `gamma(value)`,
`tint(black, white)`, `to_gray()`, `to_rgb()`, `with_alpha()`,
`without_alpha(background)`, `crop(rect)`, `scale(width, height)`,
`rotate(degrees)`, `flip(axis)`, `warp(quad, width, height)`,
`fill(value, rect=None)`, and `with_pixel(x, y, value)`. `pixel(x, y)`,
`colors(rect=None)`, `color_count(rect=None)`, and `top_color(rect=None)`
read the pixels. Rectangles are in whole pixels. `Pixmap.from_bytes(data)`
and `Pixmap.open(path)` read PNG, JPEG, PBM, PGM, PPM, PAM, BMP, GIF, TIFF,
and JPEG 2000, and `to_pil()` returns a Pillow image when Pillow is
installed.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmap = doc.pages[0].pixmap(dpi=72)
    thumbnail = pixmap.to_gray().scale(pixmap.width // 4, pixmap.height // 4)
    thumbnail.invert((0, 0, 10, 10)).save("thumbnail.png")
```

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

## Fonts that a document does not embed

A PDF may name a font without embedding its program, as many Japanese,
Chinese, and Korean documents do with MS-Gothic, MS-Mincho, SimSun, or
Batang. hayro draws such a font in a Standard 14 font, which has no CJK
glyphs, so its text renders blank, although `page.text()` reads it. The
font extras install faces that draw it instead:

=== "pip"

    ```bash
    pip install "pyropdf[jp]"
    ```

=== "uv"

    ```bash
    uv add "pyropdf[jp]"
    ```

=== "Poetry"

    ```bash
    poetry add "pyropdf[jp]"
    ```

`jp`, `ko`, `zh-cn`, and `zh-tw` install the Noto Sans and Noto Serif faces
of Japanese, Korean, Simplified Chinese, and Traditional Chinese. Every
rendering, `page.pixmap()`, `page.svg()`, `doc.render()`, and
`annotation.pixmap()`, then draws a font of the Adobe-Japan1, Korea1, GB1,
or CNS1 character collection in the faces of its language, and a font whose
name marks it as serif, such as a Mincho, in the serif face. A face is read
the first time that a page needs it.

`doc.fallback_fonts` maps the languages `"ja"`, `"ko"`, `"zh-CN"`, and
`"zh-TW"` to `FallbackFont` records, and lists the installed extras until it
is edited. Assign `FallbackFont(sans=..., serif=...)` with faces that
`Font.from_file` loads to draw a language in other faces, `del` a language
to draw it without, and assign `{}` to draw every language without:

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    print(list(doc.fallback_fonts))  # the installed extras, such as ['ja']
    doc.fallback_fonts = {}  # render without fallback fonts
    plain = doc.pages[0].pixmap()
    del doc.fallback_fonts  # back to the installed extras
```

The fonts belong to the `Document` object and are not saved. Text
extraction reads the text from the character collection without them. Only
the exact boxes of `exact_bboxes=True` and the text runs of `page.content()`
use them, to measure what a rendering draws.
