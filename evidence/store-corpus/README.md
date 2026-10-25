# Store corpus properties

The record behind the acceptance criteria of issue #36, for the suite
`crates/pyropdf-store/tests/corpus.rs`:

- `saves_without_edits_reproduce_the_corpus`: on the 14 corpus files, an
  incremental save without edits is the file byte for byte, and a full save
  reads back every indexed object with the same generation and the same
  bytes in the store, and the same value in hayro-syntax's reading of both
  files.
- `random_edits_keep_untouched_bytes_and_read_back_written_values`: 48
  random edit sequences per file, of up to 3 transactions of up to 5 writes
  of random values (including reals that `f32` cannot hold) or frees, at the
  file's object numbers or new ones. Full and incremental saves keep every
  untouched object's bytes and read back every written value, and an
  incremental save keeps the original as its prefix.

## Time

0.8 s for both tests in a debug build on an Apple M2 Pro (2026-10-26), with
the workspace's `[profile.dev.package."*"] opt-level = 2`. CI's Linux runner
runs it as part of `cargo test --workspace`, far within five minutes.

## A deliberate bug

Writing reals through `f32` in `crates/pyropdf-store/src/write/mod.rs`:

```diff
-            Value::Real(real) => self.real(real.value())?,
+            Value::Real(real) => self.real(f64::from(real.value() as f32))?,
```

makes the suite fail at once, with proptest's shrunk case:

```text
test random_edits_keep_untouched_bytes_and_read_back_written_values ... FAILED
 right: `Some(Array(Array { items: [Real(Real { value: 1e-7, lexeme: None })], lexeme: None }))`:
 bill-hr815.pdf full: written object 1
```
