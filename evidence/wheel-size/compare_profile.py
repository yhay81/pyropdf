# ruff: noqa: INP001 - standalone evidence script.
"""Compare two delivered abi3 wheels on compressed size and two-document timings."""

from __future__ import annotations

import argparse
import json
import platform
import runpy
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BENCH_SCRIPT = ROOT / "evidence" / "pymupdf-comparison" / "bench.py"
BENCH_FILES = (
    ROOT / "tests" / "assets" / "real_world" / "bill-hr815.pdf",
    ROOT / "tests" / "assets" / "real_world" / "usrguide.pdf",
)
_BENCH_RUNNER = (
    "import runpy, sys; sys.path.insert(0, sys.argv[1]); "
    "sys.argv = sys.argv[2:]; runpy.run_path(sys.argv[0], run_name='__main__')"
)


def _run_benchmark(wheel: Path, python: str) -> dict[str, Any]:
    """Install one wheel into an isolated directory and time the two benchmark documents."""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "site"
        output = Path(directory) / "timings.json"
        subprocess.run(  # noqa: S603 - explicit interpreter and wheel path
            [python, "-m", "pip", "install", "--quiet", "--no-deps", "--target", str(target), str(wheel)],
            check=True,
        )
        subprocess.run(  # noqa: S603 - isolated invocation of the checked-in benchmark script
            [
                python,
                "-I",
                "-c",
                _BENCH_RUNNER,
                str(target),
                str(BENCH_SCRIPT),
                "pyropdf",
                str(output),
                *(str(path) for path in BENCH_FILES),
            ],
            check=True,
            cwd=directory,
        )
        return json.loads(output.read_text(encoding="utf-8"))


def _rustc_version() -> str:
    """Return the active rustc release version."""
    text = subprocess.check_output(["rustc", "--version"], text=True).strip()  # noqa: S607
    return text.split()[1]


def _host_description() -> str:
    """Return a short host CPU and OS summary matching store-profile.json."""
    if sys.platform == "darwin":
        cpu = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()  # noqa: S607
        os_version = subprocess.check_output(["sw_vers", "-productVersion"], text=True).strip()  # noqa: S607
        return f"{cpu}, macOS {os_version}"
    return f"{platform.machine()}, {platform.system()} {platform.release()}"


def _rust_target() -> str:
    """Return rustc's host triple."""
    out = subprocess.check_output(["rustc", "-vV"], text=True)  # noqa: S607
    for line in out.splitlines():
        if line.startswith("host: "):
            return line.removeprefix("host: ").strip()
    return platform.machine()


def main() -> None:
    """Measure before and after delivered wheels and emit store-profile-compatible JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="directory containing the delivered baseline wheel")
    parser.add_argument("candidate", type=Path, help="directory containing the delivered candidate wheel")
    parser.add_argument("--base-revision", required=True, help="full commit SHA of the baseline build")
    parser.add_argument("--change", required=True, help="short description of the release-profile change")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter used to run bench.py")
    parser.add_argument("--maturin", default="1.16.0", help="maturin version used to build both wheels")
    parser.add_argument("--zopflipy", default="1.14", help="zopflipy version used to repack both wheels")
    parser.add_argument("--output", type=Path, help="optional path to write JSON (defaults to stdout)")
    args = parser.parse_args()

    checker = runpy.run_path(str(ROOT / "tools" / "wheel_size.py"))
    before_path = checker["only_wheel"](args.baseline)
    after_path = checker["only_wheel"](args.candidate)
    before_wheel = asdict(checker["measure"](before_path))
    after_wheel = asdict(checker["measure"](after_path))

    before_bench = _run_benchmark(before_path, args.python)
    after_bench = _run_benchmark(after_path, args.python)

    record = {
        "base_revision": args.base_revision,
        "change": args.change,
        "target": _rust_target(),
        "host": _host_description(),
        "rustc": _rustc_version(),
        "maturin": args.maturin,
        "zopflipy": args.zopflipy,
        "before": {"wheel": before_wheel, "benchmark": before_bench},
        "after": {"wheel": after_wheel, "benchmark": after_bench},
    }
    payload = json.dumps(record, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(payload)


if __name__ == "__main__":
    main()
