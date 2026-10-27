"""Read the live work queue; never claim, approve, or publish anything.

Run `python3 tools/next_work.py` from any directory. Requires an authenticated
GitHub CLI. `--json` emits the same report for other tools. No network access
is needed by the unit tests of the classification and recommendation rules.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
REPO = "yhay81/pyropdf"
PHASE = re.compile(r"Phase (\d+):")
GROUPS = ("pull_requests", "with_pr", "claimed", "ready", "decisions", "upstream", "blocked", "triage")


@dataclass(frozen=True)
class Item:
    """An open issue or PR and the metadata used to choose work."""

    number: int
    title: str
    url: str
    labels: tuple[str, ...]
    phase: int | None
    blockers: int | None
    dependents: int
    pull_request: bool
    linked_prs: tuple[int, ...] = ()


@dataclass(frozen=True)
class CI:
    """The CI run for exactly the current default-branch commit."""

    branch: str
    commit: str
    state: str
    url: str


def command_output(command: list[str]) -> str:
    """Run a read-only command, reporting failures instead of partial state."""
    result = subprocess.run(  # noqa: S603 - argv only, with fixed read-only subcommands
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout


def gh_json(*args: str) -> Any:  # noqa: ANN401 - JSON is validated where fields are consumed
    """Read JSON from the authenticated GitHub CLI."""
    gh = shutil.which("gh")
    if gh is None:
        msg = "gh is required; install GitHub CLI and run gh auth login"
        raise RuntimeError(msg)
    return json.loads(command_output([gh, *args]))


def issue_pages(pages: list[list[dict[str, Any]]]) -> list[Item]:
    """Flatten all REST pages, preserving unknown dependencies as unknown."""
    items = []
    for page in pages:
        for raw in page:
            if raw["state"] != "open":
                continue
            milestone = raw.get("milestone") or {}
            phase = PHASE.match(milestone.get("title", ""))
            summary = raw.get("issue_dependencies_summary") or {}
            blockers = summary.get("blocked_by")
            if type(blockers) is not int or blockers < 0:
                blockers = None
            dependents = summary.get("blocking", 0)
            if type(dependents) is not int or dependents < 0:
                dependents = 0
            items.append(
                Item(
                    number=raw["number"],
                    title=" ".join(raw["title"].split()),
                    url=raw["html_url"],
                    labels=tuple(label["name"] for label in raw["labels"]),
                    phase=int(phase[1]) if phase else None,
                    blockers=blockers,
                    dependents=dependents,
                    pull_request="pull_request" in raw,
                )
            )
    return items


def current_ci(branch: str, commit: str, runs: list[dict[str, Any]]) -> CI:
    """Do not substitute an older green run for the current commit's CI."""
    run = next((run for run in runs if run.get("headSha") == commit), None)
    if run is None:
        return CI(branch, commit, "missing", f"https://github.com/{REPO}/actions/workflows/ci.yml")
    status = run.get("status")
    if status == "completed":
        state = run.get("conclusion") or "unknown"
    elif status in {"queued", "in_progress", "waiting", "requested", "pending"}:
        state = "pending"
    else:
        state = "unknown"
    return CI(branch, commit, state, run["url"])


def classify(item: Item, earliest_phase: int | None) -> str | None:  # noqa: PLR0911 - ordered work states
    """Separate implementation from claims, blockers, decisions, and upstream.

    Phases order work but do not gate it: an issue in a later phase is ready
    as soon as its blocked-by links are closed.
    """
    if item.pull_request:
        return "pull_requests"
    if "tracker" in item.labels:
        return None
    if item.linked_prs:
        return "with_pr"
    if "in-progress" in item.labels:
        return "claimed"
    if item.blockers is None or item.phase is None or earliest_phase is None or item.phase < earliest_phase:
        return "triage"
    if item.blockers:
        return "blocked"
    if "decision" in item.labels:
        return "decisions"
    if "upstream" in item.labels or "scope-question" in item.labels:
        return "upstream"
    return "ready"


def next_action(groups: dict[str, list[Item]], ci: CI, branch: str) -> str:  # noqa: PLR0911 - ordered next steps
    """Recommend an inspection, never authorization to take over or merge."""
    if ci.state not in {"success", "pending"}:
        return f"Inspect {ci.branch} CI ({ci.state}) before unrelated work: {ci.url}"
    match = re.fullmatch(r"[^/]+/(\d+)-.+", branch)
    current = int(match[1]) if match else None
    for item in groups["claimed"] + groups["with_pr"]:
        if item.number == current:
            return (
                f"Resume inspection of #{item.number}: read the claim/handoff and linked PR checks/reviews. {item.url}"
            )
    if groups["pull_requests"]:
        item = groups["pull_requests"][0]
        return (
            f"Inspect existing PR #{item.number}: checks, review threads, and linked issue before new work. {item.url}"
        )
    if groups["ready"]:
        item = groups["ready"][0]
        return (
            f"Read #{item.number}, comments, DESIGN.md sections, and owned paths; recheck before claiming. {item.url}"
        )
    if groups["triage"]:
        return "Repair missing phase/dependency metadata; unknown is not ready. Start with the triage list."
    if groups["decisions"] or groups["upstream"]:
        return "Read decision/upstream issues and comments; prepare missing evidence or drafts, or resume other work."
    return "Inspect claims and blockers, roadmap #1, and phase exit criteria; no ready issue does not mean done."


def report(items: list[Item], ci: CI, branch: str) -> dict[str, Any]:
    """Build a deterministic report from a single read of the work queue."""
    phases = [
        item.phase
        for item in items
        if "tracker" in item.labels
        and item.phase is not None
        and (match := PHASE.match(item.title))
        and int(match[1]) == item.phase
    ]
    earliest_phase = min(phases) if phases else None
    groups: dict[str, list[Item]] = {group: [] for group in GROUPS}
    for item in items:
        group = classify(item, earliest_phase)
        if group is not None:
            groups[group].append(item)
    for entries in groups.values():
        entries.sort(key=lambda item: item.number)
    groups["ready"].sort(key=lambda item: (item.phase or 0, -item.dependents, item.number))
    return {
        "repository": REPO,
        "observed_at": datetime.now(UTC).isoformat(),
        "local_branch": branch,
        "earliest_phase": earliest_phase,
        "ci": asdict(ci),
        "next": next_action(groups, ci, branch),
        "groups": {group: [asdict(item) for item in entries] for group, entries in groups.items()},
    }


def link_pull_requests(items: list[Item]) -> list[Item]:
    """Exclude an issue already covered by an open PR, even without its claim label."""
    links: dict[int, list[int]] = {}
    for item in items:
        if item.pull_request:
            details = gh_json("pr", "view", str(item.number), "--repo", REPO, "--json", "closingIssuesReferences")
            for issue in details["closingIssuesReferences"]:
                if issue["url"] == f"https://github.com/{REPO}/issues/{issue['number']}":
                    links.setdefault(issue["number"], []).append(item.number)
    return [replace(item, linked_prs=tuple(sorted(links.get(item.number, [])))) for item in items]


def load_report() -> dict[str, Any]:
    """Read every issue page and exact-commit CI; fail if any request fails."""
    repository = gh_json("api", f"repos/{REPO}")
    default_branch = repository["default_branch"]
    head = gh_json("api", f"repos/{REPO}/commits/{quote(default_branch, safe='')}")["sha"]
    pages = gh_json("api", "--paginate", "--slurp", f"repos/{REPO}/issues?state=open&per_page=100")
    runs = gh_json(
        "run",
        "list",
        "--repo",
        REPO,
        "--branch",
        default_branch,
        "--commit",
        head,
        "--workflow",
        "ci.yml",
        "--event",
        "push",
        "--limit",
        "1",
        "--json",
        "headSha,status,conclusion,url",
    )
    git = shutil.which("git")
    if git is None:
        msg = "git is required to inspect the local branch"
        raise RuntimeError(msg)
    branch = command_output([git, "branch", "--show-current"]).strip()
    return report(link_pull_requests(issue_pages(pages)), current_ci(default_branch, head, runs), branch)


def render(data: dict[str, Any]) -> str:
    """Render compact state, reasons, and the commands for the next inspection."""
    ci = data["ci"]
    lines = [
        f"{REPO} | {data['observed_at']}",
        f"Earliest open phase: {data['earliest_phase']} | Local branch: {data['local_branch'] or '(detached)'}",
        f"{ci['branch']} CI: {ci['state']} at {ci['commit'][:12]} | {ci['url']}",
        f"Next: {data['next']}",
        "Explicit user assignments take precedence. This read-only report grants no approval.",
    ]
    for group, entries in data["groups"].items():
        if not entries:
            continue
        lines.append(f"\n{group.replace('_', ' ').title()}:")
        for item in entries:
            counts = f"blockers={item['blockers']}, open dependents={item['dependents']}"
            if item["linked_prs"]:
                counts += ", PRs=" + ",".join(f"#{number}" for number in item["linked_prs"])
            lines.append(f"  #{item['number']} {item['title']} ({counts}) | {item['url']}")
    lines += [
        "\nReady order: earliest phase, most direct open dependents, then lowest issue number.",
        "Phases order work but do not gate it; only blocked-by links do.",
        f"Read issue scope: gh issue view N --repo {REPO}",
        f"Read discussion: gh issue view N --repo {REPO} --comments",
        f"Read blockers: gh api --paginate repos/{REPO}/issues/N/dependencies/blocked_by",
        f"Read PR checks: gh pr checks N --repo {REPO}",
        "Decisions: read existing recommendations first; the owner decides, agents prepare evidence.",
        "Upstream: inspect release/adoption status; local drafts are allowed, publication needs authorization.",
        "Claims: read the branch/handoff comment; do not take over merely because no PR exists.",
        "Recheck immediately before claiming: this report is a snapshot, not a reservation.",
    ]
    return "\n".join(lines)


def main() -> int:
    """Print live state, or return a nonzero status instead of guessing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args()
    try:
        data = load_report()
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, RuntimeError) as error:
        sys.stderr.write(
            f"Cannot determine next work: {error}. No readiness inferred; check gh auth status and retry.\n"
        )
        return 2
    sys.stdout.write((json.dumps(data, indent=2) if args.json else render(data)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
