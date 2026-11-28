# ruff: noqa: INP001
"""Run TLC exhaustively over all TLA+ state-machine models in evidence/formal-models."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from fetch_tlc import fetch_tlc  # noqa: E402

MODELS = (
    "HistoryAndSaveNumbers",
    "MemoInvalidation",
    "SettlingProtocol",
)


def run_model(jar: Path, java: str, name: str) -> None:
    """Run TLC on `<name>.tla` with `<name>.cfg` and fail on any violation."""
    model_dir = Path(__file__).resolve().parent
    spec = model_dir / f"{name}.tla"
    config = model_dir / f"{name}.cfg"
    with tempfile.TemporaryDirectory(prefix=f"tlc-{name}-") as metadir:
        command = [
            java,
            "-XX:+UseParallelGC",
            "-cp",
            str(jar),
            "tlc2.TLC",
            "-cleanup",
            "-workers",
            "auto",
            "-metadir",
            metadir,
            "-config",
            str(config),
            str(spec),
        ]
        result = subprocess.run(command, capture_output=True, check=False, text=True)  # noqa: S603 - fixed TLC arguments.
    summary = [
        line.strip()
        for line in result.stdout.splitlines()
        if any(
            marker in line
            for marker in (
                "Model checking completed",
                "states generated",
                "Error:",
                "Invariant",
                "Deadlock",
            )
        )
    ]
    if result.returncode != 0:
        sys.stderr.write(f"FAIL {name}\n{result.stdout}\n{result.stderr}\n")
        raise SystemExit(result.returncode)
    sys.stdout.write(f"PASS {name}: {' | '.join(summary)}\n")


def main() -> None:
    """Fetch verified tla2tools.jar and check every model in MODELS."""
    java = shutil.which("java")
    if java is None:
        msg = "java is required to run TLC"
        raise RuntimeError(msg)
    jar = fetch_tlc()
    for name in MODELS:
        run_model(jar, java, name)


if __name__ == "__main__":
    main()
