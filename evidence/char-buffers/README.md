# Chars as columns

The measurement behind issue #99: the chars of a page as `Chars` columns,
from `Page.chars()`, against one `Char` record per glyph, from
`Page.layout(chars=True)` (DESIGN.md section 10).

## Approach

The core builds one buffer per column from the memoized layout, with no
Python object per char. Python reads each column through the buffer
protocol and the whole table through the Arrow PyCapsule interface, and
the row view builds a `Char` record only when one is asked for.

## Workload

On the page with the most chars of each of the three documents named in
the issue (`tests/assets/real_world`), with the layout already memoized:

- **Build**: the median of 11 calls of `layout()` without chars,
  `layout(chars=True)`, and `chars()`.
- **Heap**: the Python heap that the result holds, by `tracemalloc`, and
  the bytes of the columns, which the core allocates outside that heap.
- **`x0`**: the median of 11 reads of every char's left edge, through
  the records, through `column("x0").tolist()`, and through
  `numpy.frombuffer` with a copy.

## Running

```sh
uv run --with numpy python evidence/char-buffers/measure.py
```

NumPy is optional; without it the last column reads `n/a`.

## Results

From `results/macos-arm64.md`:

- **Build.** `chars()` takes 28 to 49 µs, against 3.3 to 7.9 ms for
  `layout(chars=True)`, about 120 to 160 times less.
  - `layout()` without chars takes 0.5 to 1.4 ms.
  - So the records cost 2.8 to 6.4 ms of that, 100 to 130 times what the
    columns cost.
- **Memory.**
  - The records hold 1.2 to 2.6 MiB of Python heap, 450 to 520 bytes a
    char.
  - The columns hold 0.19 to 0.38 MiB outside it, 73 bytes a char plus
    its UTF-8, and their Python object about 100 bytes.
- **Reading `x0`.**
  - NumPy reads a column in 1.2 to 1.8 µs.
  - `tolist` reads it in 28 to 100 µs.
  - The records give the same values in 70 µs to 0.6 ms.
