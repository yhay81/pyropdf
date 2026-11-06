# Disjoint glyph fills

We rejected the [grouped-path candidate](../../disjoint-glyphs.patch).
It changes 164 of the 1,872 corpus pixel comparisons and regresses
best-of-five totals at both resolutions in both measurement orders.

The [native probe](../glyph-phase.md) found enough disjoint pixel
envelopes to group 43–84% of eligible fill calls away in the two hot
documents. The prototype groups consecutive horizontal solid outlines
within one run, while hinting and bitmap caching are disabled. It keeps
singletons on the ordinary path. For groups, it applies each original
device transform to its outline elements and sends the combined path to
vello once. It bounds group length, path elements, device extent and
the per-page bounds cache. The measurement includes all of that work.

All mismatches were in bill-hr815, on 37 pages across 38 page/resolution
pairs. There were 12 mismatched layouts at 72 dpi and 152 at 144 dpi.
For example, on zero-based page 1 at 144 dpi, the RGB buffers differed at
one pixel out of 1440 by 2160: (737, 508) changed from (120, 120, 120)
to (121, 121, 121). The hashes establish that disjoint pixel envelopes
alone are insufficient to guarantee identical output when combining paths
in this renderer. The experiment does not identify the cause of the
rounding difference. [Both artifacts and every mismatched hash](pixels.json).

| First artifact | DPI | Best baseline ms | Best candidate ms | Ratio | Median baseline ms | Median candidate ms | Ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | 72 | 1015.5 | 1085.3 | 1.0688 | 1038.4 | 1103.9 | 1.0631 |
| Candidate | 72 | 1052.8 | 1095.3 | 1.0403 | 1106.7 | 1179.8 | 1.0661 |
| Baseline | 144 | 1827.0 | 1885.7 | 1.0321 | 1952.6 | 1929.8 | 0.9883 |
| Candidate | 144 | 1871.8 | 1958.6 | 1.0464 | 1927.0 | 2021.4 | 1.0490 |

These timings diagnose whether the approach warrants trying to correct its
pixels. They do not count toward #365's acceptance. The 72 dpi median is
6% slower in both orders, and the 144 dpi median changes direction.
[Baseline-first samples](baseline-first.json) and
[candidate-first samples](candidate-first.json).

Use the [prepared-outline reproduction steps](../prepared-outline/README.md#reproduction)
with the disjoint-glyph patch applied to a clean copy of the same hayro
revision, as a separate candidate. Compare against the same ordinary
baseline wheel. The two candidate patches are independent.
