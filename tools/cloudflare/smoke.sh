#!/usr/bin/env bash
# Verify one release wheel locally, or explicitly deploy a private disposable Worker.
set -euo pipefail

if [[ $# != 2 || ( $1 != local && $1 != hosted ) || ! -f $2 || $2 != *-pyemscripten_2026_0_wasm32.whl ]]; then
    echo "Usage: bash tools/cloudflare/smoke.sh {local|hosted} path/to/pyropdf-*-pyemscripten_2026_0_wasm32.whl" >&2
    exit 2
fi
mode=$1
if [[ $mode == hosted ]]; then
    : "${CLOUDFLARE_ACCOUNT_ID:?Set the dedicated smoke account ID}"
    : "${CLOUDFLARE_API_TOKEN:?Set the dedicated smoke account token}"
fi
fixture=$(cd "$(dirname "$0")" && pwd)
wheel=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
project=$(mktemp -d)
server=
worker=
wrangler="$project/node_modules/.bin/wrangler"

cleanup() {
    status=$?
    if [[ -n $server ]]; then
        kill "$server" 2>/dev/null || true
        wait "$server" || true
    fi
    if [[ -n $worker ]] && ! "$wrangler" delete --name "$worker" --config wrangler.jsonc --force; then
        echo "Remove private smoke Worker $worker from the smoke account." >&2
        status=1
    fi
    rm -rf -- "$project"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cd "$project"
mkdir src
cp "$fixture/wrangler.jsonc" .
cp "$fixture/src/worker.py" src/
# pywrangler also builds a host development venv; only the Workers interpreter receives the WASM artifact.
wheel_uri=$(python3 -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).as_uri())' "$wheel")
cat > pyproject.toml <<EOF
[project]
name = "pyropdf-wheel-smoke"
version = "0.0.0"
requires-python = ">=3.14"
dependencies = ["pyropdf @ $wheel_uri ; sys_platform == 'emscripten'", "workers-runtime-sdk==1.9.6"]
EOF
export WRANGLER_SEND_METRICS=false
npm install --no-save --package-lock=false --no-audit --no-fund wrangler@4.165.0
uvx --from workers-py==1.17.9 pywrangler sync
config=wrangler.jsonc
dev_mode=--local
if [[ $mode == hosted ]]; then
    # A remote service binding reaches the Worker without giving it a public URL.
    worker="pyropdf-smoke-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
    "$wrangler" deploy --name "$worker" --config wrangler.jsonc
    config=proxy.jsonc
    cat > "$config" <<EOF
{"name":"pyropdf-smoke-proxy","main":"src/proxy.mjs","compatibility_date":"2026-11-01",
 "services":[{"binding":"TARGET","service":"$worker","remote":true}]}
EOF
    echo 'export default { fetch(request, env) { return env.TARGET.fetch(request); } };' > src/proxy.mjs
    dev_mode=--remote=false
fi

port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
"$wrangler" dev --config "$config" "$dev_mode" --ip 127.0.0.1 --port "$port" --show-interactive-dev-session=false > server.log 2>&1 &
server=$!
for ((attempt=0; attempt<90; attempt++)); do
    if curl --noproxy '*' -s --max-time 5 "http://127.0.0.1:$port/runtime" -o /dev/null; then
        break
    fi
    if ! kill -0 "$server" 2>/dev/null || (( attempt == 89 )); then
        cat server.log >&2
        exit 1
    fi
    sleep 1
done
{
    echo "Cloudflare Workers ($mode), compatibility_date=$(node -p 'JSON.parse(require("fs").readFileSync("wrangler.jsonc")).compatibility_date')"
    echo "Wrangler $("$wrangler" --version), local workerd package $(node -p 'require("workerd/package.json").version')"
    python3 "$fixture/../smoke_worker.py" "$port" "$wheel"
} | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
