"""Build and replay the capability declaration distributed with a release."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import claim
from release_evidence import digest

ROOT = Path(__file__).resolve().parents[2]
MAP = Path("docs/spec-map.json")
EVIDENCE = Path("release-evidence.json")
DECISIONS = Path("decisions.toml")
CHECKERS = ("declaration.py", "claim.py", "behaviors.py", "release_evidence.py", "minimum.py")
RELEASES = "https://github.com/yhay81/pyropdf/releases"
TAG = re.compile(r"v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9][A-Za-z0-9.-]*)?")


def identity(tag: str, revision: str) -> None:
    """Require a safe release tag and a full Git commit."""
    if not TAG.fullmatch(tag) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        msg = "a release declaration requires a version tag and full Git commit"
        raise ValueError(msg)


def template(root: Path, tag: str, *, rehearsal: bool) -> Path:
    """Look up the declaration on the candidate tree, including a named rehearsal fixture."""
    if not TAG.fullmatch(tag):
        msg = "invalid release tag"
        raise ValueError(msg)
    directory = "rehearsal" if rehearsal else tag
    return root / "evidence" / "releases" / directory / "capability.toml"


def strings(value: object, field: str) -> list[str]:
    """Read a list of release-maintainer descriptions."""
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() or "\n" in item for item in value
    ):
        msg = f"{field} must be a list of nonempty, single-line descriptions"
        raise ValueError(msg)
    return value


def resolve(root: Path, tag: str, revision: str, *, rehearsal: bool) -> dict[str, Any]:
    """Bind the checked-in selectors to the release checkout after resolving its SHA."""
    identity(tag, revision)
    path = template(root, tag, rehearsal=rehearsal)
    metadata = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if set(metadata) - {"processor", "components", "limitations", "scope"}:
        msg = "unknown capability declaration field"
        raise ValueError(msg)
    processor = metadata.get("processor", "pyropdf Python API")
    if not isinstance(processor, str) or not processor.strip() or "\n" in processor:
        msg = "processor must name the tested interface and its role"
        raise ValueError(msg)
    scope = metadata.get("scope")
    if scope is not None:
        if not isinstance(scope, dict) or "revision" in scope:
            msg = "the versioned scope must omit its own revision"
            raise ValueError(msg)
        if not all(key in metadata for key in ("processor", "components", "limitations")):
            msg = "a positive scope needs its processor, components and limitations"
            raise ValueError(msg)
        scope = {**scope, "revision": revision}
    return {
        "format": 1,
        "tag": tag,
        "revision": revision,
        "rehearsal": rehearsal,
        "processor": processor,
        "components": strings(metadata.get("components", []), "components"),
        "limitations": strings(metadata.get("limitations", []), "limitations"),
        "scope": scope,
    }


def report(record: dict[str, Any], root: Path) -> str:
    """Repeat the scoped check with the bundled map, observations and checker."""
    tag, revision = record["tag"], record["revision"]
    identity(tag, revision)
    spec_map = json.loads((root / MAP).read_text(encoding="utf-8"))
    baseline = spec_map["inventory"]
    title = "Rehearsal" if record["rehearsal"] else "Release"
    lines = [
        f"# {title} {tag} capability declaration",
        "",
        f"Git revision `{revision}`.",
        f"Specification inventory: {baseline['sources']['base']['id']}, reviewed {baseline['reviewed']}.",
        f"Map SHA-256: `{digest(root / MAP)}`.",
        "",
        f"Processor role: {record['processor']}.",
        "External engines and optional runtime components: "
        + (", ".join(record["components"]) or "none declared")
        + ".",
        "",
        "## Checked scope",
        "",
    ]
    scope = record["scope"]
    if scope is None:
        lines.append("This release makes no positive capability claim.")
    else:
        if scope.get("revision") != revision:
            msg = "scope and release revisions differ"
            raise ValueError(msg)
        evidence = claim.read_evidence(root / EVIDENCE, root / MAP)
        failures, body = claim.check(spec_map, scope, evidence)
        if failures:
            raise ValueError("\n".join(failures))
        lines.extend(["```text", body.rstrip(), "```"])
    lines.extend(["", "## Release-specific limitations", "", *(f"- {item}" for item in record["limitations"])])
    if not record["limitations"]:
        lines.append("No additional limitations were declared.")
    if tag.startswith("v1.0."):
        lines.extend(
            [
                "",
                "## 1.0 qualification",
                "",
                "Whole-map decisions are in `decisions.toml` in this archive.",
                (
                    f"The [separate ten-workflow and whole-map report]({RELEASES}/download/{tag}/report.txt) "
                    "is attached to the release."
                ),
            ]
        )
        if (root / DECISIONS).is_file():
            claim.release_exceptions(tomllib.loads((root / DECISIONS).read_text(encoding="utf-8")), tag)
        else:
            msg = "the 1.0 declaration needs its documented whole-map decisions"
            raise ValueError(msg)
    return "\n".join(lines) + "\n"


def write_json(path: Path, data: dict) -> None:
    """Write portable deterministic UTF-8 JSON."""
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def verify(directory: Path, tag: str, revision: str) -> str:
    """Verify the distributed files and reproduce the declared public result offline."""
    identity(tag, revision)
    record = json.loads((directory / "declaration.json").read_text(encoding="utf-8"))
    if record.get("format") != 1 or record.get("tag") != tag or record.get("revision") != revision:
        msg = "declaration does not match the requested tag and revision"
        raise ValueError(msg)
    files = record["files"]
    required = {
        str(MAP),
        "REPORT.md",
        "RELEASE-NOTES.md",
        "README.md",
        *(f"tools/spec_map/{name}" for name in CHECKERS),
    }
    if record["scope"] is not None:
        required.add(str(EVIDENCE))
    if tag.startswith("v1.0."):
        required.add(str(DECISIONS))
    if not required <= files.keys():
        msg = "incomplete capability declaration asset"
        raise ValueError(msg)
    for name, expected in files.items():
        path = Path(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not (directory / path).is_file()
            or digest(directory / path) != expected
        ):
            msg = f"missing or mismatched capability artifact: {name}"
            raise ValueError(msg)
    result = report(record, directory)
    if (directory / "REPORT.md").read_text(encoding="utf-8") != result or (directory / "RELEASE-NOTES.md").read_text(
        encoding="utf-8"
    ) != release_notes(record):
        msg = "the published capability result differs from offline replay"
        raise ValueError(msg)
    return result


def release_notes(record: dict) -> str:
    """Link release notes to the same declaration used by both documentation sites."""
    tag = record["tag"]
    label = claim.display_label(record["scope"]) if record["scope"] is not None else "No positive capability claim"
    target = "REPORT.md" if record["rehearsal"] else f"{RELEASES}/download/{tag}/capabilities.zip"
    return (
        f"## Verified capabilities\n\n{label}. "
        "[Download the checked scope, exclusions, limitations and offline verification files]"
        f"({target}).\n"
    )


def stage_decisions(root: Path, stage: Path, tag: str) -> None:
    """Include the exact dated exceptions, or record that the release has none."""
    source = root / "evidence/releases" / tag / DECISIONS
    if source.is_file():
        shutil.copyfile(source, stage / DECISIONS)
    else:
        (stage / DECISIONS).write_text(f'release = "{tag}"\n[exceptions]\n', encoding="utf-8")


def check_checkout(root: Path, tag: str, revision: str, *, rehearsal: bool) -> None:
    """Require each packaged candidate input to match the resolved commit."""
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()  # noqa: S607
    if head != revision:
        msg = "the release revision differs from the candidate checkout"
        raise ValueError(msg)
    inputs = [
        MAP,
        template(root, tag, rehearsal=rehearsal).relative_to(root),
        *(Path("tools/spec_map") / name for name in CHECKERS),
    ]
    if tag.startswith("v1.0."):
        inputs.append(Path("evidence/releases") / tag / DECISIONS)
    dirty = subprocess.check_output(  # noqa: S603 - the paths identify release inputs in the checkout.
        ["git", "diff", "--name-only", "HEAD", "--", *inputs],  # noqa: S607
        cwd=root,
        text=True,
    )
    tracked = subprocess.check_output(  # noqa: S603
        ["git", "ls-files", "--", *inputs],  # noqa: S607
        cwd=root,
        text=True,
    ).splitlines()
    if dirty or any((root / path).exists() and str(path) not in tracked for path in inputs):
        msg = "the declaration inputs must match the committed candidate"
        raise ValueError(msg)


def build(root: Path, tag: str, revision: str, output: Path, evidence: Path | None, *, rehearsal: bool) -> None:  # noqa: PLR0913
    """Publish the completed ZIP to the output path only after its offline replay succeeds."""
    record = resolve(root, tag, revision, rehearsal=rehearsal)
    check_checkout(root, tag, revision, rehearsal=rehearsal)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        temporary_root = Path(temporary)
        stage = temporary_root / "capabilities"
        (stage / MAP.parent).mkdir(parents=True)
        shutil.copyfile(root / MAP, stage / MAP)
        scripts = stage / "tools/spec_map"
        scripts.mkdir(parents=True)
        for script in CHECKERS:
            shutil.copyfile(root / "tools/spec_map" / script, scripts / script)
        if record["scope"] is not None:
            if evidence is None:
                msg = "a positive capability claim needs the candidate CI release-evidence.json"
                raise ValueError(msg)
            shutil.copyfile(evidence, stage / EVIDENCE)
        if tag.startswith("v1.0."):
            stage_decisions(root, stage, tag)
        result = report(record, stage)
        (stage / "REPORT.md").write_text(result, encoding="utf-8")
        (stage / "RELEASE-NOTES.md").write_text(release_notes(record), encoding="utf-8")
        (stage / "README.md").write_text(
            "Use Python 3.11 or newer. Resolve the release tag's commit, extract this archive, "
            "and run from its root:\n\n"
            f"```sh\npython3 tools/spec_map/declaration.py verify --tag {tag} --revision {revision}\n```\n",
            encoding="utf-8",
        )
        record["files"] = {str(p.relative_to(stage)): digest(p) for p in sorted(stage.rglob("*")) if p.is_file()}
        write_json(stage / "declaration.json", record)
        verify(stage, tag, revision)
        archive = temporary_root / "capabilities.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    member = zipfile.ZipInfo(p.relative_to(stage).as_posix())
                    member.create_system = 3
                    member.external_attr = 0o100644 << 16
                    bundle.writestr(member, p.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        archive.replace(output)


def main() -> int:
    """Run the release rehearsal or verify a distributed declaration."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "needs-evidence", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--tag", required=True)
        command.add_argument("--revision", required=True)
        command.add_argument("--root", type=Path, default=ROOT)
        if name != "verify":
            command.add_argument("--rehearsal", action="store_true")
        if name == "build":
            command.add_argument("--evidence", type=Path)
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            sys.stdout.write(verify(args.root, args.tag, args.revision))
        elif args.command == "needs-evidence":
            print(  # noqa: T201 - workflow output
                "true"
                if resolve(args.root, args.tag, args.revision, rehearsal=args.rehearsal)["scope"] is not None
                else "false"
            )
        else:
            build(args.root, args.tag, args.revision, args.output, args.evidence, rehearsal=args.rehearsal)
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
