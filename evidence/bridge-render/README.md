# Bridge render

The measurement behind the third acceptance criterion of issue #30: an edit
followed by a render through the hayro bridge (`crates/pyropdf-engine/src/bridge/`,
[DESIGN.md](../../DESIGN.md) section 6), at the 110, 440, and 1,760 pages of
the section 2.1 workloads, split as the bridge's `Stats` reports it.

## Workload

- **Documents.** A classic file of N letter-size pages in one flat page
  tree, each page with a filled rectangle and 20 lines of Helvetica text in
  its own content stream, opened from its bytes through `FileBase`.
- **Edits.** Each of 41 edits rotates one page, drawn in turn, by writing
  its page object in one transaction, then renders that page at 72 dpi.
  Before the first edit, the unedited version's page index and bridge are
  built once, so the table shows steady edits.
- **Columns.** *Update*: the bytes after the original that the bridge
  writes, the rewritten page object and the complete cross-reference stream
  of 13 bytes per object. *Transaction*: looking up the current page index
  (a memo hit) and the transaction that rotates the page. *Page index*: the
  walk of the edited version, which the rotated page's change invalidates.
  *Bridge: store*: building the bytes, which copies the original and writes
  the update. *Bridge: hayro*: `hayro_syntax::Pdf::new` on them, which
  reads the cross-reference stream and the page tree, and the check of
  hayro's page list against the index. *Render*: interpreting and
  rasterizing the page. Each is the median of the 41 edits.

## Running

```sh
./run.sh
```

## Reading the results

The store's part and hayro's re-read grow with the document, as DESIGN.md
section 6 expects until I1 (#14): the cross-reference stream lists every
object, the copy is of the whole file, and hayro reads the whole page tree
again. The render of one page does not. At 1,760 pages the store and hayro
take 1.2 ms together, about as long as rendering the page.

## Limits

- One machine and one thread, with the timer's resolution well below the
  shortest column.
- Synthetic pages: real documents with object streams and large resources
  give hayro more to read per version.
