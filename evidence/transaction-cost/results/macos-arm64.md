# Transaction cost measurements, macos-arm64

Written by `run.sh`; the README describes the workload.

- CPU: Apple M2 Pro, 8 performance and 4 efficiency cores
- Memory: 32 GiB
- System: macOS 27.0.1 (26A434)
- Rust: rustc 1.99.1 (58cf8f9c3 2026-10-13)
- Date: 2026-10-24

## One write per transaction

Each transaction rewrites one object drawn at random from a document whose objects a first transaction wrote. Memory is the median of 101 transactions while the history keeps the version before each; time is the median and the 90th percentile of 1,001 batches of 64 transactions without a history, the lower of 3 passes.

| Numbers | Objects | Height | Chunks and branches copied | Kept alive | Allocated | Allocations | Time, median | Time, 90th percentile |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dense | 10,000 | 2 | 3 | 937 B | 1,021 B | 8 | 782 ns | 820 ns |
| dense | 100,000 | 3 | 4 | 1,217 B | 1,301 B | 9 | 1.13 µs | 1.22 µs |
| dense | 1,000,000 | 4 | 5 | 1,497 B | 1,581 B | 10 | 1.87 µs | 1.97 µs |
| scattered | 10,000 | 6 | 7 | 2,057 B | 2,141 B | 12 | 1.89 µs | 2.05 µs |
| scattered | 100,000 | 6 | 7 | 2,057 B | 2,141 B | 12 | 2.52 µs | 3.21 µs |
| scattered | 1,000,000 | 6 | 7 | 2,057 B | 2,141 B | 12 | 2.94 µs | 3.81 µs |

