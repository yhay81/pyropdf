#!/bin/sh
# Usage: run.sh <bin> <hayro checkout> [<another hayro checkout> ...]
# Runs src/main.rs (bin `inline-image-end-bench`) or src/bin/<bin>.rs against
# each checkout's hayro-syntax.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
bin=$1
shift
for hayro in "$@"; do
    hayro=$(cd "$hayro" && pwd)
    echo "== $(git -C "$hayro" log --oneline -1)"
    cargo run --quiet --release --manifest-path "$here/Cargo.toml" \
        --target-dir "$here/target" --bin "$bin" \
        --config "patch.crates-io.hayro-syntax.path=\"$hayro/hayro-syntax\""
done
