# Rotate and render

The measurement that Phase 0's exit asks for (issue #38, DESIGN.md section
2.1): the edit-and-read loop of
[evidence/pylopdf-baseline/edit_read_loop.py](../pylopdf-baseline/edit_read_loop.py),
with rendering in place of text, which arrives in Phase 1, on pyropdf and
pylopdf.

## Workload

- **Documents.** pylopdf builds 110, 440, and 1,760 pages from
  `bill-hr815.pdf` as edit_read_loop.py does: copies joined with
  `pages.extend` and written with `to_bytes()`. Both libraries open the same
  files.
- **Loops.** *Render all*: `page.pixmap(dpi=18)` for every page. *Rotate and
  render all*: for every page, `doc.pages[i]`, `page.rotation = 90`, and
  `page.pixmap(dpi=18)`. The low resolution makes the loop measure the edit
  and the re-read of the document more than rasterization. Each library runs
  in a process of its own (`side.py`); pyropdf's `uv run` builds a release
  extension.
- **Split.** `split/` runs pyropdf's loop through the store and the engine
  without Python and reports, per page, the transaction, the page index
  walk, the bridge's two parts from its instrumentation (#30): building the
  bytes, and hayro's re-read of them, and the render.

## Running

```sh
uv run python evidence/edit-render/measure.py PYLOPDF_PYTHON tests/assets/real_world/bill-hr815.pdf
```

`PYLOPDF_PYTHON` is a Python with pylopdf installed, such as a pylopdf
checkout's `.venv/bin/python`.

## Results

`results/macos-arm64.md`. At 1,760 pages, rotating and rendering every page
takes 11.1 s on pyropdf against 22.8 s on pylopdf: 2.3 times rendering
alone, against 7.1. Rendering without edits is slower on pyropdf (4.8 s
against 3.2 s), which this loop does not explain; it is not the edit cost.

The split shows where pyropdf's remaining growth is: at 1,760 pages, of
5.8 ms per page, hayro's re-read of the version takes 2.7 ms, building its
bytes 0.4 ms, and the page index walk 0.6 ms, all of which grow with the
document, while the transaction (7 µs) and the render (2.1 ms) do not. I1
(#14) removes the re-read and the bytes; a page-tree edit that kept the
walk's read set elsewhere would remove the walk.
