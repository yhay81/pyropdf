# Multi-paragraph bidi allocation probe (#493)

PR #483 calls `BidiInfo::visual_runs` once per paragraph. In unicode-bidi
0.3.18, each call clones the levels vector for the **entire** input, so input
containing many paragraphs causes quadratic cumulative allocation and copying.
This probe isolates those calls; it does not run the full pyropdf pipeline.

Run from this directory with the repository's pinned Rust toolchain:

```sh
cargo run --locked --release --bin pyropdf-bidi-probe
cargo run --locked --release --bin check
```

`pyropdf-bidi-probe` creates `BidiInfo` before starting the allocator counter.
For each input it reports the total bytes requested from the allocator while
running `visual_runs` for every paragraph. This is cumulative allocation, not
peak live memory, RSS, or an operation-budget counter. It also measures an
alternative which retains the same already-created `BidiInfo`, constructs a
`ParagraphBidiInfo` from each paragraph slice and its resolved base level, and
calls that object's `visual_runs`. Both measurements include other allocations
inside those calls. Results depend on dependency version and allocator behavior.

`check` is a deterministic comparison of the levels and visual run ranges for
20,000 generated strings, including Arabic, Hebrew, combining marks, paragraph
separators and bidi controls. It compares the alternative to `BidiInfo` on each
paragraph. This is useful regression evidence, not proof of UAX #9 equivalence.

## Observations

Measured on macOS, aarch64-apple-darwin, rustc 1.99.1, unicode-bidi 0.3.18.
Each input is `"a\n".repeat(paragraphs)`. Time is deliberately omitted because
this is an allocation experiment, not a representative benchmark.

| Input bytes | Paragraphs | Current cumulative bytes | Alternative cumulative bytes |
| ---: | ---: | ---: | ---: |
| 1,000 | 500 | 532,000 | 36,000 |
| 2,000 | 1,000 | 2,064,000 | 72,000 |
| 4,000 | 2,000 | 8,128,000 | 144,000 |
| 8,000 | 4,000 | 32,256,000 | 288,000 |
| 16,000 | 8,000 | 128,512,000 | 576,000 |

The comparison succeeded for 20,000 inputs and 190,826 paragraphs. Neither
approach is a fix for #493: bidi processing and allocation still cannot receive
the caller's operation budget, cannot refuse allocations fallibly, and cannot
poll cancellation throughout its work. The alternative also has not been
integrated with pyropdf or checked against the official Unicode conformance
suite. A harfrust fix alone would not cover bidi processing.
