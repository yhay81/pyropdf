- Python 3.15.0 on Darwin arm64
- Median of 7 rounds; first: on a newly opened document; repeated: after an undo

## Against document size

| Pages | Tagged | Delete, first | Delete, repeated | Select, first | Select, repeated |
| ---: | :---: | ---: | ---: | ---: | ---: |
| 1,000 | no | 1.82 ms | 0.36 ms | 2.06 ms | 0.60 ms |
| 1,000 | flat | 4.14 ms | 1.07 ms | 4.34 ms | 1.27 ms |
| 1,000 | nested | 3.62 ms | 0.47 ms | 3.83 ms | 0.71 ms |
| 4,000 | no | 7.69 ms | 1.54 ms | 8.48 ms | 2.57 ms |
| 4,000 | flat | 17.11 ms | 4.43 ms | 18.23 ms | 5.53 ms |
| 4,000 | nested | 14.98 ms | 1.92 ms | 15.88 ms | 2.95 ms |
| 16,000 | no | 31.61 ms | 6.81 ms | 35.35 ms | 10.59 ms |
| 16,000 | flat | 70.67 ms | 18.07 ms | 74.39 ms | 21.88 ms |
| 16,000 | nested | 61.36 ms | 8.00 ms | 66.23 ms | 12.08 ms |
| 64,000 | no | 135.08 ms | 28.66 ms | 149.81 ms | 45.07 ms |
| 64,000 | flat | 291.60 ms | 76.69 ms | 306.57 ms | 91.37 ms |
| 64,000 | nested | 255.31 ms | 32.94 ms | 271.18 ms | 49.20 ms |

## Against links to the deleted page, 4,000 pages

| Links | Delete, first | Delete, repeated |
| ---: | ---: | ---: |
| 0 | 7.78 ms | 1.56 ms |
| 100 | 8.03 ms | 1.65 ms |
| 1,000 | 10.47 ms | 2.50 ms |
| 10,000 | 34.60 ms | 10.49 ms |
