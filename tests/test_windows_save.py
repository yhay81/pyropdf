"""On Windows, ``Document.save`` keeps the target's access control list and saves to long paths (issue #60).

These tests run on Windows only: .github/workflows/windows.yml runs them on
demand and when the save code changes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_pages import numbered

import pyropdf

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows access control and paths")


def access_entries(path: Path) -> list[str]:
    """Return the access control entries that icacls lists for ``path``, without the path."""
    command = ["icacls", str(path)]
    listed = subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603
    lines = [line.strip() for line in listed.splitlines() if line.strip()]
    # The first line starts with the path; the last reports the files processed.
    first = lines[0][len(str(path)) :].strip()
    return [first, *lines[1:-1]]


def test_a_save_keeps_the_targets_access_control_list(tmp_path: Path) -> None:
    target = tmp_path / "restricted.pdf"
    target.write_bytes(b"old")
    user = os.environ["USERNAME"]
    # A protected list that differs from what a new file in the directory inherits.
    subprocess.run(  # noqa: S603
        ["icacls", str(target), "/inheritance:r", "/grant:r", f"{user}:(F)", "/grant:r", "*S-1-1-0:(R)"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    before = access_entries(target)
    fresh = tmp_path / "fresh.pdf"
    fresh.write_bytes(b"new")
    assert access_entries(fresh) != before

    pyropdf.open(numbered(2)).save(target)

    assert access_entries(target) == before
    assert len(pyropdf.open(target).pages) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fresh.pdf", "restricted.pdf"]


def test_a_save_reaches_a_path_longer_than_260_characters(tmp_path: Path) -> None:
    directory = tmp_path / ("d" * 100) / ("e" * 100)
    target = directory / ("f" * 60 + ".pdf")
    assert len(str(target)) > 260
    os.makedirs("\\\\?\\" + str(directory))  # noqa: PTH103 - the verbatim prefix reaches past MAX_PATH.

    pyropdf.open(numbered(3)).save(target)

    data = Path("\\\\?\\" + str(target)).read_bytes()
    assert len(pyropdf.open(data).pages) == 3
    # A save over it, which replaces the existing file, works too.
    pyropdf.open(numbered(1)).save(target)
    assert len(pyropdf.open(Path("\\\\?\\" + str(target)).read_bytes()).pages) == 1
