# Issue for LaurenzV/hayro

Draft for yhay81/pyropdf#433, published with the owner's approval on
2026-11-02 as https://github.com/LaurenzV/hayro/issues/1725. The published
text links the probe at commit `0ccc96b9` and ends with the model and harness.

---

**Title:** CMYK raster output: would you accept it, and in which shape?

## Problem

hayro renders to RGBA only. Applications that prepare print (proofing,
preflight, separations, a PyMuPDF-compatible `get_pixmap(colorspace=CMYK)`)
need a CMYK raster that keeps the document's CMYK values: `0 0 0 1 k` text
stays black ink only, and transparency blends in CMYK, as Ghostscript and
MuPDF render it.

Converting hayro's RGBA output afterwards cannot do that. Every DeviceCMYK
value goes CMYK → sRGB → CMYK, and transparency has already blended in RGB.
Measured against Ghostscript 10.08 rendering natively to CMYK with the same
output profile (`default_cmyk.icc`, relative colorimetric), converting
hayro 0.8.0's raster with moxcms gives:

| Fill | Ghostscript | hayro RGBA → CMYK | ΔE76 |
| --- | --- | --- | --- |
| `0 0 0 1 k` | 0 0 0 255 | 183 176 166 226 | 5.3 |
| `1 0 0 0 k` | 255 0 0 0 | 186 38 0 0 | 5.8 |
| `0.5 g` | 0 0 0 127 | 136 114 114 24 | 9.3 |
| `1 0 0 0 k` at 50 % over `0 1 0 0 k` | 128 127 0 0 | 172 199 0 0 | 19.2 |
| `0 0 0 1 k` `/Multiply` over `0 0 1 0 k` | 0 0 255 255 | 169 158 193 206 | 8.7 |

The conversion also costs 12 to 15 ms for a 2.1 Mpx page, 1.1 to 2.3
times hayro's render of a text page. Probe and full table:
`evidence/cmyk-raster` in yhay81/pyropdf (hayro 0.8.0 alone, no pyropdf
code).

## Proposal: render the CMYK complements in vello_cpu's RGBA lanes

This needs no change to vello_cpu.

1. **Colours in the output space.** hayro-interpret converts colours to a
   CMYK output space when asked, instead of to sRGB: DeviceCMYK unchanged,
   DeviceGray to `0 0 0 1-g`, every other space through an output ICC
   profile with moxcms. #1603 already gives a device the colour space and
   components; this adds the conversion of solid colours, and then of
   images and shadings, to the output space. hayro's embedded CMYK profile
   has no `B2A0` table, so the caller would supply the output profile.
   Without one, the specification's DeviceRGB to DeviceCMYK rule (ISO
   32000-2, "Conversion from DeviceRGB to DeviceCMYK") could be the fallback.
2. **Two passes.** `hayro::render_cmyk` renders the page into two vello_cpu
   contexts with the same paths, clips, and alpha: the first paints
   `1-c 1-m 1-y` in the red, green, and blue lanes, the second `1-k`. The
   raster is 255 minus those lanes. This is exact for src-over, coverage,
   opacity, and the separable blend modes, because each acts per channel
   and ISO 32000-2 ("Blend mode") defines the separable modes on complemented
   values for subtractive spaces. A device can interpret once and feed both
   contexts.

Rendering two lane PDFs (the complements written as DeviceRGB) with hayro
0.8.0 and combining them reproduces Ghostscript's CMYK raster exactly on
all twelve DeviceCMYK test patches, including the 50 % opacity and
`/Multiply` cases above.

Limits: the non-separable blend modes need all four components at once,
so a group that uses them would render in one pass or be refused; spot
colours use their alternate space as today; overprint is not part of this.

## Questions

1. Is CMYK output in scope for hayro?
2. If so, is the two-pass design acceptable, or would you rather have
   N-channel targets in vello_cpu or only the conversion of the RGBA raster?
3. Should the output-space conversion live in hayro-interpret's colour
   types (for example `Color::to_cmyk`, `RasterImage::with_cmyk`) or in a
   setting of `InterpreterSettings`?

If the design suits you, I would send three small pull requests in that
order: solid colours, images and shadings, then `render_cmyk` with tests
against Ghostscript's CMYK output.
