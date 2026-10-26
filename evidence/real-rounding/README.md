# What correctly rounded reals cost hayro-syntax

Evidence for #45. [src/main.rs](src/main.rs) times `Number::from_bytes` and
a content stream of the same numbers (`ns/op'd`: per operand, through
`UntypedIter`), and counts the reals that don't read back as the `f64` they
were written from. [results.txt](results.txt) is its output for hayro's
main (a553afcb) and the draft patch (a9560d8e, branch
`correct-real-rounding`), from:

```bash
evidence/real-rounding/run.sh <hayro main checkout> <patched hayro checkout>
```

The crate is outside pyropdf's workspace; `run.sh` points its hayro-syntax
dependency at each checkout. Each figure is the median of 15 runs over
1,000,000 numbers.

## Findings

- Integers and the short decimals that PDF writers emit (`{:.3}`) keep the
  fast path and cost the same, within noise.
- The patch reads every shortest decimal back exactly: 0% misread, from
  10.33% of uniform values in ±1e6 and 44.09% of random finite bit patterns.
- A shortest decimal of 16 or 17 digits now takes the exact parser: 15 to
  31 ns per number for uniform values in ±1e6, and 402 to 483 ns for random
  bit patterns, whose decimals run to hundreds of digits.
