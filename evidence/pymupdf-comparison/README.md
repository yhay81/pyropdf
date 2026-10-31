# pyropdf compared with PyMuPDF

The baseline for #355, which tracks the work for pyropdf to do everything
PyMuPDF does with PDF and more (DESIGN.md section 1: pymupdf's capabilities
are the floor). Measured for #354.

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
| `quality.py` | Scores text, reading order, word boxes, and table cells of six labeled pages against `quality/*.json`, for #430; `results/quality.md` holds the scores |
| `gray_probe.py` | Reproduces the engines' different `rewrite_images(set_to_gray=True)` behavior on a synthetic PDF |

```bash
python3 evidence/pymupdf-comparison/run.py
```

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

For #430: does each engine read the right text, in the right order, with
the right boxes? Neither engine is the ground truth. `quality/` labels six
pages, chosen for their kinds of layout: a bill in one column
(bill-hr815 p1), a tax form (f1040 p1), Japanese prose with a header box
(mhlw-doc p1), a wide table (nics p1), a scanned two-column patent with an
OCR layer (patent-us223898 p2), and a LaTeX manual (usrguide p2). Each
label holds:

- **Anchors**: lines in the order a reader reads the Ghostscript
  rendering. An anchor counts as found when the page text holds it after
  NFKC and the removal of all whitespace, since the engines place spaces
  between CJK characters and digits differently. Neighboring found anchors
  count as in order when their positions increase.
- **Words**: six words per page, each found once by both engines with
  boxes that overlap, spread over the page. Their ink boxes are measured
  in Ghostscript renderings at 288 dpi, inside a seed box around both
  engines' boxes, without the rules of tables and forms or the ink of
  neighboring lines (`python quality.py ink`). A word counts as placed when
  a word of the same text covers 90% of its ink box. The mean IoU compares
  the box with the ink, so it measures box conventions, such as font
  ascent and descent, more than errors. The patent's OCR layer has no ink
  of its own, so it has no word labels.
- **Table**: three rows of the nics table (Alabama, Wyoming, and Totals),
  checked against a 150 dpi rendering, and its 25 columns.

The labels were made by an agent from the renderings, and the owner
checked them by eye against the crops of `python quality.py crops DIR`
(`"checked": true`). A changed label is checked again the same way.
`tests/test_text_quality.py` holds pyropdf to the labels.

```bash
python evidence/pymupdf-comparison/quality.py collect pyropdf /tmp/pyropdf.json
target/pymupdf-comparison/pymupdf-env/bin/python evidence/pymupdf-comparison/quality.py collect pymupdf /tmp/mupdf.json
target/pymupdf-comparison/pymupdf-env/bin/python evidence/pymupdf-comparison/quality.py collect pymupdf-sort /tmp/mupdf-sort.json
python evidence/pymupdf-comparison/quality.py score evidence/pymupdf-comparison/results/quality COMMIT /tmp/mupdf.json /tmp/mupdf-sort.json /tmp/pyropdf.json
```

[`results/quality.md`](results/quality.md) holds the scores, measured with
PyMuPDF 2.0 on Python 3.13.16 and pyropdf 0.2.0 at 967fc36 on Ghostscript's
renderings. Both engines find every anchor and place every labeled word,
except where noted below.

| Difference | Kind |
| --- | --- |
| The patent page: pyropdf split the headings above the two columns at the gutter (`UNITED STATES` … `PATENT OFFICE.` after the left column) | pyropdf engine error, fixed by #460; the table shows the fixed scores |
| The patent page: PyMuPDF reads the right column before the left one ends, with or without `sort=True` | PyMuPDF engine error on a scanned page |
| The nics table: PyMuPDF's `find_tables()` finds 17 rows, with the states merged into a few cells; pyropdf finds every labeled row and cell | PyMuPDF engine error |
| mhlw-doc and nics: PyMuPDF's default puts the header box after the body and the first rows before the title | API choice: `get_text()` follows the content stream unless `sort=True`, which reads both pages in order |
| Word IoU 0.71 to 0.90 for both engines | Box conventions: both engines' boxes cover the ink |

## Limits

- Open and full save without edits are not profiled: each row is what a user
  waits for, and whether both engines do equal work for it is not measured.
- The benchmark times text and words, not their quality. The separate
  [main comparison](results/text-main.md) compares default outputs, not
  labeled accuracy. The labeled comparison covers six pages with anchors
  and six words each, not every character. Phase 1's parity evidence (`evidence/phase1-parity`)
  compares pyropdf's output with pylopdf's.
- One machine, one thread, and a 15-document corpus.
