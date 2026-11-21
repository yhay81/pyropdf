#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
project=$(git -C "$here" rev-parse --show-toplevel)
scratch=${1:?Pass a scratch directory for the upstream checkout, build and browser outputs}
mkdir -p "$scratch"
scratch=$(cd "$scratch" && pwd)
upstream="$scratch/hayro"
revision=623a2508d385852ebfc896cee16cd683996f34b6

if [[ ! -d "$upstream/.git" ]]; then
    git clone --depth 1 --no-checkout --filter=blob:none --sparse \
        https://github.com/LaurenzV/hayro.git "$upstream"
    git -C "$upstream" fetch --depth 1 origin "$revision"
    git -C "$upstream" ls-tree -d --name-only "$revision" |
        awk '/^(assets|hayro(-.*)?)$/' | git -C "$upstream" sparse-checkout set --stdin
    git -C "$upstream" checkout --detach "$revision"
fi
if [[ $(git -C "$upstream" rev-parse HEAD) != "$revision" ]] || ! git -C "$upstream" diff --quiet HEAD; then
    echo "Use a scratch checkout of the unmodified hayro commit $revision." >&2
    exit 1
fi

mkdir -p "$scratch/probe/src" "$scratch/fixtures" "$scratch/output"
cp "$here/probe.Cargo.toml" "$scratch/probe/Cargo.toml"
cp "$here/render.rs" "$scratch/probe/src/main.rs"
cp "$upstream/Cargo.lock" "$scratch/probe/Cargo.lock"
git -C "$upstream" show "$revision:tests/pdfs/text/text_layer.pdf" > "$scratch/fixtures/upstream-layer.pdf"
git -C "$upstream" show "$revision:tests/pdfs/text/text_layer_invisible.pdf" > "$scratch/fixtures/upstream-ocr.pdf"
cargo run --quiet --manifest-path "$scratch/probe/Cargo.toml" -- \
    "$scratch/output" "$here/positions.pdf" "$scratch/fixtures/upstream-layer.pdf" \
    "$scratch/fixtures/upstream-ocr.pdf" "$project/tests/assets/text/nonembedded-latin.pdf" \
    "$project/tests/assets/text/nonembedded-cjk.pdf" "$project/tests/assets/text/hebrew-niqqud.pdf"

npm install --prefix "$scratch/drivers" --no-audit --no-fund --ignore-scripts playwright-core@1.65.1 pngjs@7.0.0
"$scratch/drivers/node_modules/.bin/playwright-core" install chromium firefox webkit
NODE_PATH="$scratch/drivers/node_modules" node "$here/check.cjs" "$scratch/output" | tee "$scratch/browser.tsv"
