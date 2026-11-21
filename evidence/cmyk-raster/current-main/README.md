# Native CMYK on hayro main, 2026-11-21

The published proposal conflicts with six files on hayro main. This rehearsal
combines main `e7ada1354dff4041cdeed3d8ba56fe01b6186132` with proposal
`600842691d971fedf3628bcd4a397c2b725c047e`. It tests the native CMYK path
and the default RGBA path separately. The proposal is still a draft. A hayro
release containing it is required for the pyropdf page API in #425.
[Upstream pull request](https://github.com/LaurenzV/hayro/pull/1744)

## What the resolution changes

The resolution carries the current image decoder, gradient and mesh rendering
paths into the proposal. It keeps source components through mesh interpolation
and converts to output inks at the sample. The new fixture compares the centre
of a triangle of RGB primaries to a flat grey swatch. The centre has K=169,
versus 170 in the swatch, with at most two units of C, M or Y. A five-component
DeviceN mesh with a nonlinear tint transform gives K=29, versus 28 in the
matching flat tint. Interpolating converted ink values would change that tint.

Function-based shadings can end inside a sampling cell. In the CMYK path,
samples spanning the domain edge now use pixel centres within its bounds.
On the 300-pixel gradient, the last black sample reaches K=255. The existing
gradient test and the new mesh fixture both run in the upstream harness.

## Measurements

Ghostscript 10.08.0 rendered the 660 × 180 patch page at 72 dpi with its
`default_cmyk.icc` and relative colorimetric intent. Each comparison uses the
centre pixel and four eight-bit ink channels. All 13 process-colour and grey
patches agree exactly, including images, 50% opacity and Multiply. Red also
agrees. The two remaining RGB colours give the following maximum absolute
channel differences. [All 32 measurements](patches.tsv)

| Native output profile | Blue | Orange |
| --- | ---: | ---: |
| Built-in conversion | 59 | 31 |
| Ghostscript output profile | 13 | 4 |

The default renderer produced identical dimensions and decoded RGBA bytes
before and after the patch on all 13 pages, including text, an image mask,
gradients and meshes. The upstream harness passed 44 cases comprising 35
raster, seven native CMYK and two SVG cases. Overprint and transfer functions
remain outside this rehearsal. [Default-renderer dimensions and hashes](default-rgba.tsv)

## Replay

Use Git, Cargo, Ghostscript and the project's Python environment with Pillow.
The scratch directory retains the upstream checkout, both reports and the
raster probes. The example uses Ghostscript's CMYK output profile. Its SHA-256
in this run was `8472fa1493a024b800b67dee9424835ec0c41ab79490200ae8ec4a689fd1b9a9`.

```sh
uv run python evidence/cmyk-raster/current-main/verify.py /tmp/cmyk-replay /path/to/default_cmyk.icc
```

The script checks out the pinned main, builds the baseline, then merges the
pinned proposal with Git's `-Xours` option. The expected intermediate tree is
`dcc1db05f5cb7b44df5f16306c304d536a3a2c48`. Applying the resolution to
that tree must produce `b21e9dd7f07930beb042ab050681d42932c190d7`. The
script then runs the harness and writes `patches.tsv` and `default-rgba.tsv`.
[Conflict resolution and upstream regression fixture](reconcile.patch)
