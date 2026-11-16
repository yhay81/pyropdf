"""Capture aggregate GitHub metrics; fail without writing a snapshot if any read fails."""

# ruff: noqa: INP001 - standalone evidence script.

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = "yhay81/pyropdf"
METADATA = (
    "{description,homepage,topics,stargazers_count,forks_count,subscribers_count,"
    "created_at,updated_at,pushed_at,license:.license.spdx_id}"
)
ENDPOINTS = (
    ("repository", "", METADATA),
    ("views", "/traffic/views", "."),
    ("clones", "/traffic/clones", "."),
    ("referrers", "/traffic/popular/referrers", "."),
    ("paths", "/traffic/popular/paths", "."),
)


def main() -> None:
    """Read all endpoints before exclusively creating the requested output file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new JSON file; existing snapshots are never overwritten")
    args = parser.parse_args()
    snapshot: dict[str, object] = {
        "repository_name": REPOSITORY,
        "started_at": datetime.now(UTC).isoformat(),
    }
    for name, suffix, query in ENDPOINTS:
        result = subprocess.run(  # noqa: S603 - fixed repository and endpoints, no shell.
            ["gh", "api", f"repos/{REPOSITORY}{suffix}", "--jq", query],  # noqa: S607 - use authenticated gh.
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
        snapshot[name] = json.loads(result.stdout)
    snapshot["completed_at"] = datetime.now(UTC).isoformat()
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(snapshot, output, indent=2)
        output.write("\n")
    print(args.output)  # noqa: T201 - report the completed snapshot's path.


if __name__ == "__main__":
    main()
