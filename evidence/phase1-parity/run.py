# ruff: noqa: INP001 - a script, not a package.
"""Install frozen pylopdf in its own environment and dump both engines over the corpus.

    python3 evidence/phase1-parity/run.py PYLOPDF_CHECKOUT

PYLOPDF_CHECKOUT is a git checkout of pylopdf that holds the freeze commit.
The script only reads it, with ``git archive``. It builds pylopdf from that
commit, from a copy outside this Cargo workspace, into
``target/parity/pylopdf-env``, without extras, so neither engine has fallback
fonts. It then dumps pylopdf with that environment and pyropdf
with ``uv run``, into ``target/parity/pylopdf`` and ``target/parity/pyropdf``,
and runs ``compare.py`` on the two.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

#: The freeze commit of pylopdf (#95).
FREEZE = "ebb01d3c4c742046e8ce7fd72957cd15a2933321"
ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TARGET = ROOT / "target" / "parity"
CORPUS = sorted((ROOT / "tests" / "assets" / "real_world").glob("*.pdf"))


def _run(*command: str | Path) -> None:
    subprocess.run([str(part) for part in command], check=True, cwd=ROOT)  # noqa: S603 - fixed tools


def _install_pylopdf(checkout: Path) -> Path:
    """Build pylopdf at the freeze commit into its own environment; return its Python."""
    env = TARGET / "pylopdf-env"
    python = env / "bin" / "python"
    stamp = env / ".source-commit"
    if (
        python.exists()
        and stamp.exists()
        and stamp.read_text().strip() == FREEZE
        and subprocess.run([python, "-c", "import pylopdf"], check=False).returncode == 0  # noqa: S603
    ):
        return python
    source = Path(tempfile.mkdtemp(prefix="pylopdf-"))
    archive = subprocess.run(  # noqa: S603
        ["git", "-C", str(checkout), "archive", FREEZE],  # noqa: S607 - git from PATH
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(source, filter="data")
    _run("uv", "venv", "--clear", "--python", f"{sys.version_info.major}.{sys.version_info.minor}", env)
    _run("uv", "pip", "install", "--python", python, source)
    stamp.write_text(FREEZE + "\n")
    return python


def main() -> None:
    """Build pylopdf, dump both engines, and compare them."""
    (checkout,) = sys.argv[1:]
    python = _install_pylopdf(Path(checkout))
    _run(python, HERE / "dump.py", "pylopdf", TARGET / "pylopdf", *CORPUS)
    _run("uv", "run", "--no-sync", "python", HERE / "dump.py", "pyropdf", TARGET / "pyropdf", *CORPUS)
    _run("uv", "run", "--no-sync", "python", HERE / "compare.py", TARGET / "pylopdf", TARGET / "pyropdf")
    _run("uv", "run", "--no-sync", "python", HERE / "diagnose_annotations.py", python)
    metadata = {
        "generated_at": datetime.now(UTC).isoformat(),
        "pylopdf_commit": FREEZE,
        "pyropdf_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),  # noqa: S607
        "runner_python": sys.version,
        "pyropdf_python": subprocess.check_output(
            ["uv", "run", "--no-sync", "python", "-c", "import sys; print(sys.version)"],  # noqa: S607
            cwd=ROOT,
            text=True,
        ).strip(),
        "oracle_python": subprocess.check_output([python, "-c", "import sys; print(sys.version)"], text=True).strip(),  # noqa: S603
        "corpus_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in CORPUS},
    }
    (HERE / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    main()
