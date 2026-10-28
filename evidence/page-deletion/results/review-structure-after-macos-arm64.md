- Implementation: `1b5198f18350254a05a02572e283b0d804151e37`
- Script blob: `0ae825e68db5b45ad023d5c9cdcf2dad5caaa5c7`
- Measured at: 2026-10-28T03:24:10.123682+00:00
- Python 3.15.0 on Darwin arm64
- Median of 7 rounds; first: on a newly opened document; repeated: after an undo

## Against document size

| Pages | Tagged | Delete, first | Delete, repeated | Select, first | Select, repeated |
| ---: | :---: | ---: | ---: | ---: | ---: |
| 1,000 | no | 2.33 ms | 0.79 ms | 2.49 ms | 0.94 ms |
| 1,000 | flat | 5.84 ms | 1.91 ms | 5.16 ms | 1.99 ms |
| 1,000 | nested | 4.33 ms | 1.05 ms | 4.46 ms | 1.23 ms |
| 4,000 | no | 9.68 ms | 3.42 ms | 10.32 ms | 3.98 ms |
| 4,000 | flat | 20.52 ms | 7.40 ms | 21.27 ms | 8.15 ms |
| 4,000 | nested | 17.67 ms | 4.30 ms | 18.20 ms | 4.86 ms |
| 16,000 | no | 39.84 ms | 14.23 ms | 41.36 ms | 16.57 ms |
| 16,000 | flat | 83.85 ms | 30.67 ms | 86.12 ms | 32.74 ms |
| 16,000 | nested | 71.61 ms | 17.65 ms | 74.61 ms | 20.82 ms |
| 64,000 | no | 157.39 ms | 57.82 ms | 171.86 ms | 68.13 ms |
| 64,000 | flat | 342.56 ms | 128.99 ms | 357.53 ms | 138.54 ms |
| 64,000 | nested | 305.77 ms | 78.79 ms | 306.49 ms | 85.68 ms |

## Against links to the deleted page, 4,000 pages

| Links | Delete, first | Delete, repeated |
| ---: | ---: | ---: |
| 0 | 9.11 ms | 3.13 ms |
| 100 | 9.97 ms | 3.38 ms |
| 1,000 | 12.43 ms | 4.28 ms |
| 10,000 | 40.70 ms | 16.25 ms |
