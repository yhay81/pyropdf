"""Exercise the candidate checkout and replay only the files given to readers."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
from test_spec_map import record_evidence

ROOT = Path(__file__).resolve().parents[1]
CLI = Path("tools/spec_map/declaration.py")
TEMPLATE = Path("evidence/releases/rehearsal/capability.toml")
MAP = Path("docs/spec-map.json")
TAG = "v0.0.0-rehearsal"


def command(root: Path, *args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)  # noqa: S603


def commit(root: Path) -> str:
    command(root, "git", "add", ".").check_returncode()
    command(
        root,
        "git",
        "-c",
        "user.email=test@example.org",
        "-c",
        "user.name=Test",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-qm",
        "Test candidate",
    ).check_returncode()
    return command(root, "git", "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, str, Path]:
    root = tmp_path / "candidate"
    for path in [TEMPLATE, MAP, *(p.relative_to(ROOT) for p in (ROOT / CLI.parent).glob("*.py"))]:
        (root / path.parent).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, root / path)
    command(root, "git", "init", "-q").check_returncode()
    revision = commit(root)
    proof = tmp_path / "release-evidence.json"
    proof.write_text(json.dumps({**record_evidence(root), "revision": revision}), encoding="utf-8")
    return root, revision, proof


def build(root: Path, revision: str, *options: str | Path, tag: str = TAG) -> subprocess.CompletedProcess[str]:
    return command(
        root,
        sys.executable,
        CLI,
        "build",
        "--tag",
        tag,
        "--revision",
        revision,
        "--output",
        root.parent / "capabilities.zip",
        *options,
    )


def test_release_bundle_replays_offline_and_is_reproducible(candidate: tuple[Path, str, Path]) -> None:
    root, rev, proof = candidate
    args = ("--rehearsal", "--evidence", proof)
    build(root, rev, *args).check_returncode()
    archive = root.parent / "capabilities.zip"
    first = archive.read_bytes()
    build(root, rev, *args).check_returncode()
    assert archive.read_bytes() == first
    extracted = root.parent / "download"
    with ZipFile(archive) as bundle:
        bundle.extractall(extracted)
    result = command(extracted, sys.executable, CLI, "verify", "--tag", TAG, "--revision", rev)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (extracted / "REPORT.md").read_text()
    assert "The claim holds." in result.stdout
    assert "5 of 5 applicable dimensions" in result.stdout
    assert "hayro-syntax" in result.stdout
    assert "(REPORT.md)" in (extracted / "RELEASE-NOTES.md").read_text()
    assert command(extracted, sys.executable, CLI, "verify", "--tag", TAG, "--revision", "0" * 40).returncode
    original_map = (extracted / MAP).read_bytes()
    (extracted / MAP).write_bytes(b"{}")
    assert (
        "mismatched capability artifact"
        in command(extracted, sys.executable, CLI, "verify", "--tag", TAG, "--revision", rev).stderr
    )
    (extracted / MAP).write_bytes(original_map)
    (extracted / "release-evidence.json").unlink()
    assert command(extracted, sys.executable, CLI, "verify", "--tag", TAG, "--revision", rev).returncode


@pytest.mark.parametrize("fault", ["revision", "digest", "test", "validator", "missing", "selector", "dirty"])
def test_a_positive_release_requires_the_committed_scope_and_passing_evidence(
    candidate: tuple[Path, str, Path], fault: str
) -> None:
    root, rev, proof = candidate
    results = json.loads(proof.read_bytes())
    if fault == "revision":
        results["revision"] = "0" * 40
    elif fault == "digest":
        results["map_sha256"] = "0" * 64
    elif fault == "test":
        row = next(
            item for item in json.loads((root / MAP).read_bytes())["features"] if item["feature"] == "page-labels"
        )
        results["tests"][row["read"]["tests"][0]] = "failed"
    elif fault == "validator":
        results["checks"]["verapdf"] = "failed"
    elif fault in {"dirty", "selector"}:
        template = root / TEMPLATE
        template.write_text(
            template.read_text().replace('features = ["page-labels"]', 'features = ["unknown-feature"]')
        )
        if fault == "selector":
            rev = commit(root)
            results["revision"] = rev
    proof.write_text(json.dumps(results))
    if fault == "missing":
        proof.unlink()
    result = build(root, rev, "--rehearsal", "--evidence", proof)
    assert result.returncode
    assert result.stderr
    assert not (root.parent / "capabilities.zip").exists()


def test_release_without_a_positive_scope_includes_its_own_notes_and_separate_minimum(
    candidate: tuple[Path, str, Path],
) -> None:
    root, rev, _ = candidate
    for tag in ("v0.4.0", "v1.0.0"):
        build(root, rev, tag=tag).check_returncode()
        with ZipFile(root.parent / "capabilities.zip") as bundle:
            report = bundle.read("REPORT.md").decode()
            assert "no positive capability claim" in report
            assert "release-evidence.json" not in bundle.namelist()
            assert f"releases/download/{tag}/capabilities.zip" in bundle.read("RELEASE-NOTES.md").decode()
            if tag == "v1.0.0":
                assert bundle.read("decisions.toml") == b'release = "v1.0.0"\n[exceptions]\n'
                assert "releases/download/v1.0.0/report.txt" in report
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    publish = workflow.split("\n  publish:\n", 1)[1].split("\n  github-release:\n", 1)[0]
    assert "capability-declaration" in publish
    assert publish.index("tools/spec_map/claim.py --release") < publish.index("pypa/gh-action-pypi-publish")


def test_both_documentation_sites_identify_legacy_and_tagged_evidence() -> None:
    for lang in ("en", "ja"):
        for page in ("specification", "status"):
            text = (ROOT / "docs" / lang / f"{page}.md").read_text()
            assert "0.3.1" in text
            assert "capabilities.zip" in text
            assert "(https://github.com/yhay81/pyropdf/releases)" in text
    assert not (ROOT / "evidence/releases/v0.3.1/capability.toml").exists()
