# ruff: noqa: INP001 - standalone evidence script.
"""Report measured times and delivered wheel identities from hosted wheel-size runs."""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from datetime import datetime
from typing import Any
from zipfile import ZipFile

REPO = "yhay81/pyropdf"
TARGETS = {
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "x86_64-pc-windows-msvc",
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
}
BUILD_ACTION = "##[group]Run PyO3/maturin-action@"
SETUP_GROUP = re.compile(
    r"^(\S+) ##\[group\](?:Install Rust(?: target)?|Install maturin|Pull Docker image)\n"
    r".*?^(\S+) ##\[endgroup\]",
    re.MULTILINE | re.DOTALL,
)


def api(path: str) -> bytes:
    """Read one GitHub REST resource with the account available to gh."""
    return subprocess.check_output(["gh", "api", f"repos/{REPO}/actions/runs/{path}"])  # noqa: S603, S607


def seconds(start: str, end: str) -> float:
    """Return elapsed seconds between two UTC Actions timestamps."""
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def build_metrics(step: dict, log: str) -> dict:
    """Read the action wall time and Cargo's timed portion from the same build."""
    log, built, _ = log.partition("📦 Built wheel for ")
    if not built:
        msg = f"maturin did not report a wheel in {step['name']}"
        raise ValueError(msg)
    duration = re.search(r"Finished `release` profile \[optimized\] target\(s\) in (?:(\d+)m )?([\d.]+)s", log)
    if duration is None:
        msg = f"missing release compilation timer in {step['name']}"
        raise ValueError(msg)
    return {
        "step_s": int(seconds(step["started_at"], step["completed_at"])),
        "cargo_s": 60 * int(duration[1] or 0) + float(duration[2]),
        "tool_setup_s": round(sum(seconds(start, end) for start, end in SETUP_GROUP.findall(log)), 1),
        "compiled_crates": len(re.findall(r"^\S+\s+Compiling \S+ v\S+", log, re.MULTILINE)),
    }


def wheel_report(log: str) -> dict:
    """Find the size.json record printed by the size gate after any repack logs."""
    plain = "\n".join(line.partition(" ")[2] for line in log.splitlines())
    for block in re.findall(r'^\{\n  "head": \{.*?^\}', plain, re.MULTILINE | re.DOTALL):
        report = json.loads(block)
        if "base_revision" in report and "head_revision" in report:
            return report
    msg = "missing size-gate result"
    raise ValueError(msg)


def compare_job(job: dict, log: str) -> tuple[dict, dict]:
    """Measure the original base and head steps, excluding diagnostic rebuilds."""
    steps = {step["name"]: step for step in job["steps"]}
    builds = log.split(BUILD_ACTION)[1:]
    if len(builds) < 2:  # noqa: PLR2004 - the base and head wheel builds
        msg = f"missing one of the paired builds in {job['html_url']}"
        raise ValueError(msg)
    report = wheel_report(log)
    record: dict[str, Any] = {
        "job_id": job["id"],
        "runner": job["labels"],
        "queue_s": int(seconds(job["created_at"], job["started_at"])),
        "runner_s": int(seconds(job["started_at"], job["completed_at"])),
    }
    for side, build in zip(("base", "head"), builds[:2], strict=True):
        step = steps[f"Build {side} release wheel"]
        record[side] = {
            **build_metrics(step, build),
            "wheel_bytes": report[side]["size_bytes"],
            "wheel_sha256": report[side]["sha256"],
        }
    record["switch_s"] = int(
        seconds(steps["Build base release wheel"]["completed_at"], steps["Build head release wheel"]["started_at"])
    )
    record["wheel_tag"] = report["head"]["tag"]

    # A shared path makes the base compiler output available on this runner.
    # Without an override, Cargo defaults to the separate checkout's target/.
    def target_dir(build: str) -> str | None:
        settings = build.partition("##[endgroup]")[0]
        value = re.search(r"^\S+ +CARGO_TARGET_DIR: (.+)$", settings, flags=re.MULTILINE)
        return value[1] if value else None

    base_dir, head_dir = map(target_dir, builds[:2])
    record["cargo_target"] = "shared" if base_dir and base_dir == head_dir else "separate"
    policy = {
        key: sorted(set(re.findall(rf"^\S+ +{key}: (\S+)$", log, flags=re.MULTILINE)))
        for key in ("rust-toolchain", "maturin-version", "sccache", "manylinux")
    }
    if not all(policy.values()):
        msg = f"missing compiler policy in {job['html_url']}"
        raise ValueError(msg)
    return record, {"base": report["base_revision"], "merge": report["head_revision"], "maturin_action": policy}


def measure_run(run_id: int) -> dict:
    """Read attempt one so a later rerun cannot replace the recorded job times."""
    path = f"{run_id}/attempts/1"
    run = json.loads(api(path))
    if run["path"] != ".github/workflows/wheel-size.yml" or run["conclusion"] != "success":
        msg = f"{run_id} is not a successful wheel-size run"
        raise ValueError(msg)
    jobs = json.loads(api(f"{path}/jobs?per_page=100"))["jobs"]
    records = {}
    common = {}
    with ZipFile(io.BytesIO(api(f"{path}/logs"))) as archive:
        for job in jobs:
            match = re.fullmatch(r"Compare abi3 \(([^)]+)\)", job["name"])
            if not match or job["conclusion"] != "success":
                continue
            target = match[1]
            [filename] = [name for name in archive.namelist() if name.endswith(f"_{job['name']}.txt")]
            raw = archive.read(filename).decode("utf-8-sig")
            log = re.sub(r"\x1b\[[0-9;]*m", "", raw).replace("\r\n", "\n")
            records[target], inputs = compare_job(job, log)
            if common and inputs != common:
                msg = f"inconsistent native build revisions or toolchain in {job['html_url']}"
                raise ValueError(msg)
            common = inputs
    if set(records) != TARGETS:
        msg = f"{run_id} did not complete all five native comparisons"
        raise ValueError(msg)
    return {
        "run": run["html_url"],
        "pr_head": run["head_sha"],
        **common,
        "elapsed_s": int(seconds(run["created_at"], run["updated_at"])),
        "targets": dict(sorted(records.items())),
    }


def main() -> None:
    """Write the five matrix jobs for each requested first-attempt run as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=int, nargs="+", help="successful pull-request Wheel size run IDs")
    args = parser.parse_args()
    json.dump([measure_run(run_id) for run_id in args.runs], sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
