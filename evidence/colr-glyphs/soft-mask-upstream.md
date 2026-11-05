# Draft upstream issue: Small soft-mask groups are rasterized and cached at full page size

## Reproduce

Render [mask-1024-32.pdf](https://github.com/yhay81/pyropdf/blob/main/evidence/colr-glyphs/rendering-fixtures/mask-1024-32.pdf)
at scale 1 with `hayro::render`. This 2,135-byte PDF has six indirect objects:
one luminosity mask whose group has `/BBox [0 0 8 8]`, painted at 32 translations
on a 1,024 by 1,024 point page. There are no images, fonts, gradients,
compression, or transfer functions. A [single-placement fixture](https://github.com/yhay81/pyropdf/blob/main/evidence/colr-glyphs/rendering-fixtures/mask-1024-1.pdf)
is 778 bytes.

Tested on hayro `3de8bb0401f0410b71f37a52524b7607433ace13`, vello_cpu 0.3.0,
Windows 11, AMD Ryzen 5 3600XT, Rust 1.99.1 release. The
[standalone probe and generator](https://github.com/yhay81/pyropdf/blob/main/evidence/colr-glyphs/rendering.md)
pin the source and dependencies, compare MuPDF/PDFium, and generate gray-fill
controls whose sampled pixels agree with the masked ones to within one level.

## Measurements

The [raw results](https://github.com/yhay81/pyropdf/blob/main/evidence/colr-glyphs/results/rendering.json)
record five samples after a warmup, with fresh documents and caches. Inputs
are in memory; process startup and PNG encoding are excluded. Page dimensions
vary while each painted group stays 8 by 8 pixels. The cost scales with page
area times placement count, rather than the small painted bounds.

| Page pixels | Placements | hayro, ms | MuPDF, ms | PDFium, ms | hayro gray-fill control, ms |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 128 x 128 | 32 | 1.983 | 0.505 | 0.410 | 0.078 |
| 512 x 512 | 32 | 23.290 | 0.720 | 0.627 | 0.246 |
| 1024 x 1024 | 1 | 3.566 | 0.714 | 1.300 | 0.878 |
| 1024 x 1024 | 8 | 22.269 | 0.778 | 1.633 | 0.820 |
| 1024 x 1024 | 32 | 97.750 | 1.000 | 1.586 | 0.896 |

## Source and relevance to COLR

`hayro/src/mask.rs` creates a full render-context-sized `RenderContext`,
`Pixmap`, and `Mask` for every uncached mask. `vello_common::Mask::new_luminance`
scans all pixels and retains one byte per pixel. The transform is part of
the mask cache key, so translated placements create different entries.

An untimed instrumented run confirms 32 rasterizations of 1,024 by 1,024
pixels: 33,554,432 mask pixels for 2,048 pixels of actual group coverage.
The cached mask samples alone occupy 32 MiB, calculated from these dimensions;
this is not a measured peak-memory figure. The same file without masks has
none of those allocations.

This dominates a real page of 100 distinct COLR emoji: 2,560 mask
rasterizations at 640 by 680, over 1.11 billion mask pixels. A diagnostic
copy that replaces every `/SMask` with `/None` drops upstream hayro from
3.131 s to 0.093 s (pyropdf: 2.962 s to 0.085 s). That changes the picture;
it is attribution evidence, not a proposed writer workaround. The small
font-free fixture reproduces the cause by itself.

Could bounded masks with a device-space origin be supported here and in
vello_cpu? Cropping needs correct outside-mask values, backdrop colour,
transfer-function behaviour, transformed group bounds, nested masks, and
cache keys. Treating all pixels outside a cropped luminosity mask as zero
would not be correct in general. This is a report and measured design question,
not a production patch. It concerns ExtGState transparency-group soft masks,
as distinct from the image/stencil-mask decoding reports #1382 and #1315.
