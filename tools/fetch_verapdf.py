"""Fetch both pinned veraPDF validators from Maven Central for CI."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

VERSION = "1.28.2"
CENTRAL = (
    "https://repo.maven.apache.org/maven2",
    "https://repo1.maven.org/maven2",
    "https://maven-central.storage-download.googleapis.com/maven2",
)
JARS = {
    "greenfield-apps": "687d4d8bcfec48c9f7e931cf78ed061f1eeccfba55d4cdc000ba9b4d8aa46ce6",
    "greenfield-apps-arlington": "a780142be33c9ce00a0fd075c6d7b706074a01e87aa5c7d7328db4ff345a4464",
}


def fetch_jar(destination: Path, urls: tuple[str, ...], expected: str) -> None:
    """Install a verified jar, trying each endpoint with one retry for transient failures."""
    curl = shutil.which("curl")
    if curl is None:
        msg = "curl is required to fetch the veraPDF validators"
        raise RuntimeError(msg)
    with TemporaryDirectory(dir=destination.parent) as scratch:
        download = Path(scratch) / destination.name
        for url in urls:
            # curl retries e.g. 429 and 503; a 404 goes directly to the next endpoint.
            command = [
                curl,
                "--disable",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--connect-timeout",
                "5",
                "--max-time",
                "20",
                "--retry",
                "1",
                "--retry-max-time",
                "25",
                "--output",
                str(download),
                url,
            ]
            try:
                result = subprocess.run(command, check=False, timeout=45)  # noqa: S603 - fixed curl arguments.
            except subprocess.TimeoutExpired:
                continue
            if result.returncode:
                continue
            with download.open("rb") as file:
                actual = hashlib.file_digest(file, "sha256").hexdigest()
            if actual != expected:
                msg = f"SHA-256 mismatch for {url}: expected {expected}, got {actual}"
                raise RuntimeError(msg)
            download.replace(destination)
            return
    msg = f"Could not download {destination.name} from {', '.join(urls)}"
    raise RuntimeError(msg)


def main() -> None:
    """Fetch the two jars used by the subsequent validator step."""
    directory = Path(__file__).resolve().parent.parent / "target" / "verapdf"
    directory.mkdir(parents=True, exist_ok=True)
    for artifact, digest in JARS.items():
        path = f"org/verapdf/apps/{artifact}/{VERSION}/{artifact}-{VERSION}.jar"
        fetch_jar(directory / f"{artifact}.jar", tuple(f"{base}/{path}" for base in CENTRAL), digest)


if __name__ == "__main__":
    main()
