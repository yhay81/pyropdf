# Repeated fill by number of form fields

Implementation: `de2cbaed6754a8dbd15aa051ec70fc667e7d9099`; fixture: `5385600`. This earlier measurement used a shared stream whose declared length was zero, so the widgets did **not** start with nonempty normal appearances. Its warm-fill timings are historical and do not establish the cost of the intended existing-appearance workload. The corrected fixture and baseline are in `review-valid-appearance-macos-arm64.md`.

- Python 3.15.0 on Darwin arm64

| Fields | Median fill |
| ---: | ---: |
| 1 | 0.037 ms |
| 10 | 0.043 ms |
| 100 | 0.160 ms |
| 1,000 | 1.243 ms |
