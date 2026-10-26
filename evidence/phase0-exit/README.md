# Phase 0 exit criteria

The check of each exit criterion of the Phase 0 tracker
([#2](https://github.com/yhay81/pyropdf/issues/2)) against the evidence that
the merged work left, rerun on 2026-10-27 on an Apple M2 Pro at `main`
bbdd9c6. This record links the evidence; it does not repeat it.

| Criterion | State | Evidence |
| --- | --- | --- |
| Without edits, saves reproduce every corpus document: incremental saves byte for byte, full saves in every value | Holds | [store-corpus](../store-corpus/README.md), [save](../save/README.md) |
| An edit followed by a page read serializes and parses only the objects the edit wrote, apart from hayro's re-read of the cross-reference data and the page tree, and the benchmark publishes its cost at 110, 440, and 1,760 pages | Holds by design and measured in time; no count test | [edit-render](../edit-render/README.md) |
| A failed transaction leaves the current version unchanged without copying the document | Holds | [python-transaction](../python-transaction/README.md) |
| The fuzz targets run in CI | Hold; the weekly run on `main` found a defect, [#88](https://github.com/yhay81/pyropdf/issues/88) | `.github/workflows/fuzz.yml` |
| I2 is released, or releases withhold the untrusted-input claim | Open | [#15](https://github.com/yhay81/pyropdf/issues/15) |

## Rerun

```sh
cargo test -p pyropdf-store --test corpus
cargo test -p pyropdf-store save::
uv run python evidence/python-transaction/measure.py
```

- `corpus`: 2 passed in 0.75 s: the 14 corpus files save without edits
  byte for byte incrementally and value for value in full, and 48 random
  edit sequences per file keep every untouched object's bytes.
- `save::`: 11 passed, 1 ignored (the qpdf check, which needs qpdf).
- `python-transaction`: over 201 failed transactions, the peak resident
  memory grows by 0.09, 0.14, and 0.06 MiB at 110, 440, and 1,760 pages
  (files of 2.2, 8.8, and 35.2 MiB), and a failure takes 2.5 to 2.6 µs at
  every size: the same as `results/macos-arm64.md` of that directory.

## The edit-and-read criterion

The edit-render benchmark of 2026-10-26 publishes the cost at the three
sizes. Per page at 1,760 pages, the transaction takes 7 µs and the render
2.5 ms, and neither grows with the document. What grows is the exception
that the criterion names and the page walk:

- the bridge's bytes (0.4 ms), which are the original file and a
  cross-reference stream with one row per object number, and hayro's re-read
  of them (2.9 ms): the cross-reference data and the page tree;
- the page index walk (0.6 ms), which reads the page tree nodes through the
  version. The store parses an object on its first read and shares the
  value with every later read and version (`crates/pyropdf-store/src/version/mod.rs`),
  so the walk reads values that are already parsed; it parses only the page
  node that the rotation wrote.

No test counts the parses, so this half of the criterion rests on that
design and on the timings, not on a count.

## The fuzz finding

The weekly run on 2026-10-26
([run 40265238695](https://github.com/yhay81/pyropdf/actions/runs/40265238695))
timed out in the `bridge` target. A 220 KB input with one object numbered
160,614,652 makes the bridge write 160 million cross-reference rows: 4.2 s
and 2.6 GB in a release build. [#88](https://github.com/yhay81/pyropdf/issues/88)
tracks the fix. The targets run as the criterion asks; the defect they found
belongs to the bridge, and Phase 0 should not close with it open.
