# Paths as columns

The measurement behind issue #606: the paths of a page as `Paths`
columns, from `Page.paths()`, against one `Path` record per path, from
`Page.content()`, and against PyMuPDF's `get_cdrawings()` and
`get_drawings()` (DESIGN.md section 10).

## Approach

The binding builds one buffer per column from the interpretation that
`Page.content()` memoizes for paths, with the GIL released and no Python
object per path or segment. Python reads each column through the buffer
protocol and the whole table through the Arrow PyCapsule interface, and
the row view builds a `Path` record only when one is asked for. The page
memo keeps the interpretation's own tuples, one per path, as it does for
`Page.content()`.

## Workload

- The A1 page of the issue's reproduction, of 33,000 and of 200,000 short
  lines, each its own stroked path and one in ten red, as CAD output
  draws them.
- The corpus page with the most paths (`tests/assets/real_world`).

Each row of the tables is the median of five processes. A process opens
the document, renders the page at 36 dpi as the issue's reproduction does,
and then times one call:

- `pixmap`: `page.pixmap(dpi=36)` on a fresh document instead, the
  render that the others start with;
- `records`: `page.content(text_runs=False, images=False).paths`;
- `records_without_gc`: the same with the cyclic garbage collector off;
- `columns`: `page.paths()`;
- `columns_numpy`: `page.paths()` and `numpy.asarray` of its `to_x`,
  `to_y`, and `path` columns;
- `get_cdrawings` and `get_drawings`: PyMuPDF's, after
  `page.get_pixmap(dpi=36)`.

Every pyropdf call passes `max_elements=None`. `Time` is wall-clock time,
and `CPU time` the process's, which Windows counts in steps of 15.6 ms.
`Peak` is how far the call grew the process's peak resident set, and
`held` how much more the resident set holds once the call returns, with
its result still alive, in all and per path. Windows has no `resource`
module, so the script reads both from `GetProcessMemoryInfo`, as the
working set and its peak. On Linux and macOS it reads the peak from
`getrusage`, and `held` reads 0.

After the table of each page, the script times `content().paths` and
`paths()` once more with the interpretation memoized, which leaves only
what each builds, and adds up the bytes of the columns.

## Running

```sh
uv run --group oracles --with numpy python evidence/path-columns/measure.py
```

PyMuPDF (the `oracles` group) and NumPy are optional; without them their
rows read `n/a`. `results/windows-amd64.md` keeps one run.

## Results

From `results/windows-amd64.md`, on an AMD Ryzen 5 3600XT under Windows 11
with Python 3.14.8, hayro 0.9, and PyMuPDF 1.28.2. Processes of other work
kept the processor busy during the run, which is why each row is the median
of five processes and the tables give CPU time as well.

- **Time.**
  - `paths()` reads the 200,000-line page in 0.25 s, a little longer than
    rendering it at 36 dpi takes (0.21 s).
  - The records take 4.0 s, or 2.1 s with the garbage collector off, and
    PyMuPDF's `get_cdrawings()` takes 1.03 s.
  - On the 33,000-line page the same calls take 0.065 s, 0.081 s, 1.26 s
    (0.61 s of CPU time), and 0.19 s.
- **Where the time goes.** Once the interpretation is memoized, `paths()`
  builds the columns of 200,000 paths in 36 ms, and `content().paths`
  builds the records in 4.3 s. Interpreting the page and recording its
  paths take the rest of the 0.25 s.
- **Memory.**
  - The columns hold 224 bytes a path on the line pages: 118 for the path
    and 53 for each of its two segments.
  - A call grows the peak resident set by 581 bytes a path and leaves 693
    held. The records grow it by 2,247 and leave 2,405, and
    `get_cdrawings()` 969 and 1,475.
  - About 470 of the 693 bytes are the interpretation's own tuples, one
    per path, which the page memo keeps for `Page.content()` as well.
    Issue #615 records the paths as columns in the engine, so that the
    memo holds the 224 bytes alone.
- **The corpus page with the most paths**, `f1040.pdf` page 1 with 502
  paths and 1,604 segments.
  - `paths()` reads it in 9 ms, against 17 ms for the records and 26 ms
    for `get_cdrawings()`.
  - Interpreting the page takes nearly all of the 9 ms. From the memo, the
    columns take 0.24 ms and the records 10 ms.
  - At this size, the memory columns measure fixed costs, such as code
    that runs for the first time, more than the paths.
