- pyropdf 0.2.1, Python 3.14.8 on Windows 11 AMD64
- AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD
- PyMuPDF 1.28.2 (MuPDF 1.28.2); NumPy 2.5.4
- The median of 5 processes per row. `Time` is wall-clock time and `CPU time` the process's,
  `peak` is how far the call grew the peak resident set, and `held` how much more the resident set
  holds after it, with its result alive (Windows only).

### lines-33000: 33,000 paths, 66,000 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.081 s | 0.062 s | 13.8 MiB, 440 B a path | 6.9 MiB, 219 B a path |
| `records` | 1.261 s | 0.609 s | 65.4 MiB, 2,078 B a path | 75.8 MiB, 2,410 B a path |
| `records_without_gc` | 0.798 s | 0.391 s | 65.3 MiB, 2,075 B a path | 75.7 MiB, 2,404 B a path |
| `columns` | 0.065 s | 0.062 s | 13.5 MiB, 430 B a path | 22.0 MiB, 699 B a path |
| `columns_numpy` | 0.096 s | 0.062 s | 11.6 MiB, 370 B a path | 21.9 MiB, 695 B a path |
| `get_cdrawings` | 0.189 s | 0.172 s | 0.0 MiB, 0 B a path | 35.6 MiB, 1,132 B a path |
| `get_drawings` | 0.415 s | 0.406 s | 0.0 MiB, 0 B a path | 44.5 MiB, 1,414 B a path |

Once the interpretation is memoized, `content().paths` takes 704.2 ms and `paths()` 8.66 ms. The columns hold 7.0 MiB, 224 bytes a path.

### lines-200000: 200,000 paths, 400,000 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.207 s | 0.203 s | 26.9 MiB, 141 B a path | 17.6 MiB, 92 B a path |
| `records` | 3.961 s | 3.766 s | 428.6 MiB, 2,247 B a path | 458.7 MiB, 2,405 B a path |
| `records_without_gc` | 2.080 s | 1.953 s | 427.3 MiB, 2,240 B a path | 457.1 MiB, 2,397 B a path |
| `columns` | 0.246 s | 0.234 s | 110.9 MiB, 581 B a path | 132.2 MiB, 693 B a path |
| `columns_numpy` | 0.246 s | 0.234 s | 110.4 MiB, 579 B a path | 132.0 MiB, 692 B a path |
| `get_cdrawings` | 1.032 s | 1.016 s | 184.9 MiB, 969 B a path | 281.3 MiB, 1,475 B a path |
| `get_drawings` | 2.326 s | 2.250 s | 242.3 MiB, 1,270 B a path | 339.2 MiB, 1,778 B a path |

Once the interpretation is memoized, `content().paths` takes 4,313.9 ms and `paths()` 36.20 ms. The columns hold 42.7 MiB, 224 bytes a path.

### f1040.pdf, page 1: 502 paths, 1,604 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.016 s | 0.016 s | 6.7 MiB, 13,936 B a path | 3.7 MiB, 7,784 B a path |
| `records` | 0.017 s | 0.016 s | 0.0 MiB, 0 B a path | 3.1 MiB, 6,381 B a path |
| `records_without_gc` | 0.015 s | 0.016 s | 0.0 MiB, 0 B a path | 3.0 MiB, 6,234 B a path |
| `columns` | 0.009 s | 0.000 s | 0.0 MiB, 0 B a path | 2.3 MiB, 4,741 B a path |
| `columns_numpy` | 0.010 s | 0.016 s | 0.0 MiB, 0 B a path | 2.2 MiB, 4,618 B a path |
| `get_cdrawings` | 0.026 s | 0.031 s | 0.0 MiB, 0 B a path | 0.2 MiB, 326 B a path |
| `get_drawings` | 0.031 s | 0.031 s | 0.0 MiB, 0 B a path | 0.2 MiB, 416 B a path |

Once the interpretation is memoized, `content().paths` takes 10.4 ms and `paths()` 0.24 ms. The columns hold 0.1 MiB, 288 bytes a path.
