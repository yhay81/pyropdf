# Copy callers and target initialization

Follow-up to `profile-windows-amd64.md` for #365, using the same optimized
PDB-enabled wheel and product sources. No product implementation changed.
The existing draft PR #709 holds this evidence; #710 owns the separate
engine implementation and its full-render comparison.

## Native caller evidence

`profile_windows.py --stacks` now uses `StackWalk64` while the owned worker
is suspended, with at most 24 physical frames per sample. Context capture,
walking and DbgHelp calls run on one sampler thread. Function names and
source locations are resolved after resuming the worker. The normal
leaf-only mode remains available. This adds profiling overhead and is not
a benchmark of rendering throughput.

Reproduce after building the helper as described in the earlier report:

```powershell
target/pymupdf-comparison/pyropdf-profile-env/Scripts/python.exe evidence/pymupdf-comparison/profile_windows.py evidence/pymupdf-comparison/results/copy-stacks-bill-144.json --symbols target/365-profile-build/release --stacks --document bill-hr815 --dpi 144
```

The committed run has 881 leaf samples, no context errors and no unresolved
leaf symbols. Of its 235 `memmove` samples:

| Caller group | Samples | Evidence |
| --- | ---: | --- |
| `pyropdf_engine::render::draw` | 122 | Matching Rust PDB maps the return address to `render/mod.rs:440`, the `Pixmap::new` call |
| Python buffer copying | 96 | Stacks contain `PyBuffer_ToContiguous` and `PyBytes_FromObject` export symbols |
| Other paths | 17 | Font mapping, vector resizing, sorting, and other operations |

Python has export symbols rather than its private PDB in this run. Its
intermediate exported names are approximate address labels and must not be
interpreted as exact private Python frames. The Rust initializer mapping
uses a matching PDB and a concrete source line. Full stack labels and source
locations, with machine-specific path prefixes removed, are in the JSON.

vello_common 0.3.0's `Pixmap::new` repeats a `PremulRgba8::from_u32(0)`
element to construct its vector. The target is subsequently cleared to the
requested background during rendering. Its existing `Pixmap::from_parts`
accepts zeroed bytes with `AlphaPremultiplied` and `may_have_transparency=true`,
preserving the original all-zero pixels and metadata without the same
repeated-element construction.

## Constructor probe

`pixmap-init/` is an isolated Cargo workspace, with pinned vello_common
0.3.0 and a committed lockfile. It alternates the two constructor orders
over ten rounds for each size. Both release builds use thin LTO and one
codegen unit. Each sample includes construction, optional filling of every
byte with 255, and destruction. `black_box` keeps the operations observable.
Assertions verify the zeroed candidate's dimensions and every initial byte.

```powershell
$env:CARGO_TARGET_DIR = Join-Path (Get-Location).Path 'target/365-pixmap-init'
cargo run --release --locked --manifest-path evidence/pymupdf-comparison/pixmap-init/Cargo.toml > evidence/pymupdf-comparison/results/pixmap-init-windows.csv
```

Median milliseconds from `pixmap-init-windows.csv`:

| Size | Touch every byte | Original | Zeroed bytes | Ratio |
| --- | --- | ---: | ---: | ---: |
| 612×792 | no | 0.3295 | 0.0129 | 0.039 |
| 612×792 | yes | 0.3073 | 0.3091 | 1.006 |
| 1224×1584 | no | 1.0850 | 0.0348 | 0.032 |
| 1224×1584 | yes | 1.3946 | 1.2820 | 0.919 |
| 2480×3508 | no | 5.1879 | 0.0350 | 0.007 |
| 2480×3508 | yes | 6.8278 | 5.2032 | 0.762 |

Zeroed allocation can defer physical-page costs until the renderer writes
those pages. The constructor-only ratios therefore substantially overstate
the potential full-render gain. Touching every byte reduces the gain to
noise for the small size and about 8%/24% for the larger sizes, for this
initialization probe only. It does not simulate all rendering work.

#710 must compare complete rendering using ordinary baseline and candidate
release wheels, retain the raw-pixel copy in both measurements, check exact
output in all layouts and preserve allocation/dimension behavior. #365's
1.5-times-MuPDF target remains unchanged and unmet. No upstream publication
or dependency change is needed to evaluate this existing API locally.

The helper uses Microsoft's bounded
[StackWalk64 API](https://learn.microsoft.com/windows/win32/api/dbghelp/nf-dbghelp-stackwalk64)
with the same process handle used for symbol resolution. MSVC `/W4 /WX`,
probe `cargo fmt`/`clippy -D warnings`, and the probe lockfile RustSec audit
all pass. This source-line evidence narrows the optimization choice; it
does not claim that a product optimization has already landed.
