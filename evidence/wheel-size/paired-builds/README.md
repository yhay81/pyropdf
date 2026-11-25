# Local wheel comparison

The local experiment uses actual pyropdf revisions on one macOS arm64 machine.
Each base starts with an empty Cargo target. Each head is checked out into a
separate, freshly materialized source directory after the base finishes.
A control rebuilds the same head from the same source path with an empty target.
All builds use their own revision's maturin arguments and release postprocessor.

| Change | Base | Head | Expected head target |
| --- | --- | --- | --- |
| [#1107](https://github.com/yhay81/pyropdf/pull/1107) changes two workspace crates, a Python module and its typing stub | `6a2d2e1c` | `bbb76da7` | Reuse the base target |
| [#979](https://github.com/yhay81/pyropdf/pull/979) changes a Cargo manifest, workspace lockfile and packaging configuration | `26134041` | `a538de34` | Empty target |

The second pair uses the tested merge commit and its first parent from the
[original #979 CI receipt](https://github.com/yhay81/pyropdf/actions/runs/44923963659/artifacts/14722862953).
The host had populated Cargo source downloads. No compiled target or wheel
was restored from another run. The old base target is removed before building
the fallback head to keep disk use low.

The packaging-only comparison applies [#1087](https://github.com/yhay81/pyropdf/pull/1087)'s
release postprocessors to copies of one pre-repack #1107 head wheel. Both
revisions use the same maturin flags. The #1087 base (`23e08a1a`) ships the
input as built, while its head (`3c2aedaf`) runs `tools/repack_wheel.py`.
Each postprocessor gets a second fresh copy as its control. This isolates
packaging from compiler and SBOM variation.

## Results

[local.json](local.json) records revision IDs, timings, original and delivered
archive digests, compressed extension sizes and normalized fingerprints.
The SBOM retains its absolute workspace paths, so compare the candidate and
control fingerprints within each reproduction.

For #1107, the paired head rebuilt all four workspace crates. It took 132.692 s
in maturin, compared with 230.314 s for the same head and 197 compiled crates
from an empty target. This single local comparison saved 97.622 s (42.4%).
Postprocessing and collecting the wheels took about 21 s in each case.

Both heads produced an 8,048,641-byte wheel and passed the release gate with
identical growth of 16,505 bytes over the 8,032,136-byte base. All 88 RECORD
entries verified. Native SHA-256 and the normalized installed-file fingerprint
matched between heads. The shipped `_attachments.py` and `_core.pyi` matched
the corresponding Git blobs in all three wheels. In isolated installations,
the original attachment handle read its replacement's contents in the base
and raised `StaleObjectError` in both head builds.

For #979, both empty-target heads compiled 195 crates and contained the four
new locked dependencies in their SBOMs (`aes-kw`, `hkdf`, `keccak`, and
`sha3`). The base contained none of the four. The 71-member heads had identical
native SHA-256 and normalized contents. Only the generated SBOM and its RECORD
entry differed in raw content. The delivered candidate measured 7,913,973
bytes and the clean control 7,913,983 bytes, respectively 85,076 and 85,066
bytes below the 7,999,049-byte base. Both passed their revision's release gate.

The #1087 packaging comparison produced 8,243,427 bytes before recompression
and 8,048,641 bytes after, saving 194,786 bytes on this input. Each revision
matched its independent control's entire ZIP byte for byte. All four wheels
had the same installed-file fingerprint and native SHA-256.

## Reproduction

On a native macOS arm64 host with Git, rustup, uv and Bash, use a disposable
Python environment and run the script from the repository.

```bash
environment=$(mktemp -d)
uv venv --python 3.14 "$environment/venv"
uv pip install --python "$environment/venv/bin/python" maturin==1.16.0 zopflipy==1.14
PATH="$environment/venv/bin:$PATH" PYTHON="$environment/venv/bin/python" \
  bash evidence/wheel-size/paired-builds/reproduce.sh
```

The script prints its temporary directory and keeps the wheels, compiler logs
and fingerprints there. It uses two Cargo jobs and builds with
`maturin build --target aarch64-apple-darwin --release --locked --compression-level 9 --out dist -i "$PYTHON"`.
The same interpreter runs each revision's postprocessor. The local run used
macOS 27.2 (26B123), Apple M2 Pro, Rust 1.100.0 (0ef1bc386), Python 3.14.10,
maturin 1.16.0 and zopflipy 1.14.

[measure.py](measure.py) accepts one or more wheel filenames. It verifies the
installed bytes against every RECORD entry and fingerprints the ordered verified
manifest. The normalized fingerprint removes the generated CycloneDX UUID and
timestamp, serializes the remaining JSON with sorted keys, and substitutes
the resulting SBOM digest and length in RECORD.
Wheel size, archive SHA-256, native SHA-256 and compressed native size are
always measured from the original bytes. ZIP timestamps do not affect the
installed-file comparison.

## Five-platform wheel contents

The [hosted trial](https://github.com/yhay81/pyropdf/actions/runs/44994564240)
built the first parent `07569871` from an empty target and the tested merge
`66977363` with that target available to Cargo. The only changes between the
revisions were the comparison workflow, its tests and evidence. They packaged
the same source and dependencies. All ten distributed wheels passed RECORD
verification for all 88 members. Each pair had the same tag and passed the
absolute and growth checks. Differences in compressed size ranged from 0 to
37 bytes. [Wheel fingerprints and changed members](hosted-contents.json)

On Linux and macOS, the native payloads matched byte for byte on both
architectures. The SBOM recorded each build's UUID, timestamp and local Cargo
component references to its `base/crates` or `head/crates` checkout. Each
RECORD reflected its actual SBOM. Accounting for those values gave identical
SBOMs on all five targets. Every other installed file also matched, apart from
the Windows linker fields below.

The two 23,255,040-byte Windows extensions differed at 24 bytes in four PE
timestamps and the CodeView PDB GUID. The section data and headers matched
everywhere else. The receipt preserves both raw hashes and the SHA-256 after
zeroing just the decoded fields in memory. Both images then hashed to
`250eabb08ca7e7f0a6e030aa45a4b94ac51356a1b093a0a551b838bd4b5c21b5`.

While the trial artifacts are available, the following commands reproduce one
pair's fingerprints. Substitute any target from the receipt. The workflow at
the recorded trial revision includes the paired-wheel upload step.

```bash
target=x86_64-pc-windows-msvc
wheels=$(mktemp -d)
gh run download 44994564240 --repo yhay81/pyropdf --name "paired-wheels-$target" --dir "$wheels"
for checkout in base head; do
  python3 evidence/wheel-size/paired-builds/measure.py --checkout "$checkout" "$wheels/$checkout"/dist/*.whl
done
uv run --script evidence/wheel-size/paired-builds/windows_headers.py \
  "$wheels/base"/dist/*.whl "$wheels/head"/dist/*.whl
```

The last command reads the Windows image. It uses the COFF file header,
debug directories and CodeView record to locate the fields by name.
