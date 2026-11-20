#!/usr/bin/env bash
# Build the release wheel on Linux and test its installed extension in Node/Pyodide.
set -euo pipefail

readonly runtime=314.0.6
readonly platform=pyemscripten_2026_0_wasm32
build_dir=$(mktemp -d)
trap 'rm -rf -- "$build_dir"' EXIT

# The build frontend and maturin run on host CPython 3.14.2. The target
# interpreter comes from the selected Pyodide cross-build environment.
python -m venv "$build_dir/host"
source "$build_dir/host/bin/activate"
python -m pip install 'pyodide-build==0.39.4' 'maturin==1.16.0'
export PYODIDE_XBUILDENV_PATH="$build_dir/xbuildenv"
pyodide xbuildenv install "$runtime"
pyodide xbuildenv install-emscripten --version 5.0.3
source "$(pyodide config get emsdk_dir)/emsdk_env.sh"
rustup target add wasm32-unknown-emscripten

source_date_epoch=$(git log -1 --format=%ct)
export SOURCE_DATE_EPOCH="$source_date_epoch"
pyodide build . --outdir "$build_dir/wheels" --no-isolation -C maturin.build-args=--locked

shopt -s nullglob
wheels=("$build_dir"/wheels/pyropdf-*-"$platform".whl)
[[ ${#wheels[@]} == 1 ]]
pyodide venv "$build_dir/runtime"
"$build_dir/runtime/bin/pip" install --no-deps "${wheels[0]}"
"$build_dir/runtime/bin/python" -I -c '
import platform, sys, sysconfig, pyodide
assert sys.platform == "emscripten"
assert pyodide.__version__ == sys.argv[1]
assert platform.machine() == "wasm32"
print(f"Pyodide {pyodide.__version__}, Python {sys.version}, {sysconfig.get_platform()}")
' "$runtime"
"$build_dir/runtime/bin/python" -I tools/smoke_artifact.py . --run
mkdir -p dist
cp "${wheels[0]}" dist/
wheel=dist/$(basename "${wheels[0]}")
{
    echo "Pyodide $runtime, $platform"
    sha256sum "$wheel"
    wc -c "$wheel"
} | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
