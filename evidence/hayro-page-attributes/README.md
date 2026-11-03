# `Pdf::new` with page attributes read on first use (issue #587)

hayro-syntax's `Pdf::new` builds the page list, which is about 95% of it on
copies of `bill-hr815.pdf` (#585). Building a page read its media box, crop
box, rotation, and resources, and reading the resources resolves the page's
`/Resources` dictionary and the dictionaries in it. The upstream change reads
them on first use instead, with no change to hayro-syntax's API. This
measures `Pdf::new` at the change's base and at its head. The bridge calls
`Pdf::new` for every version that pyropdf renders (DESIGN.md section 6).

Caching the resources makes hayro-syntax's `Pages` invariant over its
lifetime, so the change shortens that lifetime with a `transmute`. A third
version, `geometry`, defers only the boxes and the rotation, which needs no
new `unsafe`, to show what each part gains.

## Method

- `Cargo.toml` depends on hayro-syntax three times, by git revision:
  `before` is LaurenzV/hayro `main` at the base of the change, `after` the
  change, and `geometry` its variant, both on yhay81/hayro. Path
  dependencies of the same version would collide in the lockfile.
- `run.py` writes documents of 110, 440, and 1,760 pages: copies of
  `tests/assets/real_world/bill-hr815.pdf` joined with `pages.extend`, as in
  evidence/edit-render.
- The harness (`src/main.rs`) first checks that all versions read the same
  pages: their count, boxes, rotations, and the names in every resource
  dictionary. It then runs the versions in turn, changing which goes first
  with every run, and prints the median of 101 runs of `Pdf::new`, and of
  `Pdf::new` followed by reading every page's attributes, the work that the
  change moves to first use.
- `--corpus` does the same for every PDF below the given directories, with
  5 runs each, and prints the totals.

```sh
uv run python evidence/hayro-page-attributes/run.py
uv run python evidence/hayro-page-attributes/run.py --corpus HAYRO/tests/downloads HAYRO/tests/pdfs
```

`HAYRO` is a hayro checkout whose test suite has run once, which downloads
its test files into `tests/downloads`.

## Result

`results/windows-x86_64.md`. `Pdf::new` is 2.2 to 2.6 times faster at every
size; deferring only the boxes and the rotation makes it 1.2 to 1.4 times
faster. The work moves to first use: opening and then reading every page's
attributes costs from 8% less to 13% more than before at 110 and 440 pages,
and 7 to 19% more at 1,760 pages. Over 1,675 PDFs of hayro's tests, all
versions read the same pages for every PDF that any of them opens, and the
total of `Pdf::new` is 1.34 times smaller.

What remains of `Pdf::new` is resolving every page's dictionary while it
walks the page tree, which still grows with the page count. An object
source (I1, #14) whose first step builds this page list keeps that part.
