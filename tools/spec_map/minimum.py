"""Match the approved 1.0 workflows to observations of the actual candidate."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from release_evidence import digest

MANIFEST = Path("tools/spec_map/minimum.toml")


def mapping(value: object) -> dict:
    """Treat absent or malformed JSON records as missing evidence."""
    return value if isinstance(value, dict) else {}


def strings(record: dict, *keys: str) -> bool:
    """Check that each required field has nonempty text."""
    return all(isinstance(record.get(key), str) and record[key].strip() for key in keys)


def sha256(value: object) -> bool:
    """Recognize a full lowercase SHA-256 digest."""
    return isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{64}", value))


def reference(value: object) -> bool:
    """Require a link to a specific evidence resource."""
    if not isinstance(value, str):
        return False
    url = urlsplit(value)
    return url.scheme == "https" and bool(url.netloc and url.path.strip("/"))


def artifact_errors(runtime: str, artifact: dict, dist: Path) -> list[str]:
    """Match the observed distribution to a wheel the release will publish."""
    if not strings(artifact, "wheel", "interpreter", "target") or not isinstance(artifact.get("extras"), list):
        return ["missing wheel, interpreter, target or extras"]
    if not all(isinstance(extra, str) and extra.strip() for extra in artifact["extras"]):
        return ["invalid wheel extras"]
    name = artifact["wheel"]
    if name != Path(name).name or not name.startswith("pyropdf-") or not name.endswith(".whl"):
        return ["invalid candidate wheel name"]
    platform = name[:-4].rsplit("-", 1)[-1]
    if artifact["target"] not in platform.split(".") or ("wasm32" in platform) != (runtime == "cloudflare-worker"):
        return ["wheel does not match the stated target/runtime"]
    if not (dist / name).is_file() or digest(dist / name) != artifact.get("sha256"):
        return ["wheel digest differs from the candidate distribution"]
    return []


def upstream_errors(root: Path, record: dict, states: dict) -> list[str]:
    """Check adoption against GitHub issue states and the candidate lockfile."""
    if any(mapping(states.get(str(n))) != {"state": "CLOSED", "stateReason": "COMPLETED"} for n in (15, 243)):
        return ["untrusted inputs require closed adoption #15 and #243"]
    packages = tomllib.loads((root / "Cargo.lock").read_text(encoding="utf-8"))["package"]
    locks = [p for p in packages if p["name"] == "hayro"]
    if (
        len(locks) != 1
        or record.get("version") != locks[0]["version"]
        or record.get("source") != locks[0].get("source")
    ):
        return ["upstream adoption must identify the locked hayro version and source"]
    if not reference(record.get("release")) or not reference(record.get("adoption")):
        return ["missing upstream release and adoption references (#15, #243)"]
    return []


def approved_names(root: Path) -> set[str]:
    """Read the approved workflow names from the design table."""
    design = (
        (root / "DESIGN.md")
        .read_text(encoding="utf-8")
        .partition("### Minimum workflows for 1.0\n")[2]
        .split("\n### ", 1)[0]
    )
    return {
        line.split("|")[1].strip()
        for line in design.splitlines()
        if line.startswith("| ") and not line.startswith(("| ---", "| Workflow |"))
    }


def guide_exists(root: Path, guide: str) -> bool:
    """Resolve a guide and its heading against the candidate checkout."""
    path, _, anchor = guide.partition("#")
    if not (root / path).is_file():
        return False
    headings = [
        line.lstrip("# ").strip().lower().replace(" ", "-")
        for line in (root / path).read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ]
    return not anchor or anchor in headings


def _outcome_errors(mode: str, expected: str, spec: dict, result: dict) -> list[str]:
    problems = []
    if result.get("outcome") != expected or not strings(result, "observation") or not reference(result.get("proof")):
        problems.append(f"expected an observed {expected} result with a report URL")
    if not sha256(result.get("input_sha256")):
        problems.append("missing input digest")
    if expected == "delivered":
        if not sha256(result.get("output_sha256")) or "refusal" in result:
            problems.append("delivery requires an output digest")
    else:
        refusal = mapping(result.get("refusal"))
        if (
            "output_sha256" in result
            or not strings(refusal, "type", "code")
            or refusal.get("output_written") is not False
        ):
            problems.append("requires a typed refusal before producing output")
        if mode == "refuse-limit" and (refusal.get("type"), refusal.get("code")) != ("LimitError", "file_size"):
            problems.append("expected the documented file_size LimitError")
    # CI establishes that the tools ran. Per-case verdicts identify what they
    # inspected, including the unchanged input after a refusal.
    subject = result.get("output_sha256" if expected == "delivered" else "input_sha256")
    for tool in spec["checks"]:
        verdict = mapping(mapping(result.get("checks")).get(tool))
        if (
            verdict.get("result") != "passed"
            or verdict.get("sha256") != subject
            or not reference(verdict.get("report"))
        ):
            problems.append(f"missing passing {tool} report for the observed bytes")
    return problems


def _save_errors(mode: str, result: dict) -> list[str]:
    problems = []
    guarantees = []
    if mode in {"full", "incremental"}:
        guarantees = ["untouched-values-and-streams", "settled-edit", "pre-save-signatures-and-profiles"]
        if mode == "incremental":
            guarantees += ["original-prefix", "identical-no-op"]
    if mode.startswith("refuse"):
        guarantees = ["atomic-refusal"]
    for guarantee in guarantees:
        evidence = mapping(mapping(result.get("guarantees")).get(guarantee))
        if evidence.get("result") != "passed" or not reference(evidence.get("report")):
            problems.append(f"missing {guarantee} evidence")
    return problems


def _deployment_errors(runtime: str, result: dict, artifact: dict) -> list[str]:
    problems = []
    if runtime != "native":
        deploy = mapping(result.get("deployment"))
        if (
            not reference(deploy.get("receipt"))
            or deploy.get("artifact_sha256") != artifact.get("sha256")
            or not strings(deploy, "deployment_id")
        ):
            problems.append("missing deployment receipt for this wheel")
    return problems


def _profile_errors(root: Path, profile: dict, result: dict) -> list[str]:
    subject = result.get("output_sha256")
    problems = []
    reviewed = mapping(result.get("human_review"))
    levels = profile["levels"]
    if (
        result.get("input_sha256") != digest(root / profile["fixture"])
        or reviewed.get("levels") != levels
        or reviewed.get("sha256") != subject
        or reviewed.get("result") != "passed"
        or not strings(reviewed, "reviewer", "reader_and_version", "reading_order", "alternative_text")
        or not reference(reviewed.get("report"))
    ):
        problems.append(
            f"missing human reading order and alternative text review for {', '.join(levels)} on the named fixture"
        )
    for level in levels:
        validation = mapping(mapping(result.get("profile_checks")).get(level))
        if (
            validation.get("result") != "passed"
            or validation.get("sha256") != subject
            or not reference(validation.get("report"))
        ):
            problems.append(f"missing passing validation for {level} on the output")

    return problems


@dataclass(frozen=True)
class Candidate:
    """Inputs for offline verification of a specific tagged build."""

    root: Path
    tag: str
    revision: str
    observations: Path
    dist: Path
    state_path: Path


def _workflow_problems(root: Path, spec: dict, ci: dict) -> list[str]:
    problems = []
    if not spec["cases"] or not spec["checks"] or not spec["tests"] or not spec["operation"].strip():
        problems.append("manifest requires cases, operation, named tests and independent checks")
    if not guide_exists(root, spec["guide"]) or not (root / spec["fixture"]).is_file():
        problems.append("guide, heading or fixture missing")
    problems.extend(f"test {n} did not pass" for n in spec["tests"] if ci["tests"].get(n) != "passed")
    problems.extend(f"validator {n} did not pass" for n in spec["checks"] if ci["checks"].get(n) != "passed")
    return problems


def _observed_cases(root: Path, spec: dict, observations: dict, artifacts: dict) -> tuple[list[str], list[str]]:
    problems, report = [], []
    for case, expected in spec["cases"].items():
        observed = mapping(observations.get(case))
        if not observed:
            problems.append(f"{case}: missing {expected} result")
            continue
        runtime, mode, *variant = case.split("/")
        artifact = mapping(artifacts.get(runtime))
        issues = _outcome_errors(mode, expected, spec, observed) + _save_errors(mode, observed)
        if not sha256(artifact.get("sha256")) or observed.get("artifact_sha256") != artifact["sha256"]:
            issues.append("observation does not identify this candidate wheel")
        issues += _deployment_errors(runtime, observed, artifact)
        if variant:
            issues += _profile_errors(root, spec["profiles"][variant[0]], observed)
        problems.extend(f"{case}: {message}" for message in issues)
        if not issues:
            report.append(f"  {case}: {expected}, {observed['proof']}")
    return problems, report


def minimum_gate(candidate: Candidate, ci: dict) -> tuple[list[str], str]:
    """Report each required case independently, including when an entire file is absent."""
    root, tag, revision = candidate.root, candidate.tag, candidate.revision
    observations, dist, state_path = candidate.observations, candidate.dist, candidate.state_path
    manifest = root / MANIFEST
    requirements = tomllib.loads(manifest.read_text(encoding="utf-8"))
    records = mapping(json.loads(observations.read_bytes())) if observations.is_file() else {}
    states = mapping(json.loads(state_path.read_bytes())) if state_path.is_file() else {}
    specs = requirements["workflows"]
    blockers: list[str] = []
    report = [f"Minimum workflows for {tag} at {revision}"]
    names = approved_names(root)
    if not names or {spec["name"] for spec in specs.values()} != names or len(names) != len(specs):
        blockers.append("workflow manifest differs from the approved DESIGN.md table")
    if (
        records.get("tag") != tag
        or records.get("revision") != revision
        or records.get("manifest_sha256") != digest(manifest)
    ):
        blockers.append("missing observations matching the candidate tag, revision and workflow manifest")
    for guarantee, tests in requirements["save-guarantees"].items():
        if not tests or any(ci["tests"].get(test) != "passed" for test in tests):
            blockers.append(f"save guarantee {guarantee}: missing passing candidate tests")
    runtimes = {case.split("/")[0] for spec in specs.values() for case in spec["cases"]}
    artifacts = mapping(records.get("artifacts"))
    for runtime in sorted(runtimes):
        blockers.extend(
            f"{runtime}: {message}" for message in artifact_errors(runtime, mapping(artifacts.get(runtime)), dist)
        )
    for key, spec in specs.items():
        owners = ", ".join(f"#{n}" for n in spec["issues"])
        report.append(f"{spec['name']} ({owners}): {spec['guide']}, input {spec['fixture']}\n  {spec['operation']}")
        problems = _workflow_problems(root, spec, ci)
        if key == "server-worker":
            problems += upstream_errors(root, mapping(records.get("upstream")), states)
        observed = mapping(mapping(records.get("workflows")).get(key))
        issues, lines = _observed_cases(root, spec, observed, artifacts)
        report.extend(lines)
        problems.extend(issues)
        blockers.extend(f"{key} ({owners}): {message}" for message in problems)
    report += ["Blockers:", *(f"- {p}" for p in blockers)] if blockers else ["All required workflows passed."]
    return blockers, "\n".join(report) + "\n"
