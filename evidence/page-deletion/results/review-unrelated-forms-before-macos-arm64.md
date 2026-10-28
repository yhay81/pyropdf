# Page deletion with unrelated form fields

Implementation: `ddcd8fd98fc3511c864b3a87880ba614034a251b`. This is the reviewed full-form planner before targeted discovery. Every field is widgetless and neither page has annotations. The first deletion warms the reference index; each row is the median of seven deletions after undo. Reproduce with `uv run python evidence/page-deletion/measure_unrelated_forms.py`.

- Python 3.15.0 on Darwin arm64

| Unrelated fields | Median deletion |
| ---: | ---: |
| 0 | 0.011 ms |
| 10 | 0.026 ms |
| 100 | 0.158 ms |
| 1,000 | 1.571 ms |
