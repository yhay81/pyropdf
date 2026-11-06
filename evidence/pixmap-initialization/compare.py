# ruff: noqa: INP001, T201, S603 - standalone benchmark, launches its own worker
"""Alternate baseline/candidate release wheels; retain all timings and pixel hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parents[1] / "tests/assets/real_world"
RUNS = 5


class Worker:
    """A persistent interpreter, with fresh documents for each timing sample."""

    def __init__(self, python: Path, wheel: Path) -> None:
        """Launch the installed artifact and verify its extension against the wheel."""
        with zipfile.ZipFile(wheel) as archive:
            extensions = [name for name in archive.namelist() if name.endswith((".pyd", ".so"))]
            if len(extensions) != 1:
                message = "Expected exactly one extension in the wheel"
                raise RuntimeError(message)
            expected = hashlib.sha256(archive.read(extensions[0])).hexdigest()
        self.process = subprocess.Popen(
            [str(python), str(HERE / "worker.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            self.metadata = self.read()
        except BaseException:
            self.close()
            raise
        if self.metadata["extension_sha256"] != expected:
            self.close()
            message = "Installed extension does not match the supplied wheel"
            raise RuntimeError(message)
        self.metadata["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()

    def read(self) -> dict:
        """Read one response or report the failed worker."""
        assert self.process.stdout is not None  # noqa: S101 - fixed PIPE above
        line = self.process.stdout.readline()
        if not line:
            message = f"Worker exited without a response: {self.process.poll()}"
            raise RuntimeError(message)
        return json.loads(line)

    def request(self, payload: dict) -> dict:
        """Write a known corpus request and read its result."""
        assert self.process.stdin is not None  # noqa: S101 - fixed PIPE above
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        return self.read()

    def close(self) -> None:
        """End the worker after its last completed request."""
        assert self.process.stdin is not None  # noqa: S101 - fixed PIPE above
        self.process.stdin.close()
        self.process.wait(timeout=30)
        assert self.process.stdout is not None  # noqa: S101 - fixed PIPE above
        self.process.stdout.close()


def measure(workers: dict[str, Worker], *, candidate_first: bool) -> dict:
    """Collect five alternating full-render samples per document and DPI."""
    samples = {}
    for path in sorted(CORPUS.glob("*.pdf")):
        for dpi in (72, 144):
            case = f"{path.stem}/{dpi}"
            samples[case] = {name: [] for name in workers}
            for run in range(RUNS):
                order = ("baseline", "candidate") if (run + candidate_first) % 2 == 0 else ("candidate", "baseline")
                for name in order:
                    result = workers[name].request({"op": "render", "document": path.stem, "dpi": dpi})
                    samples[case][name].append(result["milliseconds"])
            print(case, {name: round(min(values), 3) for name, values in samples[case].items()}, flush=True)
    return samples


def main() -> None:
    """Measure the same raw-pixel operation under both supplied artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate"):
        parser.add_argument(f"--{name}-python", type=Path, required=True)
        parser.add_argument(f"--{name}-wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-first", action="store_true", help="Reverse the alternating sample order")
    parser.add_argument("--timings-only", action="store_true", help="Repeat timings after an existing pixel comparison")
    args = parser.parse_args()
    workers = {}
    try:
        for name in ("baseline", "candidate"):
            workers[name] = Worker(getattr(args, f"{name}_python"), getattr(args, f"{name}_wheel"))
        if len({worker.metadata["python"] for worker in workers.values()}) != 1:
            message = "Both wheel environments must use the same Python version"
            raise RuntimeError(message)
        samples = measure(workers, candidate_first=args.candidate_first)
        pixel_hashes = (
            {} if args.timings_only else {name: worker.request({"op": "hash"}) for name, worker in workers.items()}
        )
        matches = None if args.timings_only else pixel_hashes["baseline"] == pixel_hashes["candidate"]
        output = {
            "platform": platform.platform(),
            "runs": RUNS,
            "candidate_first": args.candidate_first,
            "artifacts": {name: worker.metadata for name, worker in workers.items()},
            "samples_ms": samples,
            "pixel_hashes_match": matches,
            "pixel_cases": None if args.timings_only else len(pixel_hashes["baseline"]),
            "totals": {
                str(dpi): {
                    method: {
                        name: sum(reduce(row[name]) for case, row in samples.items() if case.endswith(f"/{dpi}"))
                        for name in workers
                    }
                    for method, reduce in (("best", min), ("median", statistics.median))
                }
                for dpi in (72, 144)
            },
        }
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "comparison.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        if not args.timings_only:
            (args.output / "pixel-hashes.json").write_text(
                json.dumps(pixel_hashes["baseline"] if matches else pixel_hashes, indent=2) + "\n", encoding="utf-8"
            )
        print(
            json.dumps(
                {"totals": output["totals"], "pixel_hashes_match": matches, "pixel_cases": output["pixel_cases"]}
            )
        )
        if matches is False:
            message = "Candidate pixels differ from baseline; inspect the recorded hashes"
            raise RuntimeError(message)
    finally:
        for worker in workers.values():
            worker.close()


if __name__ == "__main__":
    main()
