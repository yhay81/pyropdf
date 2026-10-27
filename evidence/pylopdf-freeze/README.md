# pylopdf freeze commit

The survey behind the recommendation in #95: which pylopdf commit becomes
the freeze commit of [DESIGN.md](../../DESIGN.md) section 14, the reference
for differential tests.

```bash
python survey.py PYLOPDF_CHECKOUT CANDIDATE [BASE]
```

`BASE` defaults to `036be111cafe77755db4bb2be17bc70a8f2f9d82`, the commit
that the ports so far name (#108 and #109). The script fetches the
checkout's origin and reads GitHub with `gh`; it changes nothing else.

## Results on 2026-10-27

Candidate `ebb01d3c4c742046e8ce7fd72957cd15a2933321`, pylopdf's main
since 2026-10-25 (yhay81/pylopdf#447). The owner chose it as the freeze
commit on 2026-10-27 (#95).

- **Tested.** pylopdf's CI runs on pull requests only. Each of the five
  commits from the base to the candidate (yhay81/pylopdf#443 to #447) has
  the tree of its pull request's head, on which all 10 test and audit
  checks passed (Linux, macOS, Windows, Python 3.11 and 3.15,
  free-threaded 3.14t and 3.15t, Pyodide, RustSec), plus a neutral
  ProofLock.
- **No drift in ported files.** None of the eight pylopdf files that the
  ports name changed from the base to the candidate.
- **In flight.** Three open pull requests, stacked, for the Rewriting
  milestone (yhay81/pylopdf#333): #430 recolor (+1,438/-13), #431 image
  rewriting (+368/-144), and #441 text replacement (+997/-689). Each is
  mergeable with green checks and has no review since 2026-10-24.
- **Unreleased.** The latest release is v0.15.0 (2026-10-20); the
  candidate holds 128 changelog fragments.
- **Known defect.** The latest fuzz run (2026-10-24, on the base) failed:
  `Page.operations` raised `ValueError: write an inline image as
  InlineImage(dictionary, data)`. The core tags a `BI` as an inline image
  only when its last operand is a stream; for a malformed inline image it
  returns a plain `BI` operation, which `Operation` refuses. The path
  (`src/pylopdf/_operations.py`, `rust/src/content/syntax.rs`, and
  `rust/src/document/replacement.rs`) is unchanged at the candidate, and
  the run's `pylopdf-fuzz-reproducer` artifact holds the input.

The defect came over with the content syntax port (#113). On pyropdf's
main at `fb9c761`:

```python
>>> pyropdf.Operations.parse(b"BI /W 1 /H 1 ID \x00 Q")
ValueError: write an inline image as InlineImage(dictionary, data)
```
