# Page deletion with bounded form discovery

Implementation: #337 on top of `5dfe6728f7272d9a038ea8b2e29910a01eb68a22`. The form rules of a removal read only the top-level fields that `affected_form_objects` finds, and repair `/P` only there (owner's option 1 on #337). The unrelated fields are widgetless. *Unaffected*: the deleted page has no annotations. *Affected*: it holds the one widget of one more top-level field, which leaves `/Fields`. The first deletion warms the reference index; each cell is the median of seven deletions after undo. Reproduce with `uv run python evidence/page-deletion/measure_unrelated_forms.py`.

- Python 3.15.0 on Darwin arm64

| Unrelated fields | Unaffected | Affected |
| ---: | ---: | ---: |
| 0 | 0.010 ms | 0.018 ms |
| 10 | 0.008 ms | 0.019 ms |
| 100 | 0.009 ms | 0.032 ms |
| 1,000 | 0.019 ms | 0.160 ms |
| 4,000 | 0.049 ms | 0.569 ms |

Before (`review-unrelated-forms-before-macos-arm64.md`), the unaffected deletion walked every field: 1.571 ms at 1,000. The affected column stays linear in the length of `/Fields`, which the removal rewrites; no field outside the reached ones is read.
