# ruff: noqa: INP001 - a script, not a package.
"""Measure page deletion and selection against document size and references (issue #180).

Run from the repository root: ``uv run python evidence/page-deletion/measure.py``.
It prints Markdown tables; results/<system>-<machine>.md keeps one run.
"""

from __future__ import annotations

import platform
import time
from typing import TYPE_CHECKING

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable

SIZES = (1_000, 4_000, 16_000, 64_000)
LINKS = (0, 100, 1_000, 10_000)
LINKED_PAGES = 4_000
ROUNDS = 7
FANOUT = 64


def build(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def document(count: int, *, tagged: str = "no", links: int = 0) -> bytes:
    """Return ``count`` pages in one flat tree.

    ``tagged`` other than ``"no"`` gives each page one structure element that
    marks its content, with the parent tree that maps the page back to it:
    as kids of the structure tree root and in one parent tree leaf for
    ``"flat"``, or in sections of ``FANOUT`` elements and in parent tree
    leaves of ``FANOUT`` pages for ``"nested"``. ``links`` puts that many link
    annotations on the first page, each leading to the middle page.
    """
    first_page = 5
    first_element = first_page + count
    first_link = first_element + count
    first_section = first_link + links
    groups = range(0, count, FANOUT)
    first_leaf = first_section + len(groups)
    middle = first_page + count // 2
    kids = " ".join(f"{first_page + i} 0 R" for i in range(count))
    catalog = "<< /Type /Catalog /Pages 2 0 R"
    if tagged != "no":
        catalog += " /StructTreeRoot 3 0 R /MarkInfo << /Marked true >>"
    objects = {1: catalog + " >>", 2: f"<< /Type /Pages /Kids [{kids}] /Count {count} >>"}

    def nums(pages: range) -> str:
        return "/Nums [" + " ".join(f"{i} [{first_element + i} 0 R]" for i in pages) + "]"

    if tagged == "flat":
        elements = " ".join(f"{first_element + i} 0 R" for i in range(count))
        objects[3] = f"<< /Type /StructTreeRoot /K [{elements}] /ParentTree 4 0 R >>"
        objects[4] = f"<< {nums(range(count))} >>"
    if tagged == "nested":
        sections = " ".join(f"{first_section + g} 0 R" for g in range(len(groups)))
        objects[3] = f"<< /Type /StructTreeRoot /K [{sections}] /ParentTree 4 0 R >>"
        objects[4] = "<< /Kids [" + " ".join(f"{first_leaf + g} 0 R" for g in range(len(groups))) + "] >>"
        for g, start in enumerate(groups):
            pages = range(start, min(start + FANOUT, count))
            elements = " ".join(f"{first_element + i} 0 R" for i in pages)
            objects[first_section + g] = f"<< /Type /StructElem /S /Sect /P 3 0 R /K [{elements}] >>"
            objects[first_leaf + g] = f"<< /Limits [{pages[0]} {pages[-1]}] {nums(pages)} >>"
    for i in range(count):
        page = "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        if tagged != "no":
            parent = 3 if tagged == "flat" else first_section + i // FANOUT
            page += f" /StructParents {i}"
            objects[first_element + i] = f"<< /Type /StructElem /S /P /P {parent} 0 R /Pg {first_page + i} 0 R /K 0 >>"
        if i == 0 and links:
            page += " /Annots [" + " ".join(f"{first_link + j} 0 R" for j in range(links)) + "]"
        objects[first_page + i] = page + " >>"
    for j in range(links):
        objects[first_link + j] = f"<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] /Dest [{middle} 0 R /Fit] >>"
    return build(objects)


def median(times: list[float]) -> float:
    """Return the median of ``times``."""
    return sorted(times)[len(times) // 2]


def measure(data: bytes, edit: Callable[[pyropdf.Document], object]) -> tuple[float, float]:
    """Return the median time of ``edit`` on a newly opened document, and repeated after an undo."""
    first = []
    repeated = []
    for _ in range(ROUNDS):
        doc = pyropdf.open(data)
        started = time.perf_counter()
        edit(doc)
        first.append(time.perf_counter() - started)
        doc.history.undo()
        started = time.perf_counter()
        edit(doc)
        repeated.append(time.perf_counter() - started)
    return median(first), median(repeated)


def delete_middle(doc: pyropdf.Document) -> None:
    """Delete the middle page."""
    del doc.pages[len(doc.pages) // 2]


def select_reversed(doc: pyropdf.Document) -> None:
    """Keep every page but the middle one, in reverse order."""
    count = len(doc.pages)
    doc.pages.select(index for index in range(count - 1, -1, -1) if index != count // 2)


def ms(seconds: float) -> str:
    """Return ``seconds`` in milliseconds."""
    return f"{seconds * 1e3:.2f} ms"


def main() -> None:
    """Print the tables."""
    print(f"- Python {platform.python_version()} on {platform.system()} {platform.machine()}")  # noqa: T201
    print(f"- Median of {ROUNDS} rounds; first: on a newly opened document; repeated: after an undo")  # noqa: T201
    print()  # noqa: T201
    print("## Against document size")  # noqa: T201
    print()  # noqa: T201
    print("| Pages | Tagged | Delete, first | Delete, repeated | Select, first | Select, repeated |")  # noqa: T201
    print("| ---: | :---: | ---: | ---: | ---: | ---: |")  # noqa: T201
    for count in SIZES:
        for tagged in ("no", "flat", "nested"):
            data = document(count, tagged=tagged)
            deleted = measure(data, delete_middle)
            selected = measure(data, select_reversed)
            row = " | ".join(ms(seconds) for seconds in (*deleted, *selected))
            print(f"| {count:,} | {tagged} | {row} |")  # noqa: T201
    print()  # noqa: T201
    print(f"## Against links to the deleted page, {LINKED_PAGES:,} pages")  # noqa: T201
    print()  # noqa: T201
    print("| Links | Delete, first | Delete, repeated |")  # noqa: T201
    print("| ---: | ---: | ---: |")  # noqa: T201
    for links in LINKS:
        first, repeated = measure(document(LINKED_PAGES, links=links), delete_middle)
        print(f"| {links:,} | {ms(first)} | {ms(repeated)} |")  # noqa: T201


if __name__ == "__main__":
    main()
