# Rewriting images

The measurement for #401: `doc.rewrite_images()` shrinks documents whose
images hold more pixels than their placements need, and their pages still
render alike at the target resolution.

## Method

`measure.py` opens each corpus document that places an image object, saves
it in full, renders every page at 100 dpi, calls
`rewrite_images(dpi_threshold=150, dpi_target=100, quality=65)`, then saves
and renders again. The differences compare one 8-bit sample of the two
renderings. Seconds time `rewrite_images()` alone.

## Results

Apple M2 Pro, 2026-10-31, after #432 (Group 4 output for bitonal images,
soft masks, CMYK and ICC-based samples, and inline images):

| Document | Images | Rewritten | Saved before | Saved after | Change | Mean diff | Max diff | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bunka-kokugo-series-019-p4.pdf | 1 | 1 | 131,036 | 84,436 | -35.6% | 0.39 | 24 | 0.14 |
| patent-us223898.pdf | 4 | 4 | 319,926 | 165,837 | -48.2% | 6.24 | 146 | 0.13 |
| pdfium-jpx-lzw.pdf | 1 | 0 | 1,224 | 1,224 | +0.0% | 0.00 | 0 | 0.00 |
| wdl6812-manuscript.pdf | 4 | 2 | 377,721 | 158,023 | -58.2% | 2.49 | 33 | 0.02 |

bunka-kokugo-series-019-p4.pdf's 3311 by 4681 gray Flate scan, placed at
400 dpi, becomes 828 by 1171 Flate samples: lossless apart from the
resampling. Of wdl6812-manuscript.pdf's two JPEGs, the one placed at
150.09 dpi becomes a 450 by 588 JPEG at 100 dpi, and the one placed at
exactly 150 dpi keeps its pixels and is recompressed at quality 65; its two
other images carry a stencil `/Mask` larger than the image, which a soft
mask of the image's size would blur, so `ImageRewriteReport.unsupported`
names them. pdfium-jpx-lzw.pdf's JPEG 2000 image names no color space and
is reported too.

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

Before #432, 2026-10-30, patent-us223898.pdf was not rewritten and the
other rows were the same.
