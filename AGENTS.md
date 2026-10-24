# AGENTS.md

This file is the canonical context for coding agents. `CLAUDE.md` only
imports it; update this file instead.

pyropdf rebuilds [pylopdf](https://github.com/yhay81/pylopdf)'s concept on one
versioned object store that every engine reads. [DESIGN.md](DESIGN.md) is the
design: its principles F1 to F9 (section 3) bind every change. This file says
how to work. The current phase is Phase 0 (Foundation); nothing is
implemented yet.

## Start here

1. Read DESIGN.md sections 3 and 4, and every section your issue names.
2. Pick a ready issue (below) and claim it.
3. Work on a branch of its own and open a pull request that closes it.

## Choosing work

All work comes from issues. The pinned issue #1 is the root of the tree:
phase trackers and engine trackers are its sub-issues, and tasks are
sub-issues of a tracker. The milestone of an issue is its phase.

An issue is ready when it is open, not a `tracker`, not `in-progress`, and
has no open blocking issue. List the ready issues of the repository:

```bash
gh api 'repos/yhay81/pyropdf/issues?state=open&per_page=100' --jq '.[] | select(.pull_request == null and .issue_dependencies_summary.blocked_by == 0) | select([.labels[].name] | (index("tracker") or index("in-progress")) | not) | "#\(.number) [\(.milestone.title // "-")] \(.title)"'
```

To see what still blocks one issue:

```bash
gh api repos/yhay81/pyropdf/issues/<number>/dependencies/blocked_by --jq '.[] | select(.state == "open") | "#\(.number) \(.title)"'
```

- **Claim before starting.** Every agent works through the owner's GitHub
  account, so assignment cannot tell agents apart. Add the `in-progress` label
  and comment the branch name. Remove both if you stop. A claim without a
  pushed commit for three days may be taken over after a comment.
- **`decision` issues are the owner's call.** Prepare the recommendation that
  the issue asks for, with measurements under `evidence/`, but never build on
  a choice the owner has not made.
- **`upstream` issues** change other repositories. Drafting a patch or a
  proposal locally, or in the issue, is fine. Opening an issue or pull request
  in another repository, or commenting there, publishes on the owner's behalf:
  ask the owner first.

## Working rules

- One issue, one branch, one pull request. Name the branch
  `<agent>/<issue>-<slug>`, for example `claude/24-xref-index`.
- Stay inside the paths that the issue lists. A change elsewhere is its own
  issue, unless the issue says otherwise.
- The pull request body says `Closes #<issue>` and checks the issue's
  acceptance criteria. Squash-merge, so one issue becomes one commit on
  `main`.
- Merge only with CI green. An agent merges its own pull request only when
  the owner has said so for that work; otherwise it leaves the pull request
  for the owner. Never enable auto-merge.
- Write code, comments, documentation, commit messages, and issues in
  English. Other languages appear only in test data.
- Do not depend on lopdf. hayro-syntax parses every object (F5).
- No recursion over input-derived structure without a bound, and no
  collection that grows from input outside the operation's budget (F6). In
  the store, `crates/pyropdf-store/clippy.toml` checks the collections.
- A pull request that claims a performance effect commits the measurement
  and its script under `evidence/`.
- When DESIGN.md is wrong or unclear for your issue, do not deviate silently:
  propose the change in the issue. A DESIGN.md change is its own pull
  request.
- Follow-up work becomes an issue, never a `TODO` comment: a sub-issue of the
  phase tracker with its milestone, and `--blocked-by` for what it waits on.

```bash
gh issue create --repo yhay81/pyropdf --parent <tracker> --milestone "Phase 0: Foundation" \
  --label area:store --blocked-by <issue> --title "..." --body-file body.md
```

## Writing an issue

An issue body has these sections, so that an agent can start without asking:

- **Context**: why the work matters, with the DESIGN.md sections it
  implements.
- **Goal**: the result in one paragraph.
- **Scope**: what is in and what is out.
- **Paths**: the files and crates that the issue owns.
- **Acceptance**: a checklist of tests and measurements that decide done.
- **Notes**: pitfalls, and the pylopdf code to read or port.

Dependencies are GitHub "blocked by" links, the parent is a sub-issue link,
and the phase is the milestone.

## Porting from pylopdf

pylopdf is the reference implementation and the differential oracle. Port a
module together with its Python tests, and name the pylopdf commit it came
from in the pull request. Do not port what DESIGN.md section 14 lists as not
ported: the guards, the reconciliation between lopdf and hayro, the
replacements for lopdf's helpers, and the rules of pylopdf's AGENTS.md.
pylopdf's checkout may be in use by other agents: read it, and never commit
to it from pyropdf work.

## Commands

`uv sync` builds the extension with maturin and installs the tools, and
`uv run` rebuilds the extension when a Rust source or manifest changes. Run
these before pushing. CI runs the same commands, with `--locked` added to
`uv sync` and to the cargo commands.

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
```

`uv run pytest` includes `tests/test_crate_dependencies.py`, which fails
when a crate's dependencies point against DESIGN.md section 11.

The audit job of CI checks `Cargo.lock` against the RustSec advisory
database and the workflows with actionlint. An unmaintained, unsound, or
yanked crate fails the audit as a vulnerability does.
`tools/install_audit_tools.sh` installs the versions that the job pins, on
Linux or macOS. Run these too when a change touches `Cargo.lock` or
`.github/workflows/`:

```bash
bash tools/install_audit_tools.sh target/audit-tools
target/audit-tools/cargo-audit audit --deny warnings
target/audit-tools/actionlint -shellcheck= -pyflakes=
```

## Tests and evidence

- Python tests in `tests/` are the public contract.
- Rust tests are for invariants of the store and the engines that no public
  API exposes: property tests, differential oracles, and fuzz targets (F9).
- Every pull request adds or updates tests for its acceptance criteria.

## Labels

| Label | Meaning |
| --- | --- |
| `tracker` | Tracks a phase or an engine; closes when its boxes and sub-issues are done |
| `decision` | The owner decides; agents may prepare a recommendation |
| `spike` | A time-boxed investigation that ends in a measured recommendation |
| `upstream` | Work that lands in an engine or dependency repository |
| `adoption` | pyropdf adopts a released engine change |
| `scope-question` | A question for an upstream maintainer about what belongs in their project |
| `in-progress` | Claimed; the latest comment names the branch |
| `area:*` | `infra`, `store`, `schema`, `engine`, `python`, or `evidence` |
| `engine:*` | `hayro` or `krilla` |

## Repository facts

- The repository is private. GitHub Actions minutes are limited, and macOS
  and Windows runners count several times as much as Linux, so CI runs on
  Linux unless an issue needs another system.
- `rust-toolchain.toml` pins the one Rust toolchain, and `uv.lock` pins the
  Python tools. `.github/workflows/ci.yml` runs the commands above on every
  pull request and on `main`.

## Keeping this file short

This file holds only rules that matter for every task. Facts and
measurements belong in DESIGN.md and `evidence/`. When a rule can be
checked by a test or by CI, add the check instead of a sentence here.
