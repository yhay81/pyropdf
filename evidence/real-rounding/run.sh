#!/bin/sh
# Usage: run.sh <hayro checkout> [<another hayro checkout> ...]
# Runs the benchmark against each checkout's hayro-syntax.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
for hayro in "$@"; do
    hayro=$(cd "$hayro" && pwd)
    echo "== $(git -C "$hayro" log --oneline -1)"
    cargo run --quiet --release --manifest-path "$here/Cargo.toml" \
        --target-dir "$here/target" \
        --config "patch.crates-io.hayro-syntax.path=\"$hayro/hayro-syntax\""
done
