"""The release workflow and the release texts agree with the repository."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import runpy
import shlex
import shutil
import struct
import subprocess
import sys
import tomllib
from pathlib import Path
from textwrap import dedent
from urllib.parse import unquote_plus
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

import pyropdf

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def ci_job(name: str) -> str:
    """Extract one top-level job from the CI workflow."""
    (job,) = re.findall(rf"\n  {name}:\n(.*?)(?=\n  \S|\Z)", CI, flags=re.DOTALL)
    return job


def test_ci_runs_the_exhaustive_grid_on_the_floor_and_quick_corpus_on_other_interpreters() -> None:
    job = ci_job("python")
    assert 'python: ["3.11", "3.15", "3.15t"]' in job
    steps = job.split("\n      - ")
    (full,) = [step for step in steps if "--junitxml=" in step]
    (quick,) = [step for step in steps if "--quick-corpus" in step]
    assert "if: matrix.python == '3.11'" in full
    assert "--quick-corpus" not in full
    assert "--junitxml=target/ci-evidence/pytest.xml" in full
    assert "junit_suite_name=pyropdf-${GITHUB_SHA}" in full
    assert "if: matrix.python != '3.11'" in quick
    (upload,) = [step for step in steps if "actions/upload-artifact@" in step]
    assert "name: python-evidence-${{ matrix.python }}" in upload
    assert "path: target/ci-evidence/" in upload
    assert "needs: python" in ci_job("evidence")


@pytest.mark.parametrize(
    ("reader", "python", "command"),
    [("verapdf", "3.15", "--arlington target/verapdf/"), ("pdfjs", "3.15t", "tools/oracles.py --readers pdfjs")],
)
def test_ci_records_the_checkout_after_its_reader_finishes(reader: str, python: str, command: str) -> None:
    (step,) = [step for step in ci_job("python").split("\n      - ") if command in step]
    assert f"if: matrix.python == '{python}'" in step
    assert step.index(command) < step.index(f"git rev-parse HEAD > target/ci-evidence/{reader}-revision")


@pytest.mark.parametrize("fault", [None, "missing-verapdf", "missing-pdfjs", "stale-reader", "stale-junit"])
def test_ci_requires_both_readers_and_the_floor_suite_at_the_same_revision(fault: str | None, tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("the Linux workflow runs its commands with Bash")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()  # noqa: S607
    target = tmp_path / "target"
    for reader, version in (("verapdf", "3.15"), ("pdfjs", "3.15t")):
        lane = target / f"python-evidence-{version}"
        lane.mkdir(parents=True)
        if fault != f"missing-{reader}":
            value = "stale" if reader == "pdfjs" and fault == "stale-reader" else revision
            (lane / f"{reader}-revision").write_text(value + "\n", encoding="utf-8", newline="\n")
    floor = target / "python-evidence-3.11"
    floor.mkdir()
    suite = "stale" if fault == "stale-junit" else revision
    (floor / "pytest.xml").write_text(
        f'<testsuite name="pyropdf-{suite}"><testcase classname="tests.test_verify" '
        'name="test_pyhanko_signatures_verify"/></testsuite>',
        encoding="utf-8",
    )

    collector = "tools/spec_map/release_evidence.py"
    (step,) = [s for s in ci_job("evidence").split("\n      - ") if collector in s]
    command = dedent(step.split("        run: |\n")[1])
    # Run the workflow's commands from the temporary download directory with this checkout's real collector.
    command = command.replace(f"python3 {collector}", shlex.join([sys.executable, str(ROOT / collector)]))
    result = subprocess.run(  # noqa: S603 - the repository's Linux workflow.
        [bash, "-e", "-c", command],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_SHA": revision},
        check=False,
    )
    output = target / "release-evidence.json"
    assert (result.returncode == 0) == (fault is None)
    assert output.exists() == (fault is None)
    if fault is None:
        checks = json.loads(output.read_bytes())["checks"]
        assert {checks[reader] for reader in ("verapdf", "arlington", "pdfjs", "pyhanko")} == {"passed"}


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

    # A repair must be able to bring an already oversized release back under the cap.
    capsys.readouterr()
    _size_fixture(base, 9 * mib + 128 * 1024)
    _size_fixture(head, 9 * mib - 64 * 1024)
    monkeypatch.delenv("PR_BODY")
    tool()
    assert json.loads(capsys.readouterr().out)["growth_bytes"] == -192 * 1024

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


def test_abi3_recompression_preserves_installed_members_and_signed_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    repack = runpy.run_path(str(ROOT / "tools/repack_wheel.py"))["repack"]
    path = tmp_path / "pyropdf-1.0-cp311-abi3-win_amd64.whl"
    files = {
        "pyropdf/_core.pyd": bytes(range(256)) * 40,
        "pyropdf/__init__.py": b"# test package",
        "pyropdf-1.0.dist-info/RECORD": b"original,record,entries",
        "pyropdf-1.0.dist-info/RECORD.jws": b"original signature bytes",
        "pyropdf-1.0.dist-info/licenses/NOTICE": b"original notice",
    }
    with ZipFile(path, "w") as archive:
        archive.comment = b"wheel comment"
        for name, content in files.items():
            member = ZipInfo(name, (2024, 1, 2, 3, 4, 6))
            member.external_attr = 0o100755 << 16
            member.comment = b"member comment"
            archive.writestr(member, content, compress_type=ZIP_DEFLATED, compresslevel=0)
    input_bytes = path.read_bytes()
    before, after = repack(path)
    delivered_bytes = path.read_bytes()
    assert after.size_bytes < before.size_bytes
    assert after.sha256 != before.sha256
    with ZipFile(path) as archive:
        assert archive.namelist() == list(files)
        assert archive.comment == b"wheel comment"
        assert archive.testzip() is None
        for member in archive.infolist():
            assert archive.read(member) == files[member.filename]
            assert member.date_time == (2024, 1, 2, 3, 4, 6)
            assert member.external_attr == 0o100755 << 16
            assert member.comment == b"member comment"
            assert member.compress_type == ZIP_DEFLATED
    assert repack(path) == (after, after)
    path.write_bytes(input_bytes)
    assert repack(path) == (before, after)
    assert path.read_bytes() == delivered_bytes


@pytest.mark.parametrize("metadata", ["utf8-comment", "local-time", "zero-dos-attributes", "prefix"])
def test_valid_zip_metadata_keeps_the_original_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: str
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    repack = runpy.run_path(str(ROOT / "tools/repack_wheel.py"))["repack"]
    path = tmp_path / "pyropdf-1.0-cp311-abi3-win_amd64.whl"
    member = ZipInfo("pyropdf/_core.pyd", (2025, 1, 2, 3, 4, 6))
    member.comment = "é".encode()
    modified = 1_735_787_045
    central_time = struct.pack("<HHBI", 0x5455, 5, 1, modified)
    if metadata == "local-time":
        member.extra = central_time
    with ZipFile(path, "w") as archive:
        member.create_system = 0
        archive.writestr(member, bytes(range(256)) * 32, compress_type=ZIP_DEFLATED, compresslevel=0)
        if metadata == "zero-dos-attributes":
            archive.getinfo(member.filename).external_attr = 0
    raw = bytearray(path.read_bytes())
    directory = raw.index(b"PK\x01\x02")
    if metadata == "utf8-comment":
        # The flag applies to the member comment even though its name is ASCII.
        struct.pack_into("<H", raw, 6, 0x800)
        struct.pack_into("<H", raw, directory + 8, 0x800)
    elif metadata == "local-time":
        # The local timestamp includes the access time alongside modification time.
        local_time = struct.pack("<HHBII", 0x5455, 9, 3, modified, modified - 13)
        start = 30 + len(member.filename)
        raw[start : start + len(central_time)] = local_time
        struct.pack_into("<H", raw, 28, len(local_time))
        struct.pack_into("<I", raw, raw.rindex(b"PK\x05\x06") + 16, directory + len(local_time) - len(central_time))
    elif metadata == "prefix":
        raw[:0] = b"self-extracting ZIP prefix"
    path.write_bytes(raw)
    before, delivered = repack(path)
    assert before == delivered
    assert path.read_bytes() == raw


@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512", "sha1"])
def test_recompression_evidence_uses_the_record_hash_algorithm(tmp_path: Path, algorithm: str) -> None:
    count = runpy.run_path(str(ROOT / "evidence/wheel-size/recompression.py"))["record_entries"]
    path = tmp_path / "wheel.zip"
    data = b"original contents"
    digest = (
        base64.urlsafe_b64encode(hashlib.new(algorithm, data, usedforsecurity=False).digest()).rstrip(b"=").decode()
    )
    record = "pyropdf-1.0.dist-info/RECORD"
    with ZipFile(path, "w") as archive:
        archive.writestr("pyropdf/data", data)
        archive.writestr(record, f"pyropdf/data,{algorithm}={digest},{len(data)}\n{record},,\n")
    with ZipFile(path) as archive:
        if algorithm == "sha1":
            with pytest.raises(ValueError, match="unsupported RECORD digest sha1"):
                count(archive)
        else:
            assert count(archive) == 2


def test_recompression_failure_leaves_the_input_wheel_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    repacker = runpy.run_path(str(ROOT / "tools/repack_wheel.py"))
    path = tmp_path / "pyropdf-1.0-cp311-abi3-win_amd64.whl"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("pyropdf/_core.pyd", b"original native code")
    before = path.read_bytes()
    writer = repacker["zopfli"].ZipFile
    write = writer.writestr

    def damage(archive: ZipFile, member: ZipInfo, data: bytes, *, compress_type: int) -> None:
        write(archive, member, data + b"!", compress_type=compress_type)

    monkeypatch.setattr(writer, "writestr", damage)
    with pytest.raises(ValueError, match=r"changed wheel member pyropdf/_core.pyd"):
        repacker["repack"](path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_pr_wheels_match_the_native_release_builds() -> None:
    comparison = (ROOT / ".github/workflows/wheel-size.yml").read_text(encoding="utf-8")
    (native,) = re.findall(r"\n  build-wheels:\n(.*?)(?=\n  \S)", WORKFLOW, flags=re.DOTALL)
    targets = r"runner: ([\w.-]+), target: ([\w-]+)"
    assert len(re.findall(targets, native)) == 5
    assert re.findall(targets, native) == re.findall(targets, comparison)
    for setting in re.findall(r"^ *(?:- )?([\w-]+: .*)$", native, flags=re.MULTILINE):
        if setting.startswith(("uses: PyO3/", "rust-toolchain:", "maturin-version:", "manylinux:")):
            assert comparison.count(setting) == 2
    assert native.index("mkdir -p dist") < native.index("uses: PyO3/maturin-action")
    assert (
        comparison.index("path: base")
        < comparison.index("mkdir -p base/dist")
        < comparison.index("- name: Build base release wheel")
        < comparison.index("path: head")
        < comparison.index("mkdir -p head/dist")
        < comparison.index("- name: Build head release wheel")
    )
    assert "CARGO_TARGET_DIR: ${{ github.workspace }}/paired-target" in comparison
    assert "CARGO_TARGET_DIR: ${{ github.workspace }}/${{ steps.base.outputs.head-target }}" in comparison
    assert "cat tools/native-wheel-args.txt" in native
    assert "args: ${{ steps.native.outputs.args }}" in native
    assert comparison.count("ref: ${{ github.sha }}") == 2
    assert "git -C base rev-parse HEAD^1" in comparison
    assert "args: ${{ steps.base.outputs.args }}" in comparison
    assert "args: ${{ steps.head.outputs.args }}" in comparison
    packaging = "uv run --locked --only-group wheel --python 3.14 python tools/repack_wheel.py dist"
    assert native.count(packaging) == comparison.count(packaging) == 1
    assert "for checkout in base head; do" in comparison[: comparison.index(packaging)].rsplit("      - name:", 1)[1]
    assert '[[ -f "$checkout/tools/repack_wheel.py" ]]' in comparison
    assert '(cd "$checkout" && ' + packaging in comparison
    assert comparison.index(packaging) < comparison.index("run: python head/tools/wheel_size.py")
    assert native.index(packaging) < native.index("tools/wheel_size.py") < native.index("tools/smoke_artifact.py")
    assert (
        native.index("tools/smoke_artifact.py")
        < native.index("actions/attest@")
        < native.index("actions/upload-artifact")
    )


@pytest.mark.parametrize(
    ("change", "target"),
    [
        ("crates/lib/src/lib.rs", "paired-target"),
        ("python/pkg/__init__.py", "paired-target"),
        ("tools/repack_wheel.py", "paired-target"),
        ("Cargo.toml", "head/target"),
        ("Cargo.lock", "head/target"),
        ("crates/lib/Cargo.toml", "head/target"),
        (".cargo/config.toml", "head/target"),
        ("rust-toolchain.toml", "head/target"),
        ("pyproject.toml", "head/target"),
        ("tools/native-wheel-args.txt", "head/target"),
    ],
)
def test_paired_wheels_select_the_base_and_compiler_directory(tmp_path: Path, change: str, target: str) -> None:
    """Execute the workflow decision on a checkout of the tested source, before its first-parent build."""
    bash = shutil.which("bash")
    git = shutil.which("git")
    if not bash or not git:
        pytest.skip("the native wheel workflow runs with Bash and Git")
    base = tmp_path / "base"
    base.mkdir()
    output = tmp_path / "outputs"
    env = os.environ | {"GITHUB_OUTPUT": str(output), "GIT_CONFIG_GLOBAL": os.devnull}

    def checked_git(*args: str) -> str:
        return subprocess.check_output([git, *args], cwd=base, env=env, text=True).strip()  # noqa: S603 - fixed test repository.

    checked_git("init", "-q", "--initial-branch=main")
    checked_git("config", "user.email", "test@example.invalid")
    checked_git("config", "user.name", "Test")
    checked_git("commit", "--allow-empty", "-qm", "common ancestor")
    checked_git("switch", "-qc", "contribution")
    changed = base / change
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("changed\n", encoding="utf-8")
    checked_git("add", "--", change)
    checked_git("commit", "-qm", "contribution")
    checked_git("switch", "-q", "main")
    args = "--release --locked --out dist"
    if change != "tools/native-wheel-args.txt":
        args = "--release --locked --compression-level 9 --out dist"
        flags = base / "tools/native-wheel-args.txt"
        flags.parent.mkdir(exist_ok=True)
        flags.write_text(args + "\n", encoding="utf-8")
        checked_git("add", "--", "tools/native-wheel-args.txt")
    checked_git("commit", "--allow-empty", "-qm", "advanced base")
    revision = checked_git("rev-parse", "HEAD")
    checked_git("merge", "--no-ff", "-qm", "tested merge", "contribution")

    workflow = (ROOT / ".github/workflows/wheel-size.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Prepare the exact base", 1)[1].split("\n      - ", 1)[0]
    script = dedent(step.split("        run: |\n", 1)[1])
    subprocess.run([bash, "-eo", "pipefail", "-c", script], cwd=tmp_path, env=env, check=True)  # noqa: S603 - checked-in workflow.
    assert checked_git("rev-parse", "HEAD") == revision
    assert output.read_text(encoding="utf-8").splitlines() == [
        f"revision={revision}",
        f"head-target={target}",
        f"args={args}",
    ]


@pytest.mark.parametrize("action", ["opened", "reopened", "synchronize"])
def test_wheel_comparison_runs_for_source_events(action: str) -> None:
    changed = runpy.run_path(str(ROOT / "tools/wheel_size.py"))["comparison_inputs_changed"]
    assert changed({"action": action})


@pytest.mark.parametrize(
    ("changes", "current", "expected"),
    [
        ({"title": {"from": "Old"}}, "Wheel growth justification: New font", False),
        ({"base": {"ref": {"from": "feature"}}}, "", True),
        ({"body": {"from": None}}, "New status", False),
        ({"body": {"from": "Mention Wheel growth justification: in prose"}}, None, False),
        ({"body": {"from": "Before"}}, "After\nWheel growth justification: New font\r\n", True),
        ({"body": {"from": "Wheel growth justification: New font"}}, None, True),
        ({"body": {"from": "Wheel growth justification: One"}}, "Wheel growth justification: Two", True),
        (
            {"body": {"from": "Before\nWheel growth justification: One "}},
            "After\nWheel growth justification: One",
            False,
        ),
    ],
)
def test_wheel_comparison_rechecks_only_changed_inputs(changes: dict, current: str | None, *, expected: bool) -> None:
    changed = runpy.run_path(str(ROOT / "tools/wheel_size.py"))["comparison_inputs_changed"]
    assert changed({"action": "edited", "changes": changes, "pull_request": {"body": current}}) is expected


def test_wheel_metadata_edits_are_isolated_before_any_runner_is_scheduled() -> None:
    workflow = (ROOT / ".github/workflows/wheel-size.yml").read_text(encoding="utf-8")
    setup, jobs = workflow.split("\njobs:\n", 1)
    assert "wheel-size-${{ github.event.action == 'edited' && format('edit-{0}', github.run_id)" in setup
    assert "|| github.event.pull_request.number }}" in setup
    assert "cancel-in-progress: true" in setup
    inputs, compare = jobs.split("\n  compare:\n", 1)
    assert "github.event.changes.base != null" in inputs
    assert "github.event.changes.body.from" in inputs
    assert "github.event.pull_request.body" in inputs
    assert "needs: edited-inputs" in compare
    assert "github.event.action != 'edited' || needs.edited-inputs.outputs.compare == 'true'" in compare
    assert "concurrency:" not in compare
