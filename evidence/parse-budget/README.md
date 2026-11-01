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
- `parse_bench.rs`: the timing below, as an example of hayro-syntax.

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
on this dictionary-heavy synthetic input. A measurement on a corpus is
still needed before an upstream proposal.
