#!/usr/bin/env bash
# Checks out krilla at the commit the patches are based on, applies the W1
# sink patch and krilla-resources.patch, runs krilla's resource tests, and
# runs the probe that stamps one face on many pages into pyropdf-store. Run
# from the repository root.
set -euo pipefail
here=evidence/krilla-resources
dir=target/krilla-resources/krilla
base=631e4645c9cdde1bd0bab652b9bf443d603d535a
if [ ! -d "$dir" ]; then
  git clone --quiet https://github.com/LaurenzV/krilla.git "$dir"
fi
git -C "$dir" checkout --quiet --force --detach "$base"
git -C "$dir" clean --quiet -fd
git -C "$dir" apply "$PWD/evidence/krilla-sink/krilla-object-sink.patch"
git -C "$dir" apply "$PWD/$here/krilla-resources.patch"
cargo test --quiet --manifest-path "$dir/Cargo.toml" -p krilla --lib resources
cargo run --quiet --release --manifest-path "$here/probe/Cargo.toml" -- "$dir/assets/fonts/NotoSans-Regular.ttf"
