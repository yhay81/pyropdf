"""Check a completeness claim against the specification map, docs/spec-map.json.

Pass a published scope and the release-evidence.json artifact from the CI run
of the claimed revision. CI records exact pytest node IDs, the two save
harnesses, and independent validator outcomes. The artifact digest binds the
report to this specification map, including its dated inventory.

    python3 tools/spec_map/claim.py SCOPE --evidence release-evidence.json

A TOML scope declares name (a private identifier), revision (full commit SHA),
pdf-versions, errata (the inventory review date), extensions, profiles and tools.
Clauses include linked Arlington objects. Explicit features select only the
mapped operations and their tests. Profiles and dated extensions add evidence
requirements. Optional input-profiles and input-extensions describe unverified
input context. An optional display-label must match the generated claim title.
A per-cell entry such as [exclusions.standard-security-handler]
with edit = "Reason for leaving editing out" removes that cell from the claim.
A scope needs at least one applicable, demonstrated cell.

The 1.0 map gate checks all cells and release evidence. Record exceptions in
evidence/releases/TAG/decisions.toml with release = "TAG" and a table for
each exception, e.g. [exceptions.feature-id.edit], with a decision URL and a
limitation for the release notes. Exceptions never satisfy a completeness claim.

    python3 tools/spec_map/claim.py --release v1.0.0 --revision SHA --evidence release-evidence.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

import behaviors
from release_evidence import SUITES, VALIDATORS, digest, suite_outcomes, test_outcomes

ROOT = Path(__file__).resolve().parent.parent.parent
MAP = Path("docs/spec-map.json")
FIELDS = ("name", "revision", "pdf-versions", "errata", "extensions", "profiles", "tools")
SELECTORS = ("clauses", "features", "profiles", "extensions")
PDF_VERSIONS = {*(f"1.{minor}" for minor in range(8)), "2.0"}
# Base PDF editions for the individual profile parts in the dated inventory.
PROFILE_BASES = {
    "ISO 19005-1:2005": "1.4",
    "ISO 19005-2:2011": "1.7",
    "ISO 19005-3:2012": "1.7",
    "ISO 19005-4:2020": "2.0",
    "ISO 14289-1:2014": "1.7",
    "ISO 14289-2:2024": "2.0",
}


class ScopeError(Exception):
    """A scope file that names something the map does not hold, or lacks a field."""


def validate_metadata(spec_map: dict[str, Any], scope: dict[str, Any]) -> None:
    """Identify the claim and its dated baseline unambiguously."""
    unexpected = set(scope) - {*FIELDS, *SELECTORS, "input-profiles", "input-extensions", "display-label", "exclusions"}
    if unexpected:
        msg = f"unknown scope fields: {', '.join(sorted(unexpected))}"
        raise ScopeError(msg)
    missing = [field for field in FIELDS if field not in scope]
    if missing:
        msg = f"the scope declares {', '.join(missing)}"
        raise ScopeError(msg)
    for field in ("name", "errata"):
        if not isinstance(scope[field], str) or not scope[field].strip():
            msg = f"the scope needs a nonempty {field}"
            raise ScopeError(msg)
    for field in ("pdf-versions", "tools", *SELECTORS, "input-profiles", "input-extensions"):
        entries = scope.get(field, [])
        if not isinstance(entries, list) or any(not isinstance(entry, str) or not entry.strip() for entry in entries):
            msg = f"invalid scope list: {field}"
            raise ScopeError(msg)
    if not scope["pdf-versions"] or scope["errata"] != spec_map["inventory"]["reviewed"]:
        msg = "the scope needs PDF versions and the review date of the selected inventory"
        raise ScopeError(msg)
    unknown = set(scope["pdf-versions"]) - PDF_VERSIONS
    if unknown:
        msg = f"unknown PDF versions: {', '.join(sorted(unknown))}"
        raise ScopeError(msg)


def standards(inventory: dict[str, Any]) -> dict[str, dict[str, tuple[str, set[str]]]]:
    """Index the dated publications by extension ID or individual conformance level."""
    entries: dict[str, dict[str, tuple[str, set[str]]]] = {"profiles": {}, "extensions": {}}
    for row in inventory["provisions"]:
        if row["source"] != "standards":
            continue
        part = row["provision"]
        if row["kind"] in {"extension", "clarification"}:
            entries["extensions"][part] = ("2.0", set(row["features"]))
        elif row["kind"] == "profile part":
            if part not in PROFILE_BASES:
                msg = f"missing base PDF version for inventory profile part: {part}"
                raise ScopeError(msg)
            for feature in row["features"]:
                family, _, level = feature.partition("-")
                if level:
                    label = {"pdfa": "PDF/A", "pdfua": "PDF/UA"}[family]
                    entries["profiles"][f"{label}-{level}"] = (PROFILE_BASES[part], {family, feature})
    return entries


def display_label(scope: dict[str, Any]) -> str:
    """Describe the actual selectors. Free-form scope names are never published as claims."""
    parts = [f"{field} {', '.join(sorted(set(scope[field])))}" for field in SELECTORS if scope.get(field)]
    label = "Mapped " + " / ".join(parts) if parts else "Empty scope"
    return label + (" with listed exclusions" if scope.get("exclusions") else "")


def clause_features(spec_map: dict[str, Any], clauses: list[str]) -> set[str]:
    """Select mapped operations from the clause rows and the base provision crosswalk."""
    base = {row["provision"]: row["features"] for row in spec_map["inventory"]["provisions"] if row["source"] == "base"}
    broad: set[str] = set()
    for clause in clauses:
        if clause not in spec_map["clauses"] and clause not in base:
            msg = f"unknown clause: {clause}"
            raise ScopeError(msg)
        selected = {
            row["feature"]
            for row in spec_map["features"]
            if row.get("clause") == clause or row.get("clause", "").startswith(clause + ".")
        }
        for provision, features in base.items():
            if provision == clause or provision.startswith(clause + "."):
                selected.update(features)
        if not selected:
            msg = f"clause has no mapped operations: {clause}"
            raise ScopeError(msg)
        broad.update(selected)
    return broad


def standard_features(inventory: dict[str, Any], scope: dict[str, Any], selected: set[str]) -> set[str]:
    """Check the declared bases and return the operations required for standards claims."""
    required: set[str] = set()
    for field, catalog in standards(inventory).items():
        claimed = set(scope[field])
        context = set(scope.get("input-" + field, []))
        unknown_ids = (claimed | context) - catalog.keys()
        if unknown_ids:
            msg = f"unknown {field} identifier: {', '.join(sorted(unknown_ids))}"
            raise ScopeError(msg)
        for identifier, (version, rows) in catalog.items():
            # A shared PDF/A or PDF/UA mechanism alone selects no individual level.
            explicit_rows = (rows - {"pdfa", "pdfua"}) & selected
            if (identifier in claimed | context or explicit_rows) and version not in scope["pdf-versions"]:
                msg = f"{identifier} requires PDF {version} in pdf-versions"
                raise ScopeError(msg)
            if identifier in claimed:
                required.update(rows)
    return required


def included_features(names: set[str], by_name: dict[str, dict[str, Any]]) -> set[str]:
    """Expand each aggregate into its operations, including nested aggregates once."""
    selected = names.copy()
    pending = list(names)
    while pending:
        for member in by_name[pending.pop()].get("includes", []):
            if member not in selected:
                selected.add(member)
                pending.append(member)
    return selected


def in_scope(spec_map: dict[str, Any], scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every feature row needed to substantiate the published selectors."""
    validate_metadata(spec_map, scope)
    by_name = {feature["feature"]: feature for feature in spec_map["features"]}
    names = set(scope.get("features", []))
    unknown = sorted(names - by_name.keys())
    if unknown:
        msg = f"the scope names what the map does not list: {', '.join(unknown)}"
        raise ScopeError(msg)
    clauses = clause_features(spec_map, scope.get("clauses", []))
    required = standard_features(spec_map["inventory"], scope, names | clauses)
    standard_objects = {obj for name in required for obj in by_name[name]["objects"]}
    names.update(clauses, required, standard_objects)
    names.update(obj for name in clauses for obj in by_name[name]["objects"])
    names = included_features(names, by_name)
    exclusions = scope.get("exclusions", {})
    validate_exclusions(exclusions, names, spec_map["dimensions"])
    omitted = included_features(required | standard_objects, by_name) & exclusions.keys()
    if omitted:
        msg = f"claimed standard requires every applicable cell of: {', '.join(sorted(omitted))}"
        raise ScopeError(msg)
    label = display_label(scope)
    if scope.get("display-label", label) != label:
        msg = f"display-label must match the resolved scope: {label}"
        raise ScopeError(msg)
    return [by_name[name] for name in sorted(names)]


def validate_exclusions(exclusions: object, names: set[str], dimensions: list[str]) -> None:
    """Require each exclusion to name a selected cell and explain its omission."""
    if not isinstance(exclusions, dict):
        msg = "exclusions must name a feature, dimension and reason"
        raise ScopeError(msg)
    for name, cells in exclusions.items():
        if name not in names or not isinstance(cells, dict) or not cells:
            msg = f"empty or out-of-scope exclusion: {name}"
            raise ScopeError(msg)
        for dimension, reason in cells.items():
            if dimension not in dimensions or not isinstance(reason, str) or not reason.strip():
                msg = f"invalid exclusion dimension or reason: {name} {dimension}"
                raise ScopeError(msg)


def read_evidence(path: Path, spec_map: Path) -> dict[str, Any]:
    """Bind the observations to the complete map, including the baseline."""
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict) or evidence.get("map_sha256") != digest(spec_map):
        msg = "evidence was recorded for a different specification map"
        raise ScopeError(msg)
    return evidence


def evidence_checks(evidence: dict[str, Any], revision: str, tools: list[str]) -> list[str]:
    """Name failed, absent or stale evidence before evaluating individual cells."""
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        msg = "the claim needs a full Git revision"
        raise ScopeError(msg)
    if evidence.get("revision") != revision:
        msg = f"missing or stale evidence for revision {revision}"
        raise ScopeError(msg)
    if not isinstance(evidence.get("tests"), dict) or not isinstance(evidence.get("checks"), dict):
        msg = "the evidence needs recorded test and validator outcomes"
        raise ScopeError(msg)
    required = set(SUITES) | set(VALIDATORS) | {tool.lower() for tool in tools}
    recorded = evidence["checks"]
    suites = suite_outcomes(evidence["tests"])
    return [
        f"- {name}: no passing evidence"
        for name in sorted(required)
        if recorded.get(name) != "passed" or (name in suites and suites[name] != "passed")
    ]


def check(spec_map: dict[str, Any], scope: dict[str, Any], evidence: dict[str, Any]) -> tuple[list[str], str]:
    """Return the blockers of the claim and its report."""
    features = in_scope(spec_map, scope)
    dimensions = spec_map["dimensions"]
    exclusions: dict[str, dict[str, str]] = scope.get("exclusions", {})
    blockers = evidence_checks(evidence, scope["revision"], scope["tools"])
    outcomes = test_outcomes(evidence["tests"])
    excluded, applicable, done = [], 0, 0
    for feature in features:
        name = feature["feature"]
        for dimension in dimensions:
            given = feature[dimension]
            reason = exclusions.get(name, {}).get(dimension)
            if reason is not None:
                if given["status"] == "n/a":
                    msg = f"cannot exclude an inapplicable cell: {name} {dimension}"
                    raise ScopeError(msg)
                excluded.append(f"- {name} {dimension} ({given['status']}): {reason}")
            elif given["status"] == "done" and given.get("tests"):
                applicable += 1
                missing = [test for test in given["tests"] if not outcomes.get(test)]
                if missing:
                    blockers.append(f"- {name} {dimension} lacks passing tests: {', '.join(missing)}")
                else:
                    done += 1
            elif given["status"] == "n/a":
                continue
            else:
                applicable += 1
                undeclared = " that the scope does not declare as an exclusion" if given["status"] == "non-goal" else ""
                owner = f" (#{given['issue']})" if given.get("issue") else ""
                blockers.append(f"- {name} {dimension} is {given['status']}{undeclared}{owner}")
    active = {
        feature["feature"]: {
            dim
            for dim in dimensions
            if feature[dim]["status"] != "n/a" and dim not in exclusions.get(feature["feature"], {})
        }
        for feature in features
    }
    inclusions = {f["feature"]: set(f.get("includes", [])) for f in features if f.get("includes")}
    behavior_blockers, reviewed = behaviors.report(spec_map["inventory"], active, evidence, inclusions)
    blockers.extend(behavior_blockers)
    if not applicable:
        blockers.append("- the claim has no applicable dimensions")
    totals = Counter(feature[dimension]["status"] for feature in spec_map["features"] for dimension in dimensions)
    share = f"{100 * done / applicable:.1f}%" if applicable else "none"
    lines = [
        f"Claim: {display_label(scope)} at {scope['revision']}",
        (
            f"Baseline: {spec_map['inventory']['sources']['base']['id']}, reviewed {scope['errata']}. "
            f"Declared PDF versions: {', '.join(scope['pdf-versions'])}. Tools: {', '.join(scope['tools']) or 'none'}"
        ),
        (
            f"Input context (declared, unverified): profiles {', '.join(scope.get('input-profiles', [])) or 'none'}, "
            f"extensions {', '.join(scope.get('input-extensions', [])) or 'none'}"
        ),
        "Resolved features: " + (", ".join(feature["feature"] for feature in features) or "none"),
        *(
            f"- {feature['feature']} {dim} tests: {', '.join(feature[dim]['tests'])}"
            for feature in features
            if feature["feature"] in scope.get("features", [])
            for dim in dimensions
            if dim not in exclusions.get(feature["feature"], {}) and feature[dim].get("tests")
        ),
        f"Errata revision: {spec_map['inventory']['sources']['errata']['revision']}",
        f"Full inventory: {len(spec_map['features'])} features in {len(dimensions)} dimensions: "
        + ", ".join(f"{count} {status}" for status, count in sorted(totals.items())),
        (
            f"Coverage of the declared scope: {done} of {applicable} applicable dimensions of {len(features)} "
            f"features done ({share}), beside {len(excluded)} excluded"
        ),
        "Behavior-specific evidence and applicability:",
        *(reviewed or ["- none"]),
        "Exclusions:",
        *(excluded or ["- none"]),
        "Blockers:",
        *(blockers or ["- none"]),
        "The claim holds." if not blockers else "The claim does not hold.",
    ]
    return blockers, "\n".join(lines) + "\n"


def release_exceptions(decisions: dict[str, Any], tag: str) -> dict[str, Any]:
    """Read the exception records declared for this specific release."""
    if decisions and decisions.get("release") != tag:
        msg = f"the release decisions do not name {tag}"
        raise ScopeError(msg)
    exceptions = decisions.get("exceptions", {})
    if not isinstance(exceptions, dict) or any(
        not isinstance(cells, dict) or any(not isinstance(value, dict) for value in cells.values())
        for cells in exceptions.values()
    ):
        msg = "exceptions must identify feature, dimension, decision and limitation"
        raise ScopeError(msg)
    return exceptions


def release_gate(
    spec_map: dict[str, Any], tag: str, revision: str, evidence: dict[str, Any], decisions: dict[str, Any]
) -> tuple[list[str], str]:
    """Check the 1.0 map against release-specific, documented limitations."""
    blockers = evidence_checks(evidence, revision, [])
    outcomes = test_outcomes(evidence["tests"])
    exceptions = release_exceptions(decisions, tag)
    limitations = []
    used = set()
    for feature in spec_map["features"]:
        name = feature["feature"]
        for dimension in spec_map["dimensions"]:
            cell = feature[dimension]
            status = cell["status"]
            if status == "partial" and (not cell.get("missing") or not cell.get("issue")):
                blockers.append(f"- {name} {dimension}: partial cell lacks missing behavior or issue")
            if status in {"partial", "non-goal"}:
                record = exceptions.get(name, {}).get(dimension, {})
                decision, note = record.get("decision"), record.get("limitation")
                if (
                    not isinstance(decision, str)
                    or not re.fullmatch(
                        r"https://github\.com/yhay81/pyropdf/(?:issues|pull)/[1-9]\d*(?:#[\w-]+)?", decision
                    )
                    or not isinstance(note, str)
                    or not note.strip()
                ):
                    blockers.append(f"- {name} {dimension}: missing release decision or limitation")
                else:
                    used.add((name, dimension))
                    limitations.append(f"- {name} {dimension} ({status}): {note} ({decision})")
            elif status == "done":
                if not cell.get("tests") or any(not outcomes.get(test) for test in cell["tests"]):
                    blockers.append(f"- {name} {dimension}: missing passing test evidence")
            elif status != "n/a":
                blockers.append(f"- {name} {dimension} is {status}")
    extra = {(name, dim) for name, cells in exceptions.items() for dim in cells} - used
    blockers.extend(f"- unaccepted release exception: {name} {dim}" for name, dim in sorted(extra))
    lines = [
        f"Release map gate for {tag} at {revision}",
        "Limitations:",
        *(limitations or ["- none"]),
        "Blockers:",
        *(blockers or ["- none"]),
    ]
    return blockers, "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Check the claim of a scope file, print its report, and return the exit status."""
    parser = argparse.ArgumentParser(description="Check a completeness claim against the specification map.")
    parser.add_argument("scope", type=Path, nargs="?", help="the scope file of the claim")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root (default: %(default)s)")
    parser.add_argument("--evidence", type=Path, required=True, help="release-evidence.json from the matching CI run")
    parser.add_argument("--release", help="check the entire 1.0 map for this tag")
    parser.add_argument("--revision", help="release checkout SHA, required with --release")
    args = parser.parse_args(argv)
    if bool(args.scope) == bool(args.release) or bool(args.release) != bool(args.revision):
        parser.error("supply either a scope or --release and --revision")
    if args.release and not re.fullmatch(r"v1\.0\.\d+(?:[-+][0-9A-Za-z.-]+)?", args.release):
        parser.error("the 1.0 release gate expects a v1.0.x tag")
    spec_map = json.loads((args.root / MAP).read_text(encoding="utf-8"))
    try:
        evidence = read_evidence(args.evidence, args.root / MAP)
        if args.release:
            path = args.root / "evidence/releases" / args.release / "decisions.toml"
            decisions = tomllib.loads(path.read_text()) if path.is_file() else {}
            blockers, report = release_gate(spec_map, args.release, args.revision, evidence, decisions)
        else:
            blockers, report = check(spec_map, tomllib.loads(args.scope.read_text(encoding="utf-8")), evidence)
    except (ScopeError, OSError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return 2
    sys.stdout.write(report)
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
