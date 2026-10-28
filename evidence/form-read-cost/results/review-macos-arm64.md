# Reviewed form-read measurement

- Source commit: `b61cba80790b04fcaf76426991deadee88789ec8`
- Command: `uv run --no-sync python evidence/form-read-cost/measure.py`
- Seven freshly opened documents per row; median wall time.

- Python 3.15.0 on Darwin arm64

| Fields | One walk | List and read | Read after listing | Walks |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 0.28 ms | 0.42 ms | 0.05 ms | 1 |
| 400 | 1.05 ms | 1.62 ms | 0.20 ms | 1 |
| 1,600 | 4.72 ms | 7.77 ms | 0.85 ms | 1 |

The 1,600-field list-and-read workload computed the memo once and took 1.65 times the first walk in this run. The earlier result in `macos-arm64.md` is retained as the original PR measurement.
