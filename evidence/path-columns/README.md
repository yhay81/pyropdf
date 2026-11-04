# Paths as columns

The measurement behind issues #606 and #615: the paths of a page as `Paths`
columns, from `Page.paths()`, against one `Path` record per path, from
`Page.content()`, and against PyMuPDF's `get_cdrawings()` and
`get_drawings()` (DESIGN.md section 10).

## Approach

The engine's content collector appends each path to one buffer per column
as hayro paints it, and the page memo holds those columns (#615).
`Page.paths()` hands the memo's buffers to Python without a copy, with the
GIL released and no Python object per path or segment. Python reads each
column through the buffer protocol and the whole table through the Arrow
PyCapsule interface, and the row view builds a `Path` record only when one
is asked for. `Page.content()` builds its tuples from the same columns.

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
  - `paths()` reads the 200,000-line page in 0.21 s, a little less than
    rendering it at 36 dpi takes (0.25 s).
  - The records take 4.5 s, or 2.3 s with the garbage collector off, and
    PyMuPDF's `get_cdrawings()` takes 1.19 s.
  - On the 33,000-line page the same calls take 0.038 s, 0.085 s, 1.11 s,
    and 0.19 s. The records' CPU time there is 0.58 s, so most of the gap
    to 1.11 s is other work on the processor.
- **Where the time goes.** Once the interpretation is memoized, `paths()`
  hands over the columns of 200,000 paths in 0.03 ms, because it copies
  nothing, and `content().paths` builds the records in 4.6 s. Before #615,
  when the binding built the columns from one tuple per path, `paths()` took
  51 ms here. Interpreting the page and recording its paths take nearly all
  of the 0.21 s.
- **Memory.**
  - The columns hold 221 bytes a path on the line pages: 115 for the path
    and 53 for each of its two segments. The content's items add 16 bytes a
    path.
  - A call grows the peak resident set by 156 bytes a path and leaves 310
    held, against 583 and 689 before #615. The records grow it by 2,083 and
    leave 2,229, and `get_cdrawings()` 967 and 1,476.
  - The columns and items account for 237 of the 310 bytes. This script does
    not break down the other 73.
- **The corpus page with the most paths**, `f1040.pdf` page 1 with 502
  paths and 1,604 segments.
  - `paths()` reads it in 10 ms, against 20 ms for the records and 28 ms
    for `get_cdrawings()`.
  - Interpreting the page takes nearly all of the 10 ms. From the memo,
    the columns take 0.03 ms and the records 7.6 ms.
  - At this size, the memory columns measure fixed costs, such as code
    that runs for the first time, more than the paths.
