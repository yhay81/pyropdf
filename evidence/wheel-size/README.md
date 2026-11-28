# Native wheel sizes (#858)

The core abi3 limit in [#258](https://github.com/yhay81/pyropdf/issues/258) is
9,437,184 bytes. Measure the `.whl` itself, including its ZIP headers and
central directory. The workflow artifact that transports the wheel is another
ZIP with a different size. One MiB is 1,048,576 bytes.

## Measured release artifacts

The [final dry run](https://github.com/yhay81/pyropdf/actions/runs/44783558184)
built `949d5bcf` and installed each wheel on its matching runner. `after.json`
identifies every file by name, tag and SHA-256. To reproduce it, use that run
with the inventory command below. The final workflow reads the same native
build arguments used in this run from `tools/native-wheel-args.txt`.

| Native abi3 target | v0.3.1 bytes (MiB) | Pre-change dry run bytes (MiB) | Final bytes (MiB) | Headroom bytes |
| --- | ---: | ---: | ---: | ---: |
| Linux x86_64 | 10,668,116 (10.174) | 11,197,995 (10.679) | 8,985,644 (8.569) | 451,540 |
| Linux aarch64 | 10,061,555 (9.595) | 10,572,291 (10.083) | 8,221,985 (7.841) | 1,215,199 |
| macOS Intel | 10,192,011 (9.720) | 10,705,480 (10.210) | 8,694,186 (8.291) | 742,998 |
| macOS arm64 | 9,191,097 (8.765) | 9,656,159 (9.209) | 7,735,185 (7.377) | 1,701,999 |
| Windows x86_64 | 10,189,695 (9.718) | 10,731,183 (10.234) | 9,090,201 (8.669) | 346,983 |

| Free-threaded target | CPython 3.14t bytes (MiB) | CPython 3.15t bytes (MiB) |
| --- | ---: | ---: |
| Linux x86_64 | 8,959,457 (8.544) | 8,959,665 (8.545) |
| Linux aarch64 | 8,201,271 (7.821) | 8,201,120 (7.821) |
| macOS Intel | 8,721,033 (8.317) | 8,720,979 (8.317) |
| macOS arm64 | 7,708,568 (7.351) | 7,708,322 (7.351) |
| Windows x86_64 | 9,061,166 (8.641) | 9,061,264 (8.641) |

## Pull request and release checks

The `Wheel size` workflow builds the PR test merge and its first parent on
each of the five release targets. This isolates the change introduced by the PR.
Both checkouts use the same runner, Rust, maturin and compatibility setting.
Each checkout supplies its own `tools/native-wheel-args.txt` and applies its own
abi3 repacking script when present, so packaging changes count toward PR growth. Older revisions use their original
`--release --locked --out dist` command. The comparison matches Python, ABI and
platform tags and uploads a `size.json` for each target. Growth over 262,144 bytes needs an
explanation in the PR body on a line beginning `Wheel growth justification:`.
Changing that justification or the target branch reruns the comparison. The absolute limit applies to every abi3 wheel.
`tests/test_release.py` exercises 262,145 bytes of growth, rejects a missing
reason and records the explanation for its synthetic font-notice fixture.

Before upload and publication, the release workflow checks the archive and
license metadata of each native wheel and runs the installed-artifact smoke
on its target. It reports cp314t and cp315t independently. The size limit and
PR growth allowance apply only to abi3.

To repeat one comparison locally, put the base and head wheels for one platform
in separate directories, then run from the head checkout:

```sh
python3 tools/wheel_size.py head/dist --abi abi3 --base base/dist
```

The JSON gives the full wheel name, compatibility tag, SHA-256, archive bytes,
binary MiB, and compressed bytes occupied by the extension inside the wheel.

## Reproduce the native inventory

The published `v0.3.1` reference uses the asset byte counts and digests of the
[immutable release](https://github.com/yhay81/pyropdf/releases/tag/v0.3.1).
`before.json` inventories the [pre-change dry run](https://github.com/yhay81/pyropdf/actions/runs/44773217955).
The compiled extension accounts for 94.75–95.48% of its five compressed abi3
wheels. The remaining roughly 0.48 MiB per wheel includes the Python API,
types, SBOM, license files and ZIP headers. OCR models and CJK fonts continue
to live in their separate data packages.

```sh
gh api repos/yhay81/pyropdf/releases/tags/v0.3.1 \
  --jq '[.assets[] | select(.name | contains("-abi3-")) | {filename:.name,sha256:.digest,size_bytes:.size}]'
run=44773217955
gh run download "$run" --pattern 'wheels-*-*' --pattern 'free-threaded-wheels-*' --dir "target/wheel-size/$run"
python3 evidence/wheel-size/measure.py "target/wheel-size/$run" --run "$run"
```

## Runtime trade-offs

The release profile keeps unwind, uses speed level 2 and fat LTO, strips native
symbols, and uses `s` for pyropdf-engine, harfrust, read-fonts and RTen. The
binding and raster and matrix-multiplication kernels keep speed optimization.
Maturin writes level-9 DEFLATE. The abi3 release job then repacks with one
Zopfli iteration. It adopts the smaller wheel after checking the installed
bytes and raw ZIP headers. If a valid header or layout cannot be reproduced
losslessly, the release keeps the original artifact.
For a native build with local symbols for profiling, set
`CARGO_PROFILE_RELEASE_STRIP=none`.

`timings-before.json` and `timings-after.json` contain the best of five fresh
documents per operation on an Apple M2 Pro, macOS 27.2, CPython 3.15.2. Both
use Rust 1.100.0 and maturin 1.16.0. The baseline rebuilds `313c217d` with its
original release profile. The candidate is the macOS arm64 abi3 wheel from
[release run 44783558184](https://github.com/yhay81/pyropdf/actions/runs/44783558184). Across `bill-hr815` and
`usrguide`, whole-document rendering changes by −1.9% to +1.0% in elapsed time.
Text extraction takes 7.0–7.4% longer, and search takes 8.2–9.3% longer. The
numbers describe these two documents on this machine. The initial whole-workspace
`z` build took 2.29–2.32 times as long to render the documents at 144 dpi
(`timings-size-only.json`, [dry run](https://github.com/yhay81/pyropdf/actions/runs/44780233783)).

To repeat the timing with each installed macOS arm64 wheel:

```sh
python evidence/pymupdf-comparison/bench.py pyropdf timings.json \
  tests/assets/real_world/bill-hr815.pdf tests/assets/real_world/usrguide.pdf
```

## Matched builds of the compiler change

[The paired PR run](https://github.com/yhay81/pyropdf/actions/runs/44783571685)
built `313c217d` and `949d5bcf` on the same runner for each target, both with
level-9 DEFLATE. This isolates the Rust profile change from compression-level
changes between releases. The compressed abi3 wheels shrank by 1,258,077 to
1,922,521 bytes. `matched.json` records each pair, including both digests and
the exact byte difference. The release and paired runs built their artifacts
independently.

```sh
gh run download 44783571685 --pattern 'abi3-size-*' --dir target/wheel-size/pairs
jq -s . target/wheel-size/pairs/abi3-size-*/size.json
```

## Windows abi3 recompression

The Windows comparison for #979 reported 9,359,600 bytes on its base and
9,467,150 on its head. The compressed head exceeded the 9,437,184-byte cap
by 29,966 bytes. [Windows comparison](https://github.com/yhay81/pyropdf/actions/runs/44903451300/job/136844014884)

Repacking the Windows wheel from the release rehearsal above reduced its size
from 9,090,201 to 8,847,599 bytes. The compressed extension accounted for
221,572 of the 242,602 bytes saved. Python's standard ZIP reader compared
all 68 archive members, including their order, names, timestamps, permissions
and data. The independent checker also verified the SHA-256 hashes and sizes
in RECORD. The inputs and outputs are identified in
[windows-recompression.json](windows-recompression.json).

### Reproducing the Windows measurement

```sh
gh run download 44783558184 --repo yhay81/pyropdf --name wheels-x86_64-pc-windows-msvc --dir original
mkdir delivered
cp original/*.whl delivered/
uv run --locked --only-group wheel --python 3.14 python tools/repack_wheel.py delivered
python3 evidence/wheel-size/recompression.py original/*.whl delivered/*.whl \
  --source https://github.com/yhay81/pyropdf/actions/runs/44783558184/artifacts/14654361426
```

## Integrated object store release profile

After #1101, the delivered Windows x86_64 wheel was 9,573,339 bytes and the
Linux x86_64 wheel was 9,454,025 bytes. Both exceeded the 9,437,184-byte
limit. The corresponding paired jobs measured the same overrun on their
bases. [Windows build](https://github.com/yhay81/pyropdf/actions/runs/44976395403/job/137067205245)
and [Linux build](https://github.com/yhay81/pyropdf/actions/runs/44976395403/job/137067205425).

On the same integrated sources at `138fd449`, compiling `pyropdf-store` at
level `s` reduced the delivered macOS arm64 wheel from 8,225,735 to
8,037,684 bytes, a saving of 188,051 bytes. The installed wheel passed the
release smoke test, including panic unwinding, rendering, extraction, editing,
and reopening full and incremental saves. [Artifact hashes and timings](store-profile.json).

To reproduce, build the base with the pinned release options, then build
it again with the one package override. Use separate output directories.

```sh
uvx --from maturin==1.16.0 maturin build --release --locked --compression-level 9 --out baseline
uvx --from maturin==1.16.0 maturin build --release --locked --compression-level 9 --out candidate \
  --config 'profile.release.package.pyropdf-store.opt-level="s"'
for directory in baseline candidate; do
  uv run --locked --only-group wheel --python 3.14 python tools/repack_wheel.py "$directory"
  python tools/smoke_artifact.py "$directory" --pattern '*.whl'
done
```

Install each delivered wheel in a separate environment and run the two-document
timing command above there. The benchmark takes the best of five freshly
opened documents per operation. On this machine whole-document text extraction
took 124.2 and 65.9 ms with the new profile, compared with 127.2 and 67.0 ms
before. The four 72 and 144 dpi rendering comparisons ranged from 2.7% faster
to 1.7% slower. The paired native-platform results are tracked in [#1108](https://github.com/yhay81/pyropdf/issues/1108).

## Cold-path crate release profile (#1164)

After #1141, #1143, #1145, and #1156, `main` at `20f2a0e1` reached 9,430,524
bytes on Windows x86_64, leaving only 6,660 bytes below the 9,437,184-byte abi3
limit and blocking PR #1162 (+21,077 bytes on Windows).

Keeping `pyropdf-python` (the root `cdylib`, which controls the fat-LTO
pipeline and symbol stripping), the rasterizer (`vello_cpu`, `vello_common`,
`ab_glyph_rasterizer`), the RTen GEMM kernels (`rten-gemm`), the core PDF
parser and interpreter (`hayro`, `hayro-interpret`, `hayro-syntax`), and Flate
compression (`flate2`, `zlib-rs`) at `opt-level = 2`, while size-optimizing
`skrifa`, `hayro-font`, `hayro-cmap`, and `pyo3` at level `s` and strictly
cold-path crates (`pyropdf-schema`, `rten` non-GEMM/model-loading and `rayon`,
cold image/SVG/XML codecs, and PAdES/X.509/CMS/PKCS/ECDSA crypto) at level `z`,
reduced the delivered macOS arm64 wheel on `20f2a0e1` from 8,112,601 to
7,712,713 bytes — a saving of 399,888 bytes (390.5 KiB; compressed extension
from 7,450,275 to 7,050,385 bytes). Overriding `pyropdf-python` itself to level
`s` was measured and rejected because it lowered the fat-LTO pipeline for
`vello_cpu` (+21.7% slower 144 dpi rendering) and prevented symbol stripping.
The candidate wheel passed `tools/smoke_artifact.py` (including panic
unwinding, rendering, extraction, editing, and reopening full and incremental
saves). [Artifact hashes and timings](cold-crates-profile.json).

To reproduce from `20f2a0e1`, build the base `Cargo.toml` and the updated
`Cargo.toml` into separate directories, repack both with Zopfli, smoke-test
both, and run [compare_profile.py](compare_profile.py):

```sh
# Build baseline from 20f2a0e1 into baseline/ and candidate from HEAD into candidate/:
uvx --from maturin==1.16.0 maturin build --release --locked --compression-level 9 --out baseline
# (apply Cargo.toml profile change)
uvx --from maturin==1.16.0 maturin build --release --locked --compression-level 9 --out candidate
for directory in baseline candidate; do
  uv run --locked --only-group wheel --python 3.14 python tools/repack_wheel.py "$directory"
  python3 tools/smoke_artifact.py "$directory" --pattern '*.whl'
done
python3 tools/wheel_size.py candidate --abi abi3 --base baseline
python3 evidence/wheel-size/compare_profile.py baseline candidate \
  --base-revision 20f2a0e1e43febd152025bb07b27dcee2d23c500 \
  --change 'opt-level = "s" for skrifa, hayro-font, hayro-cmap, pyo3; opt-level = "z" for pyropdf-schema, rten/rayon cold crates, cold image/SVG/XML codecs, and PAdES/X.509/CMS/PKCS crypto' \
  --python /opt/homebrew/bin/python3.15 \
  --output evidence/wheel-size/cold-crates-profile.json
```

Across `bill-hr815` and `usrguide` (best of five freshly opened documents per
operation on Apple M2 Pro, macOS 27.2, CPython 3.15.2), all four 72 and 144 dpi
rendering comparisons stayed flat (−0.6% to +0.4%: 377.9 vs 378.5 ms and 648.3
vs 644.7 ms on `bill-hr815`; 77.8 vs 78.2 ms and 116.1 vs 115.7 ms on
`usrguide`), text extraction moved by +2.2% and +0.3%, word extraction moved by
−2.2% and +3.8%, search moved by +0.6% and +4.3%, and save improved by 6.6% and
2.9%.

## `dyn ContentSink`, `skrifa` deduplication, and remaining cold crates (#1264)

After concurrent feature merges (`#1241`, `#1243`, `#1245`, and Lane 2A–4
hardening), `main` at `fc9e5067` reached 9,463,199 bytes on Windows x86_64
(+26,015 bytes above the 9,437,184-byte `abi3` limit) and 9,386,351 bytes on
Linux x86_64, blocking PRs `#1262` and `#1263`.

Three zero-regression reductions restored >200 KiB of compressed headroom
without lowering `opt-level` on any hot crate or on `pyropdf-store` /
`pyropdf-engine` (compiling `pyropdf-engine` at level `z` was measured and
rejected because it monomorphizes `hayro_interpret::interpret` for text
extraction at `minsize`, regressing `bill-hr815` text extraction by +37.6%):

1. **De-monomorphize `ContentCollector` over `&mut dyn ContentSink`** in
   `crates/pyropdf-engine/src/extract/model.rs` (eliminates 6 duplicate
   monomorphizations of `ContentCollector` and `hayro_interpret::interpret`):
   −138,513 bytes (−135.3 KiB).
2. **Align `pyropdf-engine`'s `skrifa` to `0.49.0`** (matching `hayro-font
   0.5.0`, removing the duplicate `skrifa 0.50.0` copy from the compiled
   extension): −48,574 bytes (−47.4 KiB).
3. **Size-optimize remaining strictly cold-path crates at level `z`** in
   `Cargo.toml` (`harfrust`, `hayro-cmap`, `zune-jpeg`, `zune-core`, `png`,
   `fdeflate`, `miniz_oxide`, `image`, `fax`, `brotli`, `j2k-*`, `unicode-*`,
   non-GEMM `rten-*`, `yoke`, `polycool`, `xmlwriter`, `base64*`, and remaining
   PDF/PAdES crypto crates), keeping all 10 hot crates at level `2` and
   `pyropdf-store`, `pyropdf-engine`, `read-fonts`, `skrifa`, `hayro-font`, and
   `pyo3` at level `s`: −24,125 bytes (−23.6 KiB).

Together, these reduced the delivered macOS arm64 wheel on `fc9e5067` from
8,189,550 to 7,978,338 bytes — a saving of 211,212 bytes (206.3 KiB; compressed
extension from 7,445,177 to 7,234,041 bytes; uncompressed `_core.abi3.so` from
16,618,816 to 16,124,864 bytes). The delivered wheel passed
`tools/smoke_artifact.py` and `cargo-audit audit --deny warnings`.
[Artifact hashes and timings](dyn-sink-skrifa-profile.json).

Across `bill-hr815` and `usrguide` (best of five freshly opened documents per
operation on Apple M2 Pro, macOS 27.2, CPython 3.15.2), all four 72 and 144 dpi
rendering comparisons stayed flat to slightly faster (−1.4% to +0.5%: 373.4 vs
368.5 ms and 647.9 vs 638.5 ms on `bill-hr815`; 76.4 vs 76.7 ms and 116.2 vs
115.8 ms on `usrguide`), and `bill-hr815` text, words, and search stayed within
+0.9% to +1.8% (68.0 vs 69.2 ms, 99.1 vs 100.6 ms, 70.0 vs 70.7 ms).
