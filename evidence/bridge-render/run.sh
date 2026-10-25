#!/bin/sh
# Runs the benchmark, and writes the results, with the machine they ran on,
# to results/<system>-<architecture>.md and .csv.
set -eu
cd "$(dirname "$0")"

system=$(uname -s | tr '[:upper:]' '[:lower:]')
[ "$system" = darwin ] && system=macos
name="$system-$(uname -m)"
mkdir -p results

machine() {
    if [ "$system" = macos ]; then
        echo "- CPU: $(sysctl -n machdep.cpu.brand_string), $(sysctl -n hw.perflevel0.physicalcpu) performance and $(sysctl -n hw.perflevel1.physicalcpu) efficiency cores"
        echo "- Memory: $(($(sysctl -n hw.memsize) / 1073741824)) GiB"
        echo "- System: macOS $(sw_vers -productVersion) ($(sw_vers -buildVersion))"
    else
        echo "- CPU: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | sed 's/^ //')"
        echo "- Memory: $(awk '/MemTotal/ { print int($2 / 1048576) " GiB" }' /proc/meminfo)"
        echo "- System: $(uname -sr)"
    fi
    echo "- Rust: $(rustc -V)"
    echo "- Date: $(date -u +%Y-%m-%d)"
}

cargo build --release --locked --quiet
{
    echo "# Bridge render measurements, $name"
    echo
    echo "Written by \`run.sh\`; the README describes the workload."
    echo
    machine
    echo
    ./target/release/bridge-render-bench "results/$name.csv"
} > "results/$name.md"
echo "wrote results/$name.md and results/$name.csv"
