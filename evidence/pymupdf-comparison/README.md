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
| `quality.py` | Scores text, reading order, word boxes, tables, and OCR of 14 labeled pages against `quality/*.json`, for #430, #840, #1149, and #1192; `results/quality.md` holds the scores |
| `compare_pymupdf4llm.py` | Compares `doc.markdown()` and `doc.to_chunks()` against `pymupdf4llm` (with and without `pymupdf-layout`) in isolated environments, for #1192; `results/pymupdf4llm.md` holds the scores |
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

For #430, #840, #1149, and #1192: does each engine read the right text, in the
right order, with the right boxes? Neither engine is the ground truth.
`quality/` labels 14 pages from `tests/assets/real_world/` (including the
redistributable Japanese business form in `tests/assets/real_world/forms/`),
chosen for their kinds of layout:

- **12 native-text pages (`mode: "native"`)**: a bill in one column
  (`bill-hr815 p1`), both pages of a tax form (`f1040 p1` and `p2`), a
  90°-rotated landscape Japanese statutory business form with bordered
  accounting and preservation tables (`forms/mhlw-deposit-report p1`),
  Japanese administrative prose with a header box and mixed-script text
  (`mhlw-doc p1` and `p2`), a wide 25-column table (`nics p1`), a 90°-rotated
  financial table with a merged bordered header and borderless multi-line rows
  (`senate-expenditures p1`), two pages of a scanned two-column patent with an
  OCR layer (`patent-us223898 p2` and `p3`), and a LaTeX manual's title/TOC
  page and body page (`usrguide p1` and `p2`).
- **2 OCR scan pages (`mode: "ocr"`)**: an image-only Japanese scan with no
  embedded PDF text layer (`bunka-kokugo-series-019-p4 p1`, `ocr_language: "jpn"`)
  and a negative-control illuminated decorative panel (`wdl6812-manuscript p1`,
  `max_spurious_words: 8`).

Each label holds:

- **Anchors**: lines in the order a reader reads the Ghostscript rendering. An
  anchor counts as found when the page text holds it after NFKC and the removal
  of all whitespace, since the engines place spaces between CJK characters and
  digits differently. Neighboring found anchors count as in order when their
  positions increase.
- **Words**: six words per native-text page with vector ink (60 words across 10
  pages) plus six OCR words on `bunka-kokugo-series-019-p4 p1`, spread over the
  page. Their ink boxes are measured in Ghostscript renderings at 288 dpi,
  inside a seed box around the word, without the rules of tables and forms or
  the ink of neighboring lines (`python quality.py ink`). A word counts as
  placed when a word of the same text covers 90% of its ink box. The mean IoU
  compares the box with the ink, so it measures box conventions, such as font
  ascent and descent, more than errors. The scanned patent pages' OCR layer has
  no ink of its own, so `patent-us223898 p2` and `p3` have no word labels.
- **Tables**: three rows of the `nics` table (Alabama, Wyoming, and Totals; 25
  columns), three voucher rows of the rotated `senate-expenditures` table
  (`DHAW20190001`, `DHAW20190031`, and `DHAW20190070`; 7 columns), and the
  6-column bordered preservation table of `mhlw-deposit-report p1`, checked
  against high-DPI Ghostscript renderings.

The six unchanged pages from #430 were checked by the owner against the crops
of `python quality.py crops DIR` (`"checked": true`). Eight pages remain
`"checked": false` awaiting the owner's visual verification by eye: the five
pending pages from #1149 (`f1040-p2`, `mhlw-doc-p2`, `patent-us223898-p3`,
`senate-expenditures-p1`, and `usrguide-p1`) and the three newly labeled pages
from #1192 (`mhlw-deposit-report-p1`, `bunka-kokugo-series-019-p4-p1`, and
`wdl6812-manuscript-p1`). Generate crops and a visual verification checklist
for all pending pages with:

```bash
uv run python evidence/pymupdf-comparison/quality.py crops target/quality-crops --pending
```

A changed label is checked again the same way; `release.py` scores only
`"checked": true` pages. `tests/test_text_quality.py` holds pyropdf to all 14
labels (both native and OCR) and verifies license records for every referenced
document.

```bash
uv run python evidence/pymupdf-comparison/quality.py collect pyropdf /tmp/pyropdf.json
target/pymupdf-comparison/pymupdf-env/bin/python evidence/pymupdf-comparison/quality.py collect pymupdf /tmp/mupdf.json
target/pymupdf-comparison/pymupdf-env/bin/python evidence/pymupdf-comparison/quality.py collect pymupdf-sort /tmp/mupdf-sort.json
uv run python evidence/pymupdf-comparison/quality.py score evidence/pymupdf-comparison/results/quality COMMIT /tmp/mupdf.json /tmp/mupdf-sort.json /tmp/pyropdf.json
```

[`results/quality.md`](results/quality.md) holds the scores, measured with
PyMuPDF 2.1 on Python 3.13.18 and pyropdf 0.4.1 at `4e0e3c00` on Ghostscript's
renderings. Both engines find every native anchor and place every labeled
unrotated native word, except where noted below.

| Difference | Kind |
| --- | --- |
| `patent-us223898 p2`: pyropdf split the headings above the two columns at the gutter (`UNITED STATES` … `PATENT OFFICE.` after the left column) | pyropdf engine error, fixed by #460; the table shows the fixed scores |
| `usrguide p1`: pyropdf pinned the footnote line baseline on its leading 6pt superscript (`∗`) and split the lowered `E` of `\LaTeX` onto a separate line | pyropdf engine error, fixed by #1149; the table shows the fixed scores |
| `senate-expenditures p1` table: pyropdf now extracts all 3 labeled voucher rows and `21/21` cells (`#1153` fixed); PyMuPDF's `find_tables()` collapses payroll and voucher rows (`0/3` rows, `0/21` cells) | pyropdf fixed (`#1153`); PyMuPDF engine error |
| `senate-expenditures p1` and `mhlw-deposit-report p1` (90°-rotated pages): PyMuPDF's default reads rotated rows out of display order (`11/15` and `5/11` pairs), `sort=True` sorts in unrotated portrait space and loses anchors (`6/16` and `10/12`), `get_text("words")` returns unrotated mediabox boxes (`0/6` placed against display-space ink), and `find_tables()` finds no tables on `mhlw-deposit-report p1` (`0/1`); pyropdf reads, places words, and extracts table cells in display coordinates (`16/16` and `12/12` anchors, `6/6` words each, `21/21` and `6/6` cells) | PyMuPDF rotated-page behavior (`get_text` / `get_text("words")` / `find_tables` operate in unrotated coordinates by default) |
| `patent-us223898 p2` and `p3`: PyMuPDF reads the right column before the left one ends (p2, both modes; p3, `sort=True`) and places the top stamp between columns by default (p3) | PyMuPDF engine error on scanned two-column pages |
| `nics` table: PyMuPDF's `find_tables()` finds 17 rows, with the states merged into a few cells; pyropdf finds every labeled row and cell (`75/75`) | PyMuPDF engine error |
| `mhlw-doc p1` and `nics p1`: PyMuPDF's default puts the header box after the body and the first rows before the title | API choice: `get_text()` follows the content stream unless `sort=True`, which reads both pages in order |
| `bunka-kokugo-series-019-p4 p1` (Japanese image-only scan): pyropdf's bundled PP-OCR finds `11/11` anchors (`10/10` pairs) and places `6/6` words (mean IoU `0.68`), with three known historical-print misreads outside the anchors (`ヨロン` → `ョロン`, ruby-annotated `文章博士` → `女章博士`, faint `【問】` → `【間】`). PyMuPDF's `get_textpage_ocr(language="jpn")` is recorded as `skipped` when Tesseract Japanese traineddata (`jpn.traineddata`) is not installed on the host | OCR engine trade-off (bundled PP-OCR vs external Tesseract traineddata) |
| `wdl6812-manuscript p1` (non-text illuminated panel negative control): pyropdf PP-OCR emits 4 low-signal ornament artifacts (within `max_spurious_words: 8`), while PyMuPDF/Tesseract (`eng`) emits 0; page 2 is classical Arabic script outside the CJK/Latin PP-OCR model alphabet | pyropdf OCR negative-control noise and script-coverage limit |
| Word IoU 0.65 to 0.90 across unrotated pages (and 0.70 / 0.90 for pyropdf on rotated `senate-expenditures p1` and `mhlw-deposit-report p1`) | Box conventions: both engines' boxes cover the ink (except PyMuPDF's unrotated boxes on rotated pages) |

## Markdown and retrieval chunks against pymupdf4llm

For #1192: [`compare_pymupdf4llm.py`](compare_pymupdf4llm.py) compares
`doc.markdown()` and `doc.to_chunks(max_chars=2000)` against `pymupdf4llm`
(both without and with `pymupdf-layout`) across all 12 native-text labeled
pages. Because `pymupdf4llm` and `pymupdf-layout` are AGPL, they never enter
`pyproject.toml` or `uv.lock`; install them into a throwaway venv under
`target/`:

```bash
uv venv --python 3.13 target/pymupdf4llm-venv
uv pip install --python target/pymupdf4llm-venv/bin/python \
    -c evidence/pymupdf-comparison/oracle-2.1-constraints.txt pymupdf==2.1
uv run python evidence/pymupdf-comparison/compare_pymupdf4llm.py collect pyropdf target/pyropdf-md.json
target/pymupdf4llm-venv/bin/python evidence/pymupdf-comparison/compare_pymupdf4llm.py collect pymupdf4llm target/mupdf4llm.json
target/pymupdf4llm-venv/bin/python evidence/pymupdf-comparison/compare_pymupdf4llm.py collect pymupdf4llm-layout target/mupdf4llm-layout.json
uv run python evidence/pymupdf-comparison/compare_pymupdf4llm.py score \
    evidence/pymupdf-comparison/results/pymupdf4llm COMMIT \
    target/pyropdf-md.json target/mupdf4llm.json target/mupdf4llm-layout.json
```

[`results/pymupdf4llm.md`](results/pymupdf4llm.md) records the baseline at
commit `4e0e3c00` (pyropdf 0.4.1 vs `pymupdf4llm 2.1` / `PyMuPDF 2.1` on
macOS/arm64, best of 3 runs per page):

| Engine | MD anchors | Ordered pairs | Chunk anchors | MD table cells | Headings | Pipe rows | MD ms | Chunks ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| pyropdf 0.4.1 | 147/152 | 134/135 | 147/152 | 93/102 | 55 | 111 | 755.6 | 740.1 |
| pymupdf4llm 2.1 (without `pymupdf-layout`) | 132/152 | 110/120 | 132/152 | 25/102 | 8 | 116 | 9044.0 | 9044.6 |
| pymupdf4llm 2.1 (with `pymupdf-layout`) | 112/152 | 98/100 | 112/152 | 23/102 | 12 | 292 | 3925.0 | 3908.5 |

Summary of wins, losses, and shared gaps in that baseline:

- **Where pyropdf leads**: higher overall Markdown and chunk anchor recall
  (`147/152` vs `132/152` without layout and `112/152` with layout), fewer
  order inversions (`134/135` vs `110/120` and `98/100`), full `75/75` GFM
  table-cell fidelity on `nics` (vs `25/75` and `5/75`), stronger recall on
  `f1040` and `usrguide` (`18/18` and `14/14` where `pymupdf-layout` drops form
  and TOC lines), section-aware chunking (`55` headings), and `5×–12×` faster
  total Markdown/chunk runtime (`755.6 ms` vs `3925.0 ms` / `9044.0 ms`).
- **Where pyropdf loses**: on `mhlw-doc-p1`, pyropdf's `doc.markdown()` places
  the document title before the top-right header box (`9/10` ordered pairs) and
  takes `61.5 ms`, while `pymupdf4llm` without layout scores `10/10` in
  `41.2 ms` (and with layout scores `10/10` in `64.5 ms`).
- **Shared gaps across all three engines**: on `mhlw-deposit-report-p1`, none
  of the three emits the 6-column multi-line preservation table as clean GFM
  pipe cells (`0/6` each) and all three miss three multi-line table anchors in
  Markdown; on `mhlw-doc-p2`, all three miss the two multi-line anchors where
  Markdown separates heading and bullet lines; on `senate-expenditures-p1`,
  pyropdf and `pymupdf4llm` with `pymupdf-layout` tie at `18/21` GFM table
  cells (each dropping 3 multi-line description cells in pipe-table rendering).

## Limits

- Open and full save without edits are not profiled: each row is what a user
  waits for, and whether both engines do equal work for it is not measured.
- The benchmark times text and words, not their quality. The separate
  [main comparison](results/text-main.md) compares default outputs, not
  labeled accuracy. The labeled comparison covers 14 pages (12 native-text
  pages including the `forms/` Japanese business form addition, plus 2 OCR scan
  pages) with 66 ink-measured words total, not every character. Phase 1's
  parity evidence (`evidence/phase1-parity`) compares pyropdf's output with
  pylopdf's.
- One machine, one thread, and a 15-document parity corpus (plus 1 Japanese
  business form under `tests/assets/real_world/forms/`).
