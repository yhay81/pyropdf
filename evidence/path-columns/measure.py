# ruff: noqa: INP001, T201 - a script, not a package; it prints its results.
"""Measure the paths of a page as columns against one record per path (issue #606).

Run from the repository root:

    uv run --group oracles --with numpy python evidence/path-columns/measure.py

It prints a Markdown table; results/<system>-<machine>.md keeps one run.
Each measurement runs in a process of its own, because pyropdf memoizes a
page's interpretation and the peak resident set only grows: the process
opens the document, renders the page at 36 dpi, as the issue's reproduction
does, and then times one call and records how far the peak resident set
grew and how much of it the result still holds. A row is the median of
``RUNS`` processes. PyMuPDF and NumPy are optional; without them their
rows read ``n/a``.
"""

from __future__ import annotations

import gc
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "tests" / "assets" / "real_world"
RUNS = 5
LINES = (33_000, 200_000)
METHODS = (
    "pixmap",
    "records",
    "records_without_gc",
    "columns",
    "columns_numpy",
    "get_cdrawings",
    "get_drawings",
)


def page_pdf(count: int) -> bytes:
    """Return an A1 page of ``count`` short lines, each its own stroked path, one in ten red, as CAD output draws them.

    The page of the issue's reproduction.
    """
    parts = [b"0.5 w"]
    for i in range(count):
        x, y = 10 + (i % 400) * 5, 10 + (i // 400) * 5
        paint = b"1 0 0 RG" if i % 10 == 0 else b"0 0 0 RG"
        parts.append(b"%s %d %d m %d %d l S" % (paint, x, y, x + 4, y + 3))
    stream = b"\n".join(parts)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 2384 1684] /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out, offsets = bytearray(b"%PDF-1.7\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def memory() -> tuple[int, int]:
    """Return the process's resident set and its peak, in bytes.

    Windows reports the working set; Linux and macOS report only the peak,
    so the resident set reads 0 there.
    """
    if sys.platform == "win32":
        import ctypes  # noqa: PLC0415 - Windows only
        from ctypes import wintypes  # noqa: PLC0415 - Windows only

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        if not kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WorkingSetSize, counters.PeakWorkingSetSize
    import resource  # noqa: PLC0415 - not on Windows

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return 0, peak if sys.platform == "darwin" else peak * 1024


def document_bytes(workload: str) -> bytes:
    """Return the PDF of ``workload``: ``lines-<count>`` or a corpus file's name."""
    if workload.startswith("lines-"):
        return page_pdf(int(workload.removeprefix("lines-")))
    return (ASSETS / workload).read_bytes()


def measure(method: str, workload: str, index: int) -> dict[str, Any]:  # noqa: C901 - one branch per method
    """Time one call of ``method`` on page ``index`` of ``workload``, in this process."""
    data = document_bytes(workload)
    if method.startswith("get_"):
        try:
            import pymupdf  # noqa: PLC0415 - optional
        except ImportError:
            return {"skipped": "PyMuPDF is not installed"}
        document = pymupdf.open(stream=data)
        page = document[index]
        page.get_pixmap(dpi=36)
        call = getattr(page, method)
    else:
        import pyropdf  # noqa: PLC0415 - imported here to keep the parent light

        document = pyropdf.open(data)
        page = document.pages[index]
        if method == "pixmap":
            # The first render of a fresh document, as the issue times it.

            def call() -> object:
                return page.pixmap(dpi=36)

        elif method.startswith("records"):

            def call() -> object:
                return page.content(text_runs=False, images=False, max_elements=None).paths

        else:
            if not hasattr(page, "paths"):
                return {"skipped": "this pyropdf has no Page.paths"}
            np = None
            if method == "columns_numpy":
                try:
                    import numpy as np  # noqa: PLC0415 - optional
                except ImportError:
                    return {"skipped": "NumPy is not installed"}

            def call() -> object:
                paths = page.paths(max_elements=None)
                if np is not None:
                    # Every segment's end point and path, as arrays over the columns.
                    return paths, [np.asarray(paths.column(name)) for name in ("to_x", "to_y", "path")]
                return paths

        if method != "pixmap":
            page.pixmap(dpi=36)
    gc.collect()
    if method == "records_without_gc":
        gc.disable()
    held_before, peak_before = memory()
    started, cpu = time.perf_counter(), time.process_time()
    result = call()
    seconds, cpu = time.perf_counter() - started, time.process_time() - cpu
    held_after, peak_after = memory()
    gc.enable()
    del result
    return {
        "seconds": seconds,
        "cpu": cpu,
        "peak": max(0, peak_after - peak_before),
        "held": max(0, held_after - held_before),
    }


def count(workload: str, index: int) -> tuple[int, int]:
    """Return the paths and segments of page ``index`` of ``workload``."""
    import pyropdf  # noqa: PLC0415 - imported here to keep the parent light

    page = pyropdf.open(document_bytes(workload)).pages[index]
    paths = page.content(text_runs=False, images=False, max_elements=None).paths
    return len(paths), sum(len(path.segments) for path in paths)


def most_paths() -> tuple[str, int]:
    """Return the corpus file and page index with the most paths."""
    import pyropdf  # noqa: PLC0415 - imported here to keep the parent light

    best = ("", 0, -1)
    for path in sorted(ASSETS.glob("*.pdf")):
        try:
            document = pyropdf.open(path)
        except pyropdf.PdfError:
            continue
        for page in document.pages:
            paths = len(page.content(text_runs=False, images=False, max_elements=None).paths)
            if paths > best[2]:
                best = (path.name, page.index, paths)
    return best[0], best[1]


def child(method: str, workload: str, index: int) -> dict[str, Any]:
    """Run :func:`measure` in a process of its own and return its result."""
    output = subprocess.run(  # noqa: S603 - this script with its own arguments
        [sys.executable, __file__, "--one", method, workload, str(index)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(output.splitlines()[-1])


def cell(results: list[dict[str, Any]], key: str, paths: int) -> str:
    """Return the median of ``key`` over ``results``, memory also per path of ``paths``."""
    if "skipped" in results[0]:
        return "n/a"
    value = statistics.median(result[key] for result in results)
    if key in {"seconds", "cpu"}:
        return f"{value:.3f} s"
    return f"{value / 2**20:.1f} MiB, {value / paths:,.0f} B a path"


def memoized(workload: str, index: int) -> tuple[float, float, int]:
    """Return the median seconds of ``content().paths`` and of ``paths()`` once the interpretation is memoized.

    Also return the bytes of the columns.
    """
    import pyropdf  # noqa: PLC0415 - imported here to keep the parent light

    page = pyropdf.open(document_bytes(workload)).pages[index]
    page.content(text_runs=False, images=False, max_elements=None)
    times: dict[str, list[float]] = {"records": [], "columns": []}
    for _ in range(RUNS):
        for name, call in (
            ("records", lambda: page.content(text_runs=False, images=False, max_elements=None).paths),
            ("columns", lambda: page.paths(max_elements=None)),
        ):
            started = time.perf_counter()
            call()
            times[name].append(time.perf_counter() - started)
    paths = page.paths(max_elements=None)
    size = sum(paths.column(name).nbytes for name in paths.names)
    return statistics.median(times["records"]), statistics.median(times["columns"]), size


def main() -> None:
    """Print the table."""
    import pyropdf  # noqa: PLC0415 - imported here to keep the children light

    try:
        import pymupdf  # noqa: PLC0415 - optional

        mupdf = f"PyMuPDF {pymupdf.__version__} (MuPDF {pymupdf.mupdf_version})"
    except ImportError:
        mupdf = "PyMuPDF not installed"
    try:
        import numpy as np  # noqa: PLC0415 - optional

        numpy_version = f"NumPy {np.__version__}"
    except ImportError:
        numpy_version = "NumPy not installed"
    system = f"{platform.system()} {platform.release()} {platform.machine()}"
    print(f"- pyropdf {pyropdf.__version__}, Python {platform.python_version()} on {system}")
    print(f"- {platform.processor() or 'unknown processor'}")
    print(f"- {mupdf}; {numpy_version}")
    print(f"- The median of {RUNS} processes per row. `Time` is wall-clock time and `CPU time` the process's,")
    print("  `peak` is how far the call grew the peak resident set, and `held` how much more the resident set")
    print("  holds after it, with its result alive (Windows only).")
    print()
    corpus = most_paths()
    workloads = [(f"lines-{lines}", 0) for lines in LINES] + [corpus]
    for workload, index in workloads:
        paths, segments = count(workload, index)
        title = f"{workload}, page {index + 1}" if not workload.startswith("lines-") else workload
        print(f"### {title}: {paths:,} paths, {segments:,} segments")
        print()
        print("| Call | Time | CPU time | Peak | Held |")
        print("| --- | ---: | ---: | ---: | ---: |")
        for method in METHODS:
            results = [child(method, workload, index) for _ in range(RUNS)]
            print(
                f"| `{method}` | {cell(results, 'seconds', paths)} | {cell(results, 'cpu', paths)} "
                f"| {cell(results, 'peak', paths)} | {cell(results, 'held', paths)} |"
            )
        records, columns, size = memoized(workload, index)
        print()
        print(
            f"Once the interpretation is memoized, `content().paths` takes {records * 1e3:,.1f} ms and `paths()` "
            f"{columns * 1e3:,.2f} ms. The columns hold {size / 2**20:.1f} MiB, {size / paths:,.0f} bytes a path."
        )
        print()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--one"]:
        print(json.dumps(measure(sys.argv[2], sys.argv[3], int(sys.argv[4]))))
    else:
        main()
