"""Smoke-test an installed pyropdf-layout-models wheel or sdist."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import sys
from pathlib import Path

EXPECTED_LABEL_COUNT = 20
EXPECTED_INPUT_SIZE = 800


def _verify_checksums(model_dir: Path) -> None:
    sums_path = model_dir / "SHA256SUMS"
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        expected_sum, name = stripped.split(None, 1)
        got = hashlib.sha256((model_dir / name).read_bytes()).hexdigest()
        if got != expected_sum:
            msg = f"SHA256SUMS mismatch for {name}: expected {expected_sum}, got {got}"
            raise ValueError(msg)


def _verify_dist_files() -> None:
    dist = importlib.metadata.distribution("pyropdf-layout-models")
    files = {Path(str(f)).name for f in (dist.files or ())}
    for required in ("LICENSE", "NOTICE", "py.typed"):
        if required not in files:
            msg = f"required distribution file missing: {required}"
            raise FileNotFoundError(msg)


def main() -> None:
    """Verify bundled layout model paths, metadata, SHA-256 digests, and license files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Expected package version.")
    args = parser.parse_args()

    mod = importlib.import_module("pyropdf_layout_models")
    if mod.__version__ != args.version:
        msg = f"version mismatch: expected {args.version!r}, got {mod.__version__!r}"
        raise RuntimeError(msg)

    paths = mod.model_paths()
    if paths.version != args.version:
        msg = f"model_paths().version mismatch: expected {args.version!r}, got {paths.version!r}"
        raise RuntimeError(msg)
    if not paths.model.is_file() or not paths.metadata.is_file():
        msg = f"bundled artifacts missing: model={paths.model}, metadata={paths.metadata}"
        raise FileNotFoundError(msg)

    meta = json.loads(paths.metadata.read_text(encoding="utf-8"))
    if meta.get("input_size") != EXPECTED_INPUT_SIZE or len(meta.get("labels", ())) != EXPECTED_LABEL_COUNT:
        msg = f"unexpected metadata: input_size={meta.get('input_size')!r}, labels={len(meta.get('labels', ()))}"
        raise ValueError(msg)

    actual_sha = hashlib.sha256(paths.model.read_bytes()).hexdigest()
    if actual_sha != meta.get("sha256"):
        msg = f"model SHA-256 mismatch: expected {meta.get('sha256')}, got {actual_sha}"
        raise ValueError(msg)

    _verify_checksums(paths.model.parent)
    _verify_dist_files()
    sys.stdout.write(
        f"pyropdf-layout-models {paths.version} OK ({paths.model.name}, "
        f"{paths.model.stat().st_size:,} B, sha256={actual_sha[:16]})\n"
    )


if __name__ == "__main__":
    main()
