#!/bin/sh
# Reproduces results/ (#435). Usage: sh evidence/svg-text/run.sh <scratch-dir>
# Builds the probes against hayro and hayro-svg 0.8.0 from crates.io; the
# second probe uses hayro-svg 0.8.0 with draft.patch applied.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
assets="$here/../../tests/assets/real_world"
scratch=$1
mkdir -p "$scratch/probe/src" "$scratch/layer/src" "$scratch/out"
cp "$here/probe.Cargo.toml" "$scratch/probe/Cargo.toml"
cp "$here/probe.rs" "$scratch/probe/src/main.rs"
(cd "$scratch/probe" && cargo build --release -q)
"$scratch/probe/target/release/probe" "$assets"/*.pdf > "$here/results/hayro-svg-0.8.0.tsv"

registry=$(dirname "$(cd "$scratch/probe" && cargo metadata --format-version 1 \
  | python3 -c 'import json,sys; print(next(p["manifest_path"] for p in json.load(sys.stdin)["packages"] if p["name"] == "hayro-svg"))')")
rm -rf "$scratch/hayro-svg"
cp -R "$registry" "$scratch/hayro-svg"
(cd "$scratch/hayro-svg" && patch -p1 < "$here/draft.patch")
sed -e 's/^name = "svgprobe"/name = "layerprobe"/' -e 's/^name = "probe"/name = "layer"/' \
  "$here/probe.Cargo.toml" > "$scratch/layer/Cargo.toml"
printf '[patch.crates-io]\nhayro-svg = { path = "../hayro-svg" }\n' >> "$scratch/layer/Cargo.toml"
cp "$here/layer.rs" "$scratch/layer/src/main.rs"
(cd "$scratch/layer" && cargo build --release -q)
"$scratch/layer/target/release/layer" "$scratch/out" "$assets"/*.pdf > "$here/results/draft-layer.tsv"
python3 -I "$here/check_layer.py" "$scratch/out"
