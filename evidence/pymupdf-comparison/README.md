# pyropdf compared with PyMuPDF

The baseline for #355, which tracks the work for pyropdf to do everything
PyMuPDF does with PDF and more (DESIGN.md section 1: pymupdf's capabilities
are the floor). Measured for #354.

## Release comparisons

[pyropdf v0.3.1 vs PyMuPDF 2.1 on macOS/arm64](releases/v0.3.1-darwin-arm64/comparison.md)
runs the two published wheels on 15 documents and seven human-verified pages.
[Observations](releases/v0.3.1-darwin-arm64/observations.json) retain five samples per timing cell,
label and document hashes, dependencies, API changes and raw scores. The compressed extractions
beside them preserve the original text, boxes and table cells.

Subsequent GitHub releases attach `comparison-vX.Y.Z.zip` with the Linux/x86-64 wheel results.
The benchmark uses the actual wheel from the release build. PyMuPDF runs as an external oracle
in a separate temporary environment. Download a compatible pair on Python 3.13.18 and reproduce
the macOS report from the repository root:

```sh
mkdir -p target/comparison-wheels
gh release download v0.3.1 --pattern '*abi3-macosx_11_0_arm64.whl' --dir target/comparison-wheels
python3.13 -m pip download --only-binary=:all: --no-deps --dest target/comparison-wheels pymupdf==2.1
python3.13 evidence/pymupdf-comparison/release.py v0.3.1 \
    target/comparison-wheels/pyropdf-0.3.1-*_arm64.whl \
    target/comparison-wheels/pymupdf-2.1-*_arm64.whl target/comparison
python3 -m unittest discover -s evidence/pymupdf-comparison -p test_release_comparison.py
```

For subsequent releases pass `--previous-report /path/to/previous/observations.json`.
The workflow finds the preceding release tag in Git ancestry and reads its published ZIP;
v0.3.1 predates the attachment and has its original observations here. The first demonstration
compares against the historical [2.0 surface and probe output](pymupdf-2.0-api.json).
The report links the measured artifact's own specification map and public test sources.
These describe contract coverage separately from the operations executed in the benchmark.
Platform, allocation behavior and short timings can vary from run to run.

When PyMuPDF publishes a new version, review its upstream release notes and PDF API changes
before the next pyropdf tag. The release workflow downloads the current compatible PyMuPDF
wheel, and the runner checks its version against the reviewed baseline. Compare
`release_worker.py info pymupdf` in the old and new oracle environments and run
`release_delta.py pymupdf` in each. Update the constraints, behavior probes, report explanation
and [task map](capabilities.tsv). Link a release test or an open issue and the concrete gap
for each affected PDF task. The 2.0-to-2.1 review covers escaped and Unicode page-label
prefixes, glyph height under non-uniform scale, the placement documentation correction,
and the import-time change. The latter has no timing workload. The official
[2.1 notes](https://github.com/pymupdf/PyMuPDF/releases/tag/2.1) identify these changes.

## Earlier baselines

| File | Role |
| --- | --- |
| `features.md` | The capabilities of PyMuPDF 2.0 against an identified pyropdf main commit, with gaps linked to their issues |
| `bench.py ENGINE OUT FILE...` | Times the shared operations for one engine |
| `run.py` | Installs PyMuPDF 2.0 and pyropdf 0.1.0 from PyPI, each in its own environment under `target/pymupdf-comparison/`, runs `bench.py` with both, and writes `results/` |
| `labels.py` | Writes `results/page-labels.json`, PyMuPDF's `get_label` of every corpus page, from the environment that `run.py` installs; `tests/test_page_labels.py` compares pyropdf with it |
| `fonts.py` | Writes `results/page-fonts.json`, PyMuPDF's `get_fonts(full=True)` of every corpus page, from the environment that `run.py` installs; `tests/test_page_fonts.py` compares pyropdf with it |
| `profile.py` | Samples pyropdf rendering bill-hr815 and usrguide at 72 and 144 dpi on macOS, and writes `results/profile-<platform>.md`, for #365; `patches/` holds the uncommitted crate changes it measures with |
| `results/darwin-arm64.md` | The measured comparison, with the raw timings in the `.json` beside it |
| `sample_hashes.py OUT` | Hashes every corpus page's samples in each raster layout, to show that a change keeps every byte; `results/rgb8-take-darwin-arm64.md` records its use for #393 |
| `compare_text.py` | Compares text hashes and word multisets on the corpus in separate engine environments; `results/text-main.md` explains its limits |
| `quality.py` | Scores text, reading order, word boxes, and table cells of 11 labeled pages against `quality/*.json`, for #430, #840, and #1149; `results/quality.md` holds the scores |
| `gray_probe.py` | Reproduces the engines' different `rewrite_images(set_to_gray=True)` behavior on a synthetic PDF |

```bash
python3 evidence/pymupdf-comparison/run.py
```

## Rendering investigations

The Windows investigation is preserved as evidence in #723. Its source
patches are experimental inputs to separately built wheels; they are not
applied by the project build. These measurements do not establish that the
shipped renderer meets #365's performance target. #715 owns the bounded
worker implementation and validation of the eventual shipping artifact.

| Investigation | Evidence and conclusion |
| --- | --- |
| Native hot paths and copies | [Native samples](results/profile-windows-amd64.md) and [copy attribution](results/copy-initialization-windows.md) identify the work to optimize |
| Exact glyph positions | [Phase measurements](results/glyph-phase.md) quantify reuse without changing subpixel positions |
| Prepared outlines | [Report](results/prepared-outline/README.md) preserves matching pixels but small, inconsistent full-render gains |
| Disjoint glyph batches | [Report](results/disjoint-glyphs/README.md) records pixel differences and slower rendering; the candidate was rejected |
| Per-call workers | [Report](results/two-workers/README.md) records setup costs and resource measurements |
| Reused workers | [Report](results/reused-workers/README.md) records faster experimental rendering and the lifetime problem that led to #715 |

To measure a checkout, first build a wheel with `maturin build --release --locked`,
then run the same comparison with that artifact:

```bash
python3 evidence/pymupdf-comparison/run.py --pyropdf-wheel /path/to/pyropdf.whl
```

Both environments use the installed Python 3.13 on Windows as well as Unix.
Local-wheel results are written to `results/<platform>-checkout.json` and `.md`,
including the wheel's SHA-256, without replacing the published-version baseline.
Record the source commit and rendering defaults alongside those measurements.
The runner uses the public rendering defaults; it does not enable hinting or
glyph caching on the caller's behalf. Reusing the checkout result name replaces
that checkout measurement, so preserve any result that should remain comparable.

The [Windows remeasurement](results/remeasurement-windows.md) records a 0.2.2
release-wheel comparison and a separate opt-in glyph-cache experiment.
`cache_probe.py OUT` records every sample of that experiment, alternating
option order. It does not change the shipped defaults or establish the
1.5 rendering target; see the report's variability and quality limitations.

[Windows native samples](results/profile-windows-amd64.md) identify the current
rasterization hot functions with a separate PDB-enabled release wheel.
`profile_windows.py` and `sample_windows.cpp` reproduce those main-thread
leaf samples without changing product code; they are not benchmark timings.
The [copy-caller follow-up](results/copy-initialization-windows.md) attributes
the Rust-side copies to target initialization and measures an existing
zeroed-storage alternative. #710 owns its implementation and full-render
validation; constructor timing alone does not satisfy #365.

PyMuPDF is AGPL. It runs only here, as a separate program in its own
environment, and is never a dependency of pyropdf.

## Method

- Both engines come from their published wheels, so both are release builds.
  They run on Python 3.13, since PyMuPDF 2.0 needs onnxruntime, which has no
  3.15 wheels.
- Every operation runs on every page of each document of
  `tests/assets/real_world`. Each run opens the document again, because
  pyropdf memoizes derived results per version. Each row is the best of five
  runs.
- Open: `pymupdf.open` against `pyropdf.open`. Text: `get_text()` against
  `text()`. Words: `get_text("words")` against `words()`. Search: "the" on
  every page. Render: `get_pixmap(dpi=...)` against `pixmap(dpi=...)`, with
  the samples read. Full save: `tobytes()` against `to_bytes()`.

## Results

For pyropdf **0.1.0**, not current main, on an Apple M-series machine
(`results/darwin-arm64.md`), as corpus totals:

| Operation | pyropdf / PyMuPDF |
| --- | ---: |
| Open | 0.21 |
| Text | 0.80 |
| Words | 0.79 |
| Search | 1.19 |
| Render 72 dpi | 2.03 |
| Render 144 dpi | 2.34 |
| Full save | 0.15 |

pyropdf is faster at text and words, and search is close. Rendering is the gap, worst on bill-hr815 (4.3 to
4.6 times) and usrguide (2.9 to 3.7 times); #365 closes it.

## Rendering profile

`profile.py` profiles this checkout's rendering of the two documents where
the gap is widest (`results/profile-darwin-arm64.md`; its docstring gives the
build it needs). On both documents, at both resolutions:

- vello_cpu fills each glyph as a path, every time it is drawn: flattening,
  tiling, sorting tiles, and generating strips. Glyph fills take 57 to 75% of
  the samples. hayro caches each glyph's outline but not its coverage, and
  vello_cpu's glyph atlas serves only its own glyph runs, which hayro does not
  use. MuPDF caches glyph bitmaps.
- Fine rasterization takes 6 to 13%.
- `to_layout` in `pyropdf_engine::render`, which narrows RGBA to RGB one
  pixel at a time, takes 7.7 to 20.1%. Taking the pixels,
  `take_rgba8(ImageAlphaType::Alpha)`, takes no samples: with an opaque
  background, vello_cpu skips the division by alpha. `patches/split-draw.patch`
  keeps both steps out of line so that each has a frame of its own; the
  profile is measured with it applied.
- The store, the bridge, and opening take 0.2 to 1.2%.

`patches/rgb8-draw.patch` replaces `to_layout` with vello_cpu's SIMD
`Pixmap::try_take_rgb8` when the raster is RGB without alpha
(`results/profile-darwin-arm64-rgb8.md`). The step then takes 1.3 to 3.6%,
and rendering, with the samples read, gets faster:

| Case | pyropdf (ms) | with the patch (ms) | change |
| --- | ---: | ---: | ---: |
| bill-hr815 72 dpi | 408.7 | 375.9 | -8% |
| bill-hr815 144 dpi | 766.1 | 638.3 | -17% |
| usrguide 72 dpi | 77.1 | 71.0 | -8% |
| usrguide 144 dpi | 135.4 | 112.4 | -17% |

Until #365, `bench.py` timed `to_bytes()` for pyropdf, which encodes a PNG,
against PyMuPDF's raw `samples`. The PNG adds 40 to 90% to pyropdf's time on
these documents. `bench.py` now reads pyropdf's samples through the buffer
protocol, `bytes(pixmap)`; the corpus totals of rendering fell from 2.65 and
3.97 to 2.03 and 2.34.

## Text quality against labels

For #430, #840, and #1149: does each engine read the right text, in the right
order, with the right boxes? Neither engine is the ground truth. `quality/`
labels 11 pages from `tests/assets/real_world/`, chosen for their kinds of
layout: a bill in one column (`bill-hr815 p1`), both pages of a tax form
(`f1040 p1` and `p2`), Japanese administrative prose with a header box and
mixed-script text (`mhlw-doc p1` and `p2`), a wide 25-column table (`nics p1`),
a 90°-rotated financial table with a merged bordered header and borderless
multi-line rows (`senate-expenditures p1`), two pages of a scanned two-column
patent with an OCR layer (`patent-us223898 p2` and `p3`), and a LaTeX manual's
title/TOC page and body page (`usrguide p1` and `p2`). Image-only scans with no
PDF text layer (`bunka-kokugo-series-019-p4.pdf` and `wdl6812-manuscript.pdf`)
have no native text to score here and are tested by the OCR suite instead. Each
label holds:

- **Anchors**: lines in the order a reader reads the Ghostscript
  rendering. An anchor counts as found when the page text holds it after
  NFKC and the removal of all whitespace, since the engines place spaces
  between CJK characters and digits differently. Neighboring found anchors
  count as in order when their positions increase.
- **Words**: six words per native-text page (54 words across 9 pages), each
  found once by both engines with boxes that overlap (after applying page
  rotation on `senate-expenditures p1`), spread over the page. Their ink boxes
  are measured in Ghostscript renderings at 288 dpi, inside a seed box around
  both engines' boxes, without the rules of tables and forms or the ink of
  neighboring lines (`python quality.py ink`). A word counts as placed when
  a word of the same text covers 90% of its ink box. The mean IoU compares
  the box with the ink, so it measures box conventions, such as font
  ascent and descent, more than errors. The scanned patent pages' OCR layer has
  no ink of its own, so `patent-us223898 p2` and `p3` have no word labels.
- **Tables**: three rows of the `nics` table (Alabama, Wyoming, and Totals;
  25 columns) and three voucher rows of the rotated `senate-expenditures` table
  (`DHAW20190001`, `DHAW20190031`, and `DHAW20190070`; 7 columns), checked
  against high-DPI Ghostscript renderings.

The six unchanged pages from #430 were checked by the owner against the crops
of `python quality.py crops DIR` (`"checked": true`). The four newly labeled
pages from #1149 (`f1040-p2`, `patent-us223898-p3`, `senate-expenditures-p1`,
and `usrguide-p1`) and `mhlw-doc-p2` (whose anchors were verified in #840 and
whose six Japanese mixed-script word ink boxes were newly agent-measured in
#1149) are marked `"checked": false` until the owner checks their crops by eye.
A changed label is checked again the same way; `release.py` scores only
`"checked": true` pages. `tests/test_text_quality.py` holds pyropdf to all
labels (recording the tracked `senate-expenditures-p1` table gap in
`KNOWN_TABLES`).

```bash
uv run python evidence/pymupdf-comparison/quality.py collect pyropdf /tmp/pyropdf.json
target/pymupdf-comparison/pymupdf-env/bin/python evidence/pymupdf-comparison/quality.py collect pymupdf /tmp/mupdf.json
target/pymupdf-comparison/pymupdf-env/bin/python evidence/pymupdf-comparison/quality.py collect pymupdf-sort /tmp/mupdf-sort.json
uv run python evidence/pymupdf-comparison/quality.py score evidence/pymupdf-comparison/results/quality COMMIT /tmp/mupdf.json /tmp/mupdf-sort.json /tmp/pyropdf.json
```

[`results/quality.md`](results/quality.md) holds the scores, measured with
PyMuPDF 2.1 on Python 3.13.18 and pyropdf 0.4.0 at `129eb3aa` with #1149 on
Ghostscript's renderings. Both engines find every anchor and place every
labeled word, except where noted below.

| Difference | Kind |
| --- | --- |
| `patent-us223898 p2`: pyropdf split the headings above the two columns at the gutter (`UNITED STATES` … `PATENT OFFICE.` after the left column) | pyropdf engine error, fixed by #460; the table shows the fixed scores |
| `usrguide p1`: pyropdf pinned the footnote line baseline on its leading 6pt superscript (`∗`) and split the lowered `E` of `\LaTeX` onto a separate line | pyropdf engine error, fixed by #1149; the table shows the fixed scores |
| `senate-expenditures p1` table: pyropdf's `page.tables()` extracts only the 2×7 bordered header and misses the 32 borderless data rows across the 1.84 pt double-rule gap; PyMuPDF's `find_tables()` collapses all payroll rows and all voucher rows into two garbled cells | pyropdf limitation tracked in #1153; PyMuPDF engine error |
| `senate-expenditures p1` reading order and word boxes: PyMuPDF's default reads payroll rows right-to-left (content-stream order), `sort=True` sorts in unrotated portrait space and loses 10/16 anchors, and `get_text("words")` returns unrotated `612×792` cropbox boxes (`0/6` placed against display-space ink); pyropdf reads and places all words in display coordinates (`16/16` anchors, `15/15` pairs, `6/6` words) | PyMuPDF rotated-page behavior (`page.get_text` / `get_text("words")` do not rotate to `page.rect` display space by default) |
| `patent-us223898 p2` and `p3`: PyMuPDF reads the right column before the left one ends (p2, both modes; p3, `sort=True`) and places the top stamp between columns by default (p3) | PyMuPDF engine error on scanned two-column pages |
| `nics` table: PyMuPDF's `find_tables()` finds 17 rows, with the states merged into a few cells; pyropdf finds every labeled row and cell (`75/75`) | PyMuPDF engine error |
| `mhlw-doc p1` and `nics p1`: PyMuPDF's default puts the header box after the body and the first rows before the title | API choice: `get_text()` follows the content stream unless `sort=True`, which reads both pages in order |
| Word IoU 0.65 to 0.90 across unrotated pages (and 0.70 for pyropdf on rotated `senate-expenditures p1`) | Box conventions: both engines' boxes cover the ink (except PyMuPDF's unrotated boxes on `senate-expenditures p1`) |

## Limits

- Open and full save without edits are not profiled: each row is what a user
  waits for, and whether both engines do equal work for it is not measured.
- The benchmark times text and words, not their quality. The separate
  [main comparison](results/text-main.md) compares default outputs, not
  labeled accuracy. The labeled comparison covers 11 pages with anchors and
  9 pages with six ink-measured words each, not every character. Phase 1's
  parity evidence (`evidence/phase1-parity`) compares pyropdf's output with
  pylopdf's.
- One machine, one thread, and a 15-document corpus.
