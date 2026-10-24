# Transaction cost

The measurement behind the second acceptance criterion of issue #25: a
transaction that writes one object copies one chunk and the branches above
it, at most six levels for 32-bit object numbers, so its time and memory grow
only with the height of the version's table ([DESIGN.md](../../DESIGN.md)
section 4.1, F2). The table is the chunk table that issue #9 chose and
[evidence/persistent-map](../persistent-map) measured; the store's own copy
is `crates/pyropdf-store/src/version/table.rs`.

## Workload

- **Documents.** 10,000, 100,000, and 1,000,000 objects, whose numbers are
  *dense*, 1 to N, as most files number their objects, or *scattered*, drawn
  uniformly from the 32-bit range: hostile input, which leaves one object in
  each chunk and needs all six levels of branches at every size. A first
  transaction writes every object, the integer 1, so that the version's
  table holds all of them.
- **Transactions.** Each measured transaction begins, rewrites one object
  drawn uniformly from the document with the integer 2, and commits, through
  `pyropdf_store::version::Document`, so the time includes serializing the
  value, the copies, the change set that the commit computes, and the
  publication. Uniform draws are the worst case for locality: no two
  transactions in a row are likely to share a path.
- **Memory.** While the history keeps every version, a counting allocator
  measures what a transaction allocates, less what it frees, with the
  version before it still alive; the history reports what that version keeps
  alive. Both are the median of 101 transactions.
- **Time.** Without a history, so that each commit frees the version before
  it, as it does when nothing else holds that version, batches of 64
  transactions are timed, so that the clock's resolution does not matter. The
  table gives the median and the 90th percentile of 1,001 batches, the lower
  of 3 passes.

The columns of the results:

- *Height*: the levels of branches that the table needs for the largest
  number.
- *Chunks and branches copied*: the chunk of the rewritten object and the
  branches above it, one per level.
- *Kept alive*: the bytes that the version before the transaction holds and
  the new version does not share, which the history counts against its
  limit: the copied chunk and branches, 272 bytes for the chunk and 280 for
  each branch, and the replaced object, 104 bytes and the one byte of its
  value, plus 108 bytes for the version record, its one-number change set,
  and its history entry. Parsed-value caches and unused deque capacity are
  excluded. The history entry is counted even when a deque allocation is
  amortized across several commits.
- *Allocated* and *Allocations*: what the new version adds while the version
  before it stays alive: its own copies and object, the version itself, and
  its change set of one number, 84 bytes for the last two together.

## Running

```bash
./run.sh
```

It writes `results/<system>-<architecture>.md` and `.csv`, and takes about
ten seconds. The scattered document of 1,000,000 objects brings the
process's peak memory to about 900 MB. The store's unit test
`one_write_keeps_one_chunk_and_the_branches_above_it_alive` asserts the
bytes kept alive at heights 2, 3, and 6 on every run of `cargo test`.

## Results

[results/macos-arm64.md](results/macos-arm64.md) and
[results/macos-arm64.csv](results/macos-arm64.csv) ran on 2026-10-24 on an
Apple M2 Pro (8 performance and 4 efficiency cores, 32 GiB) under macOS
27.0.1, with Rust 1.99.1. These results were regenerated after the review
fixes for history accounting and cancellation. They replace the original
run, whose history count omitted version records and history entries.

| Numbers | Objects | Height | Chunks and branches copied | Kept alive | Allocated | Allocations | Time, median | Time, 90th percentile |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dense | 10,000 | 2 | 3 | 1,045 B | 1,021 B | 8 | 874 ns | 891 ns |
| dense | 100,000 | 3 | 4 | 1,325 B | 1,301 B | 9 | 1.20 µs | 1.24 µs |
| dense | 1,000,000 | 4 | 5 | 1,605 B | 1,581 B | 10 | 1.87 µs | 1.93 µs |
| scattered | 10,000 | 6 | 7 | 2,165 B | 2,141 B | 12 | 1.91 µs | 1.96 µs |
| scattered | 100,000 | 6 | 7 | 2,165 B | 2,141 B | 12 | 2.53 µs | 2.60 µs |
| scattered | 1,000,000 | 6 | 7 | 2,165 B | 2,141 B | 12 | 2.87 µs | 3.00 µs |

What the results show:

- **Memory depends on the height alone.** Each level adds one branch of 280
  bytes and one allocation, and nothing else changes: scattered numbers cost
  2,165 bytes kept alive and 2,141 allocated at 10,000 objects and at
  1,000,000 alike. Six levels are the most that 32-bit numbers need, so no
  transaction that writes one object keeps more than one chunk, six
  branches, the object it replaced, and the retained version record alive.
- **Time grows with the height, and with the size through the cache.** A
  transaction takes 0.87 µs at two levels and 1.87 µs at four. At the same
  height, scattered numbers take 1.91 µs at 10,000 objects and 2.87 µs at
  1,000,000: the work is the same seven copies, but fewer of the nodes on a
  random path are in the processor's caches when the table is larger. Issue
  #9 measured the same effect for every candidate map, which is why the
  acceptance bounds the cost by the height rather than calling it constant.
  For scale, pylopdf clones the whole document for a field edit so that it
  can roll back, and a fill took 2.2 ms with 2,626 objects and 6.4 to 6.6 ms
  with 8,646 (DESIGN.md section 2.1).

## Limits

- One machine and one thread. Timings on x86-64 Linux, where CI runs, or
  under WebAssembly may differ in scale.
- The values are one-byte integers, so the time is the store's own cost.
  Larger values add the writer's time and their bytes, which the transaction
  writes once.
- The timed loop reuses one `Budget` for every transaction. A binding that
  makes a budget for each call adds that cost.
