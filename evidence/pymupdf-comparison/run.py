# ruff: noqa: INP001, T201 - a script, not a package; it prints its progress.
"""Install both engines in environments of their own and compare them.

    python3 evidence/pymupdf-comparison/run.py

Each engine is installed from its published wheels into
``target/pymupdf-comparison/<engine>-env``, so neither is a dependency of this
project and both run release builds. PyMuPDF is AGPL; it only runs here, as a
separate program. The script runs ``bench.py`` with each environment over
``tests/assets/real_world`` and writes ``results/<platform>.md`` and the raw
timings beside it. ``--pyropdf-wheel`` measures a local release-profile wheel
instead, records its SHA-256, and writes a separate ``<platform>-checkout`` result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ENGINES = {"pymupdf": "pymupdf==2.0", "pyropdf": "pyropdf==0.1.0"}
#: One Python for both; PyMuPDF 2.0 needs onnxruntime, which has no 3.15 wheels.
PYTHON = "3.13"
ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TARGET = ROOT / "target" / "pymupdf-comparison"
CORPUS = sorted((ROOT / "tests" / "assets" / "real_world").glob("*.pdf"))
LABELS = {
    "open": "Open",
    "text": "Text",
    "words": "Words",
    "search": "Search",
    "render_72": "Render 72 dpi",
    "render_144": "Render 144 dpi",
    "save": "Full save",
}


def _run(*command: str | Path) -> None:
    subprocess.run([str(part) for part in command], check=True, cwd=ROOT)  # noqa: S603 - fixed tools


def _environment(engine: str, wheel: Path | None = None) -> Path:
    """Install one engine's pinned release into its own environment; return its Python."""
    env = TARGET / f"{engine}{'-checkout' if wheel else ''}-env"
    python = env / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
    _run("uv", "venv", "--clear", "--quiet", "--python", PYTHON, env)
    _run("uv", "pip", "install", "--quiet", "--python", python, wheel or ENGINES[engine])
    return python


def _table(pymupdf: dict, pyropdf: dict) -> list[str]:
    """Return Markdown rows: per operation, the corpus totals and the ratio."""
    rows = ["| Operation | PyMuPDF (ms) | pyropdf (ms) | pyropdf / PyMuPDF |", "| --- | ---: | ---: | ---: |"]
    for key, label in LABELS.items():
        a = sum(result[key] for result in pymupdf["results"].values())
        b = sum(result[key] for result in pyropdf["results"].values())
        rows.append(f"| {label} | {a:.1f} | {b:.1f} | {b / a:.2f} |")
    return rows


def _per_document(pymupdf: dict, pyropdf: dict) -> list[str]:
    """Return Markdown rows: per document, the ratio of each operation."""
    rows = [
        "| Document | " + " | ".join(LABELS.values()) + " |",
        "| --- |" + " ---: |" * len(LABELS),
    ]
    for name, a in pymupdf["results"].items():
        b = pyropdf["results"][name]
        rows.append(f"| {name} | " + " | ".join(f"{b[key] / a[key]:.2f}" for key in LABELS) + " |")
    return rows


def main() -> None:
    """Run both engines and write the comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyropdf-wheel", type=Path, help="Use a local release-profile wheel")
    args = parser.parse_args()
    wheel = args.pyropdf_wheel.resolve() if args.pyropdf_wheel else None
    if wheel is not None and (not wheel.is_file() or wheel.suffix != ".whl"):
        parser.error("--pyropdf-wheel must name an existing wheel")
    raw: dict[str, dict] = {}
    for engine in ENGINES:
        out = TARGET / f"{engine}.json"
        _run(_environment(engine, wheel if engine == "pyropdf" else None), HERE / "bench.py", engine, out, *CORPUS)
        raw[engine] = json.loads(out.read_text(encoding="utf-8"))
    system = f"{platform.system().lower()}-{platform.machine().lower()}"
    if wheel is not None:
        system += "-checkout"
        raw["artifact"] = {"wheel": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
    results = HERE / "results"
    results.mkdir(exist_ok=True)
    (results / f"{system}.json").write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"# {system}",
        "",
        f"Measured {datetime.now(UTC).date()} on {platform.platform()} ({platform.processor() or 'unknown'}),",
        f"Python {raw['pyropdf']['python']}, {raw['pymupdf']['engine']} against {raw['pyropdf']['engine']},",
        f"best of {raw['pyropdf']['runs']} runs per operation, over the {len(CORPUS)} documents of",
        "`tests/assets/real_world`. A ratio below 1 means pyropdf is faster.",
        "",
        "## Corpus totals",
        "",
        *_table(raw["pymupdf"], raw["pyropdf"]),
        "",
        "## Ratio per document",
        "",
        *_per_document(raw["pymupdf"], raw["pyropdf"]),
        "",
    ]
    if wheel is not None:
        lines.extend(
            [
                "## Local artifact",
                "",
                f"Wheel: `{wheel.name}`. SHA-256: `{raw['artifact']['sha256']}`.",
                "Built separately in the release profile; rendering uses the public API defaults.",
                "",
            ]
        )
    (results / f"{system}.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
