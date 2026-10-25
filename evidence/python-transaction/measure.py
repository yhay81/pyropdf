# ruff: noqa: INP001 - a script, not a package.
"""Measure a failing ``doc.transaction()`` at 110, 440, and 1,760 pages (issue #34).

Each page has 20 KiB of content, so that a copy of the document would show in
the peak resident memory. Run from the repository root, once per size, since
the peak never falls: ``uv run python evidence/python-transaction/measure.py``.
"""

from __future__ import annotations

import platform
import resource
import subprocess
import sys
import time

import pyropdf

SIZES = (110, 440, 1_760)
ROUNDS = 201
CONTENT = "0 g " + "1 1 1 1 re f " * 1_600


def document(pages: int) -> bytes:
    """Return ``pages`` pages of ``CONTENT`` in one flat tree."""
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []

    def put(body: str) -> None:
        offsets.append(len(out))
        out.extend(f"{len(offsets)} 0 obj\n{body}\nendobj\n".encode())

    put("<< /Type /Catalog /Pages 2 0 R >>")
    put(f"<< /Type /Pages /Kids [{' '.join(f'{3 + 2 * i} 0 R' for i in range(pages))}] /Count {pages} >>")
    for i in range(pages):
        put(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents {4 + 2 * i} 0 R >>")
        put(f"<< /Length {len(CONTENT)} >>\nstream\n{CONTENT}\nendstream")
    xref = len(out)
    out.extend(f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode())
    out.extend(b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets))
    out.extend(f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def peak_bytes() -> int:
    """Return the peak resident memory of this process."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def measure(pages: int) -> str:
    """Return one table row for ``pages`` pages."""
    data = document(pages)
    doc = pyropdf.open(data)
    _ = len(doc.pages)
    before = peak_bytes()
    times = []
    for _ in range(ROUNDS):
        started = time.perf_counter()
        try:
            with doc.transaction("lost"):
                doc.pages[pages // 2].rotation = 90
                raise RuntimeError  # noqa: TRY301 - the failure under test.
        except RuntimeError:
            pass
        times.append(time.perf_counter() - started)
    grown = peak_bytes() - before
    assert doc.history.steps == ()  # noqa: S101 - the failure kept nothing.
    median = sorted(times)[ROUNDS // 2]
    return f"| {pages:,} | {len(data) / 2**20:.1f} MiB | {grown / 2**20:.2f} MiB | {median * 1e6:.1f} µs |"


def main() -> None:
    """Print the table, measuring each size in a process of its own."""
    if len(sys.argv) > 1:
        print(measure(int(sys.argv[1])))  # noqa: T201
        return
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print()  # noqa: T201
    print("| Pages | File | Peak memory growth over 201 failures | Failure, median |")  # noqa: T201
    print("| ---: | ---: | ---: | ---: |")  # noqa: T201
    for pages in SIZES:
        row = subprocess.run([sys.executable, __file__, str(pages)], capture_output=True, text=True, check=True)  # noqa: S603
        print(row.stdout.strip())  # noqa: T201


if __name__ == "__main__":
    main()
