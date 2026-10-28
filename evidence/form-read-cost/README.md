# Form read cost

The measurement behind issue #192: one walk of the field tree reads every
terminal field, and it is memoized with the objects that it read (DESIGN.md
section 5, F4), so reading every field's value through `Field` handles
costs about one walk.

## Workload

pylopdf's `evidence/pylopdf-baseline/field_read_cost.py`: forms of 100,
400, and 1,600 text fields without values, 50 to a page. `measure.py`
builds them from raw objects, since adding widgets waits for issue #193.
Each row is the median of 7 rounds, each on a newly opened document:

- One walk: the first `form_fields` call, which walks the tree.
- List and read: `list(doc.form.values())` and then every field's `value`,
  the walk included.
- Read after listing: only the values, as the pylopdf script times them.
- Walks: the memo's computations after a round's reads.

## Running

```sh
uv run python evidence/form-read-cost/measure.py
```

## Results

`results/review-macos-arm64.md` records the reviewed implementation. Listing
and reading 1,600 fields took 7.77 ms against 4.72 ms for one walk, and the
memo computed that walk once. The value reads alone took 0.85 ms after
listing. This is one measurement on macOS arm64; the script records the
workload and repeats each row seven times. `results/macos-arm64.md` retains
the original PR measurement. The pylopdf baseline measured 2.18 to 2.24 s
for the value reads in DESIGN.md section 2.
