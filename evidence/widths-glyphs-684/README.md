# Bounded width-range expansion (#684)

`widths_glyphs` used to expand every `c_first c_last w` range into a
`BTreeSet`, including glyphs that earlier ranges had already inserted.
100,000 copies of `0 65535 500` require 6,553,600,000 insertion attempts
with that implementation.

The replacement records the partial first and last 64-bit word of each
range, and the exclusive endpoint of its fully covered interior words.
Each starting word keeps only the largest endpoint. A prefix maximum over
those endpoints finds all fully covered words; the edge masks add partial
coverage. Enumerating the set bits returns the same sorted, unique glyphs.

For `n` width entries and `k` returned glyphs, work is O(n + 1,024 + k):
each entry does constant work, the sweep visits at most 1,024 words between
the first and last covered glyph, and each glyph is emitted once. Empty
coverage skips the sweep. The fixed tables occupy 10 KiB on the stack,
independent of input length; the result has at most 65,536 `u32` values.
There is no recursion or input-sized auxiliary collection. Parsing continues even
after full coverage, so a malformed suffix still makes the helper refuse.

The unit tests include 100,000 full ranges, mixed and overlapping ranges,
word boundaries, clipping at glyph 65,535, empty and reversed ranges,
malformed inputs, and property comparisons with individual set insertion.
The tests preserve the helper's existing treatment of ignored width
values and a missing final width rather than changing validation policy.

## Reproduce

From the repository root, in Developer PowerShell on Windows:

```text
python3 evidence/widths-glyphs-684/run.py --rounds 5
```

The script extracts the actual private function from base commit
`89cbba51f8cd` and the current checkout, builds `pyropdf-store` in release,
and compiles both functions in one optimized Rust harness against the real
store's `Value` and `Array`. It does not duplicate either algorithm or
modify the checkout's source. Generated files stay in the ignored
`target/widths-glyphs-684/` directory. No new dependencies are needed.

Each case checks full output equality before timing, alternates execution
order over the rounds, and reports median microseconds per call. Input
construction is excluded; result allocation and destruction are included.
The base is run up to 1,000 full ranges; larger cases measure only the
replacement and check it against all 65,536 glyphs. Those missing baseline
times are not extrapolated. Empty and sparse inputs expose the fixed
tables' cost as well as the overlapping-range improvement.

This is a microbenchmark of the helper, not an end-to-end PDF save claim.

## Recorded result

[Windows x86-64, Rust 1.99.1](windows-x86_64.md), five rounds:

- 1,000 full ranges: 2.313 seconds before, 0.176 milliseconds after.
- 100,000 full ranges: 1.220 milliseconds after, returning all 65,536 glyphs.
- One listed glyph: 0.093 to 0.126 microseconds. Sixteen widely separated
  glyphs: 0.369 to 0.944 microseconds. These small inputs show the fixed
  tables' overhead; they are not claimed to improve.

The source hash in the result identifies the measured function, including
the restriction of the sweep to the first and last covered words.
