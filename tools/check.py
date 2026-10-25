"""Run the pre-push checks and print only what is needed to act on them.

Run `python3 tools/check.py` from any directory. It runs the commands of
AGENTS.md in order and prints one line per check; a failed check also prints
the lines of its log that name the failures. Full logs stay in target/check/,
so a failure can be searched instead of rerun. Name checks, or the groups
`python` and `rust`, to run fewer, and pass `--locked` to match CI.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "target" / "check"
# AGENTS.md's commands in its order. As in CI, the checks after `sync` use the
# environment that it prepared instead of syncing again.
CHECKS: dict[str, tuple[str, ...]] = {
    "sync": ("uv", "sync"),
    "pytest": ("uv", "run", "--no-sync", "pytest"),
    "ruff": ("uv", "run", "--no-sync", "ruff", "check", "."),
    "ruff-format": ("uv", "run", "--no-sync", "ruff", "format", "--check", "."),
    "ty": ("uv", "run", "--no-sync", "ty", "check"),
    "fmt": ("cargo", "fmt", "--all", "--check"),
    "clippy": ("cargo", "clippy", "--workspace", "--all-targets", "--all-features", "--", "-D", "warnings"),
    "cargo-test": ("cargo", "test", "--workspace", "--all-features"),
}
GROUPS: dict[str, tuple[str, ...]] = {
    "python": ("sync", "pytest", "ruff", "ruff-format", "ty"),
    "rust": ("fmt", "clippy", "cargo-test"),
}
LOCKED = frozenset({"sync", "clippy", "cargo-test"})
# Lines that name a failure: rustc, clippy, ty, and uv errors, ruff rule codes,
# pytest's summary, files that rustfmt or ruff would change, and test panics
# and totals. Warnings are left out because they do not fail a check.
HEADLINE = re.compile(
    r"^(?:error(?:\[[\w-]+\])?: |[A-Z]+[0-9]+ |FAILED |ERROR |Would reformat: |Diff in |"
    r"thread '.+' panicked at |test result: FAILED|\s*× )"
)
LOCATION = re.compile(r"^\s*--> (\S+)")
PANIC = re.compile(r"^thread '.+' panicked at ")
# The logs are read as text, so no tool may color them.
ENV = {**os.environ, "CARGO_TERM_COLOR": "never", "NO_COLOR": "1"}


def select(names: list[str]) -> list[str]:
    """Expand groups, add `sync` before the checks that need it, and keep AGENTS.md's order."""
    chosen = {check for name in names or list(CHECKS) for check in GROUPS.get(name, (name,))}
    if chosen & set(GROUPS["python"]):
        chosen.add("sync")
    return [name for name in CHECKS if name in chosen]


def command(name: str, *, locked: bool) -> list[str]:
    """Return a check's command line, with `--locked` before any `--` when CI's lockfile check is wanted."""
    args = list(CHECKS[name])
    if locked and name in LOCKED:
        args.insert(args.index("--") if "--" in args else len(args), "--locked")
    return args


def run(name: str, args: list[str]) -> tuple[bool, float, Path]:
    """Run one check with its output in its log; a command that cannot start fails the check."""
    log = LOG_DIR / f"{name}.log"
    start = time.monotonic()
    with log.open("w", encoding="utf-8") as output:
        output.write(f"$ {' '.join(args)}\n")
        output.flush()
        try:
            completed = subprocess.run(  # noqa: S603 - argv only, from CHECKS
                args, cwd=ROOT, env=ENV, stdout=output, stderr=subprocess.STDOUT, check=False
            )
        except OSError as error:
            output.write(f"{error}\n")
            passed = False
        else:
            passed = completed.returncode == 0
    return passed, time.monotonic() - start, log


def excerpt(log: str, limit: int) -> list[str]:
    """Return the lines of a failed check's log that name its failures, or the log's tail when none do."""
    found: list[str] = []
    joinable = False
    for line in log.splitlines():
        location = LOCATION.match(line)
        if joinable and (location or PANIC.match(found[-1])):
            # A location, or a panic's message, completes the line above it.
            found[-1] += f" ({location.group(1)})" if location else f" {line.strip()}"
            joinable = False
        else:
            joinable = HEADLINE.match(line) is not None
            if joinable:
                found.append(line.strip())
    if not found:
        return log.splitlines()[-limit:]
    if len(found) > limit:
        return [*found[:limit], f"... {len(found) - limit} more"]
    return found


def main(argv: list[str] | None = None) -> int:
    """Run the chosen checks and return nonzero when any of them failed."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "checks",
        nargs="*",
        metavar="check",
        help=f"checks or groups to run, all by default: {' '.join([*CHECKS, *GROUPS])}",
    )
    parser.add_argument("--locked", action="store_true", help="pass --locked to uv sync and cargo, as CI does")
    parser.add_argument("--lines", type=int, default=30, help="most log lines to print for a failed check")
    args = parser.parse_args(argv)
    unknown = [name for name in args.checks if name not in CHECKS and name not in GROUPS]
    if unknown:
        parser.error(f"unknown check: {' '.join(unknown)}")
    if args.lines < 1:
        parser.error("--lines must be at least 1")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    skipped: list[str] = []
    for name in select(args.checks):
        if "sync" in failed and name in GROUPS["python"]:
            skipped.append(name)
            sys.stdout.write(f"skip  {name} (sync failed)\n")
            continue
        passed, seconds, log = run(name, command(name, locked=args.locked))
        if passed:
            sys.stdout.write(f"ok    {name} {seconds:.1f}s\n")
        else:
            failed.append(name)
            shown = log.relative_to(ROOT) if log.is_relative_to(ROOT) else log
            sys.stdout.write(f"FAIL  {name} {seconds:.1f}s, log {shown}\n")
            lines = excerpt(log.read_text(encoding="utf-8", errors="replace"), args.lines)
            sys.stdout.writelines(f"      {line}\n" for line in lines)
        sys.stdout.flush()
    summary = f"failed: {' '.join(failed)}" if failed else "all passed"
    if skipped:
        summary += f"; skipped: {' '.join(skipped)}"
    sys.stdout.write(summary + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
