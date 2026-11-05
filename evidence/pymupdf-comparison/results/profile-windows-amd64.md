# Windows rendering samples for #365

Measured 2026-11-05 UTC / 2026-11-06 JST on the Windows AMD64 workstation
described in `remeasurement-windows.md`. Product sources are unchanged from
`13ea858a096493bb49e681c450a2a44302ecfaa7`: hayro revision
`394e4a0d37baf8af513f6ebda15f4bada85d6f23`, vello_cpu/vello_common 0.3.0,
Python 3.13.17. Hinting and glyph caching are both disabled.

The separate release-profile wheel adds `CARGO_PROFILE_RELEASE_DEBUG=1`;
it is not the artifact used for the ordinary MuPDF timing comparison.
Its SHA-256 is
`d29681f0c9229d3522ccb226df63de616bbb1e885a86030b47ac2bb2ac5e2315`.
The JSON records the loaded extension's SHA-256 as well.

## Method and validation

`profile_windows.py` launches its own renderer and obtains the actual PID
and main-thread ID from that renderer. On Windows the venv launcher has a
different PID; the helper explicitly rejects a thread from another process.
It briefly suspends that thread, reads its instruction pointer with
`GetThreadContext`, resumes it before symbol lookup, and resolves addresses
using the matching local PDB with DbgHelp. Only this owned worker is sampled.
The helper bounds each session to 60 seconds; these runs use 15 seconds.

These are **wall-clock leaf-PC samples**, not CPU cycle counts or inclusive
call stacks. The nominal sleep is 5 ms, but scheduling and profiler overhead
give about 59 samples/second here. The worker repeatedly opens a fresh
document, renders every page and reads raw RGB bytes, as the earlier macOS
worker does. Its `worker_passes` count covers its full 18-second lifetime,
not just the 15-second sampling window. It does not report benchmark times.

All four runs have zero context-read errors, zero unresolved symbols, and
zero missing modules. Each loaded `_core` has `symbol_type=3` (PDB) and
`pdb_unmatched=false`. The raw counts and complete resolved names are in
`profile-windows-amd64.json`.

`sample_hashes.py` also compared the ordinary and PDB-enabled release wheels:
all 936 entries (156 corpus pages in six pixel layouts at 72 dpi) match
exactly. This checks the measured corpus output, not every possible PDF or
resolution. Both hash files stay under `target/365-profile-windows/` and can
be reproduced with the two wheel environments.
The helper was compiled with MSVC `/W4 /WX`, and a negative check confirmed
that pairing the launcher PID with the worker TID is rejected before
suspension. The four successful runs exercise the valid pairing.

## Sample shares

Groups match the indicated substrings in resolved function names, including
their `fearless_simd` AVX2 wrappers. Percentages use every leaf sample as the
denominator; unlisted functions make up the remainder. They are not summed
inclusive frame costs.

| Leaf function group | bill-hr815 72 | bill-hr815 144 | usrguide 72 | usrguide 144 |
| --- | ---: | ---: | ---: | ---: |
| Samples | 888 | 893 | 889 | 896 |
| `vello_common::flatten::fill` | 19.6% | 17.7% | 21.4% | 19.0% |
| `make_tiles_analytic_aa` | 15.5% | 11.2% | 11.4% | 10.5% |
| `vello_common::strip::render` | 22.2% | 15.1% | 17.9% | 16.5% |
| `memmove` | 12.5% | 20.8% | 10.1% | 21.1% |
| `core::slice::sort` | 5.7% | 4.7% | 6.3% | 4.5% |
| `vello_common::pixmap::rgba_to_rgb` | 0.9% | 2.2% | 1.1% | 2.3% |

Flattening, tile construction and strip rendering together account for
44.0–57.3% of leaf samples. This identifies current upstream rasterization
work worth investigating, rather than assuming the earlier glyph-cache
release eliminated the default-setting gap. RGB narrowing is already SIMD
and has a much smaller share, consistent with the earlier #393 change.

`memmove` is substantial at 144 dpi, but these leaf samples cannot name its
callers. Source inspection shows `Pixmap::from(Raster)` moves the vector into
an `Arc`, and `Pixmap.__getbuffer__` exposes its storage directly. Neither
adds a pixel copy. `bytes(pixmap)` still performs the requested Python copy;
MuPDF's `.samples` is the corresponding operation in the comparison. Do not
remove that operation from the metric or attribute all `memmove` samples to
it without caller evidence. The next useful evidence is its native callers
and allocation paths, followed by a measured scoped optimization or an
upstream proposal. The 1.5 target remains unmet.

The upstream release check still identifies `sparse-strips-v0.3.0` as the
latest sparse-strips release. Main contains subsequent changes, including
[line-segment clip culling (#1954)](https://github.com/linebender/vello/pull/1954)
and [relative cubic flattening (#2039)](https://github.com/linebender/vello/pull/2039).
The first reports its gain on a small clip; the second addresses translation
precision and reports additional flattening instructions. Their existence
does not establish a fix for this full-page corpus gap; any adoption still
needs a measurement of these cases.

## Reproduce

In Developer PowerShell for x64, from the repository root:

```powershell
$env:UV_PYTHON_DOWNLOADS = 'never'
$env:CARGO_PROFILE_RELEASE_DEBUG = '1'
$env:CARGO_TARGET_DIR = Join-Path (Get-Location).Path 'target/365-profile-build'
uv tool run --from 'maturin>=1.16,<2.0' maturin build --release --locked --out target/365-profile-wheels --interpreter python3.13.exe
uv venv --python 3.13 target/pymupdf-comparison/pyropdf-profile-env
uv pip install --python target/pymupdf-comparison/pyropdf-profile-env/Scripts/python.exe target/365-profile-wheels/pyropdf-0.2.2-cp311-abi3-win_amd64.whl
New-Item -ItemType Directory -Force target/365-profile-windows | Out-Null
cl /nologo /EHsc /O2 /W4 /WX /Fo:target/365-profile-windows/sample_windows.obj /Fe:target/365-profile-windows/sample_windows.exe evidence/pymupdf-comparison/sample_windows.cpp /link dbghelp.lib
target/pymupdf-comparison/pyropdf-profile-env/Scripts/python.exe evidence/pymupdf-comparison/profile_windows.py evidence/pymupdf-comparison/results/profile-windows-amd64.json --symbols target/365-profile-build/release
```

Use the PDB from the exact build installed in that environment. The helper
uses the Windows SDK's context structures, so no hand-maintained register
offsets are involved. Microsoft documents the required suspension in
[GetThreadContext](https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-getthreadcontext)
and the address-to-symbol lookup in
[Retrieving Symbol Information by Address](https://learn.microsoft.com/windows/win32/debug/retrieving-symbol-information-by-address).
