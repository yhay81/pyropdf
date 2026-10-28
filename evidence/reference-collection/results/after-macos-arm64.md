- Implementation: `779050752e4df4627171d64ae197fbeab9984548`
- Script blob: `632bd076f0db4bb1923fd32ce01d7723d9a8a05a`
- Measured at: 2026-10-28T03:36:57.838356+00:00
- Python 3.15.0 on Darwin arm64
- Median of 7 rounds; first: on a newly opened document; repeated: after an undo

## Against document size

| Pages | Tagged | Delete, first | Delete, repeated | Select, first | Select, repeated |
| ---: | :---: | ---: | ---: | ---: | ---: |
| 1,000 | no | 2.41 ms | 0.76 ms | 2.38 ms | 0.86 ms |
| 1,000 | flat | 5.32 ms | 2.21 ms | 5.37 ms | 2.29 ms |
| 1,000 | nested | 5.05 ms | 1.76 ms | 5.05 ms | 1.88 ms |
| 4,000 | no | 8.81 ms | 2.80 ms | 9.51 ms | 3.40 ms |
| 4,000 | flat | 21.88 ms | 9.05 ms | 21.94 ms | 9.39 ms |
| 4,000 | nested | 20.31 ms | 7.36 ms | 21.06 ms | 7.84 ms |
| 16,000 | no | 38.10 ms | 12.42 ms | 39.81 ms | 14.54 ms |
| 16,000 | flat | 89.13 ms | 37.05 ms | 90.36 ms | 39.33 ms |
| 16,000 | nested | 83.59 ms | 29.92 ms | 87.01 ms | 32.58 ms |
| 64,000 | no | 152.85 ms | 50.10 ms | 161.93 ms | 60.22 ms |
| 64,000 | flat | 365.72 ms | 154.05 ms | 378.94 ms | 166.29 ms |
| 64,000 | nested | 349.67 ms | 125.29 ms | 356.05 ms | 133.12 ms |

## Against links to the deleted page, 4,000 pages

| Links | Delete, first | Delete, repeated |
| ---: | ---: | ---: |
| 0 | 9.20 ms | 2.86 ms |
| 100 | 9.97 ms | 3.08 ms |
| 1,000 | 12.10 ms | 4.16 ms |
| 10,000 | 39.91 ms | 15.42 ms |
