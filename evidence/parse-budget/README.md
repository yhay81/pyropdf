# Budgeted object parsing in hayro-syntax

The local prototype behind issue #509: hayro-syntax charges the bytes it
scans and the buffers it allocates to a caller's `ParseBudget`, so
`Value::parse_with_lengths` honors the operation's memory limit, deadline
and cancellation inside one large object (DESIGN.md F5, F6, sections 4.6
and 9). Nothing here is published upstream; that needs the owner's
approval.

## Files

- `hayro-syntax-0.8.0.patch`: the prototype on the `hayro-syntax-v0.8.0`
  tag, which the store builds against, as two commits: the budget, then
  holding it by a thin reference.
- `hayro-syntax-main.patch`: the same change on hayro `main` at
  `8ac3a6dd`, the base an upstream pull request would use.
- `parse_bench.rs`: the synthetic timing below, as an example of
  hayro-syntax.
- `corpus_bench.rs` and `corpus-results.txt`: the corpus timing below.

## Reproducing the store's tests

Apply the 0.8.0 patch to a hayro checkout at `hayro-syntax-v0.8.0`, then
add to this workspace's `Cargo.toml`, without committing it:

```toml
[patch.crates-io]
hayro-syntax = { path = "<hayro checkout>/hayro-syntax" }
```

`cargo test --workspace --all-features` and `uv run pytest` then run with
the prototype. hayro `main` cannot replace 0.8.0 this way: its object
numbers are `u32` (#18), which the store's tests do not build against yet.

## Results

On the 0.8.0 base: hayro-syntax's 9 budget tests pass; its other 201 tests
pass, and two tests fail there with and without the patch because the tag
lacks their asset files. With the patch applied to the store, its two new
tests pass (a 100,000-entry dictionary is refused with `MemorySize` under
limits of 0 B, 1 KiB and 1 MiB, and the meter returns to 0; budgeted and
unbudgeted parses agree), and `cargo test --workspace --all-features`
passes.

Cost for callers without a budget (`parse_bench.rs` copied into
`hayro-syntax/examples/`, `cargo run --release`, best of 15, aarch64 macOS,
Rust 1.99.1, on hayro `main` at `8ac3a6dd`):

| Build | Time |
| --- | --- |
| hayro `main` | 50.6 to 52.3 ms |
| with the patch | 55.2 ms (three runs) |

The spread between runs was about 3 ms, so the hooks cost roughly 5 to 10%
on this dictionary-heavy synthetic input.

On the corpus (`tests/assets/real_world`, 15 files), `corpus_bench.rs`
times three phases without a budget: opening each document, walking every
object it defines through lazy reads, and reading the typed operations of
every page's content. Each run takes the best of 50 rounds per document
and phase; the five runs of each build alternated (`corpus-results.txt`).
Against `hayro-syntax-v0.8.0`, the first commit alone (medians of five
alternating runs) and both commits (medians of three alternating pairs,
each a median of five runs) cost:

| Phase | 0.8.0 | First commit | Change | 0.8.0 | Both commits | Change |
| --- | --- | --- | --- | --- | --- | --- |
| Open | 0.835 ms | 0.896 ms | +7.3% | 0.850 ms | 0.964 ms | +13.4% |
| Walk objects | 10.817 ms | 11.686 ms | +8.0% | 11.066 ms | 11.559 ms | +4.5% |
| Content operations | 8.209 ms | 8.334 ms | +1.5% | 8.379 ms | 8.519 ms | +1.7% |
| All | 19.912 ms | 20.918 ms | +5.1% | 20.257 ms | 21.040 ms | +3.9% |

The runs of each build agree within about 1.5%, below the gaps except
for content. Content parsing, the interpreter's hot path, barely changes.
The second commit holds the budget by a thin `BudgetRef`, keeps it in the
shared context data instead of every `Reader`'s context, and stops looking
it up on every read; that halves the cost of walking objects. Opening a
document costs 0.11 ms more across the corpus, which the second commit did
not reduce; the remaining cost is the charge check on each byte the reader
advances and the larger `Reader`.
