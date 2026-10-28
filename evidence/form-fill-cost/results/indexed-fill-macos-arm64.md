# Indexed field fills by form size

Implementation and measurement: `8f151f9cb7dd976f75295e0891bcde16c7003ddb`, which combines the reviewed form fill with the incremental index. Each document has one page and the indicated number of indirect text widgets with a shared nonempty normal appearance. The selected field is filled once to construct and warm the index; each result is the median of seven later fills. The external-edit column measures a separate field-value edit with history disabled; the last column measures the fill immediately after that edit. For one field the external edit targets that same field. Reproduce with `uv run python evidence/form-fill-cost/field_scale.py` on the implementation commit.

The corrected pre-index baseline in `review-valid-appearance-macos-arm64.md` measured 0.036 ms for one field and 1.391 ms for 1,000 fields. These warm measurements include complete field filling, appearance settling and the `/NeedAppearances` update. They do not include the initial full form walk or index construction; structural edits may require another walk. A change to a shared dependency can affect multiple fields or widgets and costs the affected results.

- Python 3.15.0 on Darwin arm64

| Fields | Ordinary | No history | Transaction | External edit | Fill after edit |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.043 ms | 0.038 ms | 0.035 ms | 0.008 ms | 0.037 ms |
| 10 | 0.040 ms | 0.036 ms | 0.035 ms | 0.008 ms | 0.038 ms |
| 100 | 0.036 ms | 0.036 ms | 0.036 ms | 0.009 ms | 0.042 ms |
| 1,000 | 0.045 ms | 0.043 ms | 0.040 ms | 0.008 ms | 0.043 ms |
| 4,000 | 0.043 ms | 0.045 ms | 0.042 ms | 0.009 ms | 0.049 ms |
