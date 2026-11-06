# Document-owned raster workers (#715)

The prototype keeps only worker threads between consecutive page renders.
Each page gets a new scene and releases its geometry, paints and raster
buffers. The document owns an idle pool. Closing the document prevents
in-flight renders from returning a pool to the slot. One process-wide
permit allows at most one accelerated caller, with up to four workers.
Other callers render serially. Batch and annotation rendering keep their
existing serial inner dispatcher.

On the six-core Windows host, closing the first document from a different
thread released all four extra threads. A second open document could then
acquire the permit. Dropping that document released all four again.
[Lifecycle probe](lifetime.py), [worker-only observations](lifetime.json),
and [bounded-dispatch observations](lifetime-bounded.json).

## Proposed dependency changes

These are independent local drafts against linebender/vello
`a76009ef19bc04fa2d9827293e98a49d06121f68`. Each patch also applies
to the packaged vello_cpu 0.3.0 used in the integration trial.

- [#718](https://github.com/yhay81/pyropdf/issues/718) separates the worker
  lifetime from scene storage and makes pool creation fallible.
  [Patch](vello-recycle-workers.patch) and [draft PR text](worker-pr.md).
- [#719](https://github.com/yhay81/pyropdf/issues/719) applies backpressure
  to submitted but unrecorded raster batches. The bound includes ordered
  results waiting for earlier work. This limits batch count; individual
  paths and finished scenes still have variable size.
  [Patch](vello-bounded-batches.patch) and [draft PR text](backpressure-pr.md).

Each draft passed the vello_cpu multithreaded library tests and Clippy with
warnings denied on its own upstream branch. Both together passed 104
library tests. Disabling the wait at the in-flight limit made the new
dispatcher regression test fail. The workers branch also passed a serial
`--no-default-features --features libm,u8_pipeline` build. A human must
review and own the drafts before requesting upstream review.

## Integration measurements

The [integration patch](pyropdf-integration.patch) targets pyropdf main
`98fd89220ee2fc2ba3d43a54b63b04b28102b3f7`. It requests acceleration for
consecutive reads of multi-page documents with raster area from 262,144 to
4,194,304 pixels. A first page, random access, a small raster, an occupied
permit or unavailable workers uses the serial path. Native acceleration is
enabled on Linux, macOS and Windows. WebAssembly uses the serial path.
The dependency path overrides in this patch are for local reproduction.

The ordinary baseline measured 2.07 and 1.75 times MuPDF in the official
runner. With document-owned workers, the first run measured 1.10 and 0.97
and the repeat measured 1.13 and 1.08. These initial worker-only runs
precede the added queue bound, final synchronization of unused contexts,
and the cancellation fix tracked in #717. Individual documents still
exceeded 1.5, including bill-hr815 at 72 dpi on the repeat.
[Baseline](baseline-vs-mupdf.md), [first candidate](candidate-vs-mupdf.md),
and [repeat](candidate-repeat-vs-mupdf.md).

In the balanced comparison of six interpreters, the bounded-dispatch
variant's best ratios were 1.266 and 1.091. Its median ratios were 2.279
and 1.102. At 72 dpi the bounded variant's median total was 1.924 times
the worker-only variant's, although its best total was 1.081 times.
At 144 dpi the ratios between those variants were 0.951 and 0.948.
The shared host exhibited large timing variation. Keep both statistics
when assessing the cost of the queue bound. [Raw samples](interleaved.json).

A direct worker-only versus bounded-dispatch comparison repeated the same
five alternating raw renders per document and DPI in each starting order.
The best bounded/unbounded ratios were 1.0483 and 1.0156 at 72 dpi,
1.0002 and 1.0414 at 144 dpi. Median ratios were 1.0140 and 0.9839,
1.0050 and 1.0217 respectively. [Worker-only first](pressure-baseline-first.json)
and [bounded first](pressure-candidate-first.json).

The baseline and both initial candidates matched all 2,340 sequence
comparisons, including 1,872 fixed page/layout/resolution cases, mixed DPI
and layouts, and page revisits. With two concurrent callers rendering
bill-hr815 at 144 dpi, median peak RSS was 75.4 MiB for the baseline,
77.9 MiB for worker-only, and 78.1 MiB for bounded dispatch. The RSS
probe uses three fresh processes per case and releases each page's raw
buffer immediately. [Memory records](memory.json) and [bounded pixel
results](pixels-bounded.json).

The final combined wheel also waits for pending context initialization
before returning an unused pool, and polls cancellation on both sides of
the page and annotation raster call (#717). Its SHA-256 is
`adf07f6b83c6f61dcebbf6bee8859b252e036d52c305259f6115147ac6e59dbd`.
It matched all 2,340 pixel checks. Its thread counts were 4 at startup,
8 with the first pool, 8 after warming a second document, 4 after closing
the first from another thread, 8 once the second acquired a pool and 4
after dropping the second. The official corpus ratios were 1.03 and 1.09.
Usrguide measured 1.63 at 72 dpi. All 15 document totals were below 1.5
at 144 dpi in this run. [Final official comparison](final-vs-mupdf.md),
[pixel checks](pixels-final.json) and [lifetime observations](lifetime-final.json).

The integration test for worker startup failure injects a fallible factory,
acquires the same permit after that failure, checks a concurrent caller
selects serial rendering, closes the session on another thread, and returns
the in-flight lease. The closed slot discards it and releases the permit.
The public tests exercise a refused raster size, a page rotation edit and
cancellation after warming the document's caches. The existing ordinary
wheel failed the warmed-cache cancellation case.

## Reproduce

Place clean `product` and `vello_cpu` source trees side by side. In the
product tree apply the integration patch, then apply each Vello patch at
their parent (its paths begin with `vello_cpu/`). Build using the pinned
toolchain and `maturin build --release --locked`. For the upstream tests,
apply each Vello patch at the upstream commit above and run
`cargo test --locked -p vello_cpu --features multithreading --lib` and
`cargo clippy --locked -p vello_cpu --features multithreading --lib --tests -- -D warnings`.

Use the benchmark scripts at pyropdf `66c3578a76dedca1c99c222fe754737a8ad60e10`
and the 1,872 pixel hashes at `019449775f80ca8e08bf78eeb46f79bc46851412`.
Compare fresh release-wheel environments with `run.py`, `render_matrix.py`,
`sequence_pixels.py` and `render_memory.py` as documented in the
[earlier experiment](https://github.com/yhay81/pyropdf/blob/66c3578a76dedca1c99c222fe754737a8ad60e10/evidence/pymupdf-comparison/results/reused-workers/README.md).
Run `lifetime.py` using the candidate interpreter on Windows with
PowerShell 7. #715 requires released dependency support and validation
of the eventual shipping artifact. #365 retains its performance gate.
