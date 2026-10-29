# AGENTS.md

This file is the canonical context for coding agents. `CLAUDE.md` only
imports it; update this file instead.

pyropdf rebuilds [pylopdf](https://github.com/yhay81/pylopdf)'s concept on one
versioned object store that every engine reads. [DESIGN.md](DESIGN.md) is the
design: its principles F1 to F9 (section 3) bind every change. This file says
how to work. GitHub's issue tree and merged pull requests record progress;
status text in a design document is not a work queue.

## Start or resume here

1. Follow an explicit user assignment first. Inspect `git status` and the
   branch before touching files; preserve existing work.
2. Run `python3 tools/next_work.py` (authenticated `gh` required). It reads
   live GitHub state: current default-branch CI, PRs, claims, ready work,
   decisions, upstream work, and blockers. `--json` emits the same report.
   A failed read is unknown, never an empty queue. Inspect failed main CI
   before starting unrelated work; pending CI does not prevent investigation.
3. For an existing task, read its issue **and comments**, the claim's branch,
   and its PR's diff, checks, and review threads. Continue its unfinished
   acceptance criteria before selecting another issue. A matching branch
   is a clue to inspect, not permission to take over another agent's claim.
4. For new work, take a ready task from any open phase; a later phase does
   not wait for an earlier one to exit. The report suggests ready tasks by
   earliest phase, most direct open dependents, then issue number; this is
   a tie-breaker, not product priority. Read the selected issue's
   Context, Scope, Paths, Acceptance, and Notes, then DESIGN.md sections 3
   and 4 and the sections it names. Recheck dependencies and claims before
   claiming: the report is a snapshot, not a reservation.

| Question | Source of truth |
| --- | --- |
| What is allowed and how do agents work? | This file and the user's instructions |
| What must the implementation preserve? | DESIGN.md, especially F1 to F9 |
| What is next, owned, or blocked? | Issue state, labels, milestone, parent, and blocked-by links |
| What was decided or left unfinished? | Issue comments and the linked PR; approvals come from the owner, not an agent using the same account |
| Is the change ready to land? | Acceptance evidence, review threads, and CI on the current PR head; merge authorization is separate |

When no ordinary task is ready, inspect decision recommendations, upstream
release/adoption status, and claims. Prepare missing evidence or a local
patch where allowed; do not repeat a finished recommendation or infer an
approval. If these are all waiting, report the exact dependency or owner
choice and the work already prepared. An empty ready list does not mean
that a phase's exit criteria hold.

## Choosing work

All work comes from issues. The pinned issue #1 is the root of the tree:
phase trackers and engine trackers are its sub-issues, and tasks are
sub-issues of a tracker. The milestone of an issue is its phase.

An ordinary implementation issue is ready when it is open, belongs to an
open phase, is neither `tracker` nor `in-progress`, has no open blocker
or linked open PR, and is neither `decision` nor `upstream`/`scope-question`. The report keeps
those other kinds separate because their next action differs. Missing phase
or dependency metadata needs triage, not an assumption of readiness.

Phases order work but do not gate it: only blocked-by links do. An issue
therefore names every issue it waits on, in any phase, as a blocker. When
a task turns out to need unfinished work that its links omit, add the
link and choose another task.

Read the issue with `gh issue view <number>`, then its discussion with
`gh issue view <number> --comments`.
To inspect its direct blockers, including their state:

```bash
gh api --paginate repos/yhay81/pyropdf/issues/<number>/dependencies/blocked_by
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

## Finish or hand off

Before leaving work, record a short comment on its issue: branch and PR,
what changed, checks and evidence, the exact next action, and any dependency
or owner decision still needed. Link an existing recommendation instead of
copying it. Keep the claim while actively working; release it as above if
stopping. Open PR links still keep an issue out of the new-work queue. A PR
awaiting owner review should say so and link its issue.

Before merge, check the **current head's** CI, acceptance criteria, unresolved
reviews, and authorization for this work. After merge, verify the issue
closed, remove its `in-progress` label, and inspect the main CI and newly
unblocked work with the report. A failed main check gets an issue or an
explicit handoff. Close a phase tracker only after verifying its exit
criteria, not merely because its implementation tasks are closed.

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

`python3 tools/check.py` runs these in order and prints one line per
check, with the lines of a failed check's log that name its failures; the
full logs stay in `target/check/`. Name checks, or `python` or `rust`, to
run fewer, and add `--locked` to match CI.

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

## Keeping context small

Whatever an agent reads stays in its context and is paid for again on
every later turn, so read only what the task needs:

- Check with `python3 tools/check.py`, and search a failed check's log
  instead of rerunning the command to see more of it.
- Read DESIGN.md by section: `grep -n '^##' DESIGN.md` lists them, and
  `sed -n '/^## 3\./,/^## 5\./p' DESIGN.md` prints sections 3 and 4.
- Search before reading, and read a long file by line range. Do not read
  a lockfile, `target/`, or `.venv/` whole.
- Ask `gh` for the fields you need with `--json` and `--jq`, and list a
  PR's files with `gh pr diff <number> --name-only` before reading its diff.
- Work one issue per session. Record the handoff as in "Finish or hand
  off", then start the next issue in a fresh session instead of carrying
  the old context.

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

- The repository is public. GitHub Actions minutes are limited, and macOS
  and Windows runners count several times as much as Linux, so CI runs on
  Linux unless an issue needs another system.
- `rust-toolchain.toml` pins the one Rust toolchain, and `uv.lock` pins the
  Python tools. `.github/workflows/ci.yml` runs the commands above on every
  pull request and on `main`.

## Keeping this file short

This file holds only rules that matter for every task. Facts and
measurements belong in DESIGN.md and `evidence/`. When a rule can be
checked by a test or by CI, add the check instead of a sentence here.
