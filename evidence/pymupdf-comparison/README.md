# pyropdf compared with PyMuPDF

The baseline for #355, which tracks the work for pyropdf to do everything
PyMuPDF does with PDF and more (DESIGN.md section 1: pymupdf's capabilities
are the floor). Measured for #354.

| File | Role |
| --- | --- |
| `features.md` | The capabilities of PyMuPDF 2.0 against pyropdf 0.1.0, each gap linked to its issue |
| `bench.py ENGINE OUT FILE...` | Times the shared operations for one engine |
| `run.py` | Installs PyMuPDF 2.0 and pyropdf 0.1.0 from PyPI, each in its own environment under `target/pymupdf-comparison/`, runs `bench.py` with both, and writes `results/` |
| `labels.py` | Writes `results/page-labels.json`, PyMuPDF's `get_label` of every corpus page, from the environment that `run.py` installs; `tests/test_page_labels.py` compares pyropdf with it |
| `profile.py` | Samples pyropdf rendering bill-hr815 and usrguide at 72 and 144 dpi on macOS, and writes `results/profile-<platform>.md`, for #365 |
| `results/darwin-arm64.md` | The measured comparison, with the raw timings in the `.json` beside it |

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

On an Apple M-series machine (`results/darwin-arm64.md`), as corpus totals:

| Operation | pyropdf / PyMuPDF |
| --- | ---: |
| Open | 0.19 |
| Text | 0.79 |
| Words | 0.75 |
| Search | 1.16 |
| Render 72 dpi | 2.65 |
| Render 144 dpi | 3.97 |
| Full save | 0.15 |

pyropdf is faster at text and words, and search is close. Rendering is the gap, worst on bill-hr815 (6 to 8
times) and usrguide (3 to 5 times); #365 closes it.

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
- `take_rgba8(ImageAlphaType::Alpha)` in `pyropdf_engine::render::draw`
  divides every pixel by its alpha, 7 to 20%, although an opaque background
  leaves every pixel opaque.
- The store, the bridge, and opening take 0.2 to 1.2%.

`bench.py` times `to_bytes()` for pyropdf, which encodes a PNG, against
PyMuPDF's raw `samples`. The PNG adds 40 to 90% to pyropdf's time; with the
samples read on both sides, pyropdf is 4.5 to 5.6 times slower than PyMuPDF
on these documents, not 6.4 to 10.6 times.

## Limits

- Open and full save without edits are not profiled: each row is what a user
  waits for, and whether both engines do equal work for it is not measured.
- Text and words are timed, not compared for quality. Phase 1's parity
  evidence (`evidence/phase1-parity`) compares pyropdf's output with pylopdf's.
- One machine, one thread, and a 14-document corpus.
