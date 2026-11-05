# Binding collection budgets (#677)

The shared collection lint found 548 growth sites in the binding at the
starting revision `2fe716e4c1ab6ba2c393890604b2e463dee30af3`. Fixed dictionary
keys, fixed-size geometry and Arrow schemas, pixel-count bounds, and copies
whose sizes are bounded by already-budgeted results now state those bounds
at their lint expectations. Helpers that already charged and reserved
capacity state that invariant explicitly. PyO3 argument conversion before
the binding body remains outside this issue's scope.

The audit also found allocations that needed operation-budget accounting:

- Character columns reserve their exact row and UTF-8 byte counts through
  `BudgetVec`, including on a cached layout. The text binding passes the
  same operation budget through to the column constructor.
- Type 3 name lookups reserve their result slots through the document budget.
- Nested transactions check the whole savepoint stack against the memory
  limit and charge new slots before fallible reservation. A fresh budget for
  each ordinary transaction cannot bypass the total nesting bound.
- Tagged Python arrays, dictionary entries, and borrowed bytes charge before
  conversion; copied object values use the store's budgeted copy operation.
- Signature-field traversal uses budgeted pending, visited, and result
  vectors, including inherited names that a shared child could duplicate.
- The TIFF palette workaround uses a fallible budgeted copy. The separate
  public encoded-input limit and file/buffer loading contract is tracked in
  [#697](https://github.com/yhay81/pyropdf/issues/697).

`tests/test_binding_collection_budgets.py` exercises `LimitError` with code
`memory_size`, cached results, failed-edit recovery, and both kinds of nested
transactions. The prior extension accepted the cached-column conversion,
1,000 empty Type 3 lookups, nested transactions with a one-byte memory limit,
the 1,000-null conversion with 20,000 bytes left, and a warmed signature walk
with 5,000 bytes left; the patched extension refuses those cases.

## Measurement

Run from the repository root against each release-built extension:

```powershell
uv sync --locked
.venv/Scripts/python.exe evidence/binding-collection-budgets/measure.py
```

The same machine and Python 3.14.8 were used for the baseline and patched
binding. `before.json` and `after.json` retain the raw outputs. Each time is
the median of nine batches of 100 calls. The first page of `bill-hr815.pdf`
has 1,967 characters and 145,614 exposed column bytes in both runs; layout
interpretation is warmed before timing, so the measurement includes the
binding's column reconstruction. The Type 3 case looks up 1,000 missing names
with an empty font list, isolating the result-vector path.

| Operation | Before, microseconds | After, microseconds |
| --- | ---: | ---: |
| Warm character columns | 46.566 | 40.844 |
| 1,000 Type 3 name lookups | 102.388 | 65.824 |

These are local checks for the cost of the new accounting, not a general
speedup claim. They do not measure peak resident memory, other machines, or
cold interpretation. Pixel transforms have only lint annotations and retain
their existing algorithms and fixed pixel bounds.
