#!/usr/bin/env bash
# Checks out krilla at the commit the patch is based on, applies
# krilla-object-sink.patch, runs krilla's sink tests, and runs the probe that
# writes into pyropdf-store. Run from the repository root.
set -euo pipefail
here=evidence/krilla-sink
dir=target/krilla-sink/krilla
base=631e4645c9cdde1bd0bab652b9bf443d603d535a
if [ ! -d "$dir" ]; then
  git clone --quiet https://github.com/LaurenzV/krilla.git "$dir"
fi
git -C "$dir" checkout --quiet --force --detach "$base"
git -C "$dir" clean --quiet -fd
git -C "$dir" apply "$PWD/$here/krilla-object-sink.patch"
cargo test --quiet --manifest-path "$dir/Cargo.toml" -p krilla --lib sink
cargo run --quiet --release --manifest-path "$here/probe/Cargo.toml"
