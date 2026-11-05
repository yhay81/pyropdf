# ruff: noqa: INP001, T201
"""Measure first-read extraction of shared shading paints (#645)."""

from __future__ import annotations

import gc
import json
import platform
import statistics
import time

import pyropdf


def source(count: int) -> bytes:
    """Build a page whose paths all use one axial shading, without rendering."""
    content = b"/Pattern cs /P scn\n" + b"0 0 1 1 re f\n" * count
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
            b"/Resources << /Pattern << /P 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
        b"<< /Type /Pattern /PatternType 2 /Shading 6 0 R >>",
        b"<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 100 0] /Extend [true true] /Function 7 0 R >>",
        b"<< /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >>",
    ]
    data = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(data)


def measure(count: int, *, columns: bool) -> float:
    """Median of three cold reads, with opening and collection outside timing."""
    data = source(count)
    elapsed = []
    for _ in range(3):
        gc.collect()
        with pyropdf.open(data) as doc:
            page = doc.pages[0]
            start = time.perf_counter()
            result = (
                page.paths(max_elements=None)
                if columns
                else page.content(text_runs=False, images=False, max_elements=None)
            )
            elapsed.append(time.perf_counter() - start)
            if len(result) != count:
                msg = "the fixture did not retain every painted path"
                raise AssertionError(msg)
            del result
    return round(statistics.median(elapsed), 6)


def main() -> None:
    """Print machine details and times for columns and Python item records."""
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "seconds": {
                    str(count): {"paths": measure(count, columns=True), "content": measure(count, columns=False)}
                    for count in (1000, 10000, 100000)
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
