# Draft upstream issue: Native radial gradients are shifted relative to sampled shadings

## Reproduce

Render [radial-gradient.pdf](https://github.com/yhay81/pyropdf/blob/main/evidence/colr-glyphs/rendering-fixtures/radial-gradient.pdf)
at scale 1 with `hayro::render`, a white background, and default settings.
Tested on `3de8bb0401f0410b71f37a52524b7607433ace13`, vello_cpu 0.3.0,
Rust 1.99.1, Windows 11. No font or pyropdf integration is required.

Four 32 by 32 point panels paint the same concentric radial shading:
`/Coords [16 16 8 16 16 12]`, a linear DeviceGray function from 0 to 1.
The first extends both ends, the second does not. The third and fourth use
those same two shadings inside luminosity masks. All queried points are
strictly inside the annulus, where extension cannot affect the function.

## Expected and actual

At local `(25,15)` and `(6,15)`, from the upper left, changing `/Extend` should
leave interior samples unchanged within each renderer.

| Reader | Extended, right / left | Unextended, right / left |
| --- | --- | --- |
| hayro | gray 127 / 63 | gray 96 / 96 |
| MuPDF 1.28.2, direct paint | RGB 120,121,120 / 87,88,87 | identical |
| PDFium 156.0.8076.0 | gray 67 / 129 | identical |

hayro's mask panels have the same 127/63 versus 96/96 mismatch. MuPDF's mask
samples are 120/87 in both panels; PDFium's are 67/129 in both. The invariant
is agreement between the extended and unextended interior, not a requirement
that independent renderers use the same sampling convention.

## Cause and diagnostic patch

The extended shading takes the native gradient path in `hayro/src/paint.rs`.
The unextended shading cannot cover the paint bounds and falls back to
`sample_texture`. The native path adds `Affine::translate((-0.5, -0.5))` in
device space, while the texture path samples at pixel centres.

[Removing that translation](https://github.com/yhay81/pyropdf/blob/main/evidence/colr-glyphs/native-gradient-origin.patch)
makes all eight probe pixels gray 96. This also matches the pixel-centre
calculation `round(255 * (sqrt(9.5^2 + 0.5^2) - 8) / 4)`.
It reduces differences in the original COLR example that led to the reduction.

The [standalone probe, generated fixtures, and results](https://github.com/yhay81/pyropdf/blob/main/evidence/colr-glyphs/rendering.md)
pin the upstream commit and dependencies. The patch is a diagnostic candidate:
I have not run the complete upstream reference-image suite, so changes to
other gradients and reference images still need review. The clip-coverage
problem in #1582 is separate; its bounds patch does not change these samples.
