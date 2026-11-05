# Lazy shading repaint (#645)

`Canvas.draw` repaints axial and radial path paints retained by either
`Page.content` or `Page.paths`. Extraction shares an owned hayro shading by
its source, matrix, and opacity; it does not evaluate its color function.
The first draw samples that function through hayro's color conversion under
the destination canvas's operation budget. Later draws reuse the result.
The source remains the version that was read, even after editing or closing
the original document.

The sampler uses quarter, midpoint, and three-quarter probes, an sRGB
tolerance of 0.01, and at most ten subdivisions: at most 1,025 stops. It
charges every evaluation and stop, including Python conversion. The writer
uses a Type 0 function with 1,025 16-bit samples on the same dyadic grid.
This avoids introducing stitching boundaries, whose duplicate stops change
the renderer's gradient lookup resolution at sharp color transitions.
Features between probes can be missed; this is a bounded approximation,
not preservation of the original function program.

## Image and resource checks

`tests/test_canvas_round_trip.py` compares the original and saved/reopened
copy at 72 dpi, allowing at most 16/255 per channel. Its fixtures cover:

- Axial and radial fills and strokes, all four `/Extend` combinations,
  a `[-2, 3]` domain, and Type 0 and Type 4 functions.
- Type 2/3 functions, RGB, Gray, CMYK, CalGray, CalRGB, Lab, Separation,
  DeviceN, and Indexed conversion through hayro.
- Pattern matrices, every page rotation, shading clip boxes, backgrounds,
  opacity, transfer functions, and direct `sh` operators.
- Distinct functions with identical public metadata, compacted pattern
  tables after filtering, and both content items and path columns.
- No eager sampling under a small extraction element limit; source-version
  retention after editing and closing; one sampling pass across repeated
  draws; typed memory-budget refusal and transaction rollback.

The API reference lists the remaining unsupported paints: function-based
and mesh shadings, tiling cells, and soft masks of extracted items. Existing
image and text exclusions remain documented there as well.

## Extraction measurement

Run `uv run python evidence/shading-pattern-round-trip/measure.py` in each
revision after a release extension build. The script creates valid PDFs
with 1,000, 10,000, and 100,000 rectangles sharing one axial shading. It
reports the median of three cold reads, excluding document opening and
garbage collection. Each read uses a fresh document, and checks the number
of returned paths. It does not render or draw anything.

`before.json` measures main commit
`70263454ae8cd4eac810b36335146acd238c98bc`; `after.json` measures this change.
Both use CPython 3.14.8, Windows 11 x86-64, Rust 1.99.1, and the repository's
release profile. They were run consecutively after building the baseline
from a separate source archive, with no build or test running concurrently.

Median elapsed seconds:

| Paths | `paths` before | `paths` after | `content` before | `content` after |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.001216 | 0.001153 | 0.006599 | 0.006923 |
| 10,000 | 0.010624 | 0.009719 | 0.076657 | 0.076066 |
| 100,000 | 0.111218 | 0.098582 | 1.236240 | 1.219699 |

The 100,000-path content read remains about 1.2 seconds; retaining the
shading adds no per-item color sampling.

The timings are a same-machine comparison, not a general speedup claim.
The extraction-limit and repeated-draw tests separately check that function
sampling is deferred and shared rather than relying on elapsed time alone.
