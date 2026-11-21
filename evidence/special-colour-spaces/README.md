# Calibrated and special colour spaces

The swatch tests use the public typed resource map, the current object store
and the original PDF colour operators. They check pikepdf's decoded resource
objects, function/profile/palette bytes and exact source operands after a full
and an incremental save. A second page uses its original inherited colour map
and unchanged content. Each edited typed member also has an invalid-type
control. Run the measurements with Poppler and Ghostscript on PATH.

```sh
uv run pytest -q tests/test_special_colour_spaces.py
```

The 100 × 100 point fixtures render at 72 dpi. The table gives sRGB values
inside solid swatches at (20, 80), away from shape boundaries. The ICC control
changes the three tone reproduction curves in the repository-generated RGB
display profile. On macOS ARM64, Poppler 26.11.0 and the hayro revision
`394e4a0` returned the following values before and after writing the edit.

| Resource and edit | Poppler before → after | hayro before → after |
| --- | --- | --- |
| CalGray, gamma 1 → 2.2 at 0.5 | (188,188,188) → (128,128,128) | (194,194,194) → (138,138,138) |
| CalRGB, three gammas 1 → 2.2 with sRGB primaries | (137,188,225) → (61,128,193) | (137,188,224) → (62,129,192) |
| ICCBased, three gamma 2.2 curves → linear | (62,129,192) → (137,188,225) | (62,129,192) → (137,188,224) |
| Indexed RGB, palette entry 1 | (204,51,102) → (40,200,20) | (204,51,102) → (40,200,20) |
| Separation, full-tint equivalent red → blue at tint 0.75 | (255,64,64) → (64,64,255) | (255,64,64) → (64,64,255) |

Lab tests retain the stated white point, change the a*/b* ranges and paint
on the new boundaries. For the separate (55,55,-40) swatch, changing its
WhitePoint from (0.9505,1,1.089) to (0.75,1,1.4) changed the Poppler
sample from (190,92,202) to (168,108,201). The tested hayro revision
returned (196,88,202) both times. The source definitions survive both save
modes. The interpreter discrepancy is tracked in
[#1017](https://github.com/yhay81/pyropdf/issues/1017).

The NChannel example defines a Cyan process component and a Varnish spot,
named and default solidities and dot-gain functions, printing order, a
per-colourant Separation, and a two-input calculator tint transform.
For tints (0.25,0.75), the alternate transform gives RGB (191,64,255).
Both ordinary RGB previews match. After the hint edits, pikepdf independently
reads the reversed order, Varnish solidity 0.25 and exponent 2, with the
default identity function still intact. Ghostscript 10.08.0 tiffsep outputs
Cyan 192 and Varnish 64 in their individual 8-bit plates, where white means
no ink, and 255 on the three unused process plates. An explicitly selected
proofing model that applies the optional mixing hints is tracked by
[#1016](https://github.com/yhay81/pyropdf/issues/1016).

The final operator cases preserve and edit the operands of both stroking and
nonstroking g, rg, k, cs, sc and scn, including a named device space, a spot
ink and an uncoloured tiling pattern. The independent raster samples agree
after swapping the two colours. See [the corrected colour-space and operator
requirements](https://pdf-issues.pdfa.org/32000-2-2020/clause08.html) and
[Ghostscript's separation-device description](https://ghostscript.readthedocs.io/en/gs10.08.0/Devices.html).
