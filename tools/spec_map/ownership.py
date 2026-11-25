"""Connect the map's unfinished evidence to clause-scoped tasks and check their live state."""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

UNFINISHED = frozenset({"planned", "partial", "engine"})
ISSUE_URL = "https://github.com/yhay81/pyropdf/issues/"
TRACKERS = {
    245: "Content model",
    246: "Canvas",
    247: "Reading",
    248: "Rendering",
    249: "Writing",
    250: "Rewriting",
    251: "Structure",
    252: "Persistence",
    253: "Understanding",
    254: "Verification",
    255: "Conformance",
    256: "Specification completion",
}


class OwnerError(Exception):
    """An unfinished cell cannot be traced to a scoped task."""


def assign_family(
    family: str, task: dict[str, Any], names: list[str], dims: list[str], features: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    """Assign only open cells within this family's declared product scope."""
    cells = {}
    for dim in dims:
        members = [name for name in names if features[name][dim]["status"] in UNFINISHED]
        if members:
            cells[dim] = members
        for name in members:
            entry = features[name][dim]
            if "family" in entry:
                msg = f"{name}.{dim} has overlapping owners {entry['family']} and {family}"
                raise OwnerError(msg)
            if entry.get("upstream", task["issue"]) != task["issue"] or (entry["status"] == "engine") != (
                task.get("kind") == "upstream"
            ):
                msg = f"{name}.{dim} needs its declared upstream interpretation task"
                raise OwnerError(msg)
            entry.update(family=family, issue=task["issue"])
    return cells


def assignments(
    path: Path, features: dict[str, dict[str, Any]], dimensions: tuple[str, ...], arlington: list[str]
) -> dict[str, dict[str, Any]]:
    """Attach one family to each open cell and index its exact members in reverse."""
    families = tomllib.loads(path.read_text(encoding="utf-8"))["families"]
    active = {}
    for family, definition in families.items():
        task = dict(definition)
        names = task.pop("features")
        names = arlington if names == ["@arlington"] else names
        dims = task.pop("dimensions")
        if unknown := set(names) - features.keys():
            msg = f"{family} names unknown features: {sorted(unknown)}"
            raise OwnerError(msg)
        if not dims or set(dims) - set(dimensions) or not names:
            msg = f"{family} needs feature and dimension scope"
            raise OwnerError(msg)
        if not all(task.get(field) for field in ("issue", "title", "behavior")):
            msg = f"{family} requires a task number, title and missing behavior"
            raise OwnerError(msg)
        if (
            task.get("tracker") not in TRACKERS
            or type(task["issue"]) is not int
            or task["issue"] <= 0
            or task["issue"] in TRACKERS
        ):
            msg = f"{family} needs a specification tracker and concrete task"
            raise OwnerError(msg)
        cells = assign_family(family, task, names, dims, features)
        if not cells:
            msg = f"{family} has no unfinished cells; remove the obsolete scope from families.toml"
            raise OwnerError(msg)
        active[family] = {**task, "cells": cells}
    for name, row in features.items():
        for dim in dimensions:
            if row[dim]["status"] in UNFINISHED and "family" not in row[dim]:
                msg = f"{name}.{dim} has no scoped work owner"
                raise OwnerError(msg)
    return active


def issue_link(number: int) -> str:
    """Return the link to a local work item."""
    return f"[#{number}]({ISSUE_URL}{number})"


def work_page(spec_map: dict[str, Any]) -> list[str]:
    """Publish the active families and their dimension-specific membership for all trackers."""
    families = spec_map["work"]
    counts = Counter(
        feature[dim]["status"]
        for feature in spec_map["features"]
        for dim in spec_map["dimensions"]
        if feature[dim]["status"] in UNFINISHED
    )
    lines = [
        "## Unfinished work by milestone",
        "",
        f"{counts.total():,} unfinished cells in {len(spec_map['features']):,} features: "
        + ", ".join(f"{counts[kind]:,} {kind}" for kind in sorted(UNFINISHED))
        + ". Each cell names its family and issue in `docs/spec-map.json`. The `work` index lists the exact "
        "feature IDs for each dimension, including every standalone Arlington row.",
        "",
        (
            "Find a feature in the JSON, follow its dimension's `issue`, and check that issue's blocked-by links. "
            "For example, `optional-content.interpret` leads to the upstream visibility task. "
            "Run `python3 tools/spec_map/generate.py --check --live-owners` to verify current tasks and list their "
            "open dependencies. Use `python3 tools/next_work.py` for current claims and ready work."
        ),
    ]
    arlington = {row["feature"] for row in spec_map["features"] if "clause" not in row}
    for tracker, title in TRACKERS.items():
        lines += ["", f"### {tracker} {title}", "", f"Milestone {issue_link(tracker)}.", ""]
        owned = [task for task in families.values() if task["tracker"] == tracker]
        if not owned:
            lines.append("No unfinished specification-map cells assigned.")
            continue
        for task in owned:
            lines += [f"- {issue_link(task['issue'])} {task['title']}. {task['behavior']}"]
            by_members: dict[tuple[str, ...], list[str]] = {}
            for dimension, members in task["cells"].items():
                by_members.setdefault(tuple(members), []).append(dimension)
            for members, dimensions in by_members.items():
                names = (
                    f"all {len(members)} standalone Arlington object rows"
                    if set(members) == arlington
                    else ", ".join(f"`{name}`" for name in members)
                )
                lines.append(f"    - {', '.join(dimensions)} — {names}.")
    return lines


def live_state(numbers: set[int]) -> dict[str, Any]:
    """Fetch task identity and direct dependencies together, refusing partial GitHub reads."""
    gh = shutil.which("gh")
    if not gh:
        msg = "live work ownership unknown: gh is unavailable"
        raise OwnerError(msg)
    fields = (
        "number title state parent { number } labels(first:100){nodes{name} pageInfo{hasNextPage}} "
        "blockedBy(first:100){nodes{number state} pageInfo{hasNextPage}}"
    )
    queries = " ".join(f"i{number}:issue(number:{number}){{{fields}}}" for number in sorted(numbers))
    query = 'query{repository(owner:"yhay81",name:"pyropdf"){' + queries + "}}"
    try:
        result = subprocess.run(  # noqa: S603 - fixed read-only GitHub query with numeric issue IDs
            [gh, "api", "graphql", "-f", f"query={query}"], capture_output=True, text=True, check=True, timeout=60
        )
        result_json = json.loads(result.stdout)
        if result_json.get("errors"):
            msg = f"live work ownership unknown: {result_json['errors']}"
            raise OwnerError(msg)
        return result_json["data"]["repository"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        msg = f"live work ownership unknown: {error}"
        raise OwnerError(msg) from error


def live_report(families: dict[str, dict[str, Any]]) -> list[str]:
    """Refuse a closed or unrelated owner, or a truncated response, for any unresolved cell."""
    if not families:
        return []
    try:
        return check_state(families, live_state({task["issue"] for task in families.values()}))
    except (KeyError, TypeError) as error:
        msg = f"live work ownership unknown: incomplete GitHub response ({error})"
        raise OwnerError(msg) from error


def check_state(families: dict[str, dict[str, Any]], state: dict[str, Any]) -> list[str]:
    """Compare all active families with an independently read issue snapshot."""
    lines = []
    for family, task in families.items():
        number = task["issue"]
        issue = (state or {}).get(f"i{number}")
        if not issue or any(issue[key]["pageInfo"]["hasNextPage"] for key in ("labels", "blockedBy")):
            msg = f"live work ownership unknown for {family} (#{number}): missing or incomplete GitHub response"
            raise OwnerError(msg)
        if issue["state"] != "OPEN":
            msg = f"{family} has unresolved cells but #{number} is {issue['state']}"
            raise OwnerError(msg)
        labels = {label["name"] for label in issue["labels"]["nodes"]}
        parent = (issue["parent"] or {}).get("number")
        if (
            issue["title"] != task["title"]
            or parent != task.get("parent", task["tracker"])
            or "tracker" in labels
            or (task.get("kind") == "upstream") != ("upstream" in labels)
        ):
            msg = f"{family}: #{number} is outside the recorded task scope (title, parent or work kind changed)"
            raise OwnerError(msg)
        blockers = [f"#{dep['number']}" for dep in issue["blockedBy"]["nodes"] if dep["state"] == "OPEN"]
        lines.append(f"{family}: #{number}; open dependencies: {', '.join(blockers) or 'none'}")
    return lines
