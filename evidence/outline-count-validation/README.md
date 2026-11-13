# Outline count validation (#795)

An imported outline's nonzero `/Count` cannot prove its descendant count.
The first `is_open` assignment now recounts the outline, including no-op
assignments. Later assignments reuse that verification only if its recorded
object and trailer reads are unchanged. A verified update carries those same
dependencies to its resulting version. Malformed parent links or direct nodes
continue to use the full recount.

## Reproduce

```sh
uv sync --locked
uv run --no-sync python evidence/outline-count-validation/measure.py > evidence/outline-count-validation/results.json
cargo test --locked -p pyropdf-engine --all-features navigation::outline::tests
uv run --no-sync pytest tests/test_outline.py
```

`results.json` was measured on the #795 working tree based on `049d8e02`,
using the release extension built by `uv sync`. Each row has 64 chapters with
an equal number of children. Timings exclude opening, fixture construction,
and handle lookup. Warm measurements disable history retention, exercising
the transfer of verification from a transaction snapshot to its commit.
These are local measurements, not timing thresholds for CI.

| Items | First toggle | Verified toggle | Verified 64-toggle transaction |
| ---: | ---: | ---: | ---: |
| 512 | 644.0 us | 7.8 us | 510.5 us |
| 2,048 | 2,627.5 us | 7.8 us | 511.9 us |

Initial validation scales with the outline size. With a verified tree, reads
and writes follow the affected parent chain; membership and dependency checks
also use sorted sets and the store's version difference.

The Rust invariant test uses 1,024 items at four levels. It asserts three
ancestor reads, exact changed-object sets, and no propagation above a closed
ancestor. A 16 KiB operation budget refuses the initial recount but permits a
verified toggle. Refusal and cancellation preserve the transaction, and a
property test compares repeated cached toggles with full outline replacement.
Python regressions cover inflated/deflated counts, no-ops, save/reopen, raw
count/parent/child-chain edits, undo/redo, rollback, closed ancestors, and
detached items.
