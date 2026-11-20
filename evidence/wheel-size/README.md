# Native wheel sizes (#858)

The core abi3 limit in [#258](https://github.com/yhay81/pyropdf/issues/258) is
9,437,184 bytes. Measure the `.whl` itself, including its ZIP headers and
central directory. The workflow artifact that transports the wheel is another
ZIP with a different size. One MiB is 1,048,576 bytes.

The `Wheel size` PR workflow builds the exact base and head on each of the five
release targets with the same runner, Rust, maturin, compatibility setting and
release-mode command. It compares matching Python, ABI and platform tags and
uploads a `size.json` for each target. Growth over 262,144 bytes needs an
explanation in the PR body on a line beginning `Wheel growth justification:`.
Editing the body reruns the comparison. The absolute limit still applies.
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
