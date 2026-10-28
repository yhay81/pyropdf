# Repeated fill by number of form fields

Implementation: `de2cbaed6754a8dbd15aa051ec70fc667e7d9099`. The reviewed implementation performs a fresh form walk and checks every widget. All synthetic widgets start with a valid normal appearance; each run warms the first field, then reports the median of seven later fills. Reproduce with `uv run python evidence/form-fill-cost/field_scale.py`.

- Python 3.15.0 on Darwin arm64

| Fields | Median fill |
| ---: | ---: |
| 1 | 0.037 ms |
| 10 | 0.043 ms |
| 100 | 0.160 ms |
| 1,000 | 1.243 ms |
