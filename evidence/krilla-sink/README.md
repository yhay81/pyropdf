# krilla: write into an object sink that the caller supplies (#151)

DESIGN.md section 7, item W1. Today krilla writes a complete PDF file.
pyropdf's canvas needs krilla to write its objects into the store instead.
This directory holds a local krilla patch and a probe that writes into
pyropdf-store through it. Nothing here has been proposed upstream:
publishing to LaurenzV/krilla needs the owner.

## Proposal for the maintainer

`krilla::sink` adds one trait and one result type. `Document` gains
`finish_into`, which is `finish` with a sink in place of a file:

```rust
pub trait ObjectSink {
    /// Allocates the number of a new indirect object in the receiving
    /// document. Numbers must be positive and unique.
    fn allocate(&mut self) -> i32;
    /// Receives a sequence of complete indirect objects, each numbered with
    /// a number from `allocate`.
    fn write(&mut self, objects: &[u8]);
}

pub struct SinkOutput {
    pub pages: i32,
    pub acro_form: Option<i32>,
    pub page_labels: Option<i32>,
    pub output_intents: Option<i32>,
    pub struct_tree_root: Option<i32>,
    pub outlines: Option<i32>,
    pub named_destinations: Vec<(Vec<u8>, i32)>,
    pub embedded_files: Vec<(Vec<u8>, i32)>,
}

impl Document {
    pub fn finish_into(self, sink: &mut dyn ObjectSink) -> KrillaResult<SinkOutput>;
}
```

- **Object numbers.** krilla already writes every object with a provisional
  `Ref`, then renumbers all chunks in one pass in `ChunkContainer::finish`.
  The patch moves that pass into `ChunkContainer::remap`, which takes the
  allocator as a closure. `finish` passes the sequential counter it used
  before; `finish_into` passes `ObjectSink::allocate`. The sink therefore
  decides every number, and krilla never sees the receiving document.
- **The existing serializer is one sink among others.** Both paths share
  `serialize_deferred` (fonts, pages, the page tree, forms, tags), `remap`,
  and the same chunk visit order. They differ only in where renumbered chunks
  go: appended to the `Pdf`, or handed to the sink. The validation, limit, and
  "all objects taken" checks run on both (`check`).
- **What the sink owns.** The catalog, the document information dictionary,
  the XMP metadata, and the name trees belong to the receiving document.
  `finish_into` returns the numbers that a catalog would reference, plus the
  named destinations and embedded files that `finish` writes into the
  catalog's name trees.
- **Byte form.** `write` receives pdf-writer's bytes of whole indirect
  objects, renumbered. pdf-writer does not expose object boundaries in a
  `Chunk`. A per-object `write(number, bytes)` would need that from
  pdf-writer, so this proposal leaves it as a question for the maintainer.

W2 (#152), document resources with fonts subset at finalization, builds on
this: pyropdf needs only content streams and their resources, not krilla's
page tree.

## Patch and checks

`krilla-object-sink.patch` applies to krilla main `631e4645` (local branch
`object-sink`, commit `3626e89`). With the patch applied:

- `cargo test -p krilla` passes (24 + 2 tests), including two new tests in
  `sink.rs`: the sink receives every object exactly once with its own
  numbers, and the file holds the same objects plus the catalog and the
  information dictionary.
- `cargo test -p tests` (245) passes. With `--all-features`, the 2043
  visual-regression tests fail only because Docker is not available
  locally; no other test fails.
- clippy reports nothing in the files the patch touches.

## Probe

`build.sh` checks out krilla at the base commit, applies the patch, runs
krilla's sink tests, and runs `probe/`. The probe is a crate outside the
workspace that depends on the patched krilla and on `pyropdf-store`. Its sink
allocates numbers from a store `Transaction`, parses each object with
`Value::parse` (hayro-syntax, F5), writes it, commits, and reads every object
back. It also checks that `SinkOutput::pages` names a `/Pages` dictionary.

```bash
bash evidence/krilla-sink/build.sh
```

Results on an Apple Silicon Mac, release build, one rectangle per page:

| Pages | Objects | Bytes | finish_into (ms) | stored and committed (ms) |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 4 | 329 | 0.816 | 1.017 |
| 10 | 31 | 2926 | 0.194 | 0.283 |
| 100 | 301 | 29470 | 1.498 | 2.034 |
| 1000 | 3001 | 300181 | 14.170 | 19.014 |

Both columns grow linearly with the objects. Storing adds about a third to
krilla's own time, measured from the start of `finish_into`. The first row
includes warm-up.
