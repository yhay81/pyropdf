"""Check a completeness claim against the specification map, docs/spec-map.json.

A release and a completeness claim have separate gates (DESIGN.md section 8).
``generate.py --check`` is the release check: every status is well formed and
every ``done`` names its tests, while ``partial``, ``planned``, and ``engine``
remain allowed. This script is the completeness check of one declared scope,
a TOML file that names the claim, its PDF versions, its dated errata
baseline, its extensions, its profile parts and levels, the tools it
requires, the clauses (and any other features) it covers, and its exclusions
by feature and dimension with their reasons:

    name = "Encryption"
    pdf-versions = ["2.0"]
    errata = "2024-12-01"
    extensions = []
    profiles = []
    tools = []
    clauses = ["7.6.4"]

    [exclusions.standard-security-handler]
    interpret = "Why the claim leaves this dimension out."

The claim holds only when every dimension of every feature in scope that the
scope does not exclude is ``done`` with named tests or ``n/a``: ``partial``,
``planned``, and ``engine`` block it, whatever a release accepted, and a
``non-goal`` blocks it unless the scope declares it as an exclusion. The
report shows the full inventory's counts beside the share of the declared
scope that is done, and the exclusions:

    python3 tools/spec_map/claim.py SCOPE
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
MAP = Path("docs/spec-map.json")
FIELDS = ("name", "pdf-versions", "errata", "extensions", "profiles", "tools", "clauses")


class ScopeError(Exception):
    """A scope file that names something the map does not hold, or lacks a field."""


def in_scope(spec_map: dict[str, Any], scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the features of the map that ``scope`` covers."""
    missing = [field for field in FIELDS if field not in scope]
    if missing:
        msg = f"the scope declares {', '.join(missing)}"
        raise ScopeError(msg)
    unknown = sorted(set(scope["clauses"]) - set(spec_map["clauses"]))
    by_name = {feature["feature"]: feature for feature in spec_map["features"]}
    unknown += sorted(set(scope.get("features", [])) - set(by_name))
    unknown += sorted(set(scope.get("exclusions", {})) - set(by_name))
    if unknown:
        msg = f"the scope names what the map does not list: {', '.join(unknown)}"
        raise ScopeError(msg)
    names = [feature["feature"] for feature in spec_map["features"] if feature.get("clause") in set(scope["clauses"])]
    names += [name for name in scope.get("features", []) if name not in names]
    return [by_name[name] for name in names]


def check(spec_map: dict[str, Any], scope: dict[str, Any]) -> tuple[list[str], str]:
    """Return the blockers of the claim and its report."""
    features = in_scope(spec_map, scope)
    dimensions = spec_map["dimensions"]
    exclusions: dict[str, dict[str, str]] = scope.get("exclusions", {})
    blockers, excluded, applicable, done = [], [], 0, 0
    for feature in features:
        name = feature["feature"]
        for dimension in dimensions:
            given = feature[dimension]
            reason = exclusions.get(name, {}).get(dimension)
            if reason is not None:
                excluded.append(f"- {name} {dimension} ({given['status']}): {reason}")
            elif given["status"] == "done" and given.get("tests"):
                applicable += 1
                done += 1
            elif given["status"] == "n/a":
                continue
            else:
                applicable += 1
                undeclared = " that the scope does not declare as an exclusion" if given["status"] == "non-goal" else ""
                blockers.append(f"- {name} {dimension} is {given['status']}{undeclared}")
    totals = Counter(feature[dimension]["status"] for feature in spec_map["features"] for dimension in dimensions)
    share = f"{100 * done / applicable:.1f}%" if applicable else "none"
    lines = [
        f"Claim: {scope['name']}",
        (
            f"Scope: PDF {', '.join(scope['pdf-versions'])}; errata of {scope['errata']}; "
            f"extensions {', '.join(scope['extensions']) or 'none'}; "
            f"profiles {', '.join(scope['profiles']) or 'none'}; tools {', '.join(scope['tools']) or 'none'}"
        ),
        f"Full inventory: {len(spec_map['features'])} features in {len(dimensions)} dimensions: "
        + ", ".join(f"{count} {status}" for status, count in sorted(totals.items())),
        (
            f"Coverage of the declared scope: {done} of {applicable} applicable dimensions of {len(features)} "
            f"features done ({share}), beside {len(excluded)} excluded"
        ),
        "Exclusions:",
        *(excluded or ["- none"]),
        "Blockers:",
        *(blockers or ["- none"]),
        "The claim holds." if not blockers else "The claim does not hold.",
    ]
    return blockers, "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Check the claim of a scope file, print its report, and return the exit status."""
    parser = argparse.ArgumentParser(description="Check a completeness claim against the specification map.")
    parser.add_argument("scope", type=Path, help="the scope file of the claim")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root (default: %(default)s)")
    args = parser.parse_args(argv)
    spec_map = json.loads((args.root / MAP).read_text(encoding="utf-8"))
    try:
        blockers, report = check(spec_map, tomllib.loads(args.scope.read_text(encoding="utf-8")))
    except ScopeError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    sys.stdout.write(report)
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
