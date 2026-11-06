# Glyph reuse at the original subpixel position

An exact-position bitmap cache offers few hits on the two documents whose
glyph fills dominate the native profiles. With a 4 MiB payload and 4,096
entries, its simulated document-wide hit rate is 3.15–3.87% on bill-hr815
and 0.18% on usrguide. The [prepared outline experiment](prepared-outline/README.md)
tests reuse of work on outline geometry before applying the original translation.

| Document | DPI | Eligible fills | Within page | Across pages | All exact repeats | Linear repeats |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815 | 72 | 254,489 | 6,664 | 8,023 | 20,920 | 254,125 |
| bill-hr815 | 144 | 254,489 | 8,101 | 9,851 | 26,172 | 254,125 |
| usrguide | 72 | 46,581 | 0 | 84 | 160 | 45,915 |
| usrguide | 144 | 46,581 | 0 | 84 | 160 | 45,915 |

The [native probe](../glyph_phase.rs) counts callbacks during hayro
interpretation with the pinned font and rendering dependencies. It visits all
110 and 27 pages, respectively. For positive 72/144 dpi isotropic scaling
it uses pyropdf's full-page viewport and page transform, enables annotations,
uses hayro's default font resolver, and models unhinted solid horizontal
outline fills. Type 3 procedures, patterns, mask devices and strokes are
outside this measurement. It excludes empty ink and masks exceeding hayro's
128-pixel width or height limit. The denominator is eligible fills. Counts
per page, including total observed glyphs, are in [glyph-phase.csv](glyph-phase.csv).

The key uses hayro's glyph identifier, the bit patterns of all four linear
transform coefficients, and the exact fractional x and y translation after
multiplication by the device transform. Each fractional component must
reconstruct the original coordinate when added to its integer origin.
The probe charges four bytes per enclosing pixel. The two finite FIFO
caches each hold at most 4 MiB of estimated mask bytes and 4,096 entries.
One starts empty per page, the other per document. Metadata has a separate
entry limit. The two repeat columns count earlier occurrences in the same
document with no eviction and a hard refusal above 300,000 distinct keys.
The final column omits translation from the key.

Even retaining every exact key gives at most 8.22% and 10.28% repeats on
bill-hr815 and 0.34% at either DPI on usrguide. By contrast, 99.86% and
98.57% of eligible draws repeat the same glyph and linear transform.
Those latter counts identify opportunities to reuse geometric work. They
do not establish pixel equivalence after translation or predict elapsed time.
The current bitmap fast path also uses a different rasterizer for the mask.
Changing its key alone cannot guarantee equivalence to direct vello fills.

## Reproduce

Use the repository's locked workspace at the PR revision. Copy the probe
into a vacant example path so Cargo resolves hayro and its features exactly
as it does for the engine. Run from the repository root:

```powershell
$example = 'crates/pyropdf-engine/examples/glyph_phase.rs'
if (Test-Path -LiteralPath $example) { throw 'Example path is occupied' }
New-Item -ItemType Directory -Force (Split-Path $example) | Out-Null
Copy-Item evidence/pymupdf-comparison/glyph_phase.rs $example
cargo run --locked -p pyropdf-engine --example glyph_phase > target/glyph-phase.csv
```

The CSV contains deterministic counts, so release optimization is optional.
Remove the temporary example after checking its contents against the probe.
For a geometry candidate, first compare original and candidate pixels at
real placements and clip boundaries. A candidate that passes then needs
bounded cache accounting and the full ordinary-wheel comparison on all
corpus documents at both resolutions.
