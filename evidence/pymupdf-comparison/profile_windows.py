# ruff: noqa: INP001, T201, S603 - standalone profiler, launches its own fixed worker/helper
"""Sample a Windows x64 release wheel using its matching local PDB.

Run this script with the wheel environment's Python, after compiling
sample_windows.cpp into target/365-profile-windows/sample_windows.exe.
Only the child renderer's main thread is inspected. These are wall-clock
leaf-PC samples, not inclusive call stacks or an uninstrumented timing test.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "target/365-profile-windows"
SECONDS = 15


def worker(path: Path, dpi: int) -> None:
    """Render with the public defaults, retaining the process for symbol lookup."""
    import pyropdf  # noqa: PLC0415

    print(json.dumps({"pid": os.getpid(), "tid": ctypes.windll.kernel32.GetCurrentThreadId()}), flush=True)
    sys.stdin.readline()
    passes = 0
    end = time.perf_counter() + SECONDS + 3
    while time.perf_counter() < end:
        with pyropdf.open(path) as doc:
            for page in doc.pages:
                bytes(page.pixmap(dpi=dpi))
        passes += 1
    print(passes, flush=True)
    sys.stdin.readline()


def sample(document: str, dpi: int, symbols: Path) -> dict:
    """Launch, sample, resolve symbols, and clean up one owned worker."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    worker_handle = None
    renderer = subprocess.Popen(
        [sys.executable, __file__, "--worker", str(ROOT / f"tests/assets/real_world/{document}.pdf"), str(dpi)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    sampler = None
    try:
        assert renderer.stdout is not None  # noqa: S101 - fixed PIPE above
        assert renderer.stdin is not None  # noqa: S101 - fixed PIPE above
        identity = json.loads(renderer.stdout.readline())
        # The venv launcher PID differs from the actual Python worker on Windows.
        # Retain its original handle so failure cleanup cannot act on a reused PID.
        worker_handle = kernel.OpenProcess(0x100001, 0, identity["pid"])
        if not worker_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        sampler = subprocess.Popen(
            [
                str(TARGET / "sample_windows.exe"),
                str(identity["pid"]),
                str(identity["tid"]),
                str(SECONDS),
                str(symbols),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert sampler.stdout is not None  # noqa: S101 - fixed PIPE above
        if sampler.stdout.readline().strip() != "ready":
            message = "Sampler failed to initialize"
            raise RuntimeError(message)
        renderer.stdin.write("start\n")
        renderer.stdin.flush()
        output, _ = sampler.communicate("start\n", timeout=SECONDS + 60)
        if sampler.returncode:
            message = f"Sampler failed: {sampler.returncode}"
            raise RuntimeError(message)
        result = json.loads(output)
        rendered, _ = renderer.communicate("exit\n", timeout=60)
        if renderer.returncode:
            message = f"Renderer failed: {renderer.returncode}"
            raise RuntimeError(message)
        result["worker_passes"] = int(rendered.strip())
        result["worker_seconds"] = SECONDS + 3
        result["seconds_sampled"] = SECONDS
        return result
    finally:
        if worker_handle is not None:
            if renderer.poll() is None:
                kernel.TerminateProcess(worker_handle, 1)
            kernel.CloseHandle(worker_handle)
        for process in (sampler, renderer):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()


def main() -> None:
    """Collect the four rendering cases as a reproducible JSON artifact."""
    if platform.system() != "Windows" or platform.machine().lower() != "amd64":
        message = "This profiler requires Windows x64"
        raise RuntimeError(message)
    if sys.argv[1:2] == ["--worker"]:
        worker(Path(sys.argv[2]), int(sys.argv[3]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--symbols", type=Path, required=True, help="Directory with the wheel's matching PDB")
    args = parser.parse_args()
    if not args.symbols.is_dir():
        parser.error("--symbols must name an existing directory")
    results = {}
    for document in ("bill-hr815", "usrguide"):
        for dpi in (72, 144):
            case = f"{document}-{dpi}"
            results[case] = sample(document, dpi, args.symbols.resolve())
            print(case, sorted(results[case]["symbols"].items(), key=lambda item: -item[1])[:8], flush=True)
    import pyropdf  # noqa: PLC0415
    from pyropdf import _core  # noqa: PLC0415

    output = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "pyropdf": pyropdf.__version__,
        "extension_sha256": hashlib.sha256(Path(_core.__file__).read_bytes()).hexdigest(),
        "method": "Wall-clock main-thread leaf PC, nominal 5 ms interval; no call stacks",
        "results": results,
    }
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
