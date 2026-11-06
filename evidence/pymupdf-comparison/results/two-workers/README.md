# Two raster workers per page

A local release wheel enabled vello_cpu 0.3.0's existing multithreaded
dispatcher and requested two workers for each pyropdf raster target. It
matched all 1,872 baseline pixel cases. Its first corpus comparison met
the 1.5 target in aggregate, but a repeat exceeded it at both resolutions.
The candidate is an experiment. #365 stays open.

## Full corpus

The issue's runner opens a fresh document per sample, copies raw pixels,
and sums each document's best of five. These are three separate full runs
on Python 3.13.17 and Windows amd64, in the order shown.

| Wheel | 72 dpi, MuPDF / pyropdf ms | Ratio | 144 dpi, MuPDF / pyropdf ms | Ratio |
| --- | ---: | ---: | ---: | ---: |
| Two workers | 740.7 / 880.1 | 1.19 | 1730.5 / 1732.7 | 1.00 |
| Ordinary baseline | 554.3 / 1049.1 | 1.89 | 1073.2 / 1942.5 | 1.81 |
| Two workers, repeat | 543.4 / 840.3 | 1.55 | 1050.0 / 1713.4 | 1.63 |

The MuPDF timings varied more than the candidate timings. Even in the first
run bill-hr815 alone was 1.72 times MuPDF at 72 dpi. The repeat put that
document at 2.47 and 2.34 times MuPDF. The official runner measures all
pages of each document together. These totals cannot establish a per-page
upper bound. [Initial candidate](candidate-vs-mupdf.md),
[baseline](baseline-vs-mupdf.md), and [candidate repeat](candidate-repeat-vs-mupdf.md).
Their adjacent JSON files contain the raw results and wheel hashes.

We also alternated the baseline and candidate for five samples per document
and DPI, then reversed the starting order. At 72 dpi the best totals gave
candidate/baseline ratios of 1.026 and 0.675. At 144 dpi they gave 0.907
and 0.724, but the median totals were slower in both orders (1.020 and
1.176). This shared workstation had substantial timing variability.
[Baseline-first samples](baseline-first.json) and
[candidate-first samples](candidate-first.json).

## Correctness and peak memory

The [pixel comparison](pixels.json) verifies dimensions, channel count and
the SHA-256 of raw pixels on 156 pages at two resolutions and six layouts.
Both builds use the public unhinted, uncached glyph configuration.

The [memory worker](../../render_memory.py) uses a fresh process for each
measurement. Each caller opens an independent document, synchronizes with
the other caller before each page, renders all pages, copies the raw RGB
bytes and releases each copy immediately. This isolates renderer memory
from the comparison runner's list of all output buffers. Each pair below
is ordinary baseline / two-worker candidate median peak RSS in MiB across
three processes. All page and byte counts agree. [Raw RSS records](memory.json).

| Document | DPI | One caller | Two simultaneous callers |
| --- | ---: | ---: | ---: |
| bill-hr815 | 72 | 43.7 / 46.1 | 51.1 / 59.9 |
| bill-hr815 | 144 | 58.9 / 61.0 | 75.7 / 86.5 |
| usrguide | 72 | 44.0 / 45.5 | 52.6 / 58.2 |
| usrguide | 144 | 54.4 / 55.8 | 67.7 / 72.5 |

Process RSS measures observed resident pages. It excludes uncommitted
stack reservations and cannot prove a memory bound on untrusted PDFs.
In particular, pyropdf's batch renderer can already start up to 64 page
workers. Any production design must preserve its thread and memory caps.

## Reproduction and implementation limits

Prepare sibling directories `product` (pyropdf
`13ea858a096493bb49e681c450a2a44302ecfaa7`) and `vello_cpu` (the unpacked
crates.io 0.3.0 package). Apply [experiment.patch](experiment.patch) at their
parent and build the product with its pinned toolchain and
`maturin build --release --locked`. The patch explicitly keeps the
dependency's default worker count at zero, then requests two only in
pyropdf's shared raster draw function. Hayro also sets zero workers when
deriving contexts for masks and other nested renders. This experiment
does not establish a need to fork vello for production.

Use the two-wheel comparison and baseline pixel hashes from
pyropdf commit `019449775f80ca8e08bf78eeb46f79bc46851412` as described in
the [prepared-outline reproduction](../prepared-outline/README.md#reproduction).
Run this PR's `run.py --pyropdf-wheel <wheel>` separately for each official
comparison. For RSS, invoke the Python installed with each wheel three
times per combination, e.g.
`python evidence/pymupdf-comparison/render_memory.py bill-hr815 144 2`.

The dispatcher constructs a Rayon pool for each new context and unwraps
the pool construction result. Thread creation failure therefore needs an
explicit fallback or typed refusal before this could become the default.
The same upstream constructor still unwraps at
[vello a76009e](https://github.com/linebender/vello/blob/a76009ef19bc04fa2d9827293e98a49d06121f68/vello_cpu/src/dispatch/multi_threaded.rs).
The next experiment tests amortizing setup with `reset_and_resize` on
consecutive page calls, while keeping batch rendering on its own worker
budget. It must separately test pixel identity after reuse, timing, and
resource retention.
