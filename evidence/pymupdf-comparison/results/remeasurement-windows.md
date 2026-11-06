# Rendering remeasurement, Windows

Measured 2026-11-05 UTC (2026-11-06 JST) for #365. The extension was built
from `13ea858a096493bb49e681c450a2a44302ecfaa7` with the pinned Rust toolchain,
MSVC, and `maturin build --release --locked`. Both isolated environments use
Python 3.13.17. The wheel SHA-256 is
`d9232afcac19d40db410853da4cd9af5979a26fc9064ac8337e522b64b632c63`.
No product code was modified for these measurements.

## Shipped defaults

`windows-amd64-checkout.json` and its Markdown table contain the ordinary
comparison against PyMuPDF 2.0 / MuPDF 1.28.5, using raw RGB samples and the
public defaults: `hinting=False`, `glyph_cache=False`. Each operation uses
the best of five runs on newly opened documents.

| Resolution | MuPDF total (ms) | pyropdf total (ms) | Ratio |
| --- | ---: | ---: | ---: |
| 72 dpi | 744.4 | 1901.1 | 2.55 |
| 144 dpi | 1229.8 | 3661.1 | 2.98 |

The 1.5 target is not met. bill-hr815 is the largest gap (5.53 and 5.00
times); usrguide is 1.56 and 2.10 times. The older Darwin results are on a
different machine and release, so their absolute times and ratios are not
a controlled before/after measurement of hayro 0.9.

## Opt-in cache experiment

Using the same wheel and environment:

```powershell
target/pymupdf-comparison/pyropdf-checkout-env/Scripts/python.exe evidence/pymupdf-comparison/cache_probe.py evidence/pymupdf-comparison/results/cache-windows-amd64.json
```

`cache_probe.py` alternates which setting runs first, opens a fresh document
for every sample, keeps hinting off, and records all five samples per
document/resolution/setting. Both timings include releasing the generated
pixel buffers, as `bench.py` does. The table sums each document's best sample.

| Resolution | Default (ms) | Glyph cache (ms) | Cache / default |
| --- | ---: | ---: | ---: |
| 72 dpi | 958.0 | 552.5 | 0.58 |
| 144 dpi | 1703.7 | 1177.2 | 0.69 |

The default timings differ substantially from the preceding comparison;
even bill-hr815's five 72 dpi default samples range from 697 to 810 ms.
These are noisy workstation measurements. Do not divide the cache totals
by the earlier MuPDF totals or claim the acceptance target is met. The
paired experiment supports investigating glyph rendering, but is not a
native call-stack profile or proof of a specific internal bottleneck.

The public API documents that glyph caching quantizes text positions and
has memory outside DocumentLimits. It remains opt-in; this experiment does
not establish pixel equivalence or justify changing the shipped default.

## Next evidence needed

The subsequent [Windows native samples](profile-windows-amd64.md) identify
current rasterization hot functions. They also find significant `memmove`
work whose callers still need to be identified before choosing a copy or
allocation optimization. Repeat a controlled comparison after that work.
#365 stays open until the shipped configuration meets its acceptance
criterion. No upstream report is published by these measurements.
