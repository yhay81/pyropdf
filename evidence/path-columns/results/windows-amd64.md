- pyropdf 0.2.1, Python 3.14.8 on Windows 11 AMD64
- AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD
- PyMuPDF 1.28.2 (MuPDF 1.28.2); NumPy 2.5.4
- The median of 5 processes per row. `Time` is wall-clock time and `CPU time` the process's,
  `peak` is how far the call grew the peak resident set, and `held` how much more the resident set
  holds after it, with its result alive (Windows only).

### lines-33000: 33,000 paths, 66,000 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.085 s | 0.047 s | 13.7 MiB, 435 B a path | 6.6 MiB, 211 B a path |
| `records` | 1.109 s | 0.578 s | 62.0 MiB, 1,969 B a path | 72.2 MiB, 2,294 B a path |
| `records_without_gc` | 0.455 s | 0.344 s | 61.8 MiB, 1,963 B a path | 72.2 MiB, 2,294 B a path |
| `columns` | 0.038 s | 0.047 s | 2.9 MiB, 93 B a path | 12.6 MiB, 401 B a path |
| `columns_numpy` | 0.037 s | 0.031 s | 1.0 MiB, 33 B a path | 11.0 MiB, 350 B a path |
| `get_cdrawings` | 0.185 s | 0.172 s | 0.0 MiB, 0 B a path | 35.6 MiB, 1,131 B a path |
| `get_drawings` | 0.438 s | 0.422 s | 0.0 MiB, 0 B a path | 44.9 MiB, 1,428 B a path |

Once the interpretation is memoized, `content().paths` takes 614.9 ms and `paths()` 0.05 ms. The columns hold 7.0 MiB, 221 bytes a path.

### lines-200000: 200,000 paths, 400,000 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.246 s | 0.219 s | 26.7 MiB, 140 B a path | 17.4 MiB, 91 B a path |
| `records` | 4.466 s | 4.156 s | 397.3 MiB, 2,083 B a path | 425.2 MiB, 2,229 B a path |
| `records_without_gc` | 2.288 s | 2.125 s | 396.8 MiB, 2,081 B a path | 425.0 MiB, 2,228 B a path |
| `columns` | 0.211 s | 0.188 s | 29.7 MiB, 156 B a path | 59.1 MiB, 310 B a path |
| `columns_numpy` | 0.215 s | 0.203 s | 29.8 MiB, 156 B a path | 59.6 MiB, 312 B a path |
| `get_cdrawings` | 1.192 s | 1.047 s | 184.5 MiB, 967 B a path | 281.5 MiB, 1,476 B a path |
| `get_drawings` | 3.032 s | 2.516 s | 242.5 MiB, 1,271 B a path | 339.2 MiB, 1,779 B a path |

Once the interpretation is memoized, `content().paths` takes 4,596.6 ms and `paths()` 0.03 ms. The columns hold 42.2 MiB, 221 bytes a path.

### f1040.pdf, page 1: 502 paths, 1,604 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.019 s | 0.016 s | 6.9 MiB, 14,377 B a path | 4.0 MiB, 8,445 B a path |
| `records` | 0.020 s | 0.016 s | 0.3 MiB, 636 B a path | 3.3 MiB, 6,887 B a path |
| `records_without_gc` | 0.018 s | 0.016 s | 0.3 MiB, 579 B a path | 3.3 MiB, 6,903 B a path |
| `columns` | 0.010 s | 0.000 s | 0.0 MiB, 0 B a path | 2.0 MiB, 4,267 B a path |
| `columns_numpy` | 0.012 s | 0.016 s | 0.0 MiB, 0 B a path | 2.0 MiB, 4,235 B a path |
| `get_cdrawings` | 0.028 s | 0.031 s | 0.0 MiB, 0 B a path | 0.2 MiB, 318 B a path |
| `get_drawings` | 0.030 s | 0.031 s | 0.0 MiB, 0 B a path | 0.2 MiB, 343 B a path |

Once the interpretation is memoized, `content().paths` takes 7.6 ms and `paths()` 0.03 ms. The columns hold 0.1 MiB, 285 bytes a path.
