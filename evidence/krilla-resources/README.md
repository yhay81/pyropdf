# krilla: resources that documents share (#152)

DESIGN.md section 7, item W2. With the W1 sink (`evidence/krilla-sink/`),
pyropdf writes each drawing as a krilla document into its store. krilla
subsets and embeds every font per document, so stamping one face on 100
pages embeds 100 programs. This directory holds a local krilla patch, on top
of the W1 patch, that keeps fonts and images across documents, and a probe
that stamps through it into pyropdf-store. Nothing here has been proposed
upstream: publishing to LaurenzV/krilla needs the owner.

## Proposal for the maintainer

`krilla::resources` adds one type. A document borrows it by value for its
lifetime and returns it when it is written:

```rust
pub struct Resources { /* fonts, glyphs, and numbers */ }

impl Resources {
    pub fn new(settings: SerializeSettings) -> Self;
    /// Writes every font that a written document uses, subset to every
    /// glyph drawn so far, under the number the documents refer to.
    pub fn finalize_into(&self, sink: &mut dyn ObjectSink) -> KrillaResult<()>;
    /// Forgets the numbers for which `keep` is false, such as numbers that
    /// the sink rolled back.
    pub fn retain_numbers(&mut self, keep: impl FnMut(i32) -> bool);
}

impl Document {
    pub fn new_with_resources(resources: Resources) -> Self;
    pub fn finish_with_resources(
        self,
        sink: &mut dyn ObjectSink,
    ) -> (KrillaResult<SinkOutput>, Resources);
}
```

- **Registration.** A font is registered once per `Resources`, keyed like
  krilla's own font map. The first written document that uses one of its
  PDF fonts (the CID font, or one of its Type 3 fonts) allocates that font's
  number from the sink. Later documents refer to the same number: their
  provisional reference is pinned to it when `ChunkContainer::remap` numbers
  the chunks, and they write no font object. An image is written by the
  first document that draws it, and later documents pin their reference to
  its number, so they neither encode nor write it again. Numbers enter the
  resources only after the document is written and checked, so a failed
  `finish_with_resources` leaves none of its own numbers behind.
- **Accumulation.** The document takes the resources' `FontContainer`s into
  its `SerializeContext` font map and returns them when it is written, so
  the glyph remapper, the ToUnicode entries, the widths, and the Type 3
  mapper of a font keep growing across documents. Ownership passes by value:
  the type system allows one document at a time per `Resources`, and
  `Resources` is `Send`.
- **Finalization.** `finalize_into` serializes every numbered font into a
  fresh `SerializeContext` and writes it into the sink, with its root at the
  number the documents refer to and its dependent objects (descriptor, CID
  font, ToUnicode, program) at new numbers. It may run more than once; each
  run writes the subset of everything drawn so far. Font validation errors
  surface here, where the fonts are written.
- **Glyph-ID stability.** krilla already gives a glyph its CID in order of
  first use, never changes it, and places each glyph at its CID in the
  subset (`CIDToGIDMap` Identity). Type 3 codes are likewise assigned once.
  Keeping the remapper across documents therefore keeps every code that
  earlier content wrote, at any finalization. The test
  `glyphs_keep_their_codes_across_documents_and_finalization` draws "AB"
  and then "BC" and checks the codes and the ToUnicode map.
- **What stays per document.** Standard 14 fonts, color spaces, ICC
  profiles, shadings, and patterns are still written by each document; they
  are small, and the same pinning would extend to them.
- **Misuse.** `finish` and `finish_into` panic on a document that has
  resources, and `finish_with_resources` panics on one that has none.

### Questions for the maintainer

- Whether `finish_with_resources` should be the only finisher, with `finish`
  and `finish_into` building a private `Resources`, instead of three
  finishers and two panics.
- Whether to reuse the dependent objects' numbers across finalizations
  instead of allocating new ones; a sink can recycle numbers itself today.

## Patch and checks

`krilla-resources.patch` applies to krilla main `631e4645` after
`../krilla-sink/krilla-object-sink.patch` (local branch `resource-context`,
commit `246c1a0`, on `object-sink` `3626e89`). With both applied:

- `cargo test -p krilla` passes: 30 library tests, six of them new in
  `resources.rs`, and 2 doctests.
- `cargo test -p tests` passes (245).
- clippy reports nothing in the files the patch touches, and `cargo fmt`
  leaves them unchanged.

## Probe

`build.sh` checks out krilla at the base commit, applies both patches, runs
the resource tests, and runs `probe/`, a crate outside the workspace that
depends on the patched krilla and on `pyropdf-store`. The probe stamps
`Page {i}` in one face on each of N pages into a store transaction:

- once as N krilla documents with `finish_into` (W1 alone);
- once as N documents that share `Resources`, finalized once at the end.

It parses every object with `Value::parse` (hayro-syntax, F5), commits, and
checks that every page's font reference resolves to a Type0 font in the
committed version.

```bash
bash evidence/krilla-resources/build.sh
```

`results/macos-arm64.md` holds one run. With shared resources, stamping one
face on 100 pages writes **one font program**, against 100 without them,
and 42 KB against 290 KB. Writing the documents takes a fifth of the time,
since no document subsets a font; one finalization takes about 0.15 ms for
this face's 15 glyphs. Across four runs the times varied by about 10 %,
apart from one run whose 1,000-stamp shared row took 67 ms.

## Note for pyropdf

DESIGN.md section 7 keeps a font in use as a provisional object "with its
complete program" until a save. With codes assigned in order of first use,
the complete program fits that role only for TrueType outlines, through a
`CIDToGIDMap` stream from codes to the original glyph IDs; a CFF program
has no such map. Calling `finalize_into` whenever a transaction added glyphs
gives a provisional object that is a subset of everything drawn so far,
which works for both. The choice is raised on #152.
