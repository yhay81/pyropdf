# Clip precision on large pages

The measurement behind issue #605: how far content moves when it reaches a
small raster from far away, which sets `MAX_PAGE_SIDE` in
`crates/pyropdf-engine/src/render/mod.rs`, the longest side of a page under
the matrix whose clip renders.

## Approach

A clip renders through a transform that puts the clip at the raster's
origin, so the rest of the page lies up to a page away, in pixels. hayro
and vello_cpu transform paths in `f64`, and vello_cpu then flattens, tiles,
and covers them in `f32`; it steps image and gradient positions in `f64`
and samples them in `f32`. So `f32` rounds what comes from the far side of
the page, at magnitudes up to the page's side, and the error grows with it.

Each case draws the same pixels twice: from far away, and from points just
outside the raster on the same lines, placed in `f64`. The far points lie
off the binary fractions that `f32` holds exactly, as real content does.
The tables give the largest difference of one sample, in 1/255: for an
edge, how far it moved across a pixel, in 1/255 of a pixel.

## Workload

- **Far content**: a 64 by 64 point page at 72 dpi.
  - *Fill and stroke*: from each of 64 directions, a wedge 12.5 pixels
    wide and a line 2 pixels wide, whose far ends lie `distance` pixels
    away.
  - *Image*: a 2 by 2 image without `/Interpolate`, whose texels are
    `distance` pixels wide, split between black and white 1/16 of a pixel
    past a pixel center, across and down. It differs by 255 once the split
    moves that far.
  - *Shading*: an axial shading from black to white over one pixel, whose
    coordinates lie `distance` pixels away, moved back by `cm`.
- **Clips**: the 64 by 64 pixel clip at the bottom-right corner of an A1
  page, 2,384 by 1,684 points, at the scale that makes the page's long side
  `side` pixels, with a wedge and a line from next to each of the page's
  other corners and the middles of its far edges.

## Running

```sh
uv run python evidence/clip-precision-605/measure.py
```

*Far content* runs on any build; *Clips* needs one whose limits apply to
the clipped raster, and stops at the first side it refuses.

## Results

From `results/windows-x86_64.md`, the largest difference of a sample, in
1/255 of a pixel:

| `distance` or `side` | Far: fill and stroke | Far: image | Far: shading | Clips |
| ---: | ---: | ---: | ---: | ---: |
| 2^15 | 1 | 0 | 0 | 1 |
| 2^16 | 2 | 0 | 0 | 9 |
| 2^17 | 3 | 0 | 0 | 3 |
| 2^18 | 6 | 0 | 0 | 10 |
| 2^19 | 12 | 0 | 0 | 14 |
| 2^20 | 20 | 0 | 16 | refused |
| 2^21 | 46 | 255 | 16 | |
| 2^22 | 105 | 255 | 16 | |
| 2^24 | 255 | 255 | 112 | |

- Up to 2^15 pixels, an edge moves by at most 1/255 of a pixel, the
  rounding of one sample.
- From there the error about doubles with the distance, as `f32` loses a
  bit: 0.05 of a pixel at 2^19, 0.08 at 2^20, 0.18 at 2^21, 0.4 at 2^22,
  and a whole pixel at 2^24.
- An image keeps its texel boundary within 1/16 of a pixel up to 2^20, and
  a shading moves by 1/16 of a pixel from 2^20.
- The clips agree, at up to 14/255 of a pixel at 2^19. The edges that
  arrive nearly along an axis, from the far top or left of the page, move
  the most, by 9/255 already at 2^16.

## Decision

`MAX_PAGE_SIDE` is 2^19, 524,288 pixels, the largest power of two at which
every case stays below 1/16 of a pixel. It allows an A1 page at 219 times,
15,800 dpi, where a region 11 points wide renders 2,400 pixels wide. A clip
of a larger page raises `ValueError`. `tests/test_rendering_options.py`
renders a clip at the bound and checks the far line against the near one.
