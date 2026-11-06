# ruff: noqa: INP001, T201, S603 - standalone benchmark and its fixed worker protocol
"""Interleave the official raw-pixel operations across six isolated interpreters.

The JSON manifest is a list of six records with name, engine, python, and
(for pyropdf) wheel, plus optional threads for the experimental wheel. Run
this script with the manifest and an output path. Each case uses a balanced
six-row order so each artifact appears once in each position and each
ordered pair occurs once at adjacent positions across rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import runpy
import statistics
import subprocess
import sys
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parents[1] / "tests/assets/real_world"
ROW = (0, 1, 5, 2, 4, 3)


def serve(engine: str) -> None:
    """Use bench.py's actual operation on a fresh document for each request."""
    setup = runpy.run_path(str(HERE / "bench.py"))[{"pymupdf": "_pymupdf", "pyropdf": "_pyropdf"}[engine]]
    version, opener, operations = setup()
    metadata = {"version": version, "python": platform.python_version()}
    if engine == "pyropdf":
        from pyropdf import _core  # noqa: PLC0415 - installed only in pyropdf workers

        metadata["extension_sha256"] = hashlib.sha256(Path(_core.__file__).read_bytes()).hexdigest()
    print(json.dumps(metadata), flush=True)
    for line in sys.stdin:
        name, dpi = json.loads(line)
        doc = opener(CORPUS / f"{name}.pdf")
        try:
            operation = operations[f"render_{dpi}"]
            start = time.perf_counter()
            operation(doc)
            elapsed = (time.perf_counter() - start) * 1000
        finally:
            doc.close()
        print(json.dumps(elapsed), flush=True)


class Worker:
    """One persistent worker for one artifact and configuration."""

    def __init__(self, spec: dict, stack: ExitStack) -> None:
        """Start and verify the isolated worker."""
        env = os.environ.copy()
        env.pop("PYROPDF_PROBE_THREADS", None)
        if "threads" in spec:
            env["PYROPDF_PROBE_THREADS"] = str(spec["threads"])
        self.process = subprocess.Popen(
            [spec["python"], __file__, "--worker", spec["engine"]],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        stack.callback(self.close)
        self.metadata = json.loads(self.read())
        self.metadata["threads_option"] = spec.get("threads")
        if spec["engine"] == "pyropdf":
            wheel = Path(spec["wheel"])
            with zipfile.ZipFile(wheel) as archive:
                (extension,) = [n for n in archive.namelist() if n.endswith((".pyd", ".so"))]
                digest = hashlib.sha256(archive.read(extension)).hexdigest()
            if self.metadata["extension_sha256"] != digest:
                message = f"Installed extension differs from {wheel.name}"
                raise ValueError(message)
            self.metadata["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()

    def read(self) -> str:
        """Read one response line."""
        assert self.process.stdout is not None  # noqa: S101 - pipe above
        return self.process.stdout.readline()

    def render(self, name: str, dpi: int) -> float:
        """Time one complete document render."""
        assert self.process.stdin is not None  # noqa: S101 - pipe above
        self.process.stdin.write(json.dumps([name, dpi]) + "\n")
        self.process.stdin.flush()
        return float(self.read())

    def close(self) -> None:
        """Close the input and wait for the worker to exit."""
        assert self.process.stdin is not None  # noqa: S101 - pipe above
        assert self.process.stdout is not None  # noqa: S101 - pipe above
        self.process.stdin.close()
        self.process.wait(timeout=30)
        self.process.stdout.close()


def compare(specs: list[dict], output: Path) -> None:
    """Collect every sample and the per-document minimum and median totals."""
    names = [s["name"] for s in specs]
    if len(specs) != len(ROW) or len(set(names)) != len(ROW):
        msg = "Provide exactly six distinct configurations"
        raise ValueError(msg)
    orders = [[names[(i + offset) % len(ROW)] for i in ROW] for offset in range(len(ROW))]
    samples = {}
    with ExitStack() as stack:
        workers = {s["name"]: Worker(s, stack) for s in specs}
        artifacts = {n: w.metadata for n, w in workers.items()}
        if len({info["python"] for info in artifacts.values()}) != 1:
            msg = "Python versions differ"
            raise ValueError(msg)
        for path in sorted(CORPUS.glob("*.pdf")):
            for dpi in (72, 144):
                case = f"{path.stem}/{dpi}"
                row = samples[case] = {n: [] for n in names}
                for order in orders:
                    for name in order:
                        row[name].append(workers[name].render(path.stem, dpi))
                print(case, {n: round(min(v), 1) for n, v in row.items()}, flush=True)
    totals = {
        str(dpi): {
            stat: {n: sum(reduce(v[n]) for k, v in samples.items() if k.endswith(f"/{dpi}")) for n in names}
            for stat, reduce in (("best", min), ("median", statistics.median))
        }
        for dpi in (72, 144)
    }
    result = {
        "platform": platform.platform(),
        "orders": orders,
        "artifacts": artifacts,
        "samples_ms": samples,
        "totals": totals,
    }
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1] == "--worker":
        serve(sys.argv[2])
    else:
        compare(json.loads(Path(sys.argv[1]).read_bytes()), Path(sys.argv[2]))
