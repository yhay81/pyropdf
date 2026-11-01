# Budgeted object parsing in hayro-syntax

The local prototype behind issue #509: hayro-syntax charges the bytes it
scans and the buffers it allocates to a caller's `ParseBudget`, so
`Value::parse_with_lengths` honors the operation's memory limit, deadline
and cancellation inside one large object (DESIGN.md F5, F6, sections 4.6
and 9). Nothing here is published upstream; that needs the owner's
approval.

## Files

- `hayro-syntax-0.8.0.patch`: the prototype on the `hayro-syntax-v0.8.0`
  tag, which the store builds against.
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
Medians of the five runs, against `hayro-syntax-v0.8.0`:

| Phase | 0.8.0 | With the patch | Change |
| --- | --- | --- | --- |
| Open | 0.835 ms | 0.896 ms | +7.3% |
| Walk objects | 10.817 ms | 11.686 ms | +8.0% |
| Content operations | 8.209 ms | 8.334 ms | +1.5% |
| All | 19.912 ms | 20.918 ms | +5.1% |

The runs of each build agree within about 1.5%, below the gaps. Content
parsing, the interpreter's hot path, barely changes. Object reading pays
about 8%, most likely for the larger `Reader` and the budget lookups of
lazy reads; reducing that is the first question for an upstream
proposal.
