- pyropdf 0.2.1, Python 3.14.8 on Windows 11 AMD64
- AMD64 Family 23 Model 113 Stepping 0, AuthenticAMD
- PyMuPDF 1.28.2 (MuPDF 1.28.2); NumPy 2.5.4
- The median of 5 processes per row. `Time` is wall-clock time and `CPU time` the process's,
  `peak` is how far the call grew the peak resident set, and `held` how much more the resident set
  holds after it, with its result alive (Windows only).

### lines-33000: 33,000 paths, 66,000 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.055 s | 0.047 s | 13.8 MiB, 440 B a path | 7.3 MiB, 231 B a path |
| `records` | 0.700 s | 0.594 s | 65.2 MiB, 2,072 B a path | 75.6 MiB, 2,401 B a path |
| `records_without_gc` | 0.492 s | 0.359 s | 65.3 MiB, 2,074 B a path | 76.1 MiB, 2,417 B a path |
| `columns` | 0.055 s | 0.047 s | 13.4 MiB, 427 B a path | 21.7 MiB, 691 B a path |
| `columns_numpy` | 0.057 s | 0.047 s | 12.0 MiB, 383 B a path | 20.6 MiB, 654 B a path |
| `get_cdrawings` | 0.182 s | 0.172 s | 0.0 MiB, 0 B a path | 35.7 MiB, 1,133 B a path |
| `get_drawings` | 0.466 s | 0.422 s | 0.0 MiB, 0 B a path | 44.5 MiB, 1,415 B a path |

Once the interpretation is memoized, `content().paths` takes 651.8 ms and `paths()` 7.44 ms. The columns hold 7.0 MiB, 221 bytes a path.

### lines-200000: 200,000 paths, 400,000 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.257 s | 0.219 s | 27.1 MiB, 142 B a path | 17.6 MiB, 93 B a path |
| `records` | 4.587 s | 4.203 s | 427.6 MiB, 2,242 B a path | 457.8 MiB, 2,400 B a path |
| `records_without_gc` | 2.477 s | 2.219 s | 428.4 MiB, 2,246 B a path | 458.8 MiB, 2,405 B a path |
| `columns` | 0.273 s | 0.266 s | 111.1 MiB, 583 B a path | 131.5 MiB, 689 B a path |
| `columns_numpy` | 0.313 s | 0.266 s | 110.4 MiB, 579 B a path | 131.4 MiB, 689 B a path |
| `get_cdrawings` | 1.183 s | 1.016 s | 184.4 MiB, 967 B a path | 281.5 MiB, 1,476 B a path |
| `get_drawings` | 2.626 s | 2.453 s | 242.3 MiB, 1,271 B a path | 339.1 MiB, 1,778 B a path |

Once the interpretation is memoized, `content().paths` takes 6,189.9 ms and `paths()` 51.12 ms. The columns hold 42.2 MiB, 221 bytes a path.

### f1040.pdf, page 1: 502 paths, 1,604 segments

| Call | Time | CPU time | Peak | Held |
| --- | ---: | ---: | ---: | ---: |
| `pixmap` | 0.019 s | 0.016 s | 7.0 MiB, 14,524 B a path | 4.2 MiB, 8,739 B a path |
| `records` | 0.019 s | 0.016 s | 0.0 MiB, 0 B a path | 3.0 MiB, 6,201 B a path |
| `records_without_gc` | 0.018 s | 0.016 s | 0.0 MiB, 0 B a path | 3.0 MiB, 6,356 B a path |
| `columns` | 0.010 s | 0.016 s | 0.0 MiB, 0 B a path | 2.1 MiB, 4,471 B a path |
| `columns_numpy` | 0.010 s | 0.000 s | 0.0 MiB, 0 B a path | 2.1 MiB, 4,357 B a path |
| `get_cdrawings` | 0.029 s | 0.031 s | 0.0 MiB, 0 B a path | 0.2 MiB, 367 B a path |
| `get_drawings` | 0.030 s | 0.031 s | 0.0 MiB, 0 B a path | 0.2 MiB, 318 B a path |

Once the interpretation is memoized, `content().paths` takes 8.8 ms and `paths()` 0.27 ms. The columns hold 0.1 MiB, 285 bytes a path.
