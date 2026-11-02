# Object parser budget probe (#509)

Run `bash evidence/parser-budget-509/run.sh` from this checkout. The script
compares the unchanged `main` commit `7edb085d8da316597df823a73d4ca793c8cf9309`
with the current worktree, using the same probe, lockfile, and release profile.
It creates a temporary baseline checkout from `git archive` and removes it on
exit. Set `CARGO_TARGET_DIR` to reuse a build cache if desired.

The probe constructs a dictionary with 100,000 distinct keys before measuring.
It runs `Value::parse` with a zero-byte memory budget, then with a token already
cancelled. A global allocator counts requested bytes in allocations and
reallocations during each call. The total includes every allocation in the
call and adds each reallocation's full request, so it is neither live nor peak
memory, and it does not identify the allocating component.

## Sample

macOS Darwin 27.0.0 arm64, Rust 1.99.1, `hayro-syntax` 0.8.0. Each input was
988,895 bytes. The table shows cumulative requested allocation bytes; timings
are deliberately omitted because these are correctness probes, not benchmarks.

| Call | Baseline | Current | Result |
| --- | ---: | ---: | --- |
| `MemorySize=0` | 10,747,440 | 10,747,440 | `memory_size` refusal; meter remained 0 |
| Already cancelled | 10,747,440 | 0 | `cancelled` refusal |

The current pre-check prevents an already-cancelled call from starting the
parser. It **does not** constrain or interrupt allocations and work while
hayro-syntax parses: the zero-memory case remains an explicit counterexample.
Acceptance of #509 still requires a bounded upstream parser, fallible growth,
propagation through lazy reads, and adoption of a release. The probe's counts
may vary by allocator, platform, dependency versions, and compiler.
