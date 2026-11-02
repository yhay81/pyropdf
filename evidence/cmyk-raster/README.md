# CMYK raster output from hayro (issue #433)

pyropdf's `Page.pixmap` renders gray and RGB; PyMuPDF also renders CMYK
(#425). hayro 0.8.0 converts every colour to sRGB while it interprets, and
vello_cpu rasterizes premultiplied RGBA8 only, so a CMYK raster needs an
upstream change (DESIGN.md sections 1, 3, and 6: pyropdf renders through
hayro and does not simulate a different output). This directory records the
upstream status, the design proposed to hayro's maintainer, and the
measurements behind it. [upstream-draft.md](upstream-draft.md) is the draft
issue, published as
[LaurenzV/hayro#1725](https://github.com/LaurenzV/hayro/issues/1725).

## Upstream status (2026-11-02)

- **hayro** (main at `04a801de`, 220 commits after `hayro-interpret-v0.8.0`):
  no issue or pull request asks for CMYK output. Searches for `cmyk`,
  `ICC`, `color management`, `output intent`, and `overprint` find only
  input-side work: DeviceCMYK and CalCMYK convert to RGB through an embedded
  `CGATS001Compat-v2-micro.icc`
  (`hayro-interpret/src/color/device_cmyk.rs`), which has an `A2B0` table and
  no `B2A0`, so it cannot convert to CMYK; ICC transforms are built lazily
  ([#1169](https://github.com/LaurenzV/hayro/pull/1169),
  [#1171](https://github.com/LaurenzV/hayro/pull/1171)); hayro-jpeg2000
  decodes CMYK images ([#612](https://github.com/LaurenzV/hayro/pull/612)).
  [#1603](https://github.com/LaurenzV/hayro/pull/1603), from pyropdf's
  owner and merged after 0.8.0, exposes a colour's colour space and
  components to a device, which is the first step of the design below.
  Images (`RasterImage::with_rgba`, `ImageData::{Rgb, Luma}`) and
  shadings still reach a device as RGB.
- **vello_cpu / linebender/vello**: no issue or pull request mentions CMYK,
  colour management, or output colour spaces. The design below needs no
  change there.
- **pylopdf**: tracker #248 mentions pylopdf's engine work; pylopdf renders
  gray and RGB only (its migration hints say "CMYK is not rendered" and
  suggest converting with Pillow), so there is no engine work to port.

## Designs

**A. Convert the RGBA raster.** hayro renders as today and converts the
pixels to CMYK through an output profile with moxcms, which hayro-interpret
already depends on. It is about 30 lines (`src/main.rs` is the probe) and no
vello_cpu change, but it is not a CMYK renderer: every DeviceCMYK value goes
CMYK → sRGB → CMYK, so `0 0 0 1 k` text becomes four-colour black, pure
process colours pick up other inks, DeviceGray is no longer black only, and
transparency blends in RGB. It also costs more than the render itself on
text pages (below). pyropdf could do the same on its own, which is why it is
the simulation that #425 rules out.

**B. Two-pass lanes (proposed).** hayro-interpret converts colours to the
output CMYK space instead of sRGB: DeviceCMYK unchanged, DeviceGray to
`0 0 0 1-g`, and every other space through an output profile. hayro then
renders the page through vello_cpu twice, into two RGBA contexts: the first
paints the complements `1-c 1-m 1-y` in the red, green, and blue lanes, the
second `1-k`, with the same paths, clips, coverage, and alpha in both. The
result is CMYK = 255 minus the first pass's RGB and the second pass's red.
This is exact, not an approximation, because src-over compositing, coverage,
and opacity act on each channel independently, and the PDF specification
(ISO 32000-2, "Blend mode") defines the separable blend modes for subtractive
spaces on complemented values, which is what the lanes hold. A white page is
all-ones in both passes. Soft masks are alpha only and do not change.

Limits of B: the non-separable blend modes (Hue, Saturation, Color,
Luminosity) need all four components at once and must render their group in
one pass or refuse; spot colours go through their alternate space, as they
do today; overprint stays with #371. A device can interpret once and send
each item to both contexts, so the extra cost is a second rasterization,
not a second interpretation.

**C. N-channel targets in vello_cpu.** vello_cpu would composite CMYK plus
alpha natively. This changes vello_cpu's pixel pipeline (fixed RGBA8
`u8x16` lanes) and involves a second project, for the same result as B.

## Results

[results/macos-arm64.md](results/macos-arm64.md), Apple M2 Pro, macOS,
Ghostscript 10.08.0 as the independent CMYK renderer, both renderers using
Ghostscript's `default_cmyk.icc` as output profile with the relative
colorimetric intent. Ghostscript keeps DeviceCMYK values and blends in CMYK.

- **Colour.** Design A differs from Ghostscript by ΔE76 1.5 to 5.8 on plain
  DeviceCMYK fills and images, 9.3 on 50 % DeviceGray, and 8.7 to 19.2 on
  the composited patches, and never keeps a single-ink colour single-ink.
  Design B, built from hayro 0.8.0's own RGB rendering of the two lane
  files, equals Ghostscript's CMYK raster exactly (ΔE 0.0) on all twelve
  DeviceCMYK patches, including 50 % opacity and `/Multiply`. RGB and gray
  patches have no lane files, because the lane files can only express what
  is already CMYK; their conversion is the interpreter's half of B.
- **Performance.** Converting a 150 dpi page (2.1 Mpx) with moxcms takes
  12 to 15 ms over four runs: 1.1 to 2.3 times hayro's render of the two
  text pages and a third of the scanned page's, on top of the render.
  Building the transform takes 1.5 to 2 ms once per profile. B costs at
  most a second rasterization of the same display list: for the text pages
  that is about the render time (6 to 12 ms), less than A's conversion. B
  is not implemented, so this is a bound from the measured render times,
  not a measurement. Single times vary by up to 60 % between runs on this
  machine; the committed table is the last run.

## Running

Ghostscript (`gs`) and a Rust toolchain are required; the script finds
`default_cmyk.icc` next to `gs`, or takes a profile path.

```sh
python3 evidence/cmyk-raster/compare.py > evidence/cmyk-raster/results/macos-arm64.md
```

`make_pdf.py` (run by `compare.py`) writes `patches.pdf` and the two lane
files; `src/main.rs` renders with hayro 0.8.0 and converts with moxcms 0.8.1.

## Next

The draft asks the maintainer whether hayro wants CMYK output and whether
design B is acceptable, and proposes three bounded pull requests in order:
colour conversion to an output CMYK space in hayro-interpret, images and
shadings in that space, and `hayro::render_cmyk`. No patch is written before
that answer, because the split between hayro-interpret and hayro is the
maintainer's choice.
