"""Check generated documentation on the prospective merge of two local Git revisions.

Fetch the current base first. Add --api to build the merged Python package and
check the API reference as well as the specification map.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def main() -> int:
    """Check the synthetic tree without moving the caller's branch or index."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", help="latest fetched base revision, for example origin/main")
    parser.add_argument("head", help="candidate revision, for example HEAD")
    parser.add_argument("--api", action="store_true", help="also build and check the public API reference")
    args = parser.parse_args()
    git = shutil.which("git")
    if git is None:
        parser.error("git is required")
    merge = subprocess.run(  # noqa: S603 - fixed local Git commands, revisions after --.
        [git, "merge-tree", "--write-tree", "--", args.base, args.head], capture_output=True, text=True, check=False
    )
    if merge.returncode:
        sys.stderr.write(merge.stdout + merge.stderr)
        return merge.returncode
    tree = merge.stdout.strip()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "tree.tar"
        source = root / "source"
        subprocess.run([git, "archive", "-o", str(archive), tree], check=True)  # noqa: S603
        with tarfile.open(archive) as contents:
            contents.extractall(source, filter="data")
        result = subprocess.run([sys.executable, "tools/spec_map/generate.py", "--check"], cwd=source, check=False)
        if result.returncode or not args.api:
            return result.returncode
        uv = shutil.which("uv")
        if uv is None:
            parser.error("--api requires uv")
        return subprocess.run(  # noqa: S603
            [uv, "run", "--locked", "python", "tools/api_reference.py", "--check"], cwd=source, check=False
        ).returncode


if __name__ == "__main__":
    sys.exit(main())
