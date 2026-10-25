# page.pixmap after an edit

The measurement behind the second acceptance criterion of issue #35: after
`page.rotation = 90`, the next `page.pixmap()` costs what the bridge's
benchmark ([evidence/bridge-render](../bridge-render)) reports for an edit
followed by a render.

## Workload

The documents of evidence/bridge-render, 110, 440, and 1,760 letter pages of
text, opened from bytes. Each of 41 edits sets the rotation of one page,
drawn in turn, and renders that page at 72 dpi without alpha; the table gives
the medians. *Rotation* is the transaction; *Pixmap* is the page index walk,
the bridge, hayro's re-read, and the render, with the GIL released, and the
copy into the `Pixmap`.

## Running

```sh
uv run python evidence/page-pixmap/measure.py
```

## Results

`results/macos-arm64.md`. The totals, 1.2, 1.5, and 2.9 ms, are those of
evidence/bridge-render's Rust loop (1.24, 1.22, and 2.31 ms) plus the Python
call and the copy of the raster; the edit itself is a few microseconds.
