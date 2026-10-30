# Narrowing RGB rasters through vello_cpu, darwin-arm64

Measured 2026-10-30 for #393 on macOS 27.0.1 (Apple M-series), Python 3.15.

## Change

`pyropdf_engine::render::take_samples` takes RGB samples without alpha with
vello_cpu's SIMD `Pixmap::try_take_rgb8`. Before, `to_layout` narrowed RGBA
to RGB one pixel at a time. Gray and alpha layouts, and a pixmap that
vello_cpu does not know to be opaque, still go through `to_layout`.

## Same bytes

`sample_hashes.py` hashed all 936 combinations of corpus page and layout
(15 documents; RGB, gray, and alpha, on the default and on another
background) with a build of `main` (ed84bcf) and with this change. Every hash
is equal. The Rust test `render::tests::take_samples_matches_to_layout`
checks the same equality for an opaque and a translucent pixmap.

## Speed

`profile.py` from #390 (`evidence/pymupdf-comparison/profile.py` on branch
`claude/365-render-profile`) ran twice in one session, built with line
tables and with `#[inline(never)]` on `take_samples`, so that the step has a
frame of its own. "Before" forced the `to_layout` path; "after" is this
change. Times are the sums over every page of the best of three renders, in
ms, with the samples read; the step is its share of the profile's samples.

| Case | before (ms) | after (ms) | change | step before | step after | / PyMuPDF before | / PyMuPDF after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 72 dpi | 411.6 | 378.8 | -8% | 9.8% | 1.3% | 5.22 | 4.75 |
| bill-hr815 144 dpi | 761.5 | 620.7 | -18% | 21.6% | 3.8% | 5.60 | 4.54 |
| usrguide 72 dpi | 74.9 | 70.4 | -6% | 7.7% | 1.0% | 4.48 | 4.20 |
| usrguide 144 dpi | 133.7 | 112.4 | -16% | 17.8% | 3.1% | 4.99 | 4.25 |
