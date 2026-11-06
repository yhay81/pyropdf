# COLR rendering reductions (#671)

The grinning-face comparison combines two rendering effects: duplicate clip
coverage at shading edges and a half-pixel difference between native and
sampled gradients. The large page's cost is dominated by full-page soft masks.
These reproduce independently of fonts and of pyropdf's writer.

The fixtures in [rendering-fixtures/](rendering-fixtures/) contain only paths,
shadings, functions, and transparency groups. [rendering.py](rendering.py)
writes their cross-reference offsets and stream lengths directly. No Segoe UI
Emoji font data or glyph outlines are committed by this investigation.

## Reproduction

Measured on Windows 11, AMD Ryzen 5 3600XT, Rust 1.99.1 MSVC, Python 3.14.8,
2026-11-06 JST. [results/rendering.json](results/rendering.json) records every
sample, pixel probe, input hash, and oracle version. The standalone
[hayro-probe](hayro-probe/) pins upstream
[`3de8bb0401f0410b71f37a52524b7607433ace13`](https://github.com/LaurenzV/hayro/tree/3de8bb0401f0410b71f37a52524b7607433ace13)
and its own lockfile; it does not use pyropdf's fork or object-store bridge.
pyropdf was built from `b3c7e875b27efd1a64fa6474c4be3f12c5e297ef`, which pins
hayro to `394e4a0d37baf8af513f6ebda15f4bada85d6f23`. Both use vello_cpu 0.3.0.

Run from the repository root, in Developer PowerShell on Windows:

```text
cargo build --release --locked --manifest-path evidence/colr-glyphs/hayro-probe/Cargo.toml --target-dir target/colr-rendering/upstream-target
uv run --no-sync --with pymupdf==1.28.2 --with pypdfium2==5.14.0 python evidence/colr-glyphs/rendering.py --upstream target/colr-rendering/upstream-target/release/colr-rendering-probe.exe --output target/colr-rendering/reproduced
```

The script runs one untimed warmup and takes five samples with fresh documents
and render caches. Input bytes are already in memory. Timings include opening
and rendering; the Python wrappers also close their document/page handles
inside the call. Output-image destruction, PNG encoding, and process startup
are excluded. The comparison of scaling within a reader matters more than
sub-millisecond differences between these wrappers. No compilation or other
benchmark ran concurrently with the recorded samples.

For the optional diagnostics, use a separate checkout of the pinned upstream
commit, not a shared Cargo source cache. Copy `hayro-probe/` to a scratch
directory and replace its `hayro` dependency with a path to that checkout's
`hayro/`. Keep the copied lockfile; Cargo will change only the source of the
local hayro packages. Build in a separate target directory.

1. Apply [expand-shading-bounds.patch](expand-shading-bounds.patch) and
   [trace-soft-masks.patch](trace-soft-masks.patch). Build and retain that
   executable for `--diagnostic`.
2. Also apply [native-gradient-origin.patch](native-gradient-origin.patch),
   rebuild, and use that executable for `--aligned`.

All three patches pass `git apply --check` at the pinned commit. The first two
colour patches are diagnostic candidates, not a claim that upstream's whole
reference-image suite passes. In particular, the existing upstream clip
report documents reference-image changes and a separate rasterizer regression
reachable by expanding a rectangle. Tracing is enabled only for separate
untimed calls through `COLR_TRACE_MASKS=1`.

To also repeat the original font comparison, add `--font FACE`, both diagnostic
executable options, and these uv dependencies:
`--with "blackrenderer[skia]==0.8.2" --with skia-python==144.1 --with fonttools==4.68.0`.
The font's SHA-256 is recorded. Generated font PDFs and renders stay in the
output directory. The script checks equivalent paints in MuPDF/PDFium, the
diagnostic pixels, and the masked/unmasked controls before writing its result.

## 1. A shaded edge receives coverage twice

[clipped-shading.pdf](rendering-fixtures/clipped-shading.pdf) is 1,269 bytes.
Three identical trapezoids have fractional bounds on a magenta background:
white `sh`, a white fill through a luminosity mask painted with white `sh`,
and a white fill. There are no radial gradients, variable alpha stops, fonts,
or negative radii. The trapezoid avoids rectangular-clip rounding shortcuts
in the reference readers.

At the three corresponding bottom-edge pixels `(10,35)`, `(50,35)`, `(90,35)`
at 72 dpi, RGB values are:

| Reader | `sh` | White `sh` mask | Plain fill |
| --- | --- | --- | --- |
| upstream hayro / pyropdf | 221,143,210 | 221,143,210 | 236,191,229 |
| MuPDF 1.28.2 | 234,187,227 | 234,187,227 | 234,187,227 |
| PDFium 156.0.8076.0 | 235,191,229 | 235,191,229 | 235,191,229 |
| hayro + bounds diagnostic | 236,191,229 | 236,191,229 | 236,191,229 |

The 0.75 edge coverage becomes 0.75 squared for the first two hayro shapes:
191 becomes 143 in the green channel. Expanding only the shading's fill box
to device-pixel boundaries leaves the clip to apply coverage once.

This is already reported in
[hayro#1582](https://github.com/LaurenzV/hayro/issues/1582). The prepared
[additional evidence comment](clipped-shading-upstream-comment.md) belongs
there, not in a duplicate issue.

## 2. Native and sampled gradients have different origins

[radial-gradient.pdf](rendering-fixtures/radial-gradient.pdf) has four panels.
Each uses concentric circles at `(16,16)` with radii 8 and 12 and a linear
gray function from zero to one. Panels 1 and 3 extend both ends; panels 2 and
4 do not. The last two paint through luminosity masks. All probe points are
strictly inside the annulus, where extension cannot affect the colour.

hayro uses its native gradient for the extended shading and its sampled
texture for the unextended shading. The first path has an extra `(-0.5,-0.5)`
device-space translation in `hayro/src/paint.rs`; `sample_texture` samples
pixel centres. At local `(25,15)` and `(6,15)`:

| Reader | Extended | Unextended |
| --- | --- | --- |
| upstream hayro / pyropdf | gray 127 / 63 | gray 96 / 96 |
| MuPDF, direct shading | RGB 120,121,120 / 87,88,87 | same |
| PDFium | gray 67 / 129 | same |
| hayro + origin diagnostic | gray 96 / 96 | gray 96 / 96 |

The same mismatch occurs in the mask panels. MuPDF's mask samples differ from
its direct shading by at most one channel level, but extending does not
change them. Readers use different rasterization/sampling conventions; this
test compares the two equivalent interior paints **within each reader**.
Removing hayro's extra translation makes its two paths agree. The value 96
also matches sampling the function at pixel centres:
`round(255 * (sqrt(9.5^2 + 0.5^2) - 8) / 4)`.

The [upstream report draft](native-gradient-upstream.md) includes the small
fixture and diagnostic patch. No corresponding issue was found in the
upstream searches recorded during this investigation.

## The original grinning face

The 94.0% within-32 score is reproducible. It is not evidence of a uniform
mouth-colour error: in the interior rectangle `x=80..130, y=95..105` at 144 dpi,
hayro and MuPDF differ from blackrenderer by at most two channel levels,
and PDFium by at most one. The white tooth's bottom edge and the lower face's
gradients contribute to the larger differences.

| Reader | Mean maximum-channel difference | Inked pixels within 32 |
| --- | ---: | ---: |
| upstream hayro / pyropdf | 6.20 | 93.98% |
| bounds diagnostic only | 5.69 | 95.35% |
| bounds + origin diagnostics | 4.70 | 96.88% |
| MuPDF | 5.22 | 98.45% |
| PDFium | 6.72 | 98.60% |

The diagnostics explain reproducible hayro discrepancies; they do not make
the whole glyph pixel-identical to blackrenderer. The remaining global
score includes interpolation and edge differences in all readers, and this
investigation does not assert that all of those are hayro defects. Neither
reduction involves the writer's negative-radius clipping.

## 3. Small soft masks cost the full page area

[mask-1024-1.pdf](rendering-fixtures/mask-1024-1.pdf) is 778 bytes and contains
one 8 by 8 point luminosity mask on a 1,024 by 1,024 point page.
[mask-1024-32.pdf](rendering-fixtures/mask-1024-32.pdf) is 2,135 bytes and draws
that same mask at 32 different translations. Both have only six indirect
objects and no shadings, fonts, images, compression, or transfer functions.
The controls replace masked black fills with gray fills; their sampled
output differs by at most one level. Scaling tests retain the same 8 by 8
paint and vary only page dimensions and placement count.

`hayro/src/mask.rs` passes the whole render context's width and height to each
mask context and pixmap. `vello_common::Mask::new_luminance` scans every pixel
and retains one byte per pixel. The transform is part of the mask cache key,
so translated placements get distinct entries. Instrumentation confirms 32
mask rasterizations of 1,024 by 1,024 pixels: 33,554,432 mask pixels even though
the 32 actual groups cover only 2,048 pixels. The retained mask samples alone
are 32 MiB; this is a calculation from dimensions, not a peak-memory measurement.

The [upstream performance report draft](soft-mask-upstream.md) contains the
measured scaling table. In the original 100-emoji page, instrumentation records
2,560 mask rasterizations at 640 by 680, over 1.11 billion mask pixels. The
PDF contains 2,452 mask dictionaries and 12,872 total objects; dictionary
count and rasterization count are not interchangeable.

Replacing all `/SMask` dictionaries with `/None` is an attribution experiment
only: it changes the image and must not be used as a writer optimization.
It reduces the original upstream hayro render from 3.131 seconds to 0.093
seconds on this run (pyropdf: 2.962 to 0.085 seconds). The source and the
font-free scaling test identify full-page mask processing as the dominant
cost. A production fix needs bounded mask
storage with a device origin and correct outside-mask values, including
luminosity backdrops and transfer functions; simply cropping and treating
everything outside as zero is insufficient.

## Handoff

The prepared reports and patches are local evidence. Publishing a new issue
or comment in LaurenzV/hayro still needs the owner's authorization. Adopting
upstream fixes and adding public regression tests belong to the follow-up
issues; this change does not patch pyropdf's renderer or dependencies.

- [#702](https://github.com/yhay81/pyropdf/issues/702) tracks clip coverage and
  the existing upstream #1582.
- [#703](https://github.com/yhay81/pyropdf/issues/703) holds the native-gradient
  report and diagnostic patch for upstream publication/review.
- [#704](https://github.com/yhay81/pyropdf/issues/704) holds the soft-mask
  performance report and adoption work.
