#!/usr/bin/env bash
# Rebuild the two historical comparisons with one Cargo target on disk at a time.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
work=$(mktemp -d "${TMPDIR:-/tmp}/paired-wheels.XXXXXXXX")
interpreter=$(command -v "${PYTHON:-python3}")
maturin=$(command -v maturin)
target=$(rustc -vV | awk '/^host:/ {print $2}')
export CARGO_BUILD_JOBS=2
echo "Saving wheels and build logs under $work"

warm_base=6a2d2e1c2613ea651656218d82c8e230f1f31527
warm_head=bbb76da7853899239a3710fd8325d7fa13acaeb9
fallback_base=26134041fc1afd50a3ae701a3e590f93e111e89c
fallback_head=a538de34a20d623cd9d3a680fee33b8c965c3fc1

git clone --quiet --shared --no-checkout "$repo" "$work/base"
git -C "$work/base" fetch --no-tags https://github.com/yhay81/pyropdf.git "$warm_head" "$fallback_head"

fresh_head() {
    rm -rf "$work/head"
    git clone --quiet --shared --no-checkout "$work/base" "$work/head"
    git -C "$work/head" checkout --quiet --detach "$1"
}

build() {
    local label=$1 source=$2 cargo_target=$3
    local -a args
    (
        cd "$work/$source"
        rm -rf dist
        read -r -a args < tools/native-wheel-args.txt
        export CARGO_TARGET_DIR="$work/$cargo_target"
        # The Python interpreter must have the locked wheel group (zopflipy).
        { time -p "$maturin" build --target "$target" "${args[@]}" -i "$interpreter"; } >"$work/$label.log" 2>&1
        mkdir "$work/$label-raw" "$work/$label-wheel"
        cp dist/*.whl "$work/$label-raw/"
        { time -p {
            if [[ -f tools/repack_wheel.py ]]; then
                "$interpreter" tools/repack_wheel.py dist >"$work/$label-repack.json"
            fi
            cp dist/*.whl "$work/$label-wheel/"
        }; } 2>"$work/$label-packaging.log"
        "$interpreter" "$here/measure.py" dist/*.whl >"$work/$label-fingerprint.json"
    )
}

git -C "$work/base" checkout --quiet --detach "$warm_base"
build warm-base-cold base paired-target
fresh_head "$warm_head"
build warm-head head paired-target
rm -rf "$work/paired-target"
build clean-head head paired-target
for result in warm-head clean-head; do
    PR_BODY="" BASE_REVISION="$warm_base" HEAD_REVISION="$warm_head" "$interpreter" "$work/head/tools/wheel_size.py" \
      "$work/$result-wheel" --abi abi3 --base "$work/warm-base-cold-wheel" >"$work/$result-gate.json"
done
rm -rf "$work/paired-target"

git -C "$work/base" checkout --quiet --detach "$fallback_base"
build fallback-base base paired-target
fresh_head "$fallback_head"
rm -rf "$work/paired-target"
build fallback-head head fallback-target
rm -rf "$work/fallback-target"
build fallback-reference head fallback-target
for result in fallback-head fallback-reference; do
    PR_BODY="" BASE_REVISION="$fallback_base" HEAD_REVISION="$fallback_head" "$interpreter" "$work/head/tools/wheel_size.py" \
      "$work/$result-wheel" --abi abi3 --base "$work/fallback-base-wheel" >"$work/$result-gate.json"
done
rm -rf "$work/fallback-target"

# Apply #1087's actual packaging stages to the same raw native wheel twice.
for choice in before after; do
    case "$choice" in
        before) rev=23e08a1a2a9214e717da93ec08f18780ceecfc51 ;;
        after) rev=3c2aedaf96f4af353097e7607d5d26c91c097930 ;;
    esac
    fresh_head "$rev"
    for trial in candidate reference; do
        dir="$work/pack-$choice-$trial"
        mkdir "$dir"
        cp "$work/warm-head-raw/"*.whl "$dir/"
        if [[ -f "$work/head/tools/repack_wheel.py" ]]; then
            "$interpreter" "$work/head/tools/repack_wheel.py" "$dir" >"$dir/repack.json"
        fi
        "$interpreter" "$here/measure.py" "$dir/"*.whl >"$dir/fingerprint.json"
    done
done

# Import only installed files, then exercise the known old and new attachment
# behavior. Also check the Python module and typing stub against git blobs.
for label in warm-base-cold warm-head clean-head; do
    revision="$warm_head"
    if [[ "$label" == warm-base-cold ]]; then revision="$warm_base"; fi
    installed="$work/$label-installed"
    uv pip install --quiet --no-deps --python "$interpreter" --target "$installed" "$work/$label-wheel/"*.whl
    for member in _attachments.py _core.pyi; do
        cmp "$installed/pyropdf/$member" <(git -C "$work/base" show "$revision:python/pyropdf/$member")
    done
    "$interpreter" -I - "$installed" "$label" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import pyropdf

document = pyropdf.Document()
document.pages.new()
original = document.attachments.add("report", b"first")
del document.attachments["report"]
document.attachments.add("report", b"second")
try:
    outcome = original.read().decode()
except pyropdf.StaleObjectError:
    outcome = "stale"
assert outcome == ("second" if sys.argv[2] == "warm-base-cold" else "stale"), outcome
PY
done

"$interpreter" - "$work" <<'PY'
import json
import sys
from pathlib import Path
from zipfile import ZipFile

root = Path(sys.argv[1])
def receipt(label):
    return json.loads((root / f"{label}-fingerprint.json").read_text())[0]

for candidate, clean in (("warm-head", "clean-head"), ("fallback-head", "fallback-reference")):
    assert receipt(candidate)["normalized_sha256"] == receipt(clean)["normalized_sha256"]
for base, head in (("warm-base-cold", "warm-head"), ("fallback-base", "fallback-head")):
    assert receipt(base)["native_sha256"] != receipt(head)["native_sha256"]
new_dependencies = {("aes-kw", "0.3.1"), ("hkdf", "0.13.0"), ("keccak", "0.2.2"), ("sha3", "0.11.0")}
for label in ("fallback-base", "fallback-head", "fallback-reference"):
    wheel, = (root / f"{label}-wheel").glob("*.whl")
    with ZipFile(wheel) as archive:
        name, = (n for n in archive.namelist() if n.endswith(".cyclonedx.json"))
        components = {(c["name"], c["version"]) for c in json.loads(archive.read(name))["components"]}
        assert (components & new_dependencies) == (set() if label == "fallback-base" else new_dependencies)
for choice in ("before", "after"):
    left, right = [json.loads((root / f"pack-{choice}-{trial}/fingerprint.json").read_text())[0]
                   for trial in ("candidate", "reference")]
    assert left == right
    assert left["normalized_sha256"] == receipt("warm-head")["normalized_sha256"]
PY
