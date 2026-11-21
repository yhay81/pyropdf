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
Zopfli iteration, retaining the smaller standard-DEFLATE wheel.
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
