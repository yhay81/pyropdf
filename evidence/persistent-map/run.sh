#!/bin/sh
# Runs the benchmark and the code-size probes, and writes the results, with
# the machine they ran on, to results/<system>-<architecture>.md and .csv.
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
    echo "- Crates: $(cargo tree --locked --depth 1 -e normal --prefix none | tail -n +2 | sort | paste -sd ',' - | sed 's/,/, /g')"
    echo "- Date: $(date -u +%Y-%m-%d)"
}

# The bytes of a library's sections that take file space, which page
# alignment does not round.
sections() {
    if [ "$system" = macos ]; then
        size -m "$1" | awk '/^\tSection/ && !/zerofill/ { sum += $3 } END { print sum }'
    else
        size -A "$1" | awk '$1 ~ /^\./ && $1 !~ /^\.(bss|tbss|comment|debug)/ { sum += $2 } END { print sum }'
    fi
}

probes() {
    base=''
    for probe in baseline rpds-hash rpds-tree im-hash im-ord imbl-hash imbl-ord chunks; do
        cargo build --release --locked --quiet --example probe --features "probe-$probe"
        for library in target/release/examples/libprobe.dylib target/release/examples/libprobe.so; do
            [ -f "$library" ] && break
        done
        code=$(sections "$library")
        file=$(wc -c <"$library" | tr -d ' ')
        packed=$(gzip -9 -c "$library" | wc -c | tr -d ' ')
        if [ -z "$base" ]; then
            base_code=$code
            base_file=$file
            base_packed=$packed
            base=1
        fi
        echo "| $probe | $code | $((code - base_code)) | $file | $((file - base_file)) | $packed | $((packed - base_packed)) |"
    done
}

cargo build --release --locked --quiet
{
    echo "# Persistent map measurements, $name"
    echo
    echo "Written by \`run.sh\`; the README describes the workloads."
    echo
    machine
    echo
    ./target/release/persistent-map-bench "results/$name.csv"
    echo "## Code size"
    echo
    echo "\`examples/probe.rs\` built as a shared library around each map, with"
    echo "the release profile of the workspace. Bytes of the sections that take"
    echo "file space (code and data), of the file, which keeps its symbol table"
    echo "and pads segments to pages, and of the file compressed by \`gzip -9\`, as"
    echo "a wheel's deflate would. Each Added column is the difference from the"
    echo "baseline, a plain \`Vec\` behind the same functions."
    echo
    echo "| Probe | Code and data | Added | File | Added | Compressed | Added |"
    echo "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"
    probes
} > "results/$name.md"
echo "wrote results/$name.md and results/$name.csv"
