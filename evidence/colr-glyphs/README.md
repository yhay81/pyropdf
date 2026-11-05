# COLR glyphs drawn as a Type 3 font (issue #633)

Does canvas text paint every COLR glyph of a real colour face, or refuse it as
DESIGN.md section 7 says, and what do the drawings cost and look like?

The later [Type 3 line-order experiment](line-order.md) (#670) fixes xpdf's
reading order at 1.25 em spacing and compares dictionary/procedure candidates
in four readers, including CBDT and sbix glyphs.

## Method

```bash
cargo run --release --locked --manifest-path evidence/colr-glyphs/paint/Cargo.toml -- C:/Windows/Fonts/seguiemj.ttf > evidence/colr-glyphs/results/segoe-ui-emoji.json
uv run --with "blackrenderer[skia]" --with fonttools --with pymupdf --with pypdfium2 python evidence/colr-glyphs/compare.py C:/Windows/Fonts/seguiemj.ttf OUTPUT_FOLDER > evidence/colr-glyphs/results/compare.json
```

- `paint/` checks and paints every glyph of the face with the engine's
  `fonts::colr` painter, each in a document of its own under the default
  limits, as canvas text does. It counts what the check says of each glyph
  and whether the paint agrees, and what each painted glyph writes: its
  objects, the bytes of its streams as written (compressed where that is
  shorter), and the time of its check and paint.
- `compare.py` draws each of eight emoji (those of #598) alone at 64 points
  and renders the page at 144 dpi in hayro, MuPDF, and PDFium. blackrenderer,
  a COLR v1 renderer on Skia that shares no code with pyropdf, renders the
  same glyph at the same place as the reference. A pixel's difference is the
  largest of its three channels, over white, among the pixels that the
  reference inks. It then draws one page of 100 distinct emoji, times it,
  saves it three ways, renders it, and reads it back with four readers.

Segoe UI Emoji is the face that Windows 11 installs; it is not committed.
AMD Ryzen 5 3600XT, Windows 11, 2026-11-04.

## Every glyph

`results/segoe-ui-emoji.json`:

| Glyphs | Colour glyphs | Paint | Fallback outline | Refuse | Check and paint disagree |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 64,883 | 3,487 | 3,487 | 0 | 0 | 0 |

| Per painted glyph | Median | 95th percentile | Most |
| --- | ---: | ---: | ---: |
| Objects | 214 | 412 | 975 |
| Stream bytes | 5,903 | 12,616 | 170,171 |
| Check and paint, ms | 5.4 | 14.6 | 138.1 |

All 3,487 glyphs paint: the face uses solid colours, linear and radial
gradients, transforms, and composite modes that PDF expresses, and no sweep
gradient. The 97.7% of #627 whose gradients have alpha paint through soft
masks. Their objects are mostly the shadings, functions, and soft masks of
those gradients, which equal gradients share, among glyphs too. The glyph of
170 KB is the largest of #627's samples, 163 KB of operators there.

## Against another renderer

`results/compare.json`. The mean of a pixel's difference in 8-bit levels, and
the share of inked pixels within 8 and within 32 levels:

| Emoji | hayro mean | hayro ≤ 8 | hayro ≤ 32 | MuPDF mean | MuPDF ≤ 8 | MuPDF ≤ 32 | PDFium mean | PDFium ≤ 8 | PDFium ≤ 32 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 😀 U+1F600 | 6.2 | 83.5% | 94.0% | 5.2 | 84.1% | 98.5% | 6.7 | 83.9% | 98.6% |
| 🎉 U+1F389 | 4.1 | 90.1% | 98.4% | 3.6 | 89.8% | 99.1% | 4.0 | 89.5% | 99.1% |
| 👍 U+1F44D | 5.8 | 72.9% | 99.3% | 5.2 | 75.7% | 99.6% | 7.4 | 67.5% | 99.7% |
| 🚀 U+1F680 | 2.6 | 94.8% | 99.6% | 2.2 | 96.0% | 99.9% | 3.1 | 94.2% | 100.0% |
| 🌸 U+1F338 | 5.0 | 76.4% | 99.8% | 5.5 | 73.6% | 99.9% | 18.6 | 9.9% | 98.7% |
| 🍣 U+1F363 | 4.3 | 85.7% | 99.2% | 2.9 | 92.4% | 99.6% | 3.0 | 93.0% | 99.6% |
| 🐱 U+1F431 | 8.3 | 59.6% | 97.5% | 7.1 | 59.5% | 99.9% | 11.0 | 26.5% | 99.9% |
| 🎌 U+1F38C | 2.2 | 91.6% | 99.8% | 2.6 | 91.9% | 99.3% | 4.0 | 88.9% | 99.4% |

The renders are not committed; side by side, each reader's emoji is the
reference's in shape, layers, and gradients.

## A page of 100 emoji

U+1F300 to U+1F365, the first 100 mapped characters from U+1F300 that paint,
ten lines of ten at 24 points, 30 points apart, in one `page.draw()`:

| Draw s | Objects | Type 3 fonts | Saved KiB | dedupe=True KiB | Compressed KiB |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.515 | 12,872 | 1 | 3,104 | 3,104 | 1,289 |

| Reader | Render at 144 dpi, s | Reads the text |
| --- | ---: | --- |
| pyropdf (hayro) | 4.21 | yes |
| MuPDF (PyMuPDF) | 0.66 | yes |
| PDFium (pypdfium2) | 0.74 | yes |
| `pdftotext` (xpdf 4.06) | not timed | every glyph, out of order |

"Compressed" adds `compress=True, object_streams=True` to `dedupe=True`.

## Findings

- Every colour glyph of Segoe UI Emoji paints, and none needs its fallback
  outline: the check that decides before a drawing writes anything agrees
  with the paint on all 3,487. A face with a sweep gradient, the modes that
  PDF does not express, or a stop in the text's colour draws the fallback
  outline or refuses; the committed `colour-colr-paints.ttf` has each.
- A COLR glyph costs far more than a bitmap or an outline: about 6 KiB of
  streams and 200 objects at the median, mostly the shadings, functions, and
  soft masks of gradients with alpha. 100 distinct emoji make a page of 1.3
  MiB compressed, against 38.6 KiB for 26 bitmap glyphs on 100 pages
  (`evidence/colour-bitmaps`). The cost is per glyph, not per drawing: a
  document holds each glyph's procedure and group once, and equal shadings
  once, which `tests/test_canvas_embedded.py` guards.
- Drawing is quick, 5 ms a glyph at the median, but hayro takes 4.2 s to
  render that page at 144 dpi, six times MuPDF's and PDFium's.
- The readers agree with blackrenderer to within 8 levels for 60% to 96% of
  the inked pixels, and within 32 for 94% to 100%. The rest is smoothing at
  the edges of layers and the interpolation of gradients. hayro draws the mouth of 😀
  more magenta than blackrenderer, where MuPDF, reading the same file, does
  not; PDFium draws 🌸 and 🐱 more saturated than the others.
- xpdf's `pdftotext` reads every glyph of the page, but takes each pair of
  lines as one, so their glyphs alternate. It reads the lines in order 36
  points apart, and reads outline text in order 30 points apart, so the
  difference is how it measures a Type 3 font's lines.
- Each glyph is one Form XObject, an isolated transparency group with its own
  resources, which the Type 3 font's resources name once. A glyph's layers
  need a group of their own anyway, since its soft masks and blend modes must
  composite the glyph as a whole under the text's opacity, so writing the
  layers into the glyph procedure with one shared dictionary was not tried.
