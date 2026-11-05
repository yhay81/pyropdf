# Encoded Pixmap input bounds (#697)

`Pixmap.from_bytes` and `Pixmap.open` now take a keyword-only `max_size`.
Its default is the registry's 268,435,456-byte `image_input_size` limit;
`None` removes this input limit, and does not remove the existing
64,000,000-pixel decoder limit.

The buffer path checks the exported byte length before allocating, reserves
through `BudgetVec`, and copies in C order, including non-contiguous views.
The file path checks metadata before reserving and then reads fixed 16 KiB
chunks. Each read requests at most the remaining limit plus one probe byte;
the actual length is checked before appending. Growing files and sources
whose metadata reports zero therefore use the same limit. File errors keep
their Python exception types. The first input copy and the TIFF palette
workaround share one operation budget, and allocation refusals retain their
typed `LimitError` instead of becoming format errors.

## Validation

`tests/test_pixmap_input_budget.py` covers exact limits, bytes, bytearrays,
contiguous/strided/reversed/multidimensional memoryviews, sparse oversized
files and mappings, invalid limits before input access, file errors, TIFF
palette samples and resolution, and the independent pixel bound. POSIX pipe
tests exercise actual bytes beyond zero-length metadata and require the
reader to close before a producer finishes a much larger stream. These
tests cover the read loop used when a regular file grows after metadata.
The pipe and permission tests run on Linux CI; they are skipped on Windows.
The existing Pixmap format and operation suites remain the format oracle.

## Timing

Run `uv run python evidence/pixmap-input-budgets/measure.py` after installing
the baseline or patched extension. `before.json` used main commit
`2c5ae21c06a3e06fc8e9f5eaab842c088bcd579e`; `after.json` used this change.
Both ran on the same Windows 11 machine with Python 3.14.8 and the release
extension. Each measurement is the median of nine batches of 20 decodes,
after one warm-up. The script uses deterministic 1024 x 1024 RGB PPM input
and a 256 x 256 palette TIFF; file creation is outside the measured calls.

| Decode | Before (ms/call) | After (ms/call) |
| --- | ---: | ---: |
| 3,145,745-byte PPM buffer | 15.7803 | 14.0233 |
| Same PPM file | 15.5705 | 14.9222 |
| 67,206-byte palette TIFF buffer | 0.5784 | 0.3860 |

These samples show no slowdown for these inputs, but are not a general
speedup claim or a peak-memory measurement. The safety result comes from
checking and charging before retained input growth. Decoder-internal
allocations and other formats are outside this measurement.
