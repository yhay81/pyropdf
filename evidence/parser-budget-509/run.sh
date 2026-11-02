#!/usr/bin/env bash
set -euo pipefail

root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
baseline=7edb085d8da316597df823a73d4ca793c8cf9309
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

git -C "$root" archive --format=tar "$baseline" | tar -xf - -C "$scratch"
mkdir -p "$scratch/crates/pyropdf-store/examples"
cp "$root/crates/pyropdf-store/examples/parser_budget_probe.rs" \
  "$scratch/crates/pyropdf-store/examples/parser_budget_probe.rs"

target="${CARGO_TARGET_DIR:-$root/target/evidence/parser-budget-509}"
printf '%s\n' 'baseline:'
CARGO_TARGET_DIR="$target/baseline" cargo run --locked --release --manifest-path "$scratch/Cargo.toml" \
  -p pyropdf-store --example parser_budget_probe
printf '%s\n' 'current:'
CARGO_TARGET_DIR="$target/current" cargo run --locked --release --manifest-path "$root/Cargo.toml" \
  -p pyropdf-store --example parser_budget_probe
