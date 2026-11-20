"""The release workflow and the release texts agree with the repository."""

from __future__ import annotations

import json
import re
import runpy
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote_plus
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import pyropdf

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


def test_release_requires_the_installed_pyodide_wheel() -> None:
    (job,) = re.findall(r"\n  build-pyodide-wheel:\n(.*?)(?=\n  \S)", WORKFLOW, flags=re.DOTALL)
    (publish,) = re.findall(r"\n  publish:\n(.*?)(?=\n  \S)", WORKFLOW, flags=re.DOTALL)
    assert (
        job.index("bash tools/build_pyodide.sh")
        < job.index("bash tools/cloudflare/smoke.sh local dist/")
        < job.index("actions/upload-artifact")
    )
    assert "CLOUDFLARE" not in job
    assert "id-token: write" not in job
    (dependencies,) = re.findall(r"needs:.*", publish)
    assert "build-pyodide-wheel" in dependencies
    assert "attest-pyodide-wheel" in dependencies
    assert "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/')" in publish


def test_private_workers_fixture_has_no_public_route() -> None:
    config = json.loads((ROOT / "tools/cloudflare/wrangler.jsonc").read_text(encoding="utf-8"))
    assert config["workers_dev"] is False
    assert config["preview_urls"] is False
    assert not config.get("routes")
    assert not config.get("route")
    assert config["compatibility_flags"] == ["python_workers"]
    assert (ROOT / "tools/cloudflare" / config["main"]).is_file()


def test_installed_artifact_smoke_exercises_the_public_contract() -> None:
    smoke = runpy.run_path(str(ROOT / "tools" / "smoke_artifact.py"))
    smoke["_run_smoke"]()


def test_the_tag_is_checked_against_the_workspace_version() -> None:
    version = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))["workspace"]["package"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert '["workspace"]["package"]["version"]' in WORKFLOW
    assert f"## {version}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_github_release_is_checked_to_be_immutable() -> None:
    (job,) = re.findall(r"\n  github-release:\n(.*?)(?=\n  \S|\Z)", WORKFLOW, flags=re.DOTALL)
    checked = job.index("--jq .immutable")
    assert job.index("gh release create") < checked
    assert 'if [ "${immutable}" != true ]; then' in job[checked:]
    assert "exit 1" in job[checked:]


def test_the_readme_links_to_available_good_first_issues() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)\s]+)\)", readme)
    expected = 'https://github.com/yhay81/pyropdf/issues?q=is:issue is:open label:"good first issue" -label:in-progress'
    assert expected in [unquote_plus(link) for link in links]


def test_the_readme_example_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    (example,) = re.findall(r"```python\n(.*?)```", readme, flags=re.DOTALL)
    monkeypatch.chdir(tmp_path)
    namespace: dict[str, object] = {"__name__": "__main__"}

    exec(compile(example, "README.md", "exec"), namespace)  # noqa: S102 - the README's own example.

    assert capsys.readouterr().out == "Invoice total: $42\nAfter undo: 0\n"
    assert (tmp_path / "reviewed.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    source = namespace["source"]
    assert isinstance(source, bytes)
    assert (tmp_path / "reviewed.pdf").read_bytes().startswith(source)
    with pyropdf.open(tmp_path / "reviewed.pdf") as doc:
        assert len(doc.pages) == 1
        page = doc.pages[0]
        assert page.text().strip() == "Invoice total: $42"
        assert len(page.annotations) == 1
        assert page.annotations[0].type is pyropdf.AnnotationType.HIGHLIGHT


def _size_fixture(directory: Path, size: int, abi: str = "abi3", *, notice: str = "NOTICE") -> None:
    directory.mkdir(exist_ok=True)
    python = "cp311" if abi == "abi3" else abi.removesuffix("t")
    path = directory / f"pyropdf-1.0-{python}-{abi}-win_amd64.whl"
    info = "pyropdf-1.0.dist-info"
    licenses = ["LICENSE", "NOTICE", "tools/arlington/model/LICENSE"]

    def write(padding: int) -> None:
        with ZipFile(path, "w") as archive:
            # The extension alone expands beyond the 9 MiB budget.
            archive.writestr("pyropdf/_core.pyd", bytes(10 * 1024**2), ZIP_DEFLATED)
            archive.writestr(
                f"{info}/METADATA", "License-Expression: MIT\n" + "".join(f"License-File: {n}\n" for n in licenses)
            )
            for name in licenses:
                archive.write(ROOT / (notice if name == "NOTICE" else name), f"{info}/licenses/{name}")
            archive.writestr("padding", bytes(padding))

    write(0)
    write(size - path.stat().st_size)
    assert path.stat().st_size == size


def test_compressed_wheel_limits_and_pr_justification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = runpy.run_path(str(ROOT / "tools" / "wheel_size.py"))["main"]
    mib = 1024**2
    base, head = tmp_path / "base", tmp_path / "head"
    monkeypatch.setattr(sys, "argv", ["wheel_size", str(head), "--abi", "abi3", "--base", str(base)])
    monkeypatch.delenv("PR_BODY", raising=False)
    _size_fixture(head, 9 * mib)
    _size_fixture(base, 9 * mib - mib // 4)
    tool()
    report = json.loads(capsys.readouterr().out)
    assert report["head"]["size_bytes"] == 9_437_184
    assert report["head"]["size_mib"] == 9.0
    assert report["head"]["extension_bytes"] < mib
    assert report["growth_bytes"] == 262_144

    _size_fixture(base, 9 * mib - mib // 4 - 1)
    with pytest.raises(ValueError, match="Wheel growth justification"):
        tool()
    capsys.readouterr()
    reason = "Retain the required third-party notice for the new embedded font."
    monkeypatch.setenv("PR_BODY", f"Closes #123\r\n\r\nWheel growth justification: {reason}\r\n")
    tool()
    report = json.loads(capsys.readouterr().out)
    assert (report["growth_bytes"], report["justification"]) == (262_145, reason)
    _size_fixture(head, 9 * mib + 1)
    with pytest.raises(ValueError, match="9 MiB"):
        tool()

    (other_platform,) = base.glob("*.whl")
    other_platform.rename(other_platform.with_name(other_platform.name.replace("win_amd64", "macosx_11_0_arm64")))
    with pytest.raises(ValueError, match="incomparable wheel tags"):
        tool()


def test_larger_free_threaded_wheel_reports_size_and_checks_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = runpy.run_path(str(ROOT / "tools" / "wheel_size.py"))["main"]
    monkeypatch.setattr(sys, "argv", ["wheel_size", str(tmp_path), "--abi", "cp314t"])
    _size_fixture(tmp_path, 10 * 1024**2, "cp314t")
    tool()
    _size_fixture(tmp_path, 10 * 1024**2, "cp314t", notice="LICENSE")
    with pytest.raises(ValueError, match="incorrect contents of NOTICE"):
        tool()


def test_pr_wheels_match_the_native_release_builds() -> None:
    comparison = (ROOT / ".github/workflows/wheel-size.yml").read_text(encoding="utf-8")
    (native,) = re.findall(r"\n  build-wheels:\n(.*?)(?=\n  \S)", WORKFLOW, flags=re.DOTALL)
    targets = r"runner: ([\w.-]+), target: ([\w-]+)"
    assert len(re.findall(targets, native)) == 5
    assert re.findall(targets, native) == re.findall(targets, comparison)
    for setting in re.findall(r"^ *(?:- )?([\w-]+: .*)$", native, flags=re.MULTILINE):
        if setting.startswith(("uses: PyO3/", "rust-toolchain:", "maturin-version:", "manylinux:")):
            assert comparison.count(setting) == 2
    assert "cat tools/native-wheel-args.txt" in native
    assert "args: ${{ steps.native.outputs.args }}" in native
    assert "ref: ${{ github.sha }}" in comparison
    assert "git -C head rev-parse HEAD^1" in comparison
    assert "args: ${{ steps.native.outputs.base }}" in comparison
    assert "args: ${{ steps.native.outputs.head }}" in comparison
    assert native.index("tools/wheel_size.py") < native.index("actions/upload-artifact")
