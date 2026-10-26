# Reading through object handles after an edit

Evidence for #32's acceptance: reading a property of each of 1,600 handles
after an unrelated edit costs about as much as 1,600 single reads.
[measure.py](measure.py) adds 100, 400, and 1,600 indirect dictionaries,
saves and reopens the document, and times, as the median of 21 rounds, a
fresh `doc.objects[n]["T"]` for every object and `handle["T"]` through
handles taken before an edit of the catalog that each round repeats.

Two runs on 2026-10-26, on macOS arm64, with the extension that `uv run`
built:

```bash
uv run python evidence/object-handles/measure.py
path/to/pylopdf/.venv/bin/python -I evidence/object-handles/measure.py pylopdf
```

- pyropdf 0.0.0, Python 3.15.0 on Darwin arm64

| Objects | Single reads | Handles after an edit | Ratio |
| ---: | ---: | ---: | ---: |
| 100 | 0.14 ms | 0.05 ms | 0.36 |
| 400 | 0.58 ms | 0.22 ms | 0.38 |
| 1,600 | 2.35 ms | 0.85 ms | 0.36 |

- pylopdf 0.15.0, Python 3.14.8 on Darwin arm64

| Objects | Single reads | Handles after an edit | Ratio |
| ---: | ---: | ---: | ---: |
| 100 | 0.21 ms | 0.13 ms | 0.61 |
| 400 | 0.87 ms | 0.52 ms | 0.60 |
| 1,600 | 3.65 ms | 2.20 ms | 0.60 |

## Findings

- A handle read after an edit costs about a third of a single read, which
  also looks the object up, and both grow linearly: 1,600 handles take
  0.85 ms. An edit invalidates nothing that a handle reads, since the
  version shares every object that it did not write, parsed once.
- pylopdf's raw handles are linear too. Its quadratic cost is in its
  typed handles: a `Field` walks the whole form on every access, 2.18 to
  2.24 s for 1,600 fields ([pylopdf-baseline](../pylopdf-baseline/README.md)).
  The typed layer of Phase 3 builds on these handles, so it starts from a
  constant cost per access.
