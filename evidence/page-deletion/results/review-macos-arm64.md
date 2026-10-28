- Reviewed implementation: `c592aa70f5f371ed6b799443d35542b66cba1e1c`
- Measured at: 2026-10-28T03:05:13.846776+00:00
- Python 3.15.0 on Darwin arm64
- Median of 7 rounds; first: on a newly opened document; repeated: after an undo

## Against document size

| Pages | Tagged | Delete, first | Delete, repeated | Select, first | Select, repeated |
| ---: | :---: | ---: | ---: | ---: | ---: |
| 1,000 | no | 2.44 ms | 0.79 ms | 2.41 ms | 0.91 ms |
| 1,000 | yes | 5.25 ms | 2.28 ms | 5.49 ms | 2.45 ms |
| 4,000 | no | 9.31 ms | 3.30 ms | 9.93 ms | 3.71 ms |
| 4,000 | yes | 21.98 ms | 9.35 ms | 22.54 ms | 10.03 ms |
| 16,000 | no | 37.65 ms | 12.88 ms | 39.04 ms | 15.06 ms |
| 16,000 | yes | 90.32 ms | 38.74 ms | 92.33 ms | 43.54 ms |
| 64,000 | no | 156.19 ms | 54.27 ms | 169.34 ms | 66.44 ms |
| 64,000 | yes | 377.90 ms | 164.77 ms | 382.41 ms | 175.30 ms |

## Against links to the deleted page, 4,000 pages

| Links | Delete, first | Delete, repeated |
| ---: | ---: | ---: |
| 0 | 9.24 ms | 3.12 ms |
| 100 | 9.50 ms | 3.21 ms |
| 1,000 | 12.07 ms | 4.27 ms |
| 10,000 | 39.09 ms | 15.19 ms |
