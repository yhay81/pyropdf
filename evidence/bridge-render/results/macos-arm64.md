# Bridge render measurements, macos-arm64

Written by `run.sh`; the README describes the workload.

- CPU: Apple M2 Pro, 8 performance and 4 efficiency cores
- Memory: 32 GiB
- System: macOS 27.0.1 (26A434)
- Rust: rustc 1.99.1 (58cf8f9c3 2026-10-13)
- Date: 2026-10-25

## An edit followed by a render

Each edit rotates one page, drawn in turn from the document, and renders that page at 72 dpi through the bridge; the table gives the median of 41 edits.

| Pages | File | Update | Transaction | Page index | Bridge: store | Bridge: hayro | Render | Total |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 110 | 138131 B | 4605 B | 2.2 µs | 22.8 µs | 15.2 µs | 84.5 µs | 1.11 ms | 1.24 ms |
| 440 | 558551 B | 13198 B | 1.6 µs | 57.9 µs | 34.7 µs | 242.7 µs | 881.2 µs | 1.22 ms |
| 1760 | 2260483 B | 47521 B | 2.0 µs | 212.0 µs | 301.0 µs | 934.7 µs | 865.9 µs | 2.31 ms |
