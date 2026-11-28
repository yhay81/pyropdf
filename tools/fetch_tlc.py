"""Fetch the pinned TLA+ TLC jar (tla2tools.jar) with SHA-256 verification."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

VERSION = "v1.7.4"
URLS = (f"https://github.com/tlaplus/tlaplus/releases/download/{VERSION}/tla2tools.jar",)
SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"


def jar_path() -> Path:
    """Return the local cache path for the verified tla2tools.jar."""
    return Path(__file__).resolve().parent.parent / "target" / "tlc" / "tla2tools.jar"


def is_verified(path: Path) -> bool:
    """Return whether `path` already exists with the pinned SHA-256 digest."""
    if not path.is_file():
        return False
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest() == SHA256


def fetch_tlc(destination: Path | None = None) -> Path:
    """Ensure `tla2tools.jar` is present and matches its pinned SHA-256."""
    target = destination or jar_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_verified(target):
        return target
    curl = shutil.which("curl")
    if curl is None:
        msg = "curl is required to fetch tla2tools.jar"
        raise RuntimeError(msg)
    with TemporaryDirectory(dir=target.parent) as scratch:
        download = Path(scratch) / target.name
        for url in URLS:
            command = [
                curl,
                "--disable",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--connect-timeout",
                "10",
                "--max-time",
                "60",
                "--retry",
                "2",
                "--retry-max-time",
                "90",
                "--output",
                str(download),
                url,
            ]
            try:
                result = subprocess.run(command, check=False, timeout=120)  # noqa: S603 - fixed curl arguments.
            except subprocess.TimeoutExpired:
                continue
            if result.returncode:
                continue
            with download.open("rb") as file:
                actual = hashlib.file_digest(file, "sha256").hexdigest()
            if actual != SHA256:
                msg = f"SHA-256 mismatch for {url}: expected {SHA256}, got {actual}"
                raise RuntimeError(msg)
            download.replace(target)
            return target
    msg = f"Could not download {target.name} from {', '.join(URLS)}"
    raise RuntimeError(msg)


def main() -> None:
    """Fetch and verify tla2tools.jar into target/tlc/tla2tools.jar."""
    path = fetch_tlc()
    sys.stdout.write(f"verified {path} ({SHA256})\n")


if __name__ == "__main__":
    main()
