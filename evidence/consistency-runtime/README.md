# Consistency harness runtime

On an Apple M2 Pro (macOS 27.2, CPython 3.15.2, pytest 9.2.0, qpdf 12.4.2),
the full consistency module took 149.5 seconds on `07569871` and 75.4 seconds
with shared fixtures. Both runs passed the same 7,650 test IDs. Each time is
one local wall-clock measurement with the locked environment already installed.

| Measurement | Before | After |
| --- | ---: | ---: |
| Elapsed seconds including pytest collection | 149.489 | 75.428 |
| Edit/save preparations | 7,650 | 2,550 |
| Total inspections per reader | 5,100 | 2,565 |
| Saved documents checked per reader | 2,550 | 2,550 |
| Original inspections per reader | 2,550 | 15 |

There are 15 source PDFs, 85 edits and two save modes. Each passing structural,
qpdf or pikepdf case checks its saved output once. The instrumented run counts
actual calls to each reader and to `Edit.open`. Subtracting that reader's
passed cases from its total calls gives the original inspections. Full and
incremental saves are prepared independently.

## Reproduce

Set `MEASURE` to the absolute path of this directory's `measure.py`. From
each checkout (base `07569871` and the revision containing this change), run
with qpdf on PATH, using the same Python interpreter version and machine.

```sh
uv sync --locked --python 3.15
uv run --no-sync python "$MEASURE"
uv run --no-sync python "$MEASURE" --counts
```

The first command invoking the script measures elapsed time. The second counts
calls separately. Both require every collected case to pass and print a SHA-256
digest of the sorted passed node IDs. Both revisions produced
`c10e9396da16cf5aee52d661aa90e7591d2d388f332b82135c1cce4a521bea0d`.

The [CI run that prompted this measurement](https://github.com/yhay81/pyropdf/actions/runs/44990850241)
spent 7m52s in the full Python 3.11 pytest step. Wheel building and the separate
veraPDF, Arlington and pdf.js checks run outside that step.
