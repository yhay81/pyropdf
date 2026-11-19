#!/usr/bin/env bash
# Install the cargo-audit and actionlint that the audit job of
# .github/workflows/ci.yml runs, from their official releases for Linux or
# macOS, after checking each archive against its pinned SHA-256 digest.
#
#     bash tools/install_audit_tools.sh [installation-directory]
#
# The directory defaults to one under RUNNER_TEMP or TMPDIR, and on GitHub
# Actions the script adds it to GITHUB_PATH.
set -euo pipefail

if [[ "$#" -gt 1 ]]; then
    printf 'usage: %s [installation-directory]\n' "$0" >&2
    exit 1
fi
# Official releases, pinned together with the digests of their archives:
# https://github.com/rustsec/rustsec/releases/tag/cargo-audit%2Fv0.23.0
# https://github.com/rhysd/actionlint/releases/tag/v1.7.12
# actionlint publishes its digests in actionlint_1.7.12_checksums.txt.
# cargo-audit publishes none, so its digests are the ones that GitHub records
# for the release assets, which `gh release view` prints with `--json assets`.
cargo_audit_version="0.23.0"
actionlint_version="1.7.12"
case "$(uname -s)/$(uname -m)" in
    Linux/x86_64|Linux/amd64)
        cargo_audit_target=x86_64-unknown-linux-musl
        cargo_audit_sha256="4f01c74ed84b2dffd0b1d37012a62cdf2a92c96e25bdf8eec7fcc0d4d3eb7811"
        actionlint_platform=linux_amd64
        actionlint_sha256="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
        ;;
    Linux/aarch64|Linux/arm64)
        cargo_audit_target=aarch64-unknown-linux-gnu
        cargo_audit_sha256="00eff3158429b9629abe8748e971f29f99ee53d4cff54a25072cbb15167bdda8"
        actionlint_platform=linux_arm64
        actionlint_sha256="325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6"
        ;;
    Darwin/x86_64)
        cargo_audit_target=x86_64-apple-darwin
        cargo_audit_sha256="845ec02452dd233c292be887700ac99a38a697299423081ee0d8ea1a0c961464"
        actionlint_platform=darwin_amd64
        actionlint_sha256="5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644"
        ;;
    Darwin/arm64)
        cargo_audit_target=aarch64-apple-darwin
        cargo_audit_sha256="84101204e5b8013c9a8d7251766aebdef3e7fa1a3ef040cde44d65b48b8c4056"
        actionlint_platform=darwin_arm64
        actionlint_sha256="aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f"
        ;;
    *) printf 'the audit tools installer requires Linux or macOS on x86-64 or ARM64\n' >&2; exit 1 ;;
esac
cargo_audit_name="cargo-audit-${cargo_audit_target}-v${cargo_audit_version}"
cargo_audit_url="https://github.com/rustsec/rustsec/releases/download/cargo-audit%2Fv${cargo_audit_version}/${cargo_audit_name}.tgz"
actionlint_url="https://github.com/rhysd/actionlint/releases/download/v${actionlint_version}/actionlint_${actionlint_version}_${actionlint_platform}.tar.gz"
temporary_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
installation_directory="${1:-${temporary_root}/pyropdf-audit-tools}"
download_directory="$(mktemp -d "${temporary_root}/pyropdf-audit-tools-download.XXXXXX")"
trap 'rm -rf -- "$download_directory"' EXIT

# Print the SHA-256 digest of standard input: sha256sum on Linux, shasum on macOS.
sha256() {
    if command -v sha256sum >/dev/null; then
        sha256sum
    else
        shasum --algorithm 256
    fi | cut -d ' ' -f 1
}

# Download an archive, check it against its digest, and extract one member
# into the download directory.
fetch() {
    local url="$1" expected="$2" member="$3"
    local archive="${download_directory}/${url##*/}" actual
    curl --fail --silent --show-error --location --retry 3 \
        --connect-timeout 20 --max-time 120 --output "$archive" "$url"
    actual="$(sha256 <"$archive")"
    if [[ "$actual" != "$expected" ]]; then
        printf '%s has SHA-256 %s, not the pinned %s\n' "$url" "$actual" "$expected" >&2
        exit 1
    fi
    tar -x -z -f "$archive" -C "$download_directory" "$member"
}

fetch "$cargo_audit_url" "$cargo_audit_sha256" "${cargo_audit_name}/cargo-audit"
fetch "$actionlint_url" "$actionlint_sha256" actionlint

mkdir -p -- "$installation_directory"
installation_directory="$(cd "$installation_directory" && pwd -P)"
install -m 0755 "${download_directory}/${cargo_audit_name}/cargo-audit" "${installation_directory}/cargo-audit"
install -m 0755 "${download_directory}/actionlint" "${installation_directory}/actionlint"
if [[ -n "${GITHUB_PATH:-}" ]]; then
    printf '%s\n' "$installation_directory" >> "$GITHUB_PATH"
fi
"${installation_directory}/cargo-audit" --version
"${installation_directory}/actionlint" -version
