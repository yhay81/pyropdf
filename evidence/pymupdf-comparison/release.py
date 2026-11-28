# ruff: noqa: INP001, S603, S607 - run git, pip and this repository's isolated workers.
"""Compare identified wheels against a versioned corpus from this checkout on Python 3.13."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import venv
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

import bench
import quality
from release_report import render

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ORACLE = "2.1"
ENGINES = ("pymupdf", "pyropdf")
TIMEOUT = 300


def digest(path: Path) -> str:
    """Hash exactly the artifact or input bytes used."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(*args: str) -> str:
    """Read source evidence relative to the repository."""
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def wheel_info(path: Path, expected: str) -> dict:
    """Check distribution metadata inside a wheel before labeling its measurements."""
    with ZipFile(path) as wheel:
        (name,) = (name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(wheel.read(name))
    if metadata["Name"].lower() != expected:
        msg = f"Expected {expected}, received {metadata['Name']} in {path}"
        raise ValueError(msg)
    return {"wheel": path.name, "sha256": digest(path), "version": metadata["Version"]}


def install(work: Path, engine: str, wheel: Path) -> Path:
    """Install the actual wheel in a fresh interpreter, separate from the other engine."""
    directory = work / engine
    venv.EnvBuilder(with_pip=True).create(directory)
    python = directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    constraints = ["-c", HERE / "oracle-2.1-constraints.txt"] if engine == "pymupdf" else []
    subprocess.run([python, "-E", "-m", "pip", "install", "-q", "--only-binary=:all:", *constraints, wheel], check=True)
    return python


def observe(python: Path, *args: str | Path) -> tuple[str, str]:
    """Keep completed observations and the reason for a worker's abnormal exit."""
    try:
        result = subprocess.run([python, "-E", *args], capture_output=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        return (exc.stdout or b"").decode(errors="replace"), f"timed out after {TIMEOUT}s"
    output = result.stdout.decode(errors="replace")
    if result.returncode:
        diagnostic = result.stderr.decode(errors="replace").strip().splitlines()
        return output, f"exit {result.returncode}: {diagnostic[-1] if diagnostic else 'no diagnostic'}"
    return output, ""


def timing(python: Path, engine: str, pdf: Path) -> dict:
    """Keep the complete operation manifest, including rows a crashed worker never reached."""
    output, error = observe(python, HERE / "release_worker.py", "time", engine, pdf)
    rows = {}
    for line in output.splitlines():
        if line.startswith("["):
            try:
                operation, row = json.loads(line)
            except json.JSONDecodeError:
                error = error or "incomplete worker output"
                break
            rows[operation] = row
    interrupted = bool(error)
    for operation in bench.OPERATIONS:
        if operation not in rows:
            status = "failed" if interrupted else "unmeasured"
            rows[operation] = {"status": status, "reason": error or "worker returned no observation"}
            interrupted = False
    return rows


def labeled(python: Path, engine: str, work: Path) -> tuple[dict, dict]:
    """Only checked labels contribute scores. Keep the original extraction for each scored page."""
    scores, raw = {}, {}
    out = work / "page.json"
    for path in quality.LABELS:
        label = json.loads(path.read_text(encoding="utf-8"))
        if label.get("checked") is not True:
            scores[path.stem] = {"status": "unmeasured", "reason": "label awaits human verification"}
            continue
        _, error = observe(python, HERE / "quality.py", "collect", engine, out, path)
        if error:
            scores[path.stem] = {"status": "failed", "reason": error}
            continue
        page = json.loads(out.read_text(encoding="utf-8"))["pages"][path.stem]
        raw[path.stem] = page
        if "skipped" in page:
            scores[path.stem] = {"status": "unmeasured", "reason": str(page["skipped"]).splitlines()[0]}
            continue
        entry: dict = {
            "status": "ok",
            "anchors": quality._anchors(label, page["text"]),  # noqa: SLF001
            "words": quality._words(label, page["words"]),  # noqa: SLF001
        }
        if "max_spurious_words" in label:
            entry["spurious_words"] = len(page["words"])
            entry["max_spurious_words"] = label["max_spurious_words"]
        if "table" in label:
            entry["table"] = quality._table(label, page["tables"])  # noqa: SLF001
        scores[path.stem] = entry
    return scores, raw


def task_map(revision: str) -> list[dict]:
    """Require release-source contract evidence or a classified gap for every audited task."""
    with (HERE / "capabilities.tsv").open(encoding="utf-8") as stream:
        tasks = list(csv.DictReader(stream, delimiter="\t"))
    for task in tasks:
        tests = task["Release tests"]
        if tests == "-":
            if task["Gap"] == "-" and "Excluded" not in task["Canonical pyropdf task and scope"]:
                raise ValueError(task["Task"])
            continue
        for name in tests.split(","):
            git("cat-file", "-e", f"{revision}:tests/test_{name}.py")
    return tasks


def collect(wheels: dict[str, Path], release: str, out: Path, previous_report: Path | None) -> None:
    """Build the complete release report with machine-readable inputs and observations."""
    artifacts = {engine: wheel_info(path, engine) for engine, path in wheels.items()}
    if artifacts["pymupdf"]["version"] != ORACLE or release != f"v{artifacts['pyropdf']['version']}":
        msg = f"Wheel versions must match {release} and the reviewed PyMuPDF {ORACLE} baseline"
        raise ValueError(msg)
    revision = git("rev-parse", "--verify", f"refs/tags/{release}^{{commit}}")
    tasks = task_map(revision)
    corpus = sorted(quality.CORPUS.glob("*.pdf"))
    if not corpus or not quality.LABELS:
        msg = "The comparison needs a corpus and quality labels"
        raise ValueError(msg)
    old = json.loads((HERE / "pymupdf-2.0-api.json").read_text(encoding="utf-8"))
    previous_release = None
    if previous_report:
        previous = json.loads(previous_report.read_text(encoding="utf-8"))
        previous_release = previous["release"]
        before = previous["engines"]["pymupdf"]
        old = {
            "version": previous["artifacts"]["pymupdf"]["version"],
            "surface": before["surface"],
            "probes": before["probes"],
        }
    raw = {
        "release": release,
        "release_revision": revision,
        "corpus_revision": git("rev-parse", "HEAD"),
        "corpus": {path.name: digest(path) for path in corpus},
        "labels": {path.stem: digest(path) for path in quality.LABELS},
        "task_map_sha256": digest(HERE / "capabilities.tsv"),
        "constraints_sha256": digest(HERE / "oracle-2.1-constraints.txt"),
        "scripts": {path.name: digest(path) for path in sorted(HERE.glob("*.py")) if path.name in SCRIPTS},
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "settings": {"runs": bench.RUNS, "timeout_s": TIMEOUT, "search": bench.NEEDLE, "operations": bench.OPERATIONS},
        "artifacts": artifacts,
        "engines": {},
        "previous_baseline": old["version"],
        "previous_release": previous_release,
        "previous_report_sha256": digest(previous_report) if previous_report else digest(HERE / "pymupdf-2.0-api.json"),
        "previous_probes": old["probes"],
    }
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        for engine, wheel in wheels.items():
            python = install(work, engine, wheel)
            info = json.loads(subprocess.check_output([python, "-E", HERE / "release_worker.py", "info", engine]))
            if engine == "pymupdf":
                surface = info["surface"]
                raw["surface_delta"] = {
                    key: {"previous": old["surface"].get(key), "current": surface.get(key)}
                    for key in sorted(old["surface"].keys() | surface.keys())
                    if old["surface"].get(key) != surface.get(key)
                }
                raw["surface_count"] = len(surface)
            probes, error = observe(python, HERE / "release_delta.py", engine)
            info["probes"] = {"error": error} if error else json.loads(probes)
            info["timing"] = {pdf.name: timing(python, engine, pdf) for pdf in corpus}
            info["quality"], pages = labeled(python, engine, work)
            (out / f"{engine}-pages.json.gz").write_bytes(gzip.compress(json.dumps(pages).encode(), mtime=0))
            raw["engines"][engine] = info
    (out / "observations.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "comparison.md").write_text(render(raw, tasks), encoding="utf-8")


SCRIPTS = {"bench.py", "quality.py", "release.py", "release_report.py", "release_worker.py", "release_delta.py"}


def main() -> None:
    """Use an explicit release tag and platform-compatible installed-artifact inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release")
    parser.add_argument("pyropdf_wheel", type=Path)
    parser.add_argument("pymupdf_wheel", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--previous-report", type=Path, help="Prior release observations.json")
    args = parser.parse_args()
    collect(
        {"pymupdf": args.pymupdf_wheel, "pyropdf": args.pyropdf_wheel}, args.release, args.output, args.previous_report
    )


if __name__ == "__main__":
    main()
