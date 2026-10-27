"""The work report reads complete live state and never invents readiness."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "next_work.py"
SPEC = importlib.util.spec_from_file_location("next_work", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
work = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = work
SPEC.loader.exec_module(work)


def issue(  # noqa: PLR0913 - independent issue attributes
    number: int,
    *,
    labels: tuple[str, ...] = (),
    blockers: int | None = 0,
    phase: int | None = 0,
    dependents: int = 0,
    pull: bool = False,
) -> Any:  # noqa: ANN401 - module loaded from a script
    return work.Item(
        number,
        f"Phase {phase}: Example" if "tracker" in labels else f"Work {number}",
        f"https://github.com/yhay81/pyropdf/issues/{number}",
        labels,
        phase,
        blockers,
        dependents,
        pull,
    )


def ci(state: str = "success") -> Any:  # noqa: ANN401 - module loaded from a script
    return work.CI("main", "abc123", state, "https://github.com/yhay81/pyropdf/actions/runs/1")


@pytest.mark.parametrize(
    ("item", "group"),
    [
        (issue(1, labels=("tracker",)), None),
        (issue(2, labels=("in-progress",), blockers=2), "claimed"),
        (issue(3, blockers=1), "blocked"),
        (issue(4, labels=("decision",)), "decisions"),
        (issue(5, labels=("upstream", "adoption")), "upstream"),
        (issue(6, labels=("scope-question",)), "upstream"),
        (issue(7, blockers=None), "triage"),
        (issue(8, phase=None), "triage"),
        (issue(9, phase=1), "ready"),
        (issue(10), "ready"),
        (issue(11, pull=True, labels=("in-progress",)), "pull_requests"),
    ],
)
def test_work_kinds_have_distinct_next_steps(item: Any, group: str | None) -> None:  # noqa: ANN401
    assert work.classify(item, 0) == group


def test_a_later_phase_is_ready_without_waiting_for_an_earlier_one() -> None:
    items = [
        issue(2, labels=("tracker",)),
        issue(26, dependents=2),
        issue(27, dependents=5),
        issue(19, phase=1, dependents=99),
        issue(28, dependents=5),
        issue(30, phase=1, blockers=1),
    ]
    data = work.report(items, ci(), "main")
    assert [item["number"] for item in data["groups"]["ready"]] == [27, 28, 26, 19]
    assert "Read #27" in data["next"]
    assert [item["number"] for item in data["groups"]["blocked"]] == [30]


def test_an_issue_before_the_earliest_open_phase_needs_triage() -> None:
    assert work.classify(issue(31, phase=0), 1) == "triage"


def test_missing_phase_tracker_never_opens_work() -> None:
    data = work.report([issue(27)], ci(), "main")
    assert not data["groups"]["ready"]
    assert "missing" in data["next"]


def test_claim_on_the_current_branch_is_resumed_before_new_work() -> None:
    items = [issue(2, labels=("tracker",)), issue(27), issue(55, labels=("in-progress",))]
    assert "Resume inspection of #55" in work.report(items, ci(), "codex/55-next-work")["next"]
    assert "Read #27" in work.report(items, ci(), "main")["next"]


def test_existing_pr_is_inspected_before_starting_more_work() -> None:
    data = work.report([issue(2, labels=("tracker",)), issue(27), issue(56, pull=True)], ci(), "main")
    assert "Inspect existing PR #56" in data["next"]


@pytest.mark.parametrize("state", ["failure", "missing", "cancelled", "unknown"])
def test_unhealthy_or_unknown_ci_is_the_first_inspection(state: str) -> None:
    data = work.report([issue(2, labels=("tracker",)), issue(27)], ci(state), "main")
    assert "Inspect main CI" in data["next"]


def test_a_previous_green_run_is_not_the_current_commit() -> None:
    old = [{"headSha": "old", "status": "completed", "conclusion": "success", "url": "old-run"}]
    assert work.current_ci("main", "new", old).state == "missing"
    old.append({"headSha": "new", "status": "in_progress", "conclusion": None, "url": "new-run"})
    assert work.current_ci("main", "new", old).state == "pending"


def test_every_page_is_read_and_absent_dependency_data_stays_unknown() -> None:
    def raw(number: int) -> dict[str, Any]:
        return {
            "number": number,
            "title": "Example",
            "html_url": "url",
            "state": "open",
            "labels": [],
            "milestone": {"title": "Phase 0: Foundation"},
            "issue_dependencies_summary": {"blocked_by": 0, "blocking": 1},
        }

    first = [raw(number) for number in range(1, 101)]
    unknown = raw(101)
    unknown.pop("issue_dependencies_summary")
    items = work.issue_pages([first, [unknown]])
    assert len(items) == 101
    assert items[-1].blockers is None
    assert work.classify(items[-1], 0) == "triage"


def test_no_ready_work_is_not_reported_as_completion() -> None:
    data = work.report([issue(2, labels=("tracker",)), issue(27, blockers=2)], ci(), "main")
    assert "does not mean done" in data["next"]


def test_loading_uses_all_pages_and_exact_commit_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    replies = iter([{"default_branch": "main"}, {"sha": "abc123"}, [[]], []])

    def gh(*args: str) -> Any:  # noqa: ANN401 - mocked JSON boundary
        calls.append(args)
        return next(replies)

    monkeypatch.setattr(work, "gh_json", gh)
    monkeypatch.setattr(work.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(work, "command_output", lambda _command: "main\n")
    data = work.load_report()
    assert "--paginate" in calls[2]
    assert "--slurp" in calls[2]
    assert calls[3][calls[3].index("--commit") + 1] == "abc123"
    assert data["ci"]["state"] == "missing"


def test_failed_reads_produce_no_recommendation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise subprocess.CalledProcessError(1, ["gh", "api"])

    monkeypatch.setattr(work, "load_report", fail)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    assert work.main() == 2
    output = capsys.readouterr()
    assert not output.out
    assert "No readiness inferred" in output.err


def test_unknown_ci_status_is_not_pending_or_success() -> None:
    runs = [{"headSha": "new", "url": "run"}]
    assert work.current_ci("main", "new", runs).state == "unknown"


def test_an_engine_tracker_does_not_define_the_earliest_phase() -> None:
    tracker = work.replace(issue(2, labels=("tracker",)), title="Engine work")
    assert work.report([tracker, issue(27)], ci(), "main")["earliest_phase"] is None


def test_a_linked_pr_prevents_duplicate_work_without_a_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        work,
        "gh_json",
        lambda *_args: {
            "closingIssuesReferences": [
                {"number": 27, "url": "https://github.com/yhay81/pyropdf/issues/27"},
                {"number": 26, "url": "https://github.com/another/repo/issues/26"},
            ]
        },
    )
    items = work.link_pull_requests([issue(2, labels=("tracker",)), issue(27), issue(26), issue(56, pull=True)])
    data = work.report(items, ci(), "codex/27-save")
    assert data["groups"]["with_pr"][0]["number"] == 27
    assert data["groups"]["with_pr"][0]["linked_prs"] == (56,)
    assert [item["number"] for item in data["groups"]["ready"]] == [26]
    assert "Resume inspection of #27" in data["next"]
