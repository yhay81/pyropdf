# What a linear search for the end of inline image data costs hayro-syntax

Evidence for #17 (its third item). [src/main.rs](src/main.rs) times
`TypedIter` over content streams whose inline image is followed by many
`EI`s, at doubling sizes, and
[src/bin/differential.rs](src/bin/differential.rs) prints a digest of
everything `UntypedIter` reads from 20,000,000 generated streams around
inline images. [results.txt](results.txt) is the output of both for hayro's
main (b2f4d68a) and the draft patch (61086912, branch
`linear-inline-image-end`), from:

```bash
evidence/inline-image-end/run.sh inline-image-end-bench <hayro main checkout> <patched hayro checkout>
evidence/inline-image-end/run.sh differential <hayro main checkout> <patched hayro checkout>
```

The crate is outside pyropdf's workspace; `run.sh` points its hayro-syntax
dependency at each checkout. Each time is the median of 5 runs, Apple M2
Pro; a case stops at the first size over 2 s.

## Findings

| content after the image | main | patch |
| --- | --- | --- |
| `(EI) Tj`, the image ends at the first `EI` | 5.9 ms at 1 MiB, linear | 4.6 ms at 1 MiB, linear |
| `EI ` repeated before one binary byte | 3.5 s at 64 KiB | 4.6 ms at 1 MiB, linear |
| `EI ] ` repeated, never content | 3.2 s at 256 KiB, quadratic | 5.8 ms at 1 MiB, linear |
| `EI ( ` repeated before one binary byte | 2.1 s at 128 KiB, quadratic | 4.9 s at 256 KiB, quadratic |

- hayro's #1555 already made the first case linear; pylopdf measured it on
  hayro-syntax 0.8.0, before that change.
- The patch makes late rejection linear: many candidates that a binary byte
  or bytes that are not content reject.
- A candidate that lies within a string as an earlier candidate's scan sees
  it still scans again, and its content check reads the unterminated string
  to the end, so the last case stays quadratic (1.7 times faster than
  main). Hosts must keep a CPU deadline for it.
- Both checkouts print the same digest, so every decision stays the same.
  Changing the patch so that it skips the content check, or so that a
  candidate within a string shares the earlier scan, changes the digest.
