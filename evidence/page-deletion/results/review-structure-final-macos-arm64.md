- Implementation: `0afd9b478d0315c9b7f7c9b5a90ee15c3f3f7b48` (reviewed #186 with #324 collector)
- Script blob: `ceb0a57c672e41cea536accf14e5d7634df89a3e`
- Measured at: 2026-10-28T03:45:31.435943+00:00
- Python 3.15.0 on Darwin arm64
- Median of 7 rounds; first: on a newly opened document; repeated: after an undo

## Against document size

| Pages | Tagged | Delete, first | Delete, repeated | Select, first | Select, repeated |
| ---: | :---: | ---: | ---: | ---: | ---: |
| 1,000 | no | 2.21 ms | 0.69 ms | 2.30 ms | 0.86 ms |
| 1,000 | flat | 4.85 ms | 1.58 ms | 4.98 ms | 1.77 ms |
| 1,000 | nested | 4.09 ms | 0.89 ms | 4.20 ms | 1.01 ms |
| 4,000 | no | 9.05 ms | 3.05 ms | 9.77 ms | 3.68 ms |
| 4,000 | flat | 19.11 ms | 6.50 ms | 20.77 ms | 7.53 ms |
| 4,000 | nested | 16.92 ms | 3.55 ms | 17.79 ms | 4.20 ms |
| 16,000 | no | 37.96 ms | 12.38 ms | 40.51 ms | 15.36 ms |
| 16,000 | flat | 83.40 ms | 28.28 ms | 83.42 ms | 30.22 ms |
| 16,000 | nested | 69.75 ms | 14.46 ms | 77.74 ms | 18.12 ms |
| 64,000 | no | 159.65 ms | 51.58 ms | 168.32 ms | 62.39 ms |
| 64,000 | flat | 350.47 ms | 117.21 ms | 350.72 ms | 124.06 ms |
| 64,000 | nested | 296.88 ms | 61.53 ms | 297.25 ms | 74.11 ms |

## Against links to the deleted page, 4,000 pages

| Links | Delete, first | Delete, repeated |
| ---: | ---: | ---: |
| 0 | 10.50 ms | 3.44 ms |
| 100 | 9.91 ms | 3.21 ms |
| 1,000 | 13.07 ms | 4.42 ms |
| 10,000 | 40.76 ms | 15.88 ms |

## Against unrelated structure, three pages and one removed element

| Unrelated elements | Delete, first | Delete, repeated |
| ---: | ---: | ---: |
| 0 | 0.04 ms | 0.02 ms |
| 1,000 | 1.33 ms | 0.02 ms |
| 16,000 | 22.78 ms | 0.05 ms |
| 64,000 | 88.57 ms | 0.06 ms |
