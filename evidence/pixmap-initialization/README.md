# Zeroed raster initialization: rejected candidate (#710)

The candidate preserves pixels but does not improve rendering consistently.
Do not apply `candidate.patch` to production: 144 dpi rendering regressed in
both measurement orders. No rendering optimization is shipped by this evidence.
#365's requirement of at most 1.5 times MuPDF remains unmet.

## Experiment

The copy-stack investigation in [draft PR #709](https://github.com/yhay81/pyropdf/pull/709)
identified repeated zero-pixel copying in `Pixmap::new`. The retained patch
replaces that constructor with zeroed bytes and `Pixmap::from_parts`, preserving
premultiplied alpha, transparency metadata, and existing extent checks. The
maximum 64 million pixels bound the four-byte allocation on 32-bit targets too.
The renderer still clears the target to its selected background.

Both artifacts are ordinary, uninstrumented release wheels built with the
repository's locked dependencies and pinned Rust toolchain on Windows 11 amd64.
The baseline is commit `13ea858a096493bb49e681c450a2a44302ecfaa7`; its product
sources match main `938b94d5f9584c5c21a013265a3c8c4cfe0714b4`. The candidate is
that main commit plus `candidate.patch`. Both isolated environments use Python
3.13.17. Each result records wheel and loaded-extension SHA-256; the driver
verifies that the installed extension matches the supplied wheel before timing.

For each of 15 corpus documents at 72 and 144 dpi, two persistent workers
alternate five samples. Each sample opens a fresh document before timing,
then times page iteration, default rendering, copying all raw pixel buffers,
and releasing those buffers. Document opening and closing are excluded. No PNG
encoding or optional glyph cache is used. Totals sum each document's minimum
or median, rather than treating pages as independent repetitions.

## Results

Times are milliseconds; a candidate/baseline ratio above one is slower.

| Order | DPI | Best baseline | Best candidate | Ratio | Median baseline | Median candidate | Ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline first | 72 | 1653.0705 | 1539.0521 | 0.9310 | 1741.7223 | 1624.7106 | 0.9328 |
| Baseline first | 144 | 2362.0562 | 2498.8878 | 1.0579 | 2535.9169 | 2588.0512 | 1.0206 |
| Candidate first | 72 | 964.7431 | 955.5468 | 0.9905 | 974.7696 | 974.1237 | 0.9993 |
| Candidate first | 144 | 1717.8293 | 1825.1204 | 1.0625 | 1751.9693 | 1870.1360 | 1.0674 |

Raw samples are in [the initial run](results/windows-amd64/comparison.json)
and [the reversed-order repeat](results/windows-amd64-repeat/comparison.json).
The initial run starts with baseline (its older output schema omits
`candidate_first`); the repeat explicitly records `true`.

Absolute times vary substantially between runs on this shared workstation.
The 72 dpi benefit nearly disappears on repetition, while the 144 dpi slowdown
persists. These measurements do not isolate a microarchitectural cause or prove
behavior on other platforms. Constructor-only timings can defer physical page
initialization; they are not evidence of an end-to-end gain. Do not combine
these ratios with older MuPDF comparisons to claim a new MuPDF-relative result.

All 1,872 comparisons (156 pages, six output layouts, two resolutions) have
identical dimensions, channels and raw-pixel hashes. Layouts cover RGB, gray,
alpha, opaque backgrounds and translucent backgrounds. The shared hashes are
in [pixel-hashes.json](results/windows-amd64/pixel-hashes.json); the repeat
measured timing only. The 32 added blank-raster tests also pass against the
candidate wheel, covering every pixel at small sizes and the maximum supported
width and height. They remain as public-contract coverage for the unchanged
production renderer.

## Reproduction

Use a clean checkout of main commit `938b94d5f9584c5c21a013265a3c8c4cfe0714b4`
with the evidence scripts available. Build one baseline release wheel, apply
`candidate.patch` with `git apply`, and build a second wheel into a different
directory. Use the same Python interpreter and toolchain for both builds:

```powershell
uv tool run --from 'maturin>=1.16,<2.0' maturin build --release --locked --out target/baseline-wheels --interpreter python3.13.exe
git apply evidence/pixmap-initialization/candidate.patch
uv tool run --from 'maturin>=1.16,<2.0' maturin build --release --locked --out target/candidate-wheels --interpreter python3.13.exe
git apply --reverse evidence/pixmap-initialization/candidate.patch
```

On Windows, run builds from Developer PowerShell. Install each wheel in its
own Python 3.13 environment with `uv venv --python python3.13.exe` and
`uv pip install --python <environment>/Scripts/python.exe <wheel>`.
Run the following with the actual environment and artifact paths:

```powershell
python3 evidence/pixmap-initialization/compare.py --baseline-python <baseline-python> --baseline-wheel <baseline-wheel> --candidate-python <candidate-python> --candidate-wheel <candidate-wheel> --output <results-directory>
```

Repeat with `--candidate-first --timings-only` and a different output directory
after the exact-pixel comparison succeeds. Install the repository-pinned pytest
in the candidate environment and run
`python -m pytest tests/test_rendering_options.py -k blank_raster_initializes`
using that environment's interpreter to verify the 32 boundary/background cases.

This completes #710's explicit negative-result fallback. Further work toward
#365 needs another independently measured candidate; neither its target nor
its release hold is relaxed by this result.
