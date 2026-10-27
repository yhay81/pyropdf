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

`results/macos-arm64.md`: listing and reading 1,600 fields takes 5.7 ms
against 3.8 ms for one walk, and the memo computes the walk once. The
values alone take 0.64 ms after listing, where pylopdf took 2.18 to 2.24 s
because every handle access walked the form again (DESIGN.md section 2).
The cost grows linearly: 0.34, 1.37, and 5.66 ms for 100, 400, and 1,600
fields.
