# Where hayro's `Pdf::new` goes (issue #585)

hayro-syntax's `Pdf::new` reads the cross-reference data (`root_xref`) and
then builds the page list (`CachedPages::new`), which resolves every page of
the page tree with its inherited attributes. The bridge calls it for every
version that pyropdf renders (DESIGN.md section 6), and
[evidence/edit-render](../edit-render) measures that re-read growing with the
document. This measures how the two steps share it.

## Method

- `run.py` writes documents of 110, 440, and 1,760 pages: copies of
  `tests/assets/real_world/bill-hr815.pdf` joined with `pages.extend`, as in
  evidence/edit-render.
- Both steps are crate-private, so `run.py` copies the hayro-syntax that the
  workspace locks from the Cargo registry into
  `target/evidence/hayro-page-list/`, wraps the two calls in
  `Pdf::new_with_password` in timers, and builds the harness (`src/main.rs`)
  against that copy. The repository holds no copy of hayro's code: the patch
  is the two replacements and the module in `run.py`.
- The harness opens each document 50 times and prints the median of each
  step.

```sh
uv run python evidence/hayro-page-list/run.py
```

## Result

`results/windows-x86_64.md`. Building the page list is 94 to 95% of
`Pdf::new` at every size, about 4 µs per page on that machine; reading the
cross-reference data is the rest. Both grow with the document.

The bridge hands hayro the base bytes and an update whose cross-reference
stream lists every object, so its re-read in evidence/edit-render builds the
same page list. An object source (I1, #14) whose first step still builds the
page list eagerly, as the draft behind LaurenzV/hayro#1659 did, therefore
removes the serialization and the cross-reference read but not most of the
re-read. That needs the page list built on access, or kept across versions.
