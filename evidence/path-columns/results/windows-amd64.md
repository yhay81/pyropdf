- pyropdf 0.2.1, Python 3.14.8 on Windows 11 AMD64
- AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD
- PyMuPDF 1.28.2 (MuPDF 1.28.2); NumPy 2.5.4
- The median of 5 processes per row. `Time` is wall-clock time and `CPU time` the process's,
  `peak` is how far the call grew the peak resident set, and `held` how much more the resident set
  holds after it, with its result alive (Windows only).

### lines-33000: 33,000 paths, 66,000 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.059 s | 0.062 s | 13.8 MiB, 439 B a path | 6.9 MiB, 220 B a path |
| `records` | 0.624 s | 0.562 s | 65.4 MiB, 2,078 B a path | 76.1 MiB, 2,417 B a path |
| `records_without_gc` | 0.410 s | 0.359 s | 65.5 MiB, 2,081 B a path | 76.1 MiB, 2,417 B a path |
| `columns` | 0.057 s | 0.047 s | 13.6 MiB, 431 B a path | 22.0 MiB, 699 B a path |
| `columns_numpy` | 0.055 s | 0.047 s | 12.5 MiB, 399 B a path | 22.0 MiB, 700 B a path |
| `get_cdrawings` | 0.183 s | 0.172 s | 0.0 MiB, 0 B a path | 35.7 MiB, 1,134 B a path |
| `get_drawings` | 0.456 s | 0.422 s | 0.0 MiB, 0 B a path | 44.7 MiB, 1,419 B a path |

Once the interpretation is memoized, `content().paths` takes 630.7 ms and `paths()` 7.99 ms. The columns hold 7.0 MiB, 224 bytes a path.

### lines-200000: 200,000 paths, 400,000 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.313 s | 0.266 s | 30.8 MiB, 161 B a path | 17.6 MiB, 93 B a path |
| `records` | 4.590 s | 4.312 s | 427.0 MiB, 2,238 B a path | 460.1 MiB, 2,412 B a path |
| `records_without_gc` | 2.647 s | 2.219 s | 425.2 MiB, 2,230 B a path | 458.4 MiB, 2,403 B a path |
| `columns` | 0.324 s | 0.312 s | 110.7 MiB, 581 B a path | 133.8 MiB, 701 B a path |
| `columns_numpy` | 0.331 s | 0.297 s | 109.6 MiB, 575 B a path | 133.7 MiB, 701 B a path |
| `get_cdrawings` | 1.146 s | 1.078 s | 183.9 MiB, 964 B a path | 281.3 MiB, 1,475 B a path |
| `get_drawings` | 2.902 s | 2.594 s | 242.7 MiB, 1,272 B a path | 339.4 MiB, 1,779 B a path |

Once the interpretation is memoized, `content().paths` takes 5,485.8 ms and `paths()` 61.98 ms. The columns hold 42.7 MiB, 224 bytes a path.

### f1040.pdf, page 1: 502 paths, 1,604 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.019 s | 0.016 s | 4.7 MiB, 9,816 B a path | 3.6 MiB, 7,515 B a path |
| `records` | 0.019 s | 0.031 s | 1.6 MiB, 3,305 B a path | 3.0 MiB, 6,340 B a path |
| `records_without_gc` | 0.018 s | 0.016 s | 1.5 MiB, 3,101 B a path | 3.0 MiB, 6,258 B a path |
| `columns` | 0.010 s | 0.016 s | 0.8 MiB, 1,607 B a path | 2.2 MiB, 4,626 B a path |
| `columns_numpy` | 0.011 s | 0.016 s | 0.7 MiB, 1,542 B a path | 1.7 MiB, 3,468 B a path |
| `get_cdrawings` | 0.028 s | 0.031 s | 0.0 MiB, 0 B a path | 0.1 MiB, 310 B a path |
| `get_drawings` | 0.057 s | 0.031 s | 0.0 MiB, 0 B a path | 0.1 MiB, 310 B a path |

Once the interpretation is memoized, `content().paths` takes 8.2 ms and `paths()` 0.23 ms. The columns hold 0.1 MiB, 288 bytes a path.
