# Persistent map

The measurements behind issue #9: the persistent map from object number to
written object that every version holds ([DESIGN.md](../../DESIGN.md)
sections 4.1 and 16). The recommendation is a comment on the issue; this
directory holds the benchmark, its results, and the machine they ran on.

## Candidates

| Map | Structure | Source |
| --- | --- | --- |
| rpds HashTrieMap | Hash array mapped trie, 64-way | rpds 1.2.1, `HashTrieMapSync` |
| rpds RedBlackTreeMap | Red-black tree, a node and an entry allocation per object | rpds 1.2.1, `RedBlackTreeMapSync` |
| im HashMap | Hash array mapped trie, 32-way, entries inline in fixed-size nodes | im 15.1.0 |
| im OrdMap | B-tree, 64 entries per node | im 15.1.0 |
| imbl HashMap | im's trie with SIMD-searched small nodes | imbl 7.0.2 |
| imbl OrdMap | B-tree, 16 entries per node | imbl 7.0.2 |
| chunks CxF | A flat table indexed by object number, in copy-on-write chunks of C slots under a radix directory of F-way branches | [src/chunks.rs](src/chunks.rs) |
| flat directory 64 | The same chunks under one flat directory | [src/flat.rs](src/flat.rs) |

imbl is not a candidate of the issue. It is measured beside im because im's
repository is archived and RustSec marks im unmaintained
(RUSTSEC-2026-0248), so imbl is what choosing im would mean in practice.

The chunk table is about 180 lines of safe Rust with comments. Keys are
positions, so a lookup indexes one array per level without hashing or
comparing. Cloning a table shares everything, and `Arc::make_mut` copies a
chunk or a branch only while another version still holds it, so a
transaction copies one chunk and the branches above each object it writes.
The main tables use 16-slot chunks under 32-way branches, among the
variants that wrote at the lowest cost in time and memory at every size; the
variant tables show the trade-offs of the others. The flat directory
variant shows why the directory must be a tree: its first write in a
transaction copies one pointer per chunk of the whole table.

Every map implements the `Map` trait of [src/maps.rs](src/maps.rs), which
requires `Send + Sync`; that the benchmark compiles is the check. The hash
maps use a fixed hash of the object number, MurmurHash3's finalizer, which
maps 32-bit numbers one to one: no two object numbers collide in the 32 bits
that im and imbl use or the 64 that rpds uses, so a fixed function opens no
collision attack.

## Workloads

- **Values.** A 16-byte `Slot` ([src/slot.rs](src/slot.rs)): an `Arc` to a
  written object or the generation of a free entry, as DESIGN.md section 4.1
  describes a version's map. Copying a node or chunk therefore increments one
  reference count per value it holds. The written objects are allocated
  before each measurement and hold no bytes, so memory counts only the map.
- **Object numbers.**
  - *dense*: 1 to N, as most files number their objects;
  - *sparse*: N numbers drawn uniformly from 1 to 8N, as after incremental
    updates that freed objects or a repair that found scattered numbers; at
    N = 1,000,000 the largest number nears 8,388,607, the limit in PDF 1.7's
    Annex C;
  - *scattered*: N = 100,000 numbers drawn uniformly from the 32-bit range,
    the hostile case that leaves one object in each chunk of a chunk table.
- **Operations**, at N = 10,000, 100,000, and 1,000,000:
  - *insert*: building the map in place in random object-number order, per
    object;
  - *lookup*: 2^20 lookups of present numbers drawn at random (hit), and of
    absent numbers inside the range (miss, not for dense numbers);
  - *ordered visit*: every object in object-number order, as saving needs;
    the hash maps sort their entries;
  - *memory*: the bytes that dropping the whole map frees, per object.
- **Transactions**: cloning the current version and writing 1, 100, or
  10,000 objects, the median time to commit, and the bytes the new version
  adds while the base stays alive. *update* rewrites objects drawn uniformly
  from the map, the worst case for locality; *append* writes new objects at
  the next free numbers. Short transactions run in batches of 256 or 16.
- **Code size**: [examples/probe.rs](examples/probe.rs) built as a shared
  library around each map, with the workspace's release profile, against a
  baseline of a plain `Vec` behind the same functions.

Bytes are the sizes requested from the allocator, before the system
allocator rounds them up to its size classes, so maps with many small blocks
use more than their bytes show; the Blocks column counts allocations per
object.

## Running

```bash
./run.sh
```

It writes `results/<system>-<architecture>.md` and `.csv`. The benchmark
runs single-threaded and asks macOS for a performance core. It makes two
passes over every measurement, a minute or more apart, and the tables show
the lower of the two, since other work on the machine only adds time; the
CSV keeps both. On the machine of the results the two passes took about four
minutes, and the probes build afterwards. The chunk table's tests compare it
with a `BTreeMap` over random writes and kept versions:

```bash
cargo test --release
```

Every candidate also compiles for WebAssembly, which Pyodide needs:

```bash
cargo check --lib --release --target wasm32-unknown-unknown
```

## Results

[results/macos-arm64.md](results/macos-arm64.md) holds every table, and
[results/macos-arm64.csv](results/macos-arm64.csv) every number of both
passes. They ran on 2026-10-24 on an Apple M2 Pro (8 performance and 4
efficiency cores, 32 GiB) under macOS 27.0.1, with Rust 1.99.1, while other
work kept the machine's load average between 4 and 6. Between the two
passes, the median ratio of each kind of timing was between 1.00 and 1.03,
and 90 % of the ratios fell between 0.90 and 1.37; memory was identical.
Differences in time of less than about a third are therefore not
established here.

Operations at 1,000,000 objects with dense numbers, per object, with the
lookup misses of sparse numbers and the memory of all three distributions:

| Map | Insert | Lookup, hit | Lookup, miss (sparse) | Ordered visit | Memory, dense | Memory, sparse | Memory, scattered (100,000) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 181 ns | 129 ns | 34 ns | 72 ns | 106 B | 106 B | 104 B |
| rpds RedBlackTreeMap | 935 ns | 763 ns | 845 ns | 55 ns | 88 B | 88 B | 88 B |
| im HashMap | 116 ns | 44 ns | 31 ns | 107 ns | 322 B | 322 B | 336 B |
| im OrdMap | 318 ns | 176 ns | 180 ns | 8 ns | 48 B | 48 B | 48 B |
| imbl HashMap | 136 ns | 48 ns | 32 ns | 41 ns | 134 B | 135 B | 159 B |
| imbl OrdMap | 208 ns | 174 ns | 181 ns | 15 ns | 39 B | 39 B | 39 B |
| chunks 16x32 | 29 ns | 7 ns | 14 ns | 2 ns | 18 B | 124 B | 807 B |

Transactions that rewrite objects drawn at random from a map of dense
numbers: the time to commit and the memory that the new version adds. The
last column writes new objects at the next free numbers instead.

| Map | 1 write, 10,000 objects | 1 write | 100 writes | 10,000 writes | 10,000 new objects |
| --- | ---: | ---: | ---: | ---: | ---: |
| rpds HashTrieMap | 264 ns, 1.2 KiB | 1.0 µs, 1.8 KiB | 78.4 µs, 98.9 KiB | 6.3 ms, 3.7 MiB | 4.6 ms, 3.7 MiB |
| rpds RedBlackTreeMap | 336 ns, 708 B | 1.1 µs, 1.1 KiB | 91.2 µs, 73.4 KiB | 9.7 ms, 3.6 MiB | 3.3 ms, 860.6 KiB |
| im HashMap | 467 ns, 3.4 KiB | 1.6 µs, 4.9 KiB | 145.8 µs, 297.6 KiB | 10.4 ms, 16.0 MiB | 8.8 ms, 16.3 MiB |
| im OrdMap | 787 ns, 6.2 KiB | 1.7 µs, 8.2 KiB | 126.9 µs, 417.9 KiB | 7.2 ms, 16.9 MiB | 899.3 µs, 649.3 KiB |
| imbl HashMap | 315 ns, 2.6 KiB | 1.3 µs, 4.4 KiB | 110.6 µs, 256.1 KiB | 8.3 ms, 12.0 MiB | 7.8 ms, 14.4 MiB |
| imbl OrdMap | 216 ns, 1.2 KiB | 668 ns, 1.7 KiB | 57.9 µs, 101.9 KiB | 5.8 ms, 5.2 MiB | 899.8 µs, 548.2 KiB |
| chunks 16x32 | 138 ns, 832 B | 429 ns, 1.4 KiB | 32.4 µs, 67.3 KiB | 2.7 ms, 2.9 MiB | 257.2 µs, 172.6 KiB |
| flat directory 64 | 515 ns, 2.3 KiB | 44.0 µs, 123.1 KiB | 87.7 µs, 223.3 KiB | 4.1 ms, 7.5 MiB | 154.7 µs, 403.6 KiB |

What else the results show:

- Every map's single write costs more at 1,000,000 objects than at 10,000:
  its path has more levels, and fewer of its nodes are in cache. The flat
  directory alone grows with the size of the map, to 44.0 µs and 123.1 KiB
  for one write at 1,000,000 objects.
- With sparse numbers the chunk table still looks up fastest, visits in
  order at 20 ns per object against the B-trees' 8 to 13 ns, and commits
  about as fast as imbl's OrdMap, the fastest library. It uses 124 B per
  object, more than every library but the HashMaps of im and imbl, and with
  10,000 writes into 1,000,000 objects its version adds 4.7 MiB, against 3.6
  to 5.2 MiB for rpds and imbl's OrdMap.
- Scattered numbers are the chunk table's worst case: 807 B and 2.91
  allocations per object, 47 ns per lookup, 234 ns per object to visit in
  order, and 6.5 ms and 9.5 MiB for 10,000 writes, where rpds' HashTrieMap
  takes 1.3 ms and 1.8 MiB.
- Dropping a whole version of 1,000,000 objects took 6.4 ms for the chunk
  table, 8.6 to 41 ms for the OrdMaps and imbl's HashMap, and 113 to 213 ms
  for rpds' maps and im's HashMap (`drop_all_ns` in the CSV).

### Code size

What each map adds to a shared library, from the results' probe table; the
baseline holds 258,563 bytes of code and data and compresses to 152,245.

| Map | Code and data | Compressed |
| --- | ---: | ---: |
| rpds HashTrieMap | 14,758 B | 8,174 B |
| rpds RedBlackTreeMap | 4,584 B | 2,538 B |
| im HashMap | 10,709 B | 6,842 B |
| im OrdMap | 12,441 B | 6,837 B |
| imbl HashMap | 18,182 B | 10,207 B |
| imbl OrdMap | 10,479 B | 6,240 B |
| chunks 16x32 | 5,168 B | 2,968 B |

### Licenses and maintenance

As of 2026-10-24, from crates.io, the repositories, and the RustSec
advisory database:

| Source | Release | License | State | Dependencies |
| --- | --- | --- | --- | --- |
| rpds | 1.2.1, 2026-05-15 | MIT since 1.1.2 (2025-10-06), MPL-2.0 before | Commits until 2026-10-03; no advisory | archery (MIT), triomphe, smallvec |
| im | 15.1.0, 2022-04-29 | MPL-2.0+ | Repository archived on 2026-05-03; RUSTSEC-2026-0248 (unmaintained) and RUSTSEC-2023-0126 (unsound, no fix); its sized-chunks has RUSTSEC-2026-0251 (unmaintained) and RUSTSEC-2026-0255 (unsound, no fix) | sized-chunks and bitmaps (MPL-2.0+, archived), typenum, rand_core, rand_xoshiro |
| imbl | 7.0.2, 2026-09-09 | MPL-2.0+ | Commits until 2026-09-09; no advisory against 7.0.2, whose imbl-sized-chunks 0.2.0 fixes RUSTSEC-2026-0292 | archery, triomphe, imbl-sized-chunks (MPL-2.0+), wide, safe_arch, bytemuck, rand_core, rand_xoshiro, equivalent |
| chunks | [src/chunks.rs](src/chunks.rs) | MIT, the repository's | Maintained with pyropdf | None |

The dependencies whose license the table omits offer MIT among their
licenses.

## Limits

- One machine. Timings on x86-64 Linux, where CI runs, or under
  WebAssembly may rank differently where they are close.
- One thread. The maps are shared by atomic reference counts, so readers on
  other threads would add contention that this does not measure.
- The values are reference-counted, as DESIGN.md section 4.1's written
  objects would be. Copying a chunk or node therefore touches the counts of
  the objects it holds; values without a count would make copies cheaper.
- Transactions write uniformly random objects, the worst case for the
  locality of the chunk table and the B-trees. Edits that touch neighboring
  object numbers copy fewer chunks and nodes.
- Bytes are what the maps request. The system allocator's size classes add
  more to maps of many small blocks, which the Blocks column counts.
