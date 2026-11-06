# Prepared outline experiment

The [glyph probe](../glyph-phase.md) found that more than 98% of eligible
fills repeat a glyph and its linear transform. We tested whether preparing
that part of the path once per page would improve the ordinary renderer.
The candidate matches every corpus pixel comparison but delivers little
end-to-end benefit. We rejected it for adoption.

The [local hayro patch](../../prepared-outline.patch) prepares control points
under the linear transform and leaves the original translation on the vello
context. It replaces sufficiently flat curves with the chord that vello
0.3 would select. It uses stricter squared-distance thresholds of 0.24
for quadratics and 0.10 for cubics. It only changes direct unhinted
solid outline fills with bitmap caching disabled. A per-page cache limits
path-element payloads to 4 MiB and entries to 4,096. The entry cap bounds
metadata separately. This trial does not integrate those bytes into the
document's public resource budget.

All 1,872 combinations of corpus page, output layout and resolution had
the same dimensions, channel count and pixel hash as the baseline.
[Artifact identities and comparison outcome](pixels.json).
This is a corpus check. Clipped and adversarial geometry, platform-specific
floating-point behavior, and budget integration would require separate
coverage before shipping a renderer change.

| First artifact | DPI | Best baseline ms | Best candidate ms | Ratio | Median baseline ms | Median candidate ms | Ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | 72 | 1053.3 | 1049.1 | 0.9961 | 1149.9 | 1107.9 | 0.9635 |
| Candidate | 72 | 1071.2 | 1062.6 | 0.9919 | 1195.8 | 1182.5 | 0.9889 |
| Baseline | 144 | 2036.0 | 1999.8 | 0.9823 | 2144.7 | 2123.8 | 0.9903 |
| Candidate | 144 | 2025.5 | 2022.4 | 0.9985 | 2113.3 | 2131.6 | 1.0086 |

These sums retain the five alternating raw-pixel measurements for every
document and DPI. In the reversed order the 144 dpi best totals differ by
0.15% and the median is slower. The high geometric reuse count therefore
does not predict a large render-time gain.
[Baseline-first samples](baseline-first.json) and
[candidate-first samples](candidate-first.json).

The issue's full comparison with PyMuPDF 2.0 / MuPDF 1.28.5 gave the
candidate corpus ratios of 1.84 at 72 dpi and 1.53 at 144 dpi. Both exceed
1.5. The ordinary baseline in a separate run measured 1.94 and 1.70.
The MuPDF 144 dpi total itself varied from 1066.1 to 1237.6 ms between
those invocations. The separate runs and the paired wheel experiment distinguish
workstation variability from the patch's effect. Use the paired runs to
compare the two pyropdf builds.
[Candidate versus MuPDF](candidate-vs-mupdf.md) and
[baseline versus MuPDF](baseline-vs-mupdf.md).

## Reproduction

1. Use clean sources for pyropdf `13ea858a096493bb49e681c450a2a44302ecfaa7`
   and hayro `394e4a0d37baf8af513f6ebda15f4bada85d6f23`. Build the
   ordinary pyropdf release wheel with `maturin build --release --locked`.
2. Apply `prepared-outline.patch` in the hayro copy. Point only the
   `hayro` entry of pyropdf's `[patch.crates-io]` at the local hayro
   package. Resolve its `hayro-interpret` dependency through the original
   workspace patch (remove its local path in the copied hayro workspace
   dependency). Update the disposable product lockfile with
   `cargo update --offline -p hayro`. Verify the only lockfile change is
   removal of the git source line from the `hayro` package. Build into
   a separate wheel directory with the same release settings and compiler.
3. Install the two wheels in separate Python 3.13 environments. Use
   `evidence/pixmap-initialization/compare.py` from pyropdf commit
   `019449775f80ca8e08bf78eeb46f79bc46851412`. The driver checks the
   loaded extension against each wheel. Run its full hash comparison or
   compare the candidate worker's hash map against the 1,872 verified
   baseline rows at that commit. Run timings in both alternating orders.
4. Run this branch's `evidence/pymupdf-comparison/run.py --pyropdf-wheel`
   with each artifact. Preserve the output of each invocation separately.
   The raw results record the wheel SHA-256. The paired measurements used
   Python 3.13.17 on Windows amd64 with the same corpus and public defaults.
