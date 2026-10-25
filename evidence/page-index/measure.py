# ruff: noqa: INP001 - a script, not a package.
"""Measure ``page.index`` and ``doc.pages[i]`` at 110, 440, and 1,760 pages (issue #33).

Run from the repository root: ``uv run python evidence/page-index/measure.py``.
It prints a Markdown table; results/<system>-<machine>.md keeps one run.
"""

from __future__ import annotations

import platform
import time
from typing import TYPE_CHECKING

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable

SIZES = (110, 440, 1_760)
CALLS = 1_000
ROUNDS = 21


def numbered(count: int) -> bytes:
    """Return ``count`` pages in one flat tree, as tests/test_pages.py builds them."""
    out = bytearray(b"%PDF-1.7\n")
    kids = " ".join(f"{3 + i} 0 R" for i in range(count))
    bodies = ["<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {count} >>"]
    bodies += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {100 + i} 500] >>" for i in range(count)]
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def median_seconds(action: Callable[[], object]) -> float:
    """Return the median time of one call of ``action`` over ``ROUNDS`` rounds of ``CALLS`` calls."""
    times = []
    for _ in range(ROUNDS):
        started = time.perf_counter()
        for _ in range(CALLS):
            action()
        times.append((time.perf_counter() - started) / CALLS)
    return sorted(times)[ROUNDS // 2]


def main() -> None:
    """Print the table."""
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print()  # noqa: T201
    print("| Pages | `page.index` | `doc.pages[i]` | After an edit of another page: `page.index` |")  # noqa: T201
    print("| ---: | ---: | ---: | ---: |")  # noqa: T201
    for count in SIZES:
        doc = pyropdf.open(numbered(count))
        page = doc.pages[count // 2]
        other = doc.pages[0]
        index = median_seconds(lambda page=page: page.index)
        get = median_seconds(lambda doc=doc, count=count: doc.pages[count // 2])

        # Rotating another page rewrites a page object: the page index is
        # walked again on the first read after each edit.
        def edit_then_index(page: pyropdf.Page = page, other: pyropdf.Page = other) -> None:
            other.rotation = (other.rotation + 90) % 360
            _ = page.index

        edited = median_seconds(edit_then_index)
        print(f"| {count:,} | {index * 1e6:.2f} µs | {get * 1e6:.2f} µs | {edited * 1e6:.1f} µs |")  # noqa: T201


if __name__ == "__main__":
    main()
