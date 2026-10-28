- Python 3.15.0 on Darwin arm64
- Median of 7 rounds; first: on a newly opened document; repeated: after an undo

## Against document size

| Pages | Tagged | Delete, first | Delete, repeated | Select, first | Select, repeated |
| ---: | :---: | ---: | ---: | ---: | ---: |
| 1,000 | no | 1.90 ms | 0.38 ms | 2.09 ms | 0.59 ms |
| 1,000 | yes | 4.45 ms | 1.38 ms | 4.69 ms | 1.63 ms |
| 4,000 | no | 7.60 ms | 1.56 ms | 8.48 ms | 2.53 ms |
| 4,000 | yes | 18.07 ms | 5.65 ms | 19.15 ms | 6.96 ms |
| 16,000 | no | 31.53 ms | 6.58 ms | 34.83 ms | 10.45 ms |
| 16,000 | yes | 74.95 ms | 23.74 ms | 79.59 ms | 27.93 ms |
| 64,000 | no | 131.00 ms | 27.46 ms | 146.65 ms | 43.36 ms |
| 64,000 | yes | 309.80 ms | 99.09 ms | 328.61 ms | 116.03 ms |

## Against links to the deleted page, 4,000 pages

| Links | Delete, first | Delete, repeated |
| ---: | ---: | ---: |
| 0 | 7.71 ms | 1.63 ms |
| 100 | 8.17 ms | 1.64 ms |
| 1,000 | 10.44 ms | 2.38 ms |
| 10,000 | 34.00 ms | 10.20 ms |
