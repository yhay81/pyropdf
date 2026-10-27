# ruff: noqa: INP001, T201 - a script that prints its report.
"""Survey a pylopdf commit as the freeze commit of DESIGN.md section 14.

Usage: python survey.py PYLOPDF_CHECKOUT CANDIDATE [BASE]

BASE is the commit that the ports started from. The script reads the
checkout with git, after fetching origin, and reads GitHub with an
authenticated gh. It prints:

- the commits from BASE to CANDIDATE, and whether each one's tree is the
  tree of the pull request head that CI tested, with that head's checks;
- the files of the ported modules that changed from BASE to CANDIDATE;
- the open pull requests, the latest fuzz run, and the changelog
  fragments that no release holds yet.
"""

import json
import subprocess
import sys
from collections import Counter
from typing import Any

REPO = "yhay81/pylopdf"
WITH_BASE = 3
BASE = "036be111cafe77755db4bb2be17bc70a8f2f9d82"
# The pylopdf files that pyropdf's ports name (pyropdf #108 and #109).
PORTED = [
    "rust/src/content/syntax.rs",
    "src/pylopdf/_operations.py",
    "src/pylopdf/_geometry.py",
    "tests/test_content.py",
    "tests/test_operations.py",
    "tests/test_geometry.py",
    "tests/test_pages.py",
    "tests/test_rect_quad.py",
]


def run(*args: str, cwd: str | None = None) -> str:
    """Run a command and return its standard output."""
    result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)  # noqa: S603 - argv only
    return result.stdout.strip()


def gh_json(*args: str) -> Any:  # noqa: ANN401 - gh returns any JSON value.
    """Run gh and decode its JSON output."""
    return json.loads(run("gh", *args, "--repo", REPO))


def main() -> None:
    """Print the survey."""
    checkout, candidate = sys.argv[1], sys.argv[2]
    base = sys.argv[3] if len(sys.argv) > WITH_BASE else BASE
    git = ("git", "-C", checkout)
    run(*git, "fetch", "--quiet", "origin")
    print(f"candidate {run(*git, 'log', '-1', '--format=%H %cs %s', candidate)}")
    print(f"base      {run(*git, 'log', '-1', '--format=%H %cs %s', base)}")
    print(f"origin/main {run(*git, 'rev-parse', 'origin/main')}")

    print("\ncommits from base to candidate:")
    for line in run(*git, "log", "--reverse", "--format=%H %s", f"{base}..{candidate}").splitlines():
        sha, subject = line.split(" ", 1)
        number = subject.rsplit("(#", 1)[1].rstrip(")")
        pr = gh_json("pr", "view", number, "--json", "headRefOid,statusCheckRollup")
        run(*git, "fetch", "--quiet", "origin", pr["headRefOid"])
        same = run(*git, "rev-parse", f"{sha}^{{tree}}") == run(*git, "rev-parse", f"{pr['headRefOid']}^{{tree}}")
        checks = Counter(check["conclusion"] for check in pr["statusCheckRollup"])
        print(f"  {sha[:8]} #{number} tree equals tested head: {same} | checks {dict(checks)}")

    print("\nported files changed from base to candidate:")
    print(run(*git, "diff", "--stat", base, candidate, "--", *PORTED) or "  none")

    print("\nopen pull requests:")
    for pr in gh_json("pr", "list", "--state", "open", "--json", "number,title,headRefName,additions,deletions"):
        print(f"  #{pr['number']} {pr['headRefName']} +{pr['additions']}/-{pr['deletions']} {pr['title']}")

    print("\nlatest fuzz run:")
    fields = "headSha,conclusion,createdAt,url"
    for fuzz in gh_json("run", "list", "--workflow", "fuzz.yml", "--limit", "1", "--json", fields):
        print(f"  {fuzz['headSha'][:8]} {fuzz['conclusion']} {fuzz['createdAt']} {fuzz['url']}")

    tag = run(*git, "describe", "--tags", "--abbrev=0", "--match", "v*", candidate)
    fragments = run(*git, "ls-tree", "--name-only", f"{candidate}:changelog.d").splitlines()
    print(f"\nlatest release tag {tag}; {len(fragments)} files in changelog.d at the candidate")


if __name__ == "__main__":
    main()
