# Rewriting images

The measurement for #401: `doc.rewrite_images()` shrinks documents whose
images hold more pixels than their placements need, and their pages still
render alike at the target resolution.

## Method

`measure.py` opens each corpus document that places an image object, saves
it in full with `prune=True`, renders every page at 100 dpi, calls
`rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)`, then saves
and renders again. Pruning drops the masks that a rewrite replaces, which a
plain full save keeps as unreferenced objects. The differences compare one 8-bit sample of the two
renderings. Seconds time `rewrite_images()` alone.

## Results

Apple M2 Pro, 2026-10-31, after #452 (masks of another size, `/Matte`,
stencil mask images, and inline images in Form XObjects and appearances):

| Document | Images | Rewritten | Saved before | Saved after | Change | Mean diff | Max diff | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bunka-kokugo-series-019-p4.pdf | 1 | 1 | 130,565 | 83,964 | -35.7% | 0.39 | 24 | 0.15 |
| patent-us223898.pdf | 4 | 4 | 319,503 | 165,327 | -48.3% | 6.24 | 146 | 0.13 |
| pdfium-jpx-lzw.pdf | 1 | 0 | 1,224 | 1,224 | +0.0% | 0.00 | 0 | 0.00 |
| wdl6812-manuscript.pdf | 4 | 4 | 377,500 | 141,716 | -62.5% | 3.66 | 72 | 0.08 |

bunka-kokugo-series-019-p4.pdf's 3311 by 4681 gray Flate scan, placed at
400 dpi, becomes 828 by 1171 Flate samples: lossless apart from the
resampling. Of wdl6812-manuscript.pdf's two JPEGs, the one placed at
150.09 dpi becomes a 450 by 588 JPEG at 100 dpi, and the one placed at
exactly 150 dpi keeps its pixels and is recompressed at quality 65. Its two
other images are 450 by 584 and 450 by 588 JPEGs, each under a 1350 by
1750 (or 1763) JBIG2 stencil `/Mask`: the JPEGs, already at 100 dpi, are
recompressed, and each mask is resampled at its own resolution to 450
pixels wide and stays a stencil, now CCITT Group 4 (11,793 and 17,227
bytes become 3,376 and 5,631). A soft mask of the image's size would have
blurred the mask's edges, so it stays 1-bit. Ghostscript at 100 dpi moves
the two pages by 4.61 and 4.53 of 255; at 72 dpi, the bound of
`tests/test_rewrite_images.py`, the first page moves by 5.06, of which
quality 65 alone gives 3.19 and the 1-bit masks at a third of their
resolution the rest. pdfium-jpx-lzw.pdf's JPEG 2000 image names no color space; since #451 a
rewrite reads it from the colors that hayro paints, but its 1,224 stored
bytes are smaller than any re-encoding, so it stays.

The corpus holds no CMYK JPEG, Indexed, Lab, Separation, DeviceN, or
2-, 4-, or 16-bit image. `tests/test_rewrite_images.py` builds them: CMYK
JPEGs with and without an Adobe APP14 marker, JPEG 2000 without a color
space, with `/SMaskInData`, and in ICCBased, Indexed images of 2, 4, and
8 bits, and the other spaces at 2, 4, and 16 bits, each a 128 by 128
image at 36 points resampled to 36 pixels. pyropdf and Ghostscript, each
rendering the page at 72 dpi before and after, agree within a mean of 4
of 255, and within 12 for the nearest-index resampling of Indexed images.

patent-us223898.pdf's four 2320 by 3408 CCITT scans, placed at 300 dpi,
become 774 by 1136 CCITT Group 4 images at 100 dpi; before #432 they stayed,
because 8-bit Flate would have grown. The sample differences are those of
thresholding a resampled scan at half gray: Ghostscript, rendering each
page at 100 dpi before and after, moves the mean by 1.09, 4.84, 4.05, and
2.46 of 255. `tests/test_rewrite_images.py` checks the Group 4 output
against libtiff's decoder and compares Ghostscript's renderings of the
masked, CMYK, inline, and bitonal fixtures before and after.

The largest differences sit on edges, where the resampled image and
hayro's sampling of the source round differently; the mean stays below
4 of 255, the bound that `tests/test_rewrite_images.py` checks on the two
rewritten color documents.

Before #452, without pruning, wdl6812-manuscript.pdf rewrote 2 of its 4
images (158,023 bytes, mean difference 2.49), its masked JPEGs reported.
Before #432, 2026-10-30, patent-us223898.pdf was not rewritten either.
