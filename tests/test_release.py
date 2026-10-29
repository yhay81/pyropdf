"""The release workflow and the release texts agree with the repository."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from test_text import build_text_pdf

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def test_the_release_builds_with_the_pinned_toolchain() -> None:
    toolchain = tomllib.loads((ROOT / "rust-toolchain.toml").read_text(encoding="utf-8"))["toolchain"]["channel"]
    pins = set(re.findall(r'(?:RUSTUP_TOOLCHAIN:|rust-toolchain:) "?([\d.]+)"?', WORKFLOW))
    assert pins == {toolchain}


def test_every_action_of_the_release_is_pinned_by_commit() -> None:
    actions = re.findall(r"uses: (\S+)", WORKFLOW)
    assert actions
    assert [action for action in actions if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action)] == []


def test_the_tag_is_checked_against_the_workspace_version() -> None:
    version = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))["workspace"]["package"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert '["workspace"]["package"]["version"]' in WORKFLOW
    assert f"## {version}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_readme_example_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    (example,) = re.findall(r"```python\n(.*?)```", readme, flags=re.DOTALL)
    (tmp_path / "input.pdf").write_bytes(build_text_pdf("BT /F1 20 Tf 72 700 Td (Hello) Tj ET"))
    monkeypatch.chdir(tmp_path)

    exec(compile(example, "README.md", "exec"), {})  # noqa: S102 - the README's own example.

    assert (tmp_path / "page-1.png").read_bytes().startswith(b"\x89PNG")
    assert (tmp_path / "output.pdf").read_bytes().startswith(b"%PDF")
