# Persistent map measurements, macos-arm64

Written by `run.sh`; the README describes the workloads.

- CPU: Apple M2 Pro, 8 performance and 4 efficiency cores
- Memory: 32 GiB
- System: macOS 27.0.1 (26A434)
- Rust: rustc 1.99.1 (58cf8f9c3 2026-10-13)
- Crates: im v15.1.0, imbl v7.0.2, rpds v1.2.1
- Date: 2026-10-24

## Candidates

### Operations, dense object numbers

Median times per object, the lower of 2 passes; the memory and the blocks of one whole version per object.

| Map | Objects | Insert | Lookup, hit | Lookup, miss | Ordered visit | Memory | Blocks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 10,000 | 67 ns | 13 ns | — | 18 ns | 109 B | 2.62 |
| rpds HashTrieMap | 100,000 | 77 ns | 23 ns | — | 22 ns | 104 B | 2.39 |
| rpds HashTrieMap | 1,000,000 | 181 ns | 129 ns | — | 72 ns | 106 B | 2.53 |
| rpds RedBlackTreeMap | 10,000 | 269 ns | 95 ns | — | 9 ns | 88 B | 2.00 |
| rpds RedBlackTreeMap | 100,000 | 373 ns | 160 ns | — | 11 ns | 88 B | 2.00 |
| rpds RedBlackTreeMap | 1,000,000 | 935 ns | 763 ns | — | 55 ns | 88 B | 2.00 |
| im HashMap | 10,000 | 27 ns | 8 ns | — | 32 ns | 243 B | 0.23 |
| im HashMap | 100,000 | 71 ns | 14 ns | — | 82 ns | 337 B | 0.32 |
| im HashMap | 1,000,000 | 116 ns | 44 ns | — | 107 ns | 322 B | 0.31 |
| im OrdMap | 10,000 | 108 ns | 32 ns | — | 4 ns | 47 B | 0.02 |
| im OrdMap | 100,000 | 147 ns | 50 ns | — | 4 ns | 48 B | 0.02 |
| im OrdMap | 1,000,000 | 318 ns | 176 ns | — | 8 ns | 48 B | 0.02 |
| imbl HashMap | 10,000 | 22 ns | 6 ns | — | 14 ns | 62 B | 0.11 |
| imbl HashMap | 100,000 | 50 ns | 12 ns | — | 25 ns | 159 B | 0.28 |
| imbl HashMap | 1,000,000 | 136 ns | 48 ns | — | 41 ns | 134 B | 0.21 |
| imbl OrdMap | 10,000 | 89 ns | 63 ns | — | 3 ns | 39 B | 0.10 |
| imbl OrdMap | 100,000 | 115 ns | 84 ns | — | 3 ns | 39 B | 0.10 |
| imbl OrdMap | 1,000,000 | 208 ns | 174 ns | — | 15 ns | 39 B | 0.10 |
| chunks 16x32 | 10,000 | 12 ns | 2 ns | — | 1 ns | 18 B | 0.06 |
| chunks 16x32 | 100,000 | 14 ns | 2 ns | — | 1 ns | 18 B | 0.06 |
| chunks 16x32 | 1,000,000 | 29 ns | 7 ns | — | 2 ns | 18 B | 0.06 |

### Transactions, dense object numbers, update

Median time to commit one transaction, the lower of 2 passes, and the memory that its version adds while the base stays alive.

| Map | Objects | 1 write | 100 writes | 10,000 writes |
| --- | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 10,000 | 264 ns, 1.2 KiB | 13.9 µs, 41.8 KiB | 785.5 µs, 1.0 MiB |
| rpds HashTrieMap | 100,000 | 335 ns, 1.4 KiB | 19.5 µs, 59.3 KiB | 1.3 ms, 1.8 MiB |
| rpds HashTrieMap | 1,000,000 | 1.0 µs, 1.8 KiB | 78.4 µs, 98.9 KiB | 6.3 ms, 3.7 MiB |
| rpds RedBlackTreeMap | 10,000 | 336 ns, 708 B | 23.5 µs, 37.0 KiB | 1.8 ms, 859.4 KiB |
| rpds RedBlackTreeMap | 100,000 | 507 ns, 886 B | 33.8 µs, 54.2 KiB | 2.9 ms, 1.9 MiB |
| rpds RedBlackTreeMap | 1,000,000 | 1.1 µs, 1.1 KiB | 91.2 µs, 73.4 KiB | 9.7 ms, 3.6 MiB |
| im HashMap | 10,000 | 467 ns, 3.4 KiB | 24.6 µs, 158.0 KiB | 522.8 µs, 2.3 MiB |
| im HashMap | 100,000 | 985 ns, 4.2 KiB | 72.2 µs, 234.9 KiB | 4.4 ms, 10.1 MiB |
| im HashMap | 1,000,000 | 1.6 µs, 4.9 KiB | 145.8 µs, 297.6 KiB | 10.4 ms, 16.0 MiB |
| im OrdMap | 10,000 | 787 ns, 6.2 KiB | 24.1 µs, 172.6 KiB | 602.8 µs, 456.1 KiB |
| im OrdMap | 100,000 | 1.1 µs, 6.0 KiB | 48.5 µs, 296.3 KiB | 1.5 ms, 4.5 MiB |
| im OrdMap | 1,000,000 | 1.7 µs, 8.2 KiB | 126.9 µs, 417.9 KiB | 7.2 ms, 16.9 MiB |
| imbl HashMap | 10,000 | 315 ns, 2.6 KiB | 12.3 µs, 86.5 KiB | 345.4 µs, 605.6 KiB |
| imbl HashMap | 100,000 | 633 ns, 3.6 KiB | 41.1 µs, 180.6 KiB | 2.1 ms, 5.4 MiB |
| imbl HashMap | 1,000,000 | 1.3 µs, 4.4 KiB | 110.6 µs, 256.1 KiB | 8.3 ms, 12.0 MiB |
| imbl OrdMap | 10,000 | 216 ns, 1.2 KiB | 15.3 µs, 53.3 KiB | 926.7 µs, 378.8 KiB |
| imbl OrdMap | 100,000 | 342 ns, 1.4 KiB | 21.7 µs, 78.0 KiB | 1.5 ms, 2.6 MiB |
| imbl OrdMap | 1,000,000 | 668 ns, 1.7 KiB | 57.9 µs, 101.9 KiB | 5.8 ms, 5.2 MiB |
| chunks 16x32 | 10,000 | 138 ns, 832 B | 5.2 µs, 30.5 KiB | 147.8 µs, 172.0 KiB |
| chunks 16x32 | 100,000 | 194 ns, 1.1 KiB | 9.1 µs, 49.6 KiB | 358.8 µs, 1.4 MiB |
| chunks 16x32 | 1,000,000 | 429 ns, 1.4 KiB | 32.4 µs, 67.3 KiB | 2.7 ms, 2.9 MiB |

### Transactions, dense object numbers, append

Median time to commit one transaction, the lower of 2 passes, and the memory that its version adds while the base stays alive.

| Map | Objects | 1 write | 100 writes | 10,000 writes |
| --- | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 10,000 | 213 ns, 1.1 KiB | 13.5 µs, 42.6 KiB | 908.5 µs, 1.3 MiB |
| rpds HashTrieMap | 100,000 | 259 ns, 1.4 KiB | 17.6 µs, 61.2 KiB | 1.2 ms, 2.3 MiB |
| rpds HashTrieMap | 1,000,000 | 318 ns, 1.7 KiB | 27.8 µs, 99.7 KiB | 4.6 ms, 3.7 MiB |
| rpds RedBlackTreeMap | 10,000 | 383 ns, 1.0 KiB | 20.4 µs, 9.5 KiB | 2.9 ms, 860.3 KiB |
| rpds RedBlackTreeMap | 100,000 | 472 ns, 1.1 KiB | 25.5 µs, 9.6 KiB | 3.0 ms, 860.4 KiB |
| rpds RedBlackTreeMap | 1,000,000 | 551 ns, 1.3 KiB | 30.0 µs, 9.8 KiB | 3.3 ms, 860.6 KiB |
| im HashMap | 10,000 | 338 ns, 3.1 KiB | 17.8 µs, 154.5 KiB | 598.3 µs, 4.4 MiB |
| im HashMap | 100,000 | 469 ns, 4.1 KiB | 30.8 µs, 233.3 KiB | 3.0 ms, 10.3 MiB |
| im HashMap | 1,000,000 | 491 ns, 4.1 KiB | 43.4 µs, 294.8 KiB | 8.8 ms, 16.3 MiB |
| im OrdMap | 10,000 | 757 ns, 6.2 KiB | 6.4 µs, 12.3 KiB | 602.3 µs, 647.2 KiB |
| im OrdMap | 100,000 | 1.0 µs, 6.2 KiB | 8.1 µs, 12.3 KiB | 707.2 µs, 651.3 KiB |
| im OrdMap | 1,000,000 | 956 ns, 8.2 KiB | 9.8 µs, 14.4 KiB | 899.3 µs, 649.3 KiB |
| imbl HashMap | 10,000 | 268 ns, 2.6 KiB | 12.0 µs, 89.9 KiB | 432.2 µs, 1.1 MiB |
| imbl HashMap | 100,000 | 373 ns, 3.6 KiB | 22.3 µs, 180.0 KiB | 1.3 ms, 5.5 MiB |
| imbl HashMap | 1,000,000 | 458 ns, 4.1 KiB | 36.6 µs, 272.6 KiB | 7.8 ms, 14.4 MiB |
| imbl OrdMap | 10,000 | 163 ns, 1.2 KiB | 5.8 µs, 6.3 KiB | 608.7 µs, 548.2 KiB |
| imbl OrdMap | 100,000 | 203 ns, 1.4 KiB | 7.6 µs, 6.8 KiB | 736.4 µs, 548.2 KiB |
| imbl OrdMap | 1,000,000 | 234 ns, 1.7 KiB | 9.0 µs, 7.4 KiB | 899.8 µs, 548.2 KiB |
| chunks 16x32 | 10,000 | 119 ns, 832 B | 1.8 µs, 2.4 KiB | 198.5 µs, 172.8 KiB |
| chunks 16x32 | 100,000 | 118 ns, 1.1 KiB | 2.2 µs, 2.7 KiB | 215.9 µs, 172.3 KiB |
| chunks 16x32 | 1,000,000 | 154 ns, 1.4 KiB | 2.7 µs, 3.0 KiB | 257.2 µs, 172.6 KiB |

### Operations, sparse object numbers

Median times per object, the lower of 2 passes; the memory and the blocks of one whole version per object.

| Map | Objects | Insert | Lookup, hit | Lookup, miss | Ordered visit | Memory | Blocks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 10,000 | 66 ns | 12 ns | 12 ns | 18 ns | 110 B | 2.63 |
| rpds HashTrieMap | 100,000 | 77 ns | 23 ns | 16 ns | 22 ns | 104 B | 2.39 |
| rpds HashTrieMap | 1,000,000 | 190 ns | 131 ns | 34 ns | 74 ns | 106 B | 2.53 |
| rpds RedBlackTreeMap | 10,000 | 264 ns | 91 ns | 101 ns | 9 ns | 88 B | 2.00 |
| rpds RedBlackTreeMap | 100,000 | 379 ns | 149 ns | 161 ns | 11 ns | 88 B | 2.00 |
| rpds RedBlackTreeMap | 1,000,000 | 946 ns | 748 ns | 845 ns | 54 ns | 88 B | 2.00 |
| im HashMap | 10,000 | 27 ns | 9 ns | 8 ns | 32 ns | 243 B | 0.23 |
| im HashMap | 100,000 | 67 ns | 13 ns | 11 ns | 76 ns | 339 B | 0.32 |
| im HashMap | 1,000,000 | 118 ns | 43 ns | 31 ns | 107 ns | 322 B | 0.31 |
| im OrdMap | 10,000 | 109 ns | 31 ns | 33 ns | 4 ns | 47 B | 0.02 |
| im OrdMap | 100,000 | 146 ns | 51 ns | 53 ns | 4 ns | 48 B | 0.02 |
| im OrdMap | 1,000,000 | 315 ns | 172 ns | 180 ns | 8 ns | 48 B | 0.02 |
| imbl HashMap | 10,000 | 22 ns | 6 ns | 4 ns | 14 ns | 62 B | 0.11 |
| imbl HashMap | 100,000 | 49 ns | 11 ns | 10 ns | 25 ns | 159 B | 0.28 |
| imbl HashMap | 1,000,000 | 131 ns | 49 ns | 32 ns | 41 ns | 135 B | 0.21 |
| imbl OrdMap | 10,000 | 88 ns | 62 ns | 68 ns | 3 ns | 39 B | 0.10 |
| imbl OrdMap | 100,000 | 115 ns | 84 ns | 91 ns | 3 ns | 39 B | 0.10 |
| imbl OrdMap | 1,000,000 | 196 ns | 172 ns | 181 ns | 13 ns | 39 B | 0.10 |
| chunks 16x32 | 10,000 | 20 ns | 2 ns | 3 ns | 4 ns | 123 B | 0.45 |
| chunks 16x32 | 100,000 | 25 ns | 3 ns | 5 ns | 10 ns | 124 B | 0.46 |
| chunks 16x32 | 1,000,000 | 61 ns | 18 ns | 14 ns | 20 ns | 124 B | 0.46 |

### Transactions, sparse object numbers, update

Median time to commit one transaction, the lower of 2 passes, and the memory that its version adds while the base stays alive.

| Map | Objects | 1 write | 100 writes | 10,000 writes |
| --- | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 10,000 | 261 ns, 1.2 KiB | 13.8 µs, 42.2 KiB | 779.5 µs, 1.0 MiB |
| rpds HashTrieMap | 100,000 | 318 ns, 1.4 KiB | 18.7 µs, 60.5 KiB | 1.2 ms, 1.8 MiB |
| rpds HashTrieMap | 1,000,000 | 1.0 µs, 1.8 KiB | 80.7 µs, 99.5 KiB | 6.4 ms, 3.7 MiB |
| rpds RedBlackTreeMap | 10,000 | 335 ns, 708 B | 23.4 µs, 37.0 KiB | 1.8 ms, 859.4 KiB |
| rpds RedBlackTreeMap | 100,000 | 423 ns, 886 B | 33.4 µs, 54.2 KiB | 2.9 ms, 1.9 MiB |
| rpds RedBlackTreeMap | 1,000,000 | 1.1 µs, 1.1 KiB | 89.8 µs, 73.4 KiB | 9.1 ms, 3.6 MiB |
| im HashMap | 10,000 | 460 ns, 3.2 KiB | 24.6 µs, 159.5 KiB | 528.3 µs, 2.3 MiB |
| im HashMap | 100,000 | 979 ns, 4.2 KiB | 70.2 µs, 239.1 KiB | 4.3 ms, 10.1 MiB |
| im HashMap | 1,000,000 | 1.6 µs, 4.9 KiB | 139.9 µs, 297.9 KiB | 10.5 ms, 16.0 MiB |
| im OrdMap | 10,000 | 766 ns, 6.2 KiB | 24.0 µs, 172.6 KiB | 601.1 µs, 456.1 KiB |
| im OrdMap | 100,000 | 1.1 µs, 6.0 KiB | 49.3 µs, 296.3 KiB | 1.5 ms, 4.5 MiB |
| im OrdMap | 1,000,000 | 1.7 µs, 8.2 KiB | 123.9 µs, 417.9 KiB | 7.3 ms, 16.9 MiB |
| imbl HashMap | 10,000 | 311 ns, 2.6 KiB | 12.1 µs, 86.1 KiB | 336.8 µs, 601.0 KiB |
| imbl HashMap | 100,000 | 627 ns, 3.6 KiB | 41.0 µs, 183.3 KiB | 2.0 ms, 5.4 MiB |
| imbl HashMap | 1,000,000 | 1.3 µs, 4.4 KiB | 106.2 µs, 256.3 KiB | 8.1 ms, 12.0 MiB |
| imbl OrdMap | 10,000 | 215 ns, 1.2 KiB | 15.0 µs, 53.3 KiB | 897.9 µs, 378.8 KiB |
| imbl OrdMap | 100,000 | 319 ns, 1.4 KiB | 21.4 µs, 78.0 KiB | 1.5 ms, 2.6 MiB |
| imbl OrdMap | 1,000,000 | 629 ns, 1.7 KiB | 57.7 µs, 101.9 KiB | 5.4 ms, 5.2 MiB |
| chunks 16x32 | 10,000 | 163 ns, 1.1 KiB | 8.8 µs, 48.2 KiB | 319.5 µs, 1.2 MiB |
| chunks 16x32 | 100,000 | 314 ns, 1.4 KiB | 19.5 µs, 65.3 KiB | 1.2 ms, 2.8 MiB |
| chunks 16x32 | 1,000,000 | 568 ns, 1.4 KiB | 47.0 µs, 83.1 KiB | 4.4 ms, 4.7 MiB |

### Transactions, sparse object numbers, append

Median time to commit one transaction, the lower of 2 passes, and the memory that its version adds while the base stays alive.

| Map | Objects | 1 write | 100 writes | 10,000 writes |
| --- | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 10,000 | 235 ns, 1.2 KiB | 14.1 µs, 44.6 KiB | 906.3 µs, 1.3 MiB |
| rpds HashTrieMap | 100,000 | 255 ns, 1.3 KiB | 17.7 µs, 60.7 KiB | 1.2 ms, 2.3 MiB |
| rpds HashTrieMap | 1,000,000 | 357 ns, 1.8 KiB | 28.1 µs, 98.3 KiB | 5.0 ms, 3.7 MiB |
| rpds RedBlackTreeMap | 10,000 | 383 ns, 1.0 KiB | 19.7 µs, 9.5 KiB | 2.8 ms, 860.3 KiB |
| rpds RedBlackTreeMap | 100,000 | 474 ns, 1.1 KiB | 25.2 µs, 9.6 KiB | 2.9 ms, 860.4 KiB |
| rpds RedBlackTreeMap | 1,000,000 | 548 ns, 1.3 KiB | 29.9 µs, 9.8 KiB | 3.3 ms, 860.6 KiB |
| im HashMap | 10,000 | 345 ns, 3.1 KiB | 18.1 µs, 160.7 KiB | 579.4 µs, 4.4 MiB |
| im HashMap | 100,000 | 472 ns, 4.1 KiB | 30.4 µs, 238.5 KiB | 3.3 ms, 10.3 MiB |
| im HashMap | 1,000,000 | 479 ns, 4.1 KiB | 46.2 µs, 306.0 KiB | 8.5 ms, 16.1 MiB |
| im OrdMap | 10,000 | 765 ns, 6.2 KiB | 6.5 µs, 12.3 KiB | 602.6 µs, 647.2 KiB |
| im OrdMap | 100,000 | 1.1 µs, 6.2 KiB | 8.1 µs, 12.3 KiB | 709.5 µs, 651.3 KiB |
| im OrdMap | 1,000,000 | 963 ns, 8.2 KiB | 9.8 µs, 14.4 KiB | 898.3 µs, 649.3 KiB |
| imbl HashMap | 10,000 | 275 ns, 2.6 KiB | 11.3 µs, 87.9 KiB | 421.4 µs, 1.1 MiB |
| imbl HashMap | 100,000 | 401 ns, 3.6 KiB | 22.4 µs, 180.4 KiB | 1.3 ms, 5.5 MiB |
| imbl HashMap | 1,000,000 | 457 ns, 4.1 KiB | 38.0 µs, 288.9 KiB | 7.7 ms, 14.6 MiB |
| imbl OrdMap | 10,000 | 162 ns, 1.2 KiB | 5.7 µs, 6.3 KiB | 600.4 µs, 548.2 KiB |
| imbl OrdMap | 100,000 | 205 ns, 1.4 KiB | 7.6 µs, 6.8 KiB | 736.3 µs, 548.2 KiB |
| imbl OrdMap | 1,000,000 | 231 ns, 1.7 KiB | 9.1 µs, 7.4 KiB | 898.1 µs, 548.2 KiB |
| chunks 16x32 | 10,000 | 139 ns, 1.1 KiB | 2.3 µs, 2.7 KiB | 215.8 µs, 172.6 KiB |
| chunks 16x32 | 100,000 | 179 ns, 1.4 KiB | 2.7 µs, 3.0 KiB | 257.1 µs, 172.8 KiB |
| chunks 16x32 | 1,000,000 | 192 ns, 1.4 KiB | 2.8 µs, 3.5 KiB | 257.2 µs, 172.8 KiB |

### Operations, scattered object numbers

Median times per object, the lower of 2 passes; the memory and the blocks of one whole version per object.

| Map | Objects | Insert | Lookup, hit | Lookup, miss | Ordered visit | Memory | Blocks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 100,000 | 77 ns | 23 ns | 16 ns | 23 ns | 104 B | 2.39 |
| rpds RedBlackTreeMap | 100,000 | 376 ns | 155 ns | 175 ns | 12 ns | 88 B | 2.00 |
| im HashMap | 100,000 | 71 ns | 15 ns | 12 ns | 80 ns | 336 B | 0.32 |
| im OrdMap | 100,000 | 152 ns | 51 ns | 54 ns | 4 ns | 48 B | 0.02 |
| imbl HashMap | 100,000 | 51 ns | 11 ns | 10 ns | 26 ns | 159 B | 0.28 |
| imbl OrdMap | 100,000 | 117 ns | 84 ns | 92 ns | 3 ns | 39 B | 0.10 |
| chunks 16x32 | 100,000 | 116 ns | 47 ns | 14 ns | 234 ns | 807 B | 2.91 |

### Transactions, scattered object numbers, update

Median time to commit one transaction, the lower of 2 passes, and the memory that its version adds while the base stays alive.

| Map | Objects | 1 write | 100 writes | 10,000 writes |
| --- | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 100,000 | 341 ns, 1.4 KiB | 22.2 µs, 60.3 KiB | 1.3 ms, 1.8 MiB |
| rpds RedBlackTreeMap | 100,000 | 510 ns, 886 B | 36.1 µs, 54.2 KiB | 3.0 ms, 1.9 MiB |
| im HashMap | 100,000 | 1.0 µs, 4.2 KiB | 75.8 µs, 237.9 KiB | 4.5 ms, 10.1 MiB |
| im OrdMap | 100,000 | 1.2 µs, 6.0 KiB | 52.2 µs, 296.3 KiB | 1.6 ms, 4.5 MiB |
| imbl HashMap | 100,000 | 719 ns, 3.6 KiB | 43.0 µs, 182.4 KiB | 2.5 ms, 5.4 MiB |
| imbl OrdMap | 100,000 | 344 ns, 1.4 KiB | 21.9 µs, 78.0 KiB | 1.6 ms, 2.6 MiB |
| chunks 16x32 | 100,000 | 775 ns, 1.9 KiB | 70.7 µs, 133.5 KiB | 6.5 ms, 9.5 MiB |

### Transactions, scattered object numbers, append

Median time to commit one transaction, the lower of 2 passes, and the memory that its version adds while the base stays alive.

| Map | Objects | 1 write | 100 writes | 10,000 writes |
| --- | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 100,000 | 261 ns, 1.4 KiB | 17.3 µs, 60.7 KiB | 1.2 ms, 2.3 MiB |
| rpds RedBlackTreeMap | 100,000 | 492 ns, 1.1 KiB | 26.4 µs, 9.6 KiB | 3.1 ms, 860.4 KiB |
| im HashMap | 100,000 | 385 ns, 3.1 KiB | 31.0 µs, 238.5 KiB | 3.4 ms, 10.3 MiB |
| im OrdMap | 100,000 | 1.1 µs, 6.2 KiB | 8.2 µs, 12.3 KiB | 709.1 µs, 651.3 KiB |
| imbl HashMap | 100,000 | 337 ns, 3.1 KiB | 22.9 µs, 184.5 KiB | 1.3 ms, 5.5 MiB |
| imbl OrdMap | 100,000 | 203 ns, 1.4 KiB | 7.6 µs, 6.8 KiB | 743.9 µs, 548.2 KiB |
| chunks 16x32 | 100,000 | 238 ns, 1.9 KiB | 3.6 µs, 3.5 KiB | 348.7 µs, 173.1 KiB |

## Chunk table variants

### Operations, dense object numbers

Median times per object, the lower of 2 passes; the memory and the blocks of one whole version per object.

| Map | Objects | Insert | Lookup, hit | Lookup, miss | Ordered visit | Memory | Blocks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| chunks 16x32 | 10,000 | 12 ns | 2 ns | — | 1 ns | 18 B | 0.06 |
| chunks 16x32 | 100,000 | 14 ns | 2 ns | — | 1 ns | 18 B | 0.06 |
| chunks 16x32 | 1,000,000 | 29 ns | 7 ns | — | 2 ns | 18 B | 0.06 |
| chunks 16x16 | 10,000 | 15 ns | 2 ns | — | 1 ns | 18 B | 0.07 |
| chunks 16x16 | 100,000 | 17 ns | 3 ns | — | 1 ns | 18 B | 0.07 |
| chunks 16x16 | 1,000,000 | 27 ns | 8 ns | — | 2 ns | 18 B | 0.07 |
| chunks 16x64 | 10,000 | 12 ns | 2 ns | — | 1 ns | 18 B | 0.06 |
| chunks 16x64 | 100,000 | 16 ns | 2 ns | — | 1 ns | 18 B | 0.06 |
| chunks 16x64 | 1,000,000 | 27 ns | 6 ns | — | 2 ns | 18 B | 0.06 |
| chunks 32x32 | 10,000 | 12 ns | 1 ns | — | 1 ns | 17 B | 0.03 |
| chunks 32x32 | 100,000 | 15 ns | 2 ns | — | 1 ns | 17 B | 0.03 |
| chunks 32x32 | 1,000,000 | 25 ns | 6 ns | — | 2 ns | 17 B | 0.03 |
| chunks 32x64 | 10,000 | 12 ns | 1 ns | — | 1 ns | 17 B | 0.03 |
| chunks 32x64 | 100,000 | 12 ns | 2 ns | — | 1 ns | 17 B | 0.03 |
| chunks 32x64 | 1,000,000 | 25 ns | 6 ns | — | 2 ns | 17 B | 0.03 |
| chunks 64x16 | 10,000 | 12 ns | 2 ns | — | 1 ns | 16 B | 0.02 |
| chunks 64x16 | 100,000 | 14 ns | 2 ns | — | 1 ns | 16 B | 0.02 |
| chunks 64x16 | 1,000,000 | 27 ns | 7 ns | — | 1 ns | 16 B | 0.02 |
| chunks 64x64 | 10,000 | 13 ns | 2 ns | — | 1 ns | 17 B | 0.02 |
| chunks 64x64 | 100,000 | 12 ns | 2 ns | — | 1 ns | 16 B | 0.02 |
| chunks 64x64 | 1,000,000 | 25 ns | 6 ns | — | 1 ns | 16 B | 0.02 |
| chunks 128x64 | 10,000 | 13 ns | 2 ns | — | 1 ns | 16 B | 0.01 |
| chunks 128x64 | 100,000 | 11 ns | 2 ns | — | 1 ns | 16 B | 0.01 |
| chunks 128x64 | 1,000,000 | 24 ns | 5 ns | — | 1 ns | 16 B | 0.01 |
| chunks 256x64 | 10,000 | 9 ns | 1 ns | — | 1 ns | 17 B | 0.00 |
| chunks 256x64 | 100,000 | 12 ns | 2 ns | — | 1 ns | 16 B | 0.00 |
| chunks 256x64 | 1,000,000 | 19 ns | 4 ns | — | 1 ns | 16 B | 0.00 |
| chunks 64x256 | 10,000 | 10 ns | 1 ns | — | 1 ns | 17 B | 0.02 |
| chunks 64x256 | 100,000 | 12 ns | 2 ns | — | 1 ns | 16 B | 0.02 |
| chunks 64x256 | 1,000,000 | 23 ns | 5 ns | — | 1 ns | 16 B | 0.02 |
| flat directory 64 | 10,000 | 9 ns | 1 ns | — | 1 ns | 17 B | 0.02 |
| flat directory 64 | 100,000 | 9 ns | 1 ns | — | 1 ns | 16 B | 0.02 |
| flat directory 64 | 1,000,000 | 18 ns | 4 ns | — | 1 ns | 16 B | 0.02 |

### Transactions, dense object numbers, update

Median time to commit one transaction, the lower of 2 passes, and the memory that its version adds while the base stays alive.

| Map | Objects | 1 write | 100 writes | 10,000 writes |
| --- | ---: | ---: | ---: | ---: |
| chunks 16x32 | 10,000 | 138 ns, 832 B | 5.2 µs, 30.5 KiB | 147.8 µs, 172.0 KiB |
| chunks 16x32 | 100,000 | 194 ns, 1.1 KiB | 9.1 µs, 49.6 KiB | 358.8 µs, 1.4 MiB |
| chunks 16x32 | 1,000,000 | 429 ns, 1.4 KiB | 32.4 µs, 67.3 KiB | 2.7 ms, 2.9 MiB |
| chunks 16x16 | 10,000 | 147 ns, 728 B | 5.6 µs, 30.8 KiB | 172.3 µs, 172.8 KiB |
| chunks 16x16 | 100,000 | 190 ns, 880 B | 9.7 µs, 43.3 KiB | 382.8 µs, 1.4 MiB |
| chunks 16x16 | 1,000,000 | 366 ns, 880 B | 32.9 µs, 55.9 KiB | 2.9 ms, 3.0 MiB |
| chunks 16x64 | 10,000 | 219 ns, 1.3 KiB | 5.4 µs, 30.5 KiB | 147.1 µs, 172.0 KiB |
| chunks 16x64 | 100,000 | 339 ns, 1.8 KiB | 13.0 µs, 60.6 KiB | 358.9 µs, 1.4 MiB |
| chunks 16x64 | 1,000,000 | 625 ns, 1.8 KiB | 41.9 µs, 85.2 KiB | 2.7 ms, 2.9 MiB |
| chunks 32x32 | 10,000 | 177 ns, 1.1 KiB | 8.3 µs, 47.9 KiB | 148.7 µs, 164.4 KiB |
| chunks 32x32 | 100,000 | 231 ns, 1.3 KiB | 13.1 µs, 69.0 KiB | 407.6 µs, 1.5 MiB |
| chunks 32x32 | 1,000,000 | 456 ns, 1.3 KiB | 38.6 µs, 85.9 KiB | 3.4 ms, 4.6 MiB |
| chunks 32x64 | 10,000 | 266 ns, 1.6 KiB | 8.4 µs, 48.1 KiB | 151.1 µs, 164.5 KiB |
| chunks 32x64 | 100,000 | 328 ns, 1.6 KiB | 14.9 µs, 73.2 KiB | 390.8 µs, 1.5 MiB |
| chunks 32x64 | 1,000,000 | 647 ns, 2.1 KiB | 47.0 µs, 103.5 KiB | 3.3 ms, 4.6 MiB |
| chunks 64x16 | 10,000 | 229 ns, 1.3 KiB | 12.6 µs, 77.4 KiB | 144.1 µs, 161.1 KiB |
| chunks 64x16 | 100,000 | 270 ns, 1.5 KiB | 18.8 µs, 108.6 KiB | 387.2 µs, 1.6 MiB |
| chunks 64x16 | 1,000,000 | 531 ns, 1.6 KiB | 49.2 µs, 123.3 KiB | 4.0 ms, 7.5 MiB |
| chunks 64x64 | 10,000 | 354 ns, 2.1 KiB | 12.6 µs, 77.9 KiB | 156.7 µs, 161.5 KiB |
| chunks 64x64 | 100,000 | 399 ns, 2.1 KiB | 19.7 µs, 111.4 KiB | 365.0 µs, 1.6 MiB |
| chunks 64x64 | 1,000,000 | 770 ns, 2.6 KiB | 59.7 µs, 146.9 KiB | 3.9 ms, 7.5 MiB |
| chunks 128x64 | 10,000 | 561 ns, 3.1 KiB | 18.8 µs, 117.1 KiB | 158.2 µs, 160.8 KiB |
| chunks 128x64 | 100,000 | 615 ns, 3.1 KiB | 33.4 µs, 194.2 KiB | 366.9 µs, 1.5 MiB |
| chunks 128x64 | 1,000,000 | 1.0 µs, 3.6 KiB | 83.4 µs, 237.5 KiB | 4.3 ms, 11.2 MiB |
| chunks 256x64 | 10,000 | 673 ns, 4.5 KiB | 22.7 µs, 147.6 KiB | 121.0 µs, 161.1 KiB |
| chunks 256x64 | 100,000 | 826 ns, 5.1 KiB | 57.8 µs, 354.2 KiB | 359.0 µs, 1.5 MiB |
| chunks 256x64 | 1,000,000 | 1.3 µs, 5.1 KiB | 116.2 µs, 421.4 KiB | 4.6 ms, 14.2 MiB |
| chunks 64x256 | 10,000 | 625 ns, 3.0 KiB | 12.5 µs, 77.8 KiB | 121.0 µs, 161.5 KiB |
| chunks 64x256 | 100,000 | 1.0 µs, 5.1 KiB | 20.3 µs, 113.9 KiB | 375.0 µs, 1.6 MiB |
| chunks 64x256 | 1,000,000 | 1.5 µs, 5.1 KiB | 86.8 µs, 202.2 KiB | 4.1 ms, 7.5 MiB |
| flat directory 64 | 10,000 | 515 ns, 2.3 KiB | 12.9 µs, 77.1 KiB | 114.4 µs, 160.7 KiB |
| flat directory 64 | 100,000 | 3.8 µs, 13.3 KiB | 19.8 µs, 110.4 KiB | 362.3 µs, 1.6 MiB |
| flat directory 64 | 1,000,000 | 44.0 µs, 123.1 KiB | 87.7 µs, 223.3 KiB | 4.1 ms, 7.5 MiB |

### Transactions, dense object numbers, append

Median time to commit one transaction, the lower of 2 passes, and the memory that its version adds while the base stays alive.

| Map | Objects | 1 write | 100 writes | 10,000 writes |
| --- | ---: | ---: | ---: | ---: |
| chunks 16x32 | 10,000 | 119 ns, 832 B | 1.8 µs, 2.4 KiB | 198.5 µs, 172.8 KiB |
| chunks 16x32 | 100,000 | 118 ns, 1.1 KiB | 2.2 µs, 2.7 KiB | 215.9 µs, 172.3 KiB |
| chunks 16x32 | 1,000,000 | 154 ns, 1.4 KiB | 2.7 µs, 3.0 KiB | 257.2 µs, 172.6 KiB |
| chunks 16x16 | 10,000 | 110 ns, 728 B | 2.2 µs, 2.3 KiB | 214.0 µs, 172.8 KiB |
| chunks 16x16 | 100,000 | 141 ns, 880 B | 2.7 µs, 2.6 KiB | 258.4 µs, 173.0 KiB |
| chunks 16x16 | 1,000,000 | 140 ns, 880 B | 2.7 µs, 2.5 KiB | 257.9 µs, 173.0 KiB |
| chunks 16x64 | 10,000 | 181 ns, 1.3 KiB | 1.9 µs, 2.9 KiB | 171.7 µs, 172.6 KiB |
| chunks 16x64 | 100,000 | 244 ns, 1.8 KiB | 2.3 µs, 3.4 KiB | 213.2 µs, 173.1 KiB |
| chunks 16x64 | 1,000,000 | 221 ns, 1.8 KiB | 2.3 µs, 3.4 KiB | 213.2 µs, 173.1 KiB |
| chunks 32x32 | 10,000 | 137 ns, 1.1 KiB | 1.8 µs, 2.6 KiB | 166.5 µs, 165.2 KiB |
| chunks 32x32 | 100,000 | 138 ns, 1.3 KiB | 2.2 µs, 2.9 KiB | 209.4 µs, 164.9 KiB |
| chunks 32x32 | 1,000,000 | 189 ns, 1.3 KiB | 2.2 µs, 2.9 KiB | 209.2 µs, 164.9 KiB |
| chunks 32x64 | 10,000 | 230 ns, 1.6 KiB | 1.9 µs, 3.1 KiB | 166.5 µs, 165.6 KiB |
| chunks 32x64 | 100,000 | 247 ns, 1.6 KiB | 1.9 µs, 3.1 KiB | 169.7 µs, 165.1 KiB |
| chunks 32x64 | 1,000,000 | 247 ns, 2.1 KiB | 2.3 µs, 3.6 KiB | 208.7 µs, 165.6 KiB |
| chunks 64x16 | 10,000 | 163 ns, 1.3 KiB | 1.8 µs, 2.3 KiB | 181.9 µs, 161.5 KiB |
| chunks 64x16 | 100,000 | 206 ns, 1.5 KiB | 2.3 µs, 3.5 KiB | 209.8 µs, 161.4 KiB |
| chunks 64x16 | 1,000,000 | 203 ns, 1.6 KiB | 2.6 µs, 2.6 KiB | 250.0 µs, 161.5 KiB |
| chunks 64x64 | 10,000 | 224 ns, 2.1 KiB | 1.8 µs, 3.1 KiB | 167.3 µs, 161.5 KiB |
| chunks 64x64 | 100,000 | 261 ns, 2.1 KiB | 1.9 µs, 4.1 KiB | 166.0 µs, 161.5 KiB |
| chunks 64x64 | 1,000,000 | 302 ns, 2.6 KiB | 2.3 µs, 3.6 KiB | 214.7 µs, 162.1 KiB |
| chunks 128x64 | 10,000 | 309 ns, 3.1 KiB | 1.8 µs, 3.1 KiB | 160.3 µs, 160.8 KiB |
| chunks 128x64 | 100,000 | 328 ns, 3.1 KiB | 1.9 µs, 5.1 KiB | 161.8 µs, 160.8 KiB |
| chunks 128x64 | 1,000,000 | 471 ns, 3.6 KiB | 2.5 µs, 5.6 KiB | 204.1 µs, 161.3 KiB |
| chunks 256x64 | 10,000 | 422 ns, 4.5 KiB | 1.6 µs, 4.5 KiB | 135.3 µs, 162.2 KiB |
| chunks 256x64 | 100,000 | 599 ns, 5.1 KiB | 2.4 µs, 9.1 KiB | 166.7 µs, 161.7 KiB |
| chunks 256x64 | 1,000,000 | 559 ns, 5.1 KiB | 2.1 µs, 5.1 KiB | 164.3 µs, 161.7 KiB |
| chunks 64x256 | 10,000 | 559 ns, 3.0 KiB | 1.8 µs, 4.1 KiB | 140.3 µs, 165.5 KiB |
| chunks 64x256 | 100,000 | 470 ns, 5.1 KiB | 2.1 µs, 7.1 KiB | 166.9 µs, 163.5 KiB |
| chunks 64x256 | 1,000,000 | 600 ns, 5.1 KiB | 2.2 µs, 6.1 KiB | 169.5 µs, 163.5 KiB |
| flat directory 64 | 10,000 | 459 ns, 2.3 KiB | 1.6 µs, 4.5 KiB | 113.2 µs, 161.9 KiB |
| flat directory 64 | 100,000 | 3.8 µs, 13.3 KiB | 5.0 µs, 27.5 KiB | 115.1 µs, 183.9 KiB |
| flat directory 64 | 1,000,000 | 43.7 µs, 123.1 KiB | 43.9 µs, 246.2 KiB | 154.7 µs, 403.6 KiB |

### Operations, sparse object numbers

Median times per object, the lower of 2 passes; the memory and the blocks of one whole version per object.

| Map | Objects | Insert | Lookup, hit | Lookup, miss | Ordered visit | Memory | Blocks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| chunks 16x32 | 10,000 | 20 ns | 2 ns | 3 ns | 4 ns | 123 B | 0.45 |
| chunks 16x32 | 100,000 | 25 ns | 3 ns | 5 ns | 10 ns | 124 B | 0.46 |
| chunks 16x32 | 1,000,000 | 61 ns | 18 ns | 14 ns | 20 ns | 124 B | 0.46 |
| chunks 16x16 | 10,000 | 23 ns | 3 ns | 4 ns | 4 ns | 124 B | 0.47 |
| chunks 16x16 | 100,000 | 25 ns | 4 ns | 5 ns | 10 ns | 125 B | 0.47 |
| chunks 16x16 | 1,000,000 | 62 ns | 20 ns | 18 ns | 24 ns | 125 B | 0.47 |
| chunks 16x64 | 10,000 | 21 ns | 2 ns | 4 ns | 3 ns | 123 B | 0.44 |
| chunks 16x64 | 100,000 | 24 ns | 3 ns | 4 ns | 10 ns | 124 B | 0.45 |
| chunks 16x64 | 1,000,000 | 62 ns | 19 ns | 16 ns | 18 ns | 124 B | 0.45 |
| chunks 32x32 | 10,000 | 19 ns | 2 ns | 2 ns | 10 ns | 132 B | 0.25 |
| chunks 32x32 | 100,000 | 23 ns | 3 ns | 4 ns | 13 ns | 132 B | 0.25 |
| chunks 32x32 | 1,000,000 | 58 ns | 16 ns | 14 ns | 27 ns | 132 B | 0.25 |
| chunks 32x64 | 10,000 | 17 ns | 2 ns | 1 ns | 10 ns | 132 B | 0.25 |
| chunks 32x64 | 100,000 | 22 ns | 3 ns | 4 ns | 13 ns | 132 B | 0.25 |
| chunks 32x64 | 1,000,000 | 53 ns | 14 ns | 12 ns | 25 ns | 132 B | 0.25 |
| chunks 64x16 | 10,000 | 18 ns | 2 ns | 2 ns | 10 ns | 131 B | 0.13 |
| chunks 64x16 | 100,000 | 25 ns | 4 ns | 5 ns | 12 ns | 131 B | 0.13 |
| chunks 64x16 | 1,000,000 | 61 ns | 18 ns | 17 ns | 26 ns | 131 B | 0.13 |
| chunks 64x64 | 10,000 | 16 ns | 2 ns | 1 ns | 9 ns | 131 B | 0.13 |
| chunks 64x64 | 100,000 | 25 ns | 4 ns | 5 ns | 13 ns | 131 B | 0.13 |
| chunks 64x64 | 1,000,000 | 54 ns | 14 ns | 12 ns | 25 ns | 131 B | 0.13 |
| chunks 128x64 | 10,000 | 16 ns | 2 ns | 1 ns | 9 ns | 130 B | 0.06 |
| chunks 128x64 | 100,000 | 23 ns | 3 ns | 4 ns | 11 ns | 130 B | 0.06 |
| chunks 128x64 | 1,000,000 | 51 ns | 13 ns | 11 ns | 20 ns | 130 B | 0.06 |
| chunks 256x64 | 10,000 | 17 ns | 2 ns | 1 ns | 8 ns | 129 B | 0.03 |
| chunks 256x64 | 100,000 | 21 ns | 2 ns | 2 ns | 10 ns | 129 B | 0.03 |
| chunks 256x64 | 1,000,000 | 52 ns | 13 ns | 12 ns | 15 ns | 129 B | 0.03 |
| chunks 64x256 | 10,000 | 16 ns | 2 ns | 1 ns | 10 ns | 131 B | 0.13 |
| chunks 64x256 | 100,000 | 22 ns | 2 ns | 3 ns | 12 ns | 131 B | 0.13 |
| chunks 64x256 | 1,000,000 | 53 ns | 14 ns | 12 ns | 27 ns | 131 B | 0.13 |
| flat directory 64 | 10,000 | 13 ns | 1 ns | 1 ns | 9 ns | 132 B | 0.13 |
| flat directory 64 | 100,000 | 18 ns | 2 ns | 2 ns | 13 ns | 132 B | 0.12 |
| flat directory 64 | 1,000,000 | 39 ns | 7 ns | 6 ns | 28 ns | 131 B | 0.12 |

### Transactions, sparse object numbers, update

Median time to commit one transaction, the lower of 2 passes, and the memory that its version adds while the base stays alive.

| Map | Objects | 1 write | 100 writes | 10,000 writes |
| --- | ---: | ---: | ---: | ---: |
| chunks 16x32 | 10,000 | 163 ns, 1.1 KiB | 8.8 µs, 48.2 KiB | 319.5 µs, 1.2 MiB |
| chunks 16x32 | 100,000 | 314 ns, 1.4 KiB | 19.5 µs, 65.3 KiB | 1.2 ms, 2.8 MiB |
| chunks 16x32 | 1,000,000 | 568 ns, 1.4 KiB | 47.0 µs, 83.1 KiB | 4.4 ms, 4.7 MiB |
| chunks 16x16 | 10,000 | 178 ns, 880 B | 8.4 µs, 42.6 KiB | 346.1 µs, 1.2 MiB |
| chunks 16x16 | 100,000 | 276 ns, 880 B | 18.3 µs, 54.7 KiB | 1.3 ms, 2.8 MiB |
| chunks 16x16 | 1,000,000 | 574 ns, 1.0 KiB | 44.2 µs, 67.3 KiB | 4.2 ms, 4.1 MiB |
| chunks 16x64 | 10,000 | 320 ns, 1.8 KiB | 12.1 µs, 57.6 KiB | 325.3 µs, 1.2 MiB |
| chunks 16x64 | 100,000 | 465 ns, 1.8 KiB | 26.4 µs, 81.9 KiB | 1.3 ms, 2.8 MiB |
| chunks 16x64 | 1,000,000 | 1.0 µs, 2.4 KiB | 78.9 µs, 115.9 KiB | 5.4 ms, 5.5 MiB |
| chunks 32x32 | 10,000 | 203 ns, 1.3 KiB | 12.0 µs, 67.3 KiB | 340.4 µs, 1.3 MiB |
| chunks 32x32 | 100,000 | 320 ns, 1.3 KiB | 23.6 µs, 83.5 KiB | 1.7 ms, 4.4 MiB |
| chunks 32x32 | 1,000,000 | 686 ns, 1.6 KiB | 56.9 µs, 103.7 KiB | 5.1 ms, 6.5 MiB |
| chunks 32x64 | 10,000 | 306 ns, 1.6 KiB | 13.9 µs, 70.0 KiB | 319.8 µs, 1.3 MiB |
| chunks 32x64 | 100,000 | 515 ns, 2.1 KiB | 32.4 µs, 100.9 KiB | 1.7 ms, 4.4 MiB |
| chunks 32x64 | 1,000,000 | 1.0 µs, 2.1 KiB | 81.7 µs, 129.3 KiB | 5.5 ms, 6.8 MiB |
| chunks 64x16 | 10,000 | 254 ns, 1.5 KiB | 17.0 µs, 107.3 KiB | 317.5 µs, 1.3 MiB |
| chunks 64x16 | 100,000 | 382 ns, 1.6 KiB | 30.1 µs, 121.7 KiB | 2.1 ms, 7.0 MiB |
| chunks 64x16 | 1,000,000 | 655 ns, 1.8 KiB | 55.0 µs, 134.6 KiB | 5.3 ms, 10.4 MiB |
| chunks 64x64 | 10,000 | 377 ns, 2.1 KiB | 17.9 µs, 108.9 KiB | 310.8 µs, 1.3 MiB |
| chunks 64x64 | 100,000 | 624 ns, 2.6 KiB | 40.2 µs, 144.4 KiB | 2.2 ms, 6.9 MiB |
| chunks 64x64 | 1,000,000 | 993 ns, 2.6 KiB | 83.1 µs, 168.8 KiB | 5.0 ms, 10.5 MiB |
| chunks 128x64 | 10,000 | 573 ns, 3.1 KiB | 27.9 µs, 192.7 KiB | 283.9 µs, 1.2 MiB |
| chunks 128x64 | 100,000 | 802 ns, 3.6 KiB | 55.7 µs, 234.1 KiB | 2.4 ms, 9.9 MiB |
| chunks 128x64 | 1,000,000 | 1.1 µs, 3.6 KiB | 97.5 µs, 260.1 KiB | 6.3 ms, 18.7 MiB |
| chunks 256x64 | 10,000 | 730 ns, 5.1 KiB | 44.2 µs, 351.7 KiB | 279.1 µs, 1.2 MiB |
| chunks 256x64 | 100,000 | 981 ns, 5.1 KiB | 72.8 µs, 416.4 KiB | 2.3 ms, 11.8 MiB |
| chunks 256x64 | 1,000,000 | 1.4 µs, 5.6 KiB | 119.0 µs, 453.2 KiB | 8.4 ms, 33.8 MiB |
| chunks 64x256 | 10,000 | 978 ns, 5.1 KiB | 17.5 µs, 110.1 KiB | 301.3 µs, 1.3 MiB |
| chunks 64x256 | 100,000 | 1.3 µs, 5.1 KiB | 60.2 µs, 189.3 KiB | 2.1 ms, 6.9 MiB |
| chunks 64x256 | 1,000,000 | 3.2 µs, 7.1 KiB | 179.3 µs, 290.1 KiB | 5.0 ms, 10.5 MiB |
| flat directory 64 | 10,000 | 3.1 µs, 10.8 KiB | 17.0 µs, 107.8 KiB | 268.4 µs, 1.2 MiB |
| flat directory 64 | 100,000 | 35.4 µs, 98.7 KiB | 61.3 µs, 198.8 KiB | 2.0 ms, 6.9 MiB |
| flat directory 64 | 1,000,000 | 847.5 µs, 977.6 KiB | 862.8 µs, 1.1 MiB | 4.8 ms, 10.5 MiB |

### Transactions, sparse object numbers, append

Median time to commit one transaction, the lower of 2 passes, and the memory that its version adds while the base stays alive.

| Map | Objects | 1 write | 100 writes | 10,000 writes |
| --- | ---: | ---: | ---: | ---: |
| chunks 16x32 | 10,000 | 139 ns, 1.1 KiB | 2.3 µs, 2.7 KiB | 215.8 µs, 172.6 KiB |
| chunks 16x32 | 100,000 | 179 ns, 1.4 KiB | 2.7 µs, 3.0 KiB | 257.1 µs, 172.8 KiB |
| chunks 16x32 | 1,000,000 | 192 ns, 1.4 KiB | 2.8 µs, 3.5 KiB | 257.2 µs, 172.8 KiB |
| chunks 16x16 | 10,000 | 134 ns, 880 B | 2.7 µs, 2.5 KiB | 258.0 µs, 173.0 KiB |
| chunks 16x16 | 100,000 | 150 ns, 880 B | 2.7 µs, 2.6 KiB | 258.6 µs, 173.0 KiB |
| chunks 16x16 | 1,000,000 | 174 ns, 1.0 KiB | 3.1 µs, 3.0 KiB | 301.5 µs, 173.3 KiB |
| chunks 16x64 | 10,000 | 181 ns, 1.8 KiB | 2.3 µs, 3.4 KiB | 213.1 µs, 172.6 KiB |
| chunks 16x64 | 100,000 | 193 ns, 1.8 KiB | 2.3 µs, 3.4 KiB | 213.0 µs, 172.6 KiB |
| chunks 16x64 | 1,000,000 | 318 ns, 2.4 KiB | 2.9 µs, 4.2 KiB | 260.6 µs, 173.6 KiB |
| chunks 32x32 | 10,000 | 149 ns, 1.3 KiB | 2.2 µs, 2.9 KiB | 209.1 µs, 165.2 KiB |
| chunks 32x32 | 100,000 | 170 ns, 1.3 KiB | 2.2 µs, 2.9 KiB | 209.2 µs, 164.7 KiB |
| chunks 32x32 | 1,000,000 | 192 ns, 1.6 KiB | 2.7 µs, 3.7 KiB | 252.2 µs, 165.7 KiB |
| chunks 32x64 | 10,000 | 190 ns, 1.6 KiB | 1.8 µs, 3.1 KiB | 168.8 µs, 165.0 KiB |
| chunks 32x64 | 100,000 | 237 ns, 2.1 KiB | 2.3 µs, 3.6 KiB | 208.4 µs, 165.6 KiB |
| chunks 32x64 | 1,000,000 | 276 ns, 2.1 KiB | 2.3 µs, 4.1 KiB | 208.9 µs, 166.1 KiB |
| chunks 64x16 | 10,000 | 181 ns, 1.5 KiB | 2.2 µs, 2.5 KiB | 209.6 µs, 161.4 KiB |
| chunks 64x16 | 100,000 | 197 ns, 1.6 KiB | 2.7 µs, 3.6 KiB | 250.0 µs, 161.5 KiB |
| chunks 64x16 | 1,000,000 | 242 ns, 1.8 KiB | 3.1 µs, 3.8 KiB | 293.7 µs, 162.7 KiB |
| chunks 64x64 | 10,000 | 237 ns, 2.1 KiB | 1.9 µs, 3.1 KiB | 168.2 µs, 161.5 KiB |
| chunks 64x64 | 100,000 | 249 ns, 2.6 KiB | 2.3 µs, 4.6 KiB | 210.1 µs, 162.1 KiB |
| chunks 64x64 | 1,000,000 | 291 ns, 2.6 KiB | 2.4 µs, 4.6 KiB | 210.1 µs, 163.1 KiB |
| chunks 128x64 | 10,000 | 439 ns, 3.1 KiB | 2.1 µs, 5.1 KiB | 158.7 µs, 160.8 KiB |
| chunks 128x64 | 100,000 | 438 ns, 3.6 KiB | 2.5 µs, 5.6 KiB | 204.0 µs, 161.3 KiB |
| chunks 128x64 | 1,000,000 | 426 ns, 3.6 KiB | 2.5 µs, 5.6 KiB | 204.1 µs, 163.3 KiB |
| chunks 256x64 | 10,000 | 515 ns, 5.1 KiB | 2.1 µs, 5.1 KiB | 164.4 µs, 162.2 KiB |
| chunks 256x64 | 100,000 | 576 ns, 5.1 KiB | 2.4 µs, 9.1 KiB | 164.7 µs, 162.2 KiB |
| chunks 256x64 | 1,000,000 | 565 ns, 5.6 KiB | 2.8 µs, 9.6 KiB | 208.6 µs, 166.2 KiB |
| chunks 64x256 | 10,000 | 905 ns, 5.1 KiB | 2.5 µs, 6.1 KiB | 167.5 µs, 165.5 KiB |
| chunks 64x256 | 100,000 | 1.0 µs, 5.1 KiB | 2.6 µs, 7.1 KiB | 167.8 µs, 165.5 KiB |
| chunks 64x256 | 1,000,000 | 1.5 µs, 7.1 KiB | 3.7 µs, 9.1 KiB | 210.7 µs, 166.5 KiB |
| flat directory 64 | 10,000 | 3.0 µs, 10.8 KiB | 4.2 µs, 21.6 KiB | 113.3 µs, 179.0 KiB |
| flat directory 64 | 100,000 | 34.9 µs, 98.7 KiB | 36.2 µs, 198.4 KiB | 144.4 µs, 354.8 KiB |
| flat directory 64 | 1,000,000 | 839.5 µs, 977.6 KiB | 834.5 µs, 1.9 MiB | 892.4 µs, 2.1 MiB |

## Code size

`examples/probe.rs` built as a shared library around each map, with
the release profile of the workspace. Bytes of the sections that take
file space (code and data), of the file, which keeps its symbol table
and pads segments to pages, and of the file compressed by `gzip -9`, as
a wheel's deflate would. Each Added column is the difference from the
baseline, a plain `Vec` behind the same functions.

| Probe | Code and data | Added | File | Added | Compressed | Added |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 258563 | 0 | 383824 | 0 | 152245 | 0 |
| rpds-hash | 273321 | 14758 | 409008 | 25184 | 160419 | 8174 |
| rpds-tree | 263147 | 4584 | 385936 | 2112 | 154783 | 2538 |
| im-hash | 269272 | 10709 | 406448 | 22624 | 159087 | 6842 |
| im-ord | 271004 | 12441 | 403136 | 19312 | 159082 | 6837 |
| imbl-hash | 276745 | 18182 | 408960 | 25136 | 162452 | 10207 |
| imbl-ord | 269042 | 10479 | 404848 | 21024 | 158485 | 6240 |
| chunks | 263731 | 5168 | 384176 | 352 | 155213 | 2968 |
