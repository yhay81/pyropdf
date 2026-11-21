"""Generate the specification map, docs/spec-map.json (DESIGN.md section 8).

The map lists every object of the Arlington PDF Model, vendored under
tools/arlington/model, and the features of tools/spec_map/features.toml, with
a status in five dimensions: read, interpret, create, edit, and preserve. A
status is ``done`` only when a test marked ``pytest.mark.spec(feature,
*dimensions)`` proves it; the marker ``arlington`` stands for every object,
proved by the case of a test parametrized by object name. features.toml gives
every other status with what that status needs: ``planned`` its milestone,
``partial`` what is missing and its milestone, ``engine`` the
upstream issue, ``n/a`` its reason, and ``non-goal`` its decision and reason.
``partial`` and ``engine`` keep the tests of what is done. An ``n/a`` reason
starts with the clause it rests on: a policy exclusion is a ``non-goal``, and
so is creating a feature that features.toml marks ``deprecated``. features.toml's
``[clauses]`` lists the feature-defining clauses of ISO 32000-2, its
extensions, and the profiles it names, each with pyropdf's own title; every
feature names one of them, and each of them has a feature.
``[feature-defaults]`` gives the statuses of the dimensions that a feature
does not declare. families.toml assigns each unfinished dimension to a scoped
work item. ``--live-owners`` verifies their identities, state and direct dependencies.

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

from ownership import OwnerError, assignments, live_report, work_page

ROOT = Path(__file__).resolve().parent.parent.parent
MODEL = Path("tools/arlington/model")
MAP_DIR = Path("tools/spec_map")
FEATURES = MAP_DIR / "features.toml"
TESTS = Path("tests")
MAP = Path("docs/spec-map.json")
COVERAGE = Path("docs/spec-coverage.md")
COMMAND = "python3 tools/spec_map/generate.py"

DIMENSIONS = ("read", "interpret", "create", "edit", "preserve")
ARLINGTON = "arlington"
NEEDS = {
    "planned": ("milestone",),
    "partial": ("missing", "milestone"),
    "engine": ("upstream",),
    "n/a": ("reason",),
    "non-goal": ("decision", "reason"),
}
EVIDENCED = frozenset({"partial", "engine"})
# An n/a reason starts with the clause that makes the dimension meaningless.
CITED = re.compile(r"(?:\d+(?:\.\d+)*|Annex [A-Z](?:\.\d+)*|ISO(?:/TS)? \d+(?:-\d+)?)(?::| )")
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


def profile_cases(node: ast.FunctionDef, where: str) -> dict[str, str]:
    """Use the literal pytest parameter IDs so that one profile cannot prove another."""
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or ast.unparse(decorator.func) != "pytest.mark.parametrize":
            continue
        match decorator.args:
            case [ast.Constant(value="profile"), ast.List(elts=parameters)] if not decorator.keywords:
                if all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in parameters):
                    return {
                        str(arg.value): f"{where}[{arg.value}]" for arg in parameters if isinstance(arg, ast.Constant)
                    }
    msg = f"{where}: the {{profile}} marker requires literal profile parameters with default pytest IDs"
    raise MapError(msg)


def arlington_cases(root: Path, names: list[str], feature: str, where: str) -> dict[str, str]:
    """Expand schema-wide evidence to the exact descriptor cases that were exercised."""
    cases = {name: f"{where}[{name}]" for name in names} if feature == ARLINGTON else {feature: where}
    if feature in {"arlington-values", "arlington-authoring", "arlington-legacy-edits"}:
        fixture_name = "arlington-values.tsv" if feature == "arlington-values" else "arlington-authoring.tsv"
        fixture = root / "tests/fixtures" / fixture_name
        mode = {"arlington-authoring": "create", "arlington-legacy-edits": "existing"}.get(feature)
        with fixture.open(encoding="utf-8", newline="") as samples:
            cases = {
                row["Object"]: f"{where}[{row['Object']}]"
                for row in csv.DictReader(samples, delimiter="\t")
                if mode is None or row["Mode"] == mode
            }
    if feature == "arlington-roles":
        cases = {}
        for name in names:
            with (root / MODEL / "tsv" / f"{name}.tsv").open(encoding="utf-8", newline="") as source:
                if any(row["Link"].strip("[];") for row in csv.DictReader(source, delimiter="\t")):
                    cases[name] = f"{where}[{name}]"
    return cases


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
                cases = arlington_cases(root, names, feature, where)
                if feature == "{profile}":
                    cases = profile_cases(node, where)
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
    if kind == "n/a" and not CITED.match(str(declared.get("reason", ""))):
        msg = f"{where} is n/a with a reason that cites no clause; a policy exclusion is a non-goal"
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
        objects = set(rule.pop("objects", []))
        if bool(keys) == bool(objects) or objects - set(names) or set(rule) - set(DIMENSIONS):
            msg = f"{where} names keys and statuses for {', '.join(DIMENSIONS)}"
            raise MapError(msg)
        applied = {dimension: status(value, f"{where}.{dimension}") for dimension, value in rule.items()}
        for name in names:
            if name in objects or (keys and keys <= object_keys(root, name)):
                features[name].update(applied)
    for name, table in config.get("features", {}).items():
        features[name] = feature(name, dict(table), names)
    for name, row in features.items():
        unknown = set(row.get("includes", [])) - features.keys()
        if unknown:
            msg = f"{name} includes unknown features: {', '.join(sorted(unknown))}"
            raise MapError(msg)
    return features


def feature(name: str, table: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """Return the feature ``name`` of features.toml with its declared statuses."""
    where = f"features.{name}"
    if not FEATURE_NAME.fullmatch(name):
        msg = f"{where} is named in lowercase words joined by hyphens"
        raise MapError(msg)
    included = table.pop("includes", [])
    deprecated = table.pop("deprecated", False)
    about = {field: table.pop(field) for field in ("clause", "description", "objects") if field in table}
    unknown = sorted(set(about.get("objects", [])) - set(names))
    if unknown or set(about) != {"clause", "description", "objects"} or set(table) - set(DIMENSIONS):
        msg = f"{where} takes a clause, a description, objects of the model, and statuses; {unknown or ''}"
        raise MapError(msg)
    if not isinstance(included, list) or not all(isinstance(item, str) for item in included):
        msg = f"{where}.includes must list feature names"
        raise MapError(msg)
    if included:
        about["includes"] = included
    if deprecated is not True and deprecated is not False:
        msg = f"{where}.deprecated is true or false"
        raise MapError(msg)
    if deprecated:
        about["deprecated"] = True
    statuses = {dimension: status(value, f"{where}.{dimension}") for dimension, value in table.items()}
    if deprecated and statuses.get("create", {}).get("status") == "n/a":
        msg = f"{where}.create is n/a, but creating a deprecated feature is a non-goal with its decision"
        raise MapError(msg)
    return {"feature": name, **about, **statuses}


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


def inventory(root: Path, features: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Read the independently maintained edition crosswalk, rejecting dangling references."""
    directory = root / MAP_DIR
    baseline = tomllib.loads((directory / "baseline.toml").read_text(encoding="utf-8"))
    provisions = []
    with (directory / "provisions.tsv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            names = row["features"].split()
            if row["source"] not in baseline["sources"] or not row["summary"]:
                msg = f"missing source or rationale for {row['provision']}"
                raise MapError(msg)
            require_features(names, features, row["provision"])
            provisions.append({**row, "features": names})

    corrections = []
    with (directory / "errata.tsv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            names = row["features"].split()
            dimensions = row["dimensions"].split()
            if set(dimensions) - set(DIMENSIONS) or (dimensions and not names):
                msg = f"invalid feature/dimension mapping for pdf-issues#{row['issue']}"
                raise MapError(msg)
            require_features(names, features, f"pdf-issues#{row['issue']}")
            corrections.append(
                {
                    "issue": int(row["issue"]),
                    "locations": row["locations"].split(),
                    "work": [int(number) for number in row["work"].split()],
                    "features": names,
                    "dimensions": dimensions,
                    "summary": row["summary"],
                }
            )
    return {**baseline, "provisions": provisions, "corrections": corrections}


def require_features(names: list[str], features: dict[str, dict[str, Any]], provision: str) -> None:
    """Fail even when both the corresponding clause declaration and its feature have disappeared."""
    missing = sorted(set(names) - features.keys())
    if missing:
        msg = f"{provision} requires missing features: {', '.join(missing)}"
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
    baseline = inventory(root, features)
    work = assignments(root / MAP_DIR / "families.toml", features, DIMENSIONS, names)
    return {
        "arlington": commit,
        "work": work,
        "inventory": baseline,
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
    baseline = spec_map["inventory"]
    inventory_head = {key: value for key, value in baseline.items() if key not in {"provisions", "corrections"}}
    inventory_text = json.dumps(inventory_head, indent=2, ensure_ascii=False)[:-2]
    for key in ("provisions", "corrections"):
        rows = ",\n    ".join(json.dumps(row, ensure_ascii=False) for row in baseline[key])
        inventory_text += f',\n  "{key}": [\n    {rows}\n  ]'
    inventory_json = (inventory_text + "\n}").replace("\n", "\n  ")
    head = {key: value for key, value in spec_map.items() if key not in {"features", "inventory", "work"}}
    head_text = json.dumps({**head, "coverage": coverage(spec_map)}, indent=2, ensure_ascii=False)
    work_text = ",\n    ".join(
        f"{json.dumps(family)}: {json.dumps(task, ensure_ascii=False)}" for family, task in spec_map["work"].items()
    )
    lines = [json.dumps(feature, ensure_ascii=False) for feature in spec_map["features"]]
    return (
        head_text[:-2]
        + ',\n  "work": {\n    '
        + work_text
        + '\n  },\n  "inventory": '
        + inventory_json
        + ',\n  "features": [\n    '
        + ",\n    ".join(lines)
        + "\n  ]\n}\n"
    )


def inventory_page(baseline: dict[str, Any]) -> list[str]:
    """Describe the source editions, changes, crosswalk and the scope of the correction evidence."""
    map_url = "https://github.com/yhay81/pyropdf/blob/main/docs/spec-map.json"
    lines = ["## Specification baseline", "", f"Reviewed {baseline['reviewed']}.", ""]
    lines.extend(f"- [{src['id']}]({src['url']}), {src['revision']}." for src in baseline["sources"].values())
    lines += [
        "",
        f"Changes since `{baseline['previous']}`. {baseline['changes']}",
        "",
        baseline["grouping"],
        "",
        baseline["watchlist"],
        "",
        baseline["review"],
        "",
        "| Provision | Kind | Feature IDs | Reason for grouping |",
        "| --- | --- | --- | --- |",
    ]
    for row in baseline["provisions"]:
        names = ", ".join(f"`{name}`" for name in row["features"])
        lines.append(f"| {row['provision']} | {row['kind']} | {names or '—'} | {row['summary']} |")
    lines += [
        "",
        "### Correction register",
        "",
        "Compare a checkout of the upstream pages with the committed register using",
        "`python3 tools/spec_map/check_errata.py /path/to/pdf-issues`.",
        "",
        f"The [JSON inventory]({map_url}) lists {len(baseline['corrections'])} corrections.",
        "Each record has its upstream issue number, page anchors, affected feature IDs, dimensions to check,",
        "and an original summary. Empty dimension lists identify changes to wording, cross-references or examples.",
        "Feature tests and release decisions remain on the feature rows. An erratum needs a targeted regression",
        "before a test can be cited for that specific correction. The register gives no automatic coverage credit.",
    ]
    source = baseline["sources"]["errata"]
    lines += [
        "",
        '<details markdown="1"><summary>Corrections by upstream issue number</summary>',
        "",
        "| Issue | Corrected provision | Features | Dimensions to check | Related work |",
        "| --- | --- | --- | --- | --- |",
    ]
    for correction in baseline["corrections"]:
        number = correction["issue"]
        link = str(number) if number in source["page_only"] else f"[{number}]({source['issue_url']}{number})"
        summary = f"[{correction['summary']}]({source['location_url']}{correction['locations'][0]})"
        names = ", ".join(f"`{name}`" for name in correction["features"])
        dimensions = ", ".join(correction["dimensions"]) or "Editorial"
        work = ", ".join(f"[#{n}](https://github.com/yhay81/pyropdf/issues/{n})" for n in correction["work"])
        lines.append(f"| {link} | {summary} | {names or '—'} | {dimensions} | {work or '—'} |")
    lines += ["", "</details>"]
    return lines


def profile_table(spec_map: dict[str, Any]) -> list[str]:
    """Return coverage by profile part and level for both language editions."""
    lines = [
        "",
        "## PDF/A and PDF/UA profiles",
        "",
        "Each row refers to its own fixture and parameterized test in `docs/spec-map.json`.",
        (
            "The machine and human checks are documented in the "
            "[corpus review](https://github.com/yhay81/pyropdf/blob/main/tests/assets/pdfua/README.md)."
        ),
        "",
        "| Profile | " + " | ".join(DIMENSIONS) + " |",
        "| --- |" + " --- |" * len(DIMENSIONS),
    ]
    for feature in spec_map["features"]:
        if feature["feature"].startswith(("pdfa-", "pdfua-")):
            statuses = [feature[dimension]["status"] for dimension in DIMENSIONS]
            lines.append(f"| `{feature['feature']}` | " + " | ".join(statuses) + " |")
    return lines


def page(spec_map: dict[str, Any]) -> str:
    """Return docs/spec-coverage.md: coverage per dimension and per clause, and the exclusions."""
    lines = [
        "# Specification coverage",
        "",
        f"Generated by `{COMMAND}` with `{MAP.as_posix()}`; do not edit.",
        "Each cell counts the features of a clause by status in that dimension.",
        "The map's Arlington objects, one feature each, appear in the totals only.",
        "",
        "## Reading a release claim",
        "",
        "The [1.0 release criteria](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#13-phases) define",
        "the required workflows. The coverage below describes the mapped revision. A release capability claim",
        "identifies its tag, commit, scope and observed results.",
        "",
        "Run `python3 tools/spec_map/claim.py scope.toml --evidence release-evidence.json` with the release's map.",
        "A clause selects its mapped operations, subclauses and linked Arlington objects. An explicit feature selects",
        'only the operations and tests on its own row. For example, `features = ["page-tree"]` covers the named',
        "page operations. A declaration of clause 7.7.3 also needs the mapped page dictionaries.",
        "",
        "Use `profiles` for individual levels such as `PDF/UA-2` and `extensions` for dated publications such as",
        "`ISO/TS 32003:2023`. Each adds its own evidence requirements and requires its base PDF version in",
        "`pdf-versions`. Use `input-profiles` or `input-extensions` to describe input context without claiming",
        "conformance. The report identifies this context as unverified.",
        "",
        "The public heading is generated from the selectors. The scope's `name` is a private identifier.",
        "An optional `display-label` must match the generated heading. The report lists resolved features,",
        "per-cell exclusions and work items for unfinished cells. A named profile or extension requires all",
        "its applicable cells. Multiple profiles are checked individually. A workflow that promises simultaneous",
        "conformance needs evidence for that combination. Use specific feature rows for a narrower promise.",
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
    lines += ["", *work_page(spec_map)]
    lines += profile_table(spec_map)
    lines += ["", *inventory_page(spec_map["inventory"]), "", "## Exclusions", ""]
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
    parser.add_argument("--live-owners", action="store_true", help="check live task scope, state and blockers")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root (default: %(default)s)")
    args = parser.parse_args(argv)
    root: Path = args.root
    try:
        spec_map = build(root)
        if args.live_owners:
            sys.stdout.write("\n".join(live_report(spec_map["work"])) + "\n")
    except (MapError, OwnerError) as error:
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
