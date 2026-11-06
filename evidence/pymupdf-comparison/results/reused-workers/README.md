# Reusing a raster context between consecutive pages

Four workers in a reused context brought aggregate rendering to 1.04
times MuPDF at 72 dpi and 1.05 at 144 dpi on this Windows six-core,
twelve-logical-processor machine. The experiment warrants a production
implementation. Its thread-local cache keeps workers alive after a
partial read followed by document close, and individual documents still
exceed 1.5. #365 therefore remains open.

The [experimental patch](experiment.patch) takes one context from the
current document's thread-local cache when reading the next page. It
resets and resizes the context, renders with fresh image resources, and
returns it to the slot only when there is another page. Discontinuous
access replaces the context. Batch rendering uses its existing page workers
and requests zero internal raster workers. A process-local experiment
switch selects zero, two or four workers in the same wheel.

## Rendering and pixels

| Reused workers | Official 72 dpi | Official 144 dpi |
| ---: | ---: | ---: |
| 0 | 1.63 | 1.69 |
| 2 | 1.36 | 1.39 |
| 4 | 1.04 | 1.05 |

Each entry is the corpus sum of per-document best-of-five times divided
by the corresponding MuPDF sum. See the tables and accompanying JSON for
[zero](workers-0-vs-mupdf.md), [two](workers-2-vs-mupdf.md) and
[four](workers-4-vs-mupdf.md) workers. With four workers, bill-hr815
is 1.54 at 72 dpi. The two short fixtures pdfium-ocg-invisible and
pdfium-type3 exceed 1.5 at both resolutions. Document totals cannot
establish individual-page timings.

To check for bias between separate full runs, a second driver alternates
six isolated interpreters on each document and resolution using the exact
raw-pixel operations from bench.py. Each artifact occupies each position
once. Each ordered pair of different artifacts appears consecutively once
across the six rows. Worker setup and opening and closing documents are
outside the timer. The driver verifies each pyropdf extension against
its wheel. The table gives ratios to MuPDF from that same measurement.
[All samples and orders](interleaved.json).

| Configuration | 72 best | 72 median | 144 best | 144 median |
| --- | ---: | ---: | ---: | ---: |
| Ordinary baseline | 1.873 | 1.893 | 1.704 | 1.745 |
| New context, two workers | 1.321 | 1.361 | 1.376 | 1.353 |
| Reuse, zero workers | 1.853 | 1.800 | 1.641 | 1.693 |
| Reuse, two workers | 1.175 | 1.261 | 1.301 | 1.272 |
| Reuse, four workers | 1.038 | 1.049 | 1.030 | 1.013 |

The baseline and all three reuse settings each passed 2,340 pixel
comparisons. The 1,872 fixed-layout cases render all pages consecutively
so multi-page documents actually reuse a context. Another 156 checks
vary resolution and layout on adjacent pages, and 312 revisit pages out
of order. Every dimension, channel count and raw hash matched the original
corpus reference. [Pixel verification](sequence-pixels.json).

## Memory and lifetime

Peak working-set medians in MiB for the four-worker variant, one / two
simultaneous document callers:

| Document | 72 dpi | 144 dpi |
| --- | ---: | ---: |
| bill-hr815 | 45.8 / 58.0 | 63.1 / 88.7 |
| usrguide | 46.2 / 58.3 | 57.6 / 75.6 |

The ordinary bill-hr815 baseline at 144 dpi was 58.9 / 75.7 MiB. Each
case uses three fresh processes and releases page bytes immediately.
See [all reuse samples](memory.json) and the
[baseline measurements](../two-workers/memory.json).

On a long-lived calling thread we also opened bill-hr815, rendered its
first two pages, closed it, collected Python garbage, and rendered a
different single-page document. The four measured process thread counts
were 4, 8, 8, 4 for four workers and 4, 6, 6, 4 for two. The ordinary
baseline and fresh-context variant stayed at 4 at every observation point.
This confirms that the thread-local slot retains workers after close until
it is replaced. [Lifecycle observations](retention.json).

A production version needs a bounded document-owned lifetime, a bound on
total live workers across concurrent callers, a serial path for small
jobs and unsupported targets, and recoverable pool startup failure. It
must release scratch on close/drop and preserve batch worker limits.
Peak RSS on these two files cannot establish those properties.

## Reproduce

Use the same source pair and locked build procedure as the
[two-worker trial](../two-workers/README.md#reproduction-and-implementation-limits),
applying this directory's complete patch to clean copies. Its wheel
SHA-256 is `5cf94c7ad4e49d92432bf12ab9f6f97feb24363a82198d22093fb9729b83a16d`.
Set `PYROPDF_PROBE_THREADS` to 0, 2 or 4 before starting the interpreter.
Run this branch's `run.py --pyropdf-wheel <wheel>` for each setting.
Run `sequence_pixels.py <reference-json> <output-json>` with its interpreter
and the 1,872 baseline hashes recorded in #711. Use `render_memory.py` as
in the two-worker trial; run `worker_retention.py` on Windows with PowerShell 7.

For `render_matrix.py <manifest-json> <output-json>` supply six records in
order: MuPDF, baseline, fresh-2, reuse-0, reuse-2, reuse-4. Each has
`name`, `engine` (`pymupdf` or `pyropdf`) and `python` (interpreter path).
Each pyropdf record also has `wheel` (artifact path); set `threads` to
0, 2, or 4 for the three reuse records. The committed JSON identifies the
exact loaded artifacts and stores all six balanced sample orders.
