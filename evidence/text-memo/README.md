# Memoized page results

The measurement behind issue #132: a page's text, words, layout, and content
are memoized with the objects that the page reaches (DESIGN.md section 5,
F4), so a later version whose edits miss those objects reuses them.

## Workload

The first page of four corpus documents (`tests/assets/real_world`). Each
row is the median of 21 rounds:

- Computed: the first call on a newly opened document. hayro interprets the
  page and the engine builds the result.
- Reused after an edit elsewhere: the same call after adding an object that
  nothing refers to. The memo checks the read set against the change and
  returns the stored result; the binding still builds new Python records.

## Running

```sh
uv run python evidence/text-memo/measure.py
```

## Results

`results/macos-arm64.md`: a reused `text` takes 2.5 to 6 µs against 1.4 to
5.2 ms computed, 400 to 1,300 times faster. `words`, `layout(chars=True)`,
and `content` save the interpretation, 1.5 to 6 ms a page, but every call
builds new records, since their lists are mutable: that conversion is what
remains, 0.3 to 12 ms, so they run 1.3 to 8 times faster.
