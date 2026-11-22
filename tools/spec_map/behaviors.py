"""Evidence for independently observable provisions and substantive corrections."""

from __future__ import annotations

import re
from collections import Counter

from release_evidence import SUITES, VALIDATORS


def validate(rows: list[dict], names: set[str], dimensions: tuple, corrections: list[dict]) -> None:
    """Reject duplicate identities, unknown features and dispositions without evidence or an owner."""
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        msg = "duplicate behavior ID in the crosswalk"
        raise ValueError(msg)
    numbered = {row["issue"]: row for row in corrections}
    for row in rows:
        if not all(isinstance(row.get(k), str) and row[k].strip() for k in ("id", "edition", "provision", "summary")):
            msg = "a behavior requires its ID, edition, provision and original summary"
            raise ValueError(msg)
        if (
            not row["features"]
            or set(row["features"]) - names
            or not row["dimensions"]
            or set(row["dimensions"]) - set(dimensions)
        ):
            msg = f"{row['id']}: invalid feature or dimension"
            raise ValueError(msg)
        if sum(bool(row.get(k)) for k in ("tests", "issue", "rationale")) != 1:
            msg = f"{row['id']}: require targeted tests, a concrete issue or an applicability rationale"
            raise ValueError(msg)
        if any(not re.fullmatch(r"tests/test_[\w/]+\.py::test_[^\s]+", test) for test in row.get("tests", [])):
            msg = f"{row['id']}: use exact pytest node IDs"
            raise ValueError(msg)
        if "issue" in row and (type(row["issue"]) is not int or row["issue"] <= 0):
            msg = f"{row['id']}: use a positive issue number"
            raise ValueError(msg)
        if set(row.get("tools", [])) - set(SUITES) - set(VALIDATORS):
            msg = f"{row['id']}: unknown release evidence tool"
            raise ValueError(msg)
        if "correction" in row and (
            row["correction"] not in numbered or not re.fullmatch(r"[a-f0-9]{64}", row.get("reviewed_sha256", ""))
        ):
            msg = f"{row['id']}: invalid source correction or review digest"
            raise ValueError(msg)


def _unreviewed(corrections: dict, active: dict[str, set[str]], covered: set) -> list[str]:
    blockers = []
    for number, correction in corrections.items():
        names = set(correction["features"])
        # A shared PDF/A or PDF/UA mechanism does not pull in another profile's corrections.
        for family in ("pdfa", "pdfua"):
            if any(n.startswith(family + "-") for n in names):
                names.discard(family)
        if any(
            (number, name) not in covered and active.get(name, set()).intersection(correction["dimensions"])
            for name in names
        ):
            owners = ", ".join(f"#{n}" for n in correction["work"]) or "inventory #258"
            blockers.append(
                f"- pdf-issues#{number}: {correction['summary']}. Missing behavior-specific review ({owners})."
            )
    return blockers


def report(
    inventory: dict, active: dict[str, set[str]], evidence: dict, includes: dict[str, set[str]]
) -> tuple[list[str], list[str]]:
    """Check the actual recorded node and validator outcomes relevant to the included cells."""
    corrections = {c["issue"]: c for c in inventory["corrections"]}
    rows = inventory.get("behaviors", [])
    covered = set()
    reviewed = []
    blockers = []
    for row in rows:
        reviewed_features = {
            name for name in row["features"] if active.get(name, set()).intersection(row["dimensions"])
        }
        if not reviewed_features:
            continue
        number = row.get("correction")
        source = f"{row['edition']} {row['provision']}"
        if number is not None:
            source += f", pdf-issues#{number}"
            covered.update((number, name) for name in reviewed_features)
            covered.update((number, parent) for parent, children in includes.items() if reviewed_features & children)
        label = f"{row['id']} ({source}): {row['summary']}"
        if number is not None and row["reviewed_sha256"] != corrections[number].get("section_sha256"):
            blockers.append(f"- {label} Review the changed source section.")
        elif "issue" in row:
            blockers.append(f"- {label} Missing evidence (#{row['issue']}).")
        elif "rationale" in row:
            reviewed.append(f"- {label} {row['rationale']}")
        else:
            tests, tools = row["tests"], row.get("tools", [])
            missing = [n for n in tests if evidence["tests"].get(n) != "passed"]
            missing.extend(t for t in tools if evidence["checks"].get(t) != "passed")
            if missing:
                blockers.append(f"- {label} Missing passing evidence: {', '.join(missing)}")
            else:
                reviewed.append(f"- {label} Passed: {', '.join([*tests, *tools])}")
    blockers.extend(_unreviewed(corrections, active, covered))
    return blockers, reviewed


def overview(inventory: dict) -> list[str]:
    """Summarize future whole-standard work without equating correction IDs with requirements."""
    rows = inventory.get("behaviors", [])
    classified = {r["correction"] for r in rows if "correction" in r}
    substantive = {r["issue"] for r in inventory["corrections"] if r["dimensions"]}
    counts = Counter(
        "targeted evidence" if "tests" in row else "open work" if "issue" in row else "reasoned disposition"
        for row in rows
    )
    lines = [
        "",
        "### Reviewed behavioral requirements",
        "",
        (
            f"Of {len(substantive)} correction IDs with behavioral dimensions, "
            f"{len(classified & substantive)} have a reviewed crosswalk in at least one scope. "
            f"{len(substantive - classified)} await one. One correction can have several role or revision variants."
        ),
        "",
        ", ".join(f"{count} {name}" for name, count in sorted(counts.items())) + ".",
        "",
        "| Behavior | Edition and provision | Correction | Requirement and evidence |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        number = row.get("correction")
        source = f"[{number}](https://github.com/pdf-association/pdf-issues/issues/{number})" if number else "—"
        proof = ", ".join(f"`{n}`" for n in row.get("tests", [])) or row.get("rationale") or f"#{row.get('issue')}"
        refs = ", ".join(row.get("references", []))
        description = row["summary"] + (" References: " + refs + "." if refs else "")
        lines.append(f"| {row['id']} | {row['edition']} {row['provision']} | {source} | {description} {proof} |")
    return lines
