# Rendering one document in several threads

Evidence for #97 (DESIGN.md section 4.6). [measure.py](measure.py) renders
every page of one shared `Document` with `page.pixmap(dpi=72)` from a pool
of 1, 2, 4, and 8 threads and reports the median of five runs.
[results-3.15.txt](results-3.15.txt) and
[results-3.15t.txt](results-3.15t.txt) are its output on CPython 3.15 and
its free-threaded build, on an Apple M2 Pro (8 performance and 4
efficiency cores), from:

```bash
uv run python evidence/threads/measure.py
UV_PROJECT_ENVIRONMENT=target/ft-venv uv sync --python 3.15t
UV_PROJECT_ENVIRONMENT=target/ft-venv uv run --no-sync python evidence/threads/measure.py
```

## Findings

| File | Pages | 1 thread | 4 threads | 8 threads | 8 threads, free-threaded |
| --- | ---: | ---: | ---: | ---: | ---: |
| bill-hr815.pdf | 110 | 0.422 s | 3.62x | 5.62x | 6.40x |
| usrguide.pdf | 27 | 0.107 s | 2.74x | 4.86x | 4.39x |

- Rendering scales with threads on both builds: a render clones the
  version under a short lock and then reads it without locks, and
  `page.pixmap` releases the GIL while hayro renders.
- The free-threaded build adds little at this size, because the Python
  around each render is small next to the render. It matters for reads
  that spend their time in Python, such as walking objects.
- Each pool thread starts with an empty render cache (the cache is
  thread-local, `crates/pyropdf-engine/src/render/mod.rs`), so these
  numbers include filling one cache per thread; 8 threads stay below an
  8x speed-up partly for that reason and partly because four of the cores
  are efficiency cores.
- One noisy run with other builds on the machine gave a free-threaded
  single-thread time of 0.749 s; the recorded runs were taken with the
  machine otherwise idle.

That reads in other threads see complete versions only, and that a
transaction belongs to the thread that opened it, is tests/test_threads.py,
which CI runs on 3.11, 3.15, and 3.15t.
