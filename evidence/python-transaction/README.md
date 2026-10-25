# A failing transaction

The measurement behind the first acceptance criterion of issue #34: an
exception inside `doc.transaction()` leaves the document as it was without
copying it (DESIGN.md sections 4.1 and 10).

## Workload

Flat documents of 110, 440, and 1,760 pages, each with 20 KiB of content, so
that a copy of the document, 35 MiB at 1,760 pages, would show in the peak
resident memory. Each of 201 rounds opens a transaction, reads a page,
rotates it, and raises; the transaction discards the rotation. Each size runs
in a process of its own, since the peak never falls.

## Running

```sh
uv run python evidence/python-transaction/measure.py
```

## Results

`results/macos-arm64.md`: the peak grows by about 0.1 MiB over all 201
failures at every size, and a failure takes 2.5 µs whatever the size. The
transaction's draft shares the committed version; the rotation copies one
chunk and its branches, and the exception drops them.

A first run took 310 µs at 1,760 pages: a read after a write inside one
transaction replaced the memoized page index of the committed version with
that of a short-lived snapshot. The binding now memoizes the index of
transaction snapshots apart (`draft_pages` in `crates/pyropdf-python`).
