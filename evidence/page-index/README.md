# page.index

The measurement behind the second acceptance criterion of issue #33:
`page.index` takes constant time (DESIGN.md section 10) on documents of 110,
440, and 1,760 pages, the section 2.1 sizes.

## Workload

One flat page tree of N pages (built as `tests/test_pages.py` builds its documents). Each
column is the median of 21 rounds of 1,000 calls on the middle page:

- `page.index`: a hit of the memoized page index (#26, #29) and a binary
  search by object number.
- `doc.pages[i]`: the same hit, and a new handle.
- After an edit: rotating another page, then `page.index`. The rotation
  rewrites a page object that the walk read, so the index is walked again:
  that cost grows with the pages, and is paid once per edit, not per read.

## Running

```sh
uv run python evidence/page-index/measure.py
```

## Results

`results/macos-arm64.md`: 0.18 to 0.19 µs at every size, so reads do not grow
with the document. A re-walk after an edit takes 24 µs at 110 pages and
317 µs at 1,760.
