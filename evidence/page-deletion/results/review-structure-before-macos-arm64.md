- Implementation: `c592aa70f5f371ed6b799443d35542b66cba1e1c` (reviewed #185)
- Script blob: `0ae825e68db5b45ad023d5c9cdcf2dad5caaa5c7` (the nested workload added by #186)
- Measured: 2026-10-28; previously built #185 extension, using `uv run --no-sync python /tmp/pyropdf-186-measure.py` before rebuilding #186
- Python 3.15.0 on Darwin arm64
- Median of 7 rounds; first: on a newly opened document; repeated: after an undo

## Against document size

| Pages | Tagged | Delete, first | Delete, repeated | Select, first | Select, repeated |
| ---: | :---: | ---: | ---: | ---: | ---: |
| 1,000 | no | 2.35 ms | 0.80 ms | 2.44 ms | 0.98 ms |
| 1,000 | flat | 5.60 ms | 2.44 ms | 5.88 ms | 2.80 ms |
| 1,000 | nested | 5.31 ms | 2.04 ms | 5.36 ms | 2.14 ms |
| 4,000 | no | 9.49 ms | 3.34 ms | 10.53 ms | 3.94 ms |
| 4,000 | flat | 24.03 ms | 10.28 ms | 24.18 ms | 10.80 ms |
| 4,000 | nested | 22.97 ms | 8.61 ms | 23.30 ms | 8.93 ms |
| 16,000 | no | 39.19 ms | 14.02 ms | 41.64 ms | 16.60 ms |
| 16,000 | flat | 94.23 ms | 40.97 ms | 97.58 ms | 44.47 ms |
| 16,000 | nested | 88.23 ms | 33.50 ms | 91.15 ms | 36.50 ms |
| 64,000 | no | 163.52 ms | 58.14 ms | 174.77 ms | 69.01 ms |
| 64,000 | flat | 393.62 ms | 171.87 ms | 397.89 ms | 179.74 ms |
| 64,000 | nested | 356.55 ms | 134.44 ms | 369.28 ms | 149.52 ms |

## Against links to the deleted page, 4,000 pages

| Links | Delete, first | Delete, repeated |
| ---: | ---: | ---: |
| 0 | 9.42 ms | 3.25 ms |
| 100 | 9.88 ms | 3.48 ms |
| 1,000 | 12.87 ms | 4.56 ms |
| 10,000 | 41.64 ms | 15.81 ms |
