"""Generate the specification map, docs/spec-map.json (DESIGN.md section 8).

The map lists every object of the Arlington PDF Model, vendored under
tools/arlington/model, and the features of tools/spec_map/features.toml, with
a status in five dimensions: read, interpret, create, edit, and preserve. A
status is ``done`` only when a test marked ``pytest.mark.spec(feature,
*dimensions)`` proves it; the marker ``arlington`` stands for every object,
proved by the case of a test parametrized by object name. features.toml gives
every other status with what that status needs: ``planned`` its milestone,
``partial`` what is missing with its issue and milestone, ``engine`` the
upstream issue, ``n/a`` its reason, and ``non-goal`` its decision and reason.
``partial`` and ``engine`` keep the tests of what is done. features.toml's
``[clauses]`` lists the feature-defining clauses of ISO 32000-2, its
extensions, and the profiles it names, each with pyropdf's own title; every
feature names one of them, and each of them has a feature.
``[feature-defaults]`` gives the statuses of the dimensions that a feature
does not declare.

Run from the repository root: ``python3 tools/spec_map/generate.py`` writes
the map and docs/spec-coverage.md, its coverage per clause and dimension
with every exclusion, and prints its coverage; ``--check`` writes nothing and fails when
the map is stale or claims ``done`` without a named test.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
MODEL = Path("tools/arlington/model")
FEATURES = Path("tools/spec_map/features.toml")
TESTS = Path("tests")
MAP = Path("docs/spec-map.json")
COVERAGE = Path("docs/spec-coverage.md")
COMMAND = "python3 tools/spec_map/generate.py"

DIMENSIONS = ("read", "interpret", "create", "edit", "preserve")
ARLINGTON = "arlington"
NEEDS = {
    "planned": ("milestone",),
    "partial": ("missing", "issue", "milestone"),
    "engine": ("upstream",),
    "n/a": ("reason",),
    "non-goal": ("decision", "reason"),
}
EVIDENCED = frozenset({"partial", "engine"})
FEATURE_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class MapError(Exception):
    """A test marker or features.toml that the generator refuses."""


def objects(root: Path) -> list[str]:
    """Return the object types of the model, sorted."""
    return sorted(path.stem for path in (root / MODEL / "tsv").glob("*.tsv"))


def spec_marker(decorator: ast.expr, where: str) -> tuple[str, ...] | None:
    """Return the arguments of a ``pytest.mark.spec`` decorator, or ``None`` for another decorator."""
    if not isinstance(decorator, ast.Call) or ast.unparse(decorator.func) != "pytest.mark.spec":
        return None
    if decorator.keywords or not all(
        isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in decorator.args
    ):
        msg = f"{where}: pytest.mark.spec takes a feature and dimensions as string literals"
        raise MapError(msg)
    arguments = tuple(str(arg.value) for arg in decorator.args if isinstance(arg, ast.Constant))
    if len(arguments) < 2 or any(dimension not in DIMENSIONS for dimension in arguments[1:]):  # noqa: PLR2004 - a feature and a dimension.
        msg = f"{where}: pytest.mark.spec takes a feature and dimensions among {', '.join(DIMENSIONS)}"
        raise MapError(msg)
    return arguments


def marked(root: Path, names: list[str]) -> dict[tuple[str, str], list[str]]:
    """Return the tests that prove each feature in each dimension, by pytest node ID."""
    proved: dict[tuple[str, str], list[str]] = {}
    for path in sorted((root / TESTS).glob("test_*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            where = f"{relative}::{node.name}"
            for decorator in node.decorator_list:
                marker = spec_marker(decorator, where)
                if marker is None:
                    continue
                feature, *dimensions = marker
                cases = {name: f"{where}[{name}]" for name in names} if feature == ARLINGTON else {feature: where}
                for name, test in cases.items():
                    for dimension in dimensions:
                        proved.setdefault((name, dimension), []).append(test)
    return proved


def status(declared: object, where: str) -> dict[str, Any]:
    """Return a status of features.toml, refusing one that lacks what it needs."""
    if not isinstance(declared, dict) or "status" not in declared:
        msg = f"{where} is a table with a status"
        raise MapError(msg)
    kind = declared["status"]
    if kind == "done":
        msg = f"{where} is done only when a test marked pytest.mark.spec proves it"
        raise MapError(msg)
    if kind not in NEEDS:
        msg = f"{where} has unknown status {kind!r}"
        raise MapError(msg)
    fields = set(declared) - {"status"}
    if fields != set(NEEDS[kind]):
        msg = f"{where} is {kind}, which takes {', '.join(NEEDS[kind])}, not {', '.join(sorted(fields)) or 'nothing'}"
        raise MapError(msg)
    return dict(declared)


def declared(root: Path, config: dict[str, Any], names: list[str]) -> dict[str, dict[str, Any]]:
    """Return every feature of the map with the statuses that features.toml declares for it."""
    features: dict[str, dict[str, Any]] = {}
    arlington = dict(config.get(ARLINGTON, {}))
    rules = arlington.pop("rule", [])
    defaults = {dimension: status(value, f"{ARLINGTON}.{dimension}") for dimension, value in arlington.items()}
    if set(defaults) - set(DIMENSIONS):
        msg = f"{ARLINGTON} has statuses only for {', '.join(DIMENSIONS)}"
        raise MapError(msg)
    for name in names:
        features[name] = {"feature": name, "objects": [name], **defaults}
    for index, rule in enumerate(rules):
        where = f"{ARLINGTON}.rule[{index}]"
        keys = set(rule.pop("keys", []))
        if not keys or set(rule) - set(DIMENSIONS):
            msg = f"{where} names keys and statuses for {', '.join(DIMENSIONS)}"
            raise MapError(msg)
        applied = {dimension: status(value, f"{where}.{dimension}") for dimension, value in rule.items()}
        for name in names:
            if keys <= object_keys(root, name):
                features[name].update(applied)
    for name, table in config.get("features", {}).items():
        where = f"features.{name}"
        if not FEATURE_NAME.fullmatch(name):
            msg = f"{where} is named in lowercase words joined by hyphens"
            raise MapError(msg)
        about = {field: table.pop(field) for field in ("clause", "description", "objects") if field in table}
        unknown = sorted(set(about.get("objects", [])) - set(names))
        if unknown or set(about) != {"clause", "description", "objects"} or set(table) - set(DIMENSIONS):
            msg = f"{where} takes a clause, a description, objects of the model, and statuses; {unknown or ''}"
            raise MapError(msg)
        statuses = {dimension: status(value, f"{where}.{dimension}") for dimension, value in table.items()}
        features[name] = {"feature": name, **about, **statuses}
    return features


def feature_defaults(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the statuses of the dimensions that a feature neither declares nor proves."""
    defaults = {
        dimension: status(value, f"feature-defaults.{dimension}")
        for dimension, value in config.get("feature-defaults", {}).items()
    }
    if set(defaults) - set(DIMENSIONS):
        msg = f"feature-defaults has statuses only for {', '.join(DIMENSIONS)}"
        raise MapError(msg)
    return defaults


def clauses(config: dict[str, Any], features: dict[str, dict[str, Any]]) -> None:
    """Refuse a feature whose clause is not listed, and a listed clause without a feature."""
    listed = config.get("clauses")
    if not isinstance(listed, dict) or not all(isinstance(title, str) for title in listed.values()):
        msg = "clauses lists each feature-defining clause with its title"
        raise MapError(msg)
    named = {feature["clause"] for feature in features.values() if "clause" in feature}
    unlisted = sorted(named - set(listed))
    if unlisted:
        msg = f"features name clauses that clauses does not list: {', '.join(unlisted)}"
        raise MapError(msg)
    uncovered = [clause for clause in listed if clause not in named]
    if uncovered:
        msg = f"clauses without a feature: {', '.join(uncovered)}"
        raise MapError(msg)


def object_keys(root: Path, name: str) -> set[str]:
    """Return the keys of the object type ``name``."""
    with (root / MODEL / "tsv" / f"{name}.tsv").open(encoding="utf-8", newline="") as file:
        return {row["Key"] for row in csv.DictReader(file, delimiter="\t", quoting=csv.QUOTE_NONE)}


def build(root: Path) -> dict[str, Any]:
    """Return the map: every feature with its status in each dimension."""
    names = objects(root)
    config = tomllib.loads((root / FEATURES).read_text(encoding="utf-8"))
    features = declared(root, config, names)
    clauses(config, features)
    defaults = feature_defaults(config)
    proved = marked(root, names)
    unknown = sorted({feature for feature, _ in proved} - set(features))
    if unknown:
        msg = f"tests mark features that the map does not list: {', '.join(unknown)}"
        raise MapError(msg)
    for name, feature in features.items():
        for dimension in DIMENSIONS:
            tests = proved.get((name, dimension), [])
            given = feature.get(dimension)
            if given is None and not tests and name in config.get("features", {}):
                given = defaults.get(dimension)
            if given is None and not tests:
                msg = f"{name} has no {dimension} status: mark a test that proves it, or declare one in {FEATURES}"
                raise MapError(msg)
            if given is not None and tests and given["status"] not in EVIDENCED:
                msg = f"{name} is {given['status']} for {dimension}, but tests prove it: {', '.join(tests)}"
                raise MapError(msg)
            feature[dimension] = {"status": "done"} if given is None else dict(given)
            if tests:
                feature[dimension]["tests"] = tests
        # The dimensions follow the feature's description, in their order.
        for dimension in DIMENSIONS:
            feature[dimension] = feature.pop(dimension)
    commit = (root / MODEL / "COMMIT").read_text(encoding="utf-8").strip()
    return {
        "arlington": commit,
        "dimensions": list(DIMENSIONS),
        "clauses": config["clauses"],
        "features": list(features.values()),
    }


def coverage(spec_map: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Return the count of each status in each dimension."""
    counts = {dimension: Counter[str]() for dimension in DIMENSIONS}
    for feature in spec_map["features"]:
        for dimension in DIMENSIONS:
            counts[dimension][feature[dimension]["status"]] += 1
    return {dimension: dict(sorted(counts[dimension].items())) for dimension in DIMENSIONS}


def render(spec_map: dict[str, Any]) -> str:
    """Return the map as JSON with one feature per line."""
    head = {key: value for key, value in spec_map.items() if key != "features"}
    lines = [json.dumps(feature, ensure_ascii=False) for feature in spec_map["features"]]
    head_text = json.dumps({**head, "coverage": coverage(spec_map)}, indent=2, ensure_ascii=False)
    return head_text[:-2] + ',\n  "features": [\n    ' + ",\n    ".join(lines) + "\n  ]\n}\n"


def page(spec_map: dict[str, Any]) -> str:
    """Return docs/spec-coverage.md: coverage per dimension and per clause, and the exclusions."""
    lines = [
        "# Specification coverage",
        "",
        f"Generated by `{COMMAND}` with `{MAP.as_posix()}`; do not edit.",
        "Each cell counts the features of a clause by status in that dimension.",
        "The map's Arlington objects, one feature each, appear in the totals only.",
        "",
        "## Totals",
        "",
        "| Dimension | " + " | ".join([*sorted(NEEDS), "done"]) + " |",
        "| --- |" + " --- |" * (len(NEEDS) + 1),
    ]
    totals = coverage(spec_map)
    for dimension in DIMENSIONS:
        counts = totals[dimension]
        lines.append(
            f"| {dimension} | " + " | ".join(str(counts.get(name, 0)) for name in [*sorted(NEEDS), "done"]) + " |"
        )
    lines += ["", "## Clauses", "", "| Clause | Title | Features | " + " | ".join(DIMENSIONS) + " |"]
    lines.append("| --- | --- | --- |" + " --- |" * len(DIMENSIONS))
    by_clause: dict[str, list[dict[str, Any]]] = {}
    for feature in spec_map["features"]:
        if "clause" in feature:
            by_clause.setdefault(feature["clause"], []).append(feature)
    for clause, title in spec_map["clauses"].items():
        features = by_clause[clause]
        cells = []
        for dimension in DIMENSIONS:
            counts = Counter(feature[dimension]["status"] for feature in features)
            cells.append(", ".join(f"{count} {name}" for name, count in sorted(counts.items())))
        names = ", ".join(f"`{feature['feature']}`" for feature in features)
        lines.append(f"| {clause} | {title} | {names} | " + " | ".join(cells) + " |")
    lines += ["", "## Exclusions", ""]
    excluded = []
    for feature in spec_map["features"]:
        for dimension in DIMENSIONS:
            given = feature[dimension]
            if given["status"] in {"n/a", "non-goal"}:
                decision = f"{given['decision']}: " if "decision" in given else ""
                excluded.append(f"- `{feature['feature']}` {dimension}: {given['status']}, {decision}{given['reason']}")
    lines += excluded or ["None."]
    return "\n".join(lines) + "\n"


def unproved(text: str) -> list[str]:
    """Return the statuses of a map that are ``done`` without a named test."""
    spec_map = json.loads(text)
    return [
        f"{feature['feature']} is done for {dimension} without a named test"
        for feature in spec_map.get("features", [])
        for dimension in DIMENSIONS
        if feature.get(dimension, {}).get("status") == "done" and not feature[dimension].get("tests")
    ]


def read(path: Path) -> str | None:
    """Return the text of ``path``, or ``None`` when it does not exist."""
    return path.read_text(encoding="utf-8") if path.exists() else None


def main(argv: list[str] | None = None) -> int:
    """Write the map, or with --check report a stale map or a done status without a named test."""
    parser = argparse.ArgumentParser(description="Generate the specification map from the model and test markers.")
    parser.add_argument("--check", action="store_true", help="write nothing; fail when the map is stale")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root (default: %(default)s)")
    args = parser.parse_args(argv)
    root: Path = args.root
    try:
        spec_map = build(root)
    except MapError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    text = render(spec_map)
    current = read(root / MAP)
    coverage_text = page(spec_map)
    coverage_current = read(root / COVERAGE)
    if args.check:
        problems = unproved(current) if current is not None else []
        if current != text:
            problems.append(f"{MAP.as_posix()} is stale; run `{COMMAND}`")
        if coverage_current != coverage_text:
            problems.append(f"{COVERAGE.as_posix()} is stale; run `{COMMAND}`")
        for problem in problems:
            sys.stderr.write(f"{problem}\n")
        return 1 if problems else 0
    if current != text:
        (root / MAP).write_text(text, encoding="utf-8", newline="\n")
        sys.stdout.write(f"wrote {MAP.as_posix()}\n")
    if coverage_current != coverage_text:
        (root / COVERAGE).write_text(coverage_text, encoding="utf-8", newline="\n")
        sys.stdout.write(f"wrote {COVERAGE.as_posix()}\n")
    for dimension, counts in coverage(spec_map).items():
        sys.stdout.write(f"{dimension:<10} {', '.join(f'{count} {name}' for name, count in counts.items())}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
