# ruff: noqa: INP001 - a script, not a package.
"""Write the fuzz targets' seeds: the corpus and its damaged inputs, and their objects for ``value``.

Run from the repository root: ``python3 -I fuzz/seeds.py``. The seeds go to
fuzz/corpus/<target>/, which git ignores; tests/corpus.py keeps the damage's
one definition.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from corpus import DAMAGE, NAMES, damaged  # noqa: E402

ASSETS = ROOT / "tests" / "assets"
TARGETS = ("index", "save", "bridge")
#: The values of uncompressed objects, as the ``value`` target reads them.
OBJECT = re.compile(rb"\d+\s+\d+\s+obj\b(.*?)\bendobj", re.DOTALL)
MAX_VALUE_SEED = 4096


def write(target: str, data: bytes) -> None:
    """Write one seed of ``target``, named by its hash."""
    directory = ROOT / "fuzz" / "corpus" / target
    directory.mkdir(parents=True, exist_ok=True)
    (directory / hashlib.sha256(data).hexdigest()[:16]).write_bytes(data)


def main() -> None:
    """Write every seed."""
    files = [(ASSETS / "real_world" / name).read_bytes() for name in NAMES]
    files += [path.read_bytes() for path in sorted((ASSETS / "encrypted").glob("*.pdf"))]
    files += [damaged(name, kind) for name in NAMES for kind in DAMAGE]
    for data in files:
        for target in TARGETS:
            write(target, data)
        for match in OBJECT.finditer(data):
            value = match.group(1).strip()
            if value and len(value) <= MAX_VALUE_SEED and b"stream" not in value:
                write("value", value)


if __name__ == "__main__":
    main()
