# ruff: noqa: INP001, T201, S603 - reproducible benchmark runner script.
"""Benchmark damaged-PDF repair coverage across pyropdf, PyMuPDF, qpdf, and pdfium.

    python3 evidence/repair-coverage/run.py

PyMuPDF (AGPL) and pypdfium2 are installed into separate temporary virtual
environments under ``target/repair-coverage/``, never into this project's
dependencies. Each engine evaluates the exact same deterministic damaged-PDF
matrix from ``cases.py`` in its own subprocess (with automatic crash attribution
and resume if a native C library aborts on a damaged PDF) and writes JSON
results that this runner aggregates into ``results/<platform>.json`` and
``results/<platform>.md``.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TARGET = ROOT / "target" / "repair-coverage"
sys.path.insert(0, str(HERE))

from cases import Case, all_cases  # noqa: E402

#: Python version for external oracle venvs (PyMuPDF + pypdfium2 wheels).
ORACLE_PYTHON = "3.13"
ORACLE_PACKAGES = {
    "pymupdf": "pymupdf>=1.25,<2",
    "pdfium": "pypdfium2>=5.0",
}
ENGINES = ("pyropdf", "pymupdf", "qpdf", "pdfium")
METRICS = (
    ("open", "Open"),
    ("pages_ok", "Pages >= 1"),
    ("exact_pages", "Exact pages"),
    ("text_ok", "Text p0"),
    ("render_ok", "Render p0"),
    ("full_save_ok", "Full save"),
)


def _run(*command: str | Path) -> None:
    subprocess.run([str(part) for part in command], check=True, cwd=ROOT)


def _oracle_env(name: str, *, reuse: bool) -> Path:
    """Create (or reuse) an isolated virtual environment for one external oracle."""
    env = TARGET / f"{name}-env"
    python = env / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
    if reuse and python.is_file():
        return python
    _run("uv", "venv", "--clear", "--quiet", "--python", ORACLE_PYTHON, env)
    _run("uv", "pip", "install", "--quiet", "--python", python, ORACLE_PACKAGES[name])
    return python


def _pyropdf_python(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    venv_py = ROOT / ".venv" / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
    if venv_py.is_file():
        return venv_py
    return Path(sys.executable)


def _run_engine(engine: str, python: Path, cases: list[Case]) -> dict[str, Any]:
    """Run ``worker.py`` for one engine, attributing and resuming past native crashes."""
    jsonl = TARGET / f"{engine}.jsonl"
    jsonl.unlink(missing_ok=True)
    meta: dict[str, str] = {"engine": engine, "python": "unknown"}
    results: dict[str, dict[str, Any]] = {}
    start = 0

    while start < len(cases):
        proc = subprocess.run(
            [str(python), str(HERE / "worker.py"), engine, str(jsonl), "--start", str(start)],
            cwd=ROOT,
            check=False,
        )
        completed = start - 1
        if jsonl.is_file():
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("_meta"):
                    meta["engine"] = rec["engine"]
                    meta["python"] = rec["python"]
                else:
                    results[rec["case_id"]] = rec["result"]
                    completed = max(completed, int(rec["index"]))
        if proc.returncode == 0:
            break
        if proc.returncode > 0 and completed < start:
            msg = f"worker for {engine} failed with exit code {proc.returncode} before processing case {start}"
            raise RuntimeError(msg)
        crashed_idx = completed + 1
        if crashed_idx >= len(cases):
            break
        crashed_case = cases[crashed_idx]
        sig_desc = f"signal {-proc.returncode}" if proc.returncode < 0 else f"exit {proc.returncode}"
        print(f"  [{engine}] native crash ({sig_desc}) on {crashed_case.case_id}", flush=True)
        crash_entry: dict[str, Any] = {
            "open": False,
            "is_repaired": None,
            "page_count": None,
            "pages_ok": False,
            "exact_pages": False,
            "text_ok": None if engine == "qpdf" else False,
            "render_ok": None if engine == "qpdf" else False,
            "full_save_ok": False,
            "full_save_clean": False,
            "crashed": True,
            "error": f"native crash ({sig_desc})",
        }
        results[crashed_case.case_id] = crash_entry
        with jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"index": crashed_idx, "case_id": crashed_case.case_id, "result": crash_entry}) + "\n"
            )
        start = crashed_idx + 1

    return {"engine": meta["engine"], "python": meta["python"], "results": results}


def _fmt_rate(passed: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{passed}/{total} ({100.0 * passed / total:.1f}%)"


def _summary_table(cases: list[Case], raw: dict[str, dict[str, Any]], suite: str | None = None) -> list[str]:
    filtered = [c for c in cases if suite is None or c.suite == suite]
    rows = [
        "| Metric | " + " | ".join(ENGINES) + " |",
        "| --- |" + " ---: |" * len(ENGINES),
    ]
    for key, label in METRICS:
        cells = []
        for engine in ENGINES:
            results = raw[engine]["results"]
            applicable = [results[c.case_id][key] for c in filtered if results[c.case_id][key] is not None]
            if not applicable:
                cells.append("n/a")
            else:
                cells.append(_fmt_rate(sum(1 for v in applicable if v), len(applicable)))
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    crashes = [str(sum(1 for c in filtered if raw[e]["results"][c.case_id].get("crashed"))) for e in ENGINES]
    rows.append("| Native crashes | " + " | ".join(crashes) + " |")
    return rows


def _category_table(cases: list[Case], raw: dict[str, dict[str, Any]], suite: str, metric: str) -> list[str]:
    grouped: dict[str, list[Case]] = defaultdict(list)
    for case in cases:
        if case.suite == suite:
            grouped[case.category].append(case)
    rows = [
        "| Category | Cases | " + " | ".join(ENGINES) + " |",
        "| --- | ---: |" + " ---: |" * len(ENGINES),
    ]
    for category, group in grouped.items():
        cells = []
        for engine in ENGINES:
            results = raw[engine]["results"]
            vals = [results[c.case_id][metric] for c in group if results[c.case_id][metric] is not None]
            cells.append(_fmt_rate(sum(1 for v in vals if v), len(vals)) if vals else "n/a")
        rows.append(f"| `{category}` | {len(group)} | " + " | ".join(cells) + " |")
    return rows


def _gaps(cases: list[Case], raw: dict[str, dict[str, Any]]) -> list[str]:
    """List every case where another engine recovers pages/save/text/render and pyropdf does not."""
    lines: list[str] = []
    for case in cases:
        pyro = raw["pyropdf"]["results"][case.case_id]
        others = {e: raw[e]["results"][case.case_id] for e in ENGINES if e != "pyropdf"}
        missing: list[str] = []
        for key, label in (
            ("pages_ok", "pages"),
            ("text_ok", "text"),
            ("render_ok", "render"),
            ("full_save_ok", "save"),
        ):
            if not pyro[key]:
                winners = [e for e, res in others.items() if res[key] is True]
                if winners:
                    missing.append(f"{label} (recovered by {', '.join(winners)})")
        if missing:
            err = f" — `{pyro['error']}`" if pyro["error"] else ""
            lines.append(f"- `{case.case_id}`: {', '.join(missing)}{err}")
    return lines


def _build_markdown(cases: list[Case], raw: dict[str, dict[str, Any]], system: str) -> str:
    corpus_n = sum(1 for c in cases if c.suite == "corpus")
    targeted_n = sum(1 for c in cases if c.suite == "targeted")
    gaps = _gaps(cases, raw)
    pyro_repaired = [
        raw["pyropdf"]["results"][c.case_id]
        for c in cases
        if raw["pyropdf"]["results"][c.case_id]["is_repaired"] is True
    ]
    incr_refused = sum(1 for r in pyro_repaired if r.get("incremental_refused") is True)
    lines = [
        f"# Damaged-PDF repair coverage ({system})",
        "",
        f"Measured {datetime.now(UTC).date()} on {platform.platform()} ({platform.processor() or 'unknown'}).",
        "",
        "## Engines",
        "",
        *(f"- **{e}**: `{raw[e]['engine']}` (Python {raw[e]['python']})" for e in ENGINES),
        "",
        (
            f"Matrix: **{len(cases)}** damaged PDFs ({corpus_n} corpus-derived across 15 real-world PDFs × "
            f"19 damage kinds, plus {targeted_n} targeted synthetic malformed fixtures)."
        ),
        "",
        f"Incremental save refusal on repaired pyropdf documents: **{incr_refused}/{len(pyro_repaired)}** refused.",
        "",
        "## Overall recovery rates",
        "",
        *_summary_table(cases, raw),
        "",
        "## Corpus-derived suite summary",
        "",
        *_summary_table(cases, raw, suite="corpus"),
        "",
        "### Corpus recovery by category (`Pages >= 1`)",
        "",
        *_category_table(cases, raw, "corpus", "pages_ok"),
        "",
        "### Corpus full-save recovery by category (`Full save`)",
        "",
        *_category_table(cases, raw, "corpus", "full_save_ok"),
        "",
        "## Targeted malformed fixtures summary",
        "",
        *_summary_table(cases, raw, suite="targeted"),
        "",
        "### Targeted fixtures by category (`Pages >= 1`)",
        "",
        *_category_table(cases, raw, "targeted", "pages_ok"),
        "",
        "### Targeted fixtures full-save recovery by category (`Full save`)",
        "",
        *_category_table(cases, raw, "targeted", "full_save_ok"),
        "",
        f"## Remaining gaps vs other engines ({len(gaps)})",
        "",
        *(gaps or ["None — pyropdf matched or exceeded every oracle on all cases."]),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    """Run all four engines and write the coverage report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyropdf-python", type=Path, help="Python executable with pyropdf installed")
    parser.add_argument("--reuse-oracles", action="store_true", help="Reuse existing oracle venvs in target/")
    parser.add_argument("--tag", default="", help="Optional filename suffix (e.g. baseline)")
    args = parser.parse_args()

    TARGET.mkdir(parents=True, exist_ok=True)
    py_pyro = _pyropdf_python(args.pyropdf_python)
    py_mupdf = _oracle_env("pymupdf", reuse=args.reuse_oracles)
    py_pdfium = _oracle_env("pdfium", reuse=args.reuse_oracles)

    runners: dict[str, Path] = {
        "pyropdf": py_pyro,
        "pymupdf": py_mupdf,
        "qpdf": Path(sys.executable),
        "pdfium": py_pdfium,
    }
    cases = all_cases()
    raw: dict[str, dict[str, Any]] = {}
    for engine in ENGINES:
        print(f"Running {engine}...", flush=True)
        raw[engine] = _run_engine(engine, runners[engine], cases)

    system = f"{platform.system().lower()}-{platform.machine().lower()}"
    stem = f"{system}-{args.tag}" if args.tag else system
    results_dir = HERE / "results"
    results_dir.mkdir(exist_ok=True)
    (results_dir / f"{stem}.json").write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    md = _build_markdown(cases, raw, system)
    (results_dir / f"{stem}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
