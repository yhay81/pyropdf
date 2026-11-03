# ruff: noqa: INP001, T201 - a script that prints its tables.
"""Measure how far content from far away strays in a small raster (issue #605).

Run from the repository root: ``uv run python evidence/clip-precision-605/measure.py``.
It prints Markdown tables; results/<system>-<machine>.md keeps one run.

vello_cpu takes paths and paint transforms in ``f64`` and rasterizes in
``f32``, so content that reaches a raster from far away lands where ``f32``
rounds it. Each case draws the same pixels twice: once from far away
(*global*), and once from points just outside the raster on the same lines
(*local*), placed in ``f64``, whose rounding lies far below what ``f32``
shows. The far points lie off the binary fractions that ``f32`` holds
exactly, as real content does. The tables give the largest difference of
one sample, in 1/255: for an edge, that is how far it moved across the
pixel, in 1/255 of a pixel; for the shading, which turns from black to white
over one pixel, likewise. The image is split between a black and a white
texel 1/16 of a pixel past a pixel center, so it differs by 255 once the
split moves that far.

*Far content* renders a 64 by 64 pt page at 72 dpi whose far points lie
``distance`` pixels away; it runs on any build. *Clips* render a 64 by 64
pixel clip at the bottom-right corner of an A1 page, whose content comes from
the page's other corners, at the scale that makes the page's longer side
``side`` pixels long; they need a build whose limits apply to the clipped
raster, and stop at the first scale that it refuses.
"""

from __future__ import annotations

import math
import platform
from decimal import Decimal
from typing import TYPE_CHECKING

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    Point = tuple[float, float]
    Case = tuple[tuple[float, float], bytes, bytes, dict[bytes, bytes]]

SIZE = 64
"""The side of every raster, in pixels."""

DIRECTIONS = 64
"""The directions from which the edges of *far content* arrive."""

NUDGE = (0.3141592653589793, 0.2718281828459045)
"""How far each apex lies off a short binary fraction, so that ``f32`` has to round it, in points."""

A1 = (2384.0, 1684.0)
"""The landscape A1 page of the clips, in points."""

GRAY_FUNCTION = b"/Function << /FunctionType 2 /Domain [0 1] /C0 [0] /C1 [1] /N 1 >>"


def number(value: float) -> bytes:
    """Return ``value`` as a PDF real: the shortest decimal that reads back as ``value``, without an exponent."""
    return format(Decimal(repr(value)), "f").encode()


def points(*pairs: Point) -> bytes:
    """Return the coordinates of ``pairs`` separated by spaces."""
    return b" ".join(number(c) for pair in pairs for c in pair)


def build(pages: Sequence[tuple[tuple[float, float], bytes, dict[bytes, bytes]]]) -> bytes:
    """Return a PDF of ``pages``: each a size, a content stream, and the objects that its resources name."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    for (width, height), content, named in pages:
        page = len(objects) + 1
        kids.append(page)
        kinds: dict[bytes, list[bytes]] = {b"/XObject": [], b"/Shading": []}
        for at, (name, body) in enumerate(named.items()):
            kind = b"/XObject" if b"/Subtype /Image" in body else b"/Shading"
            kinds[kind].append(b"%s %d 0 R" % (name, page + 2 + at))
        resources = b" ".join(b"%s << %s >>" % (kind, b" ".join(refs)) for kind, refs in kinds.items() if refs)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s] /Resources << %s >> /Contents %d 0 R >>"
            % (number(width), number(height), resources, page + 1)
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        objects.extend(named.values())
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % kid for kid in kids), len(kids))
    out, offsets = bytearray(b"%PDF-1.7\n"), []
    for at, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % at + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def toward(start: Point, end: Point, fraction: float) -> Point:
    """Return the point ``fraction`` of the way from ``start`` to ``end``."""
    return (start[0] + fraction * (end[0] - start[0]), start[1] + fraction * (end[1] - start[1]))


def wedge(apex: Point, base: tuple[Point, Point], near: float | None) -> bytes:
    """Fill the triangle of ``apex`` and ``base``, or with ``near`` its part within ``near`` of the way to the apex."""
    first, second = base
    if near is None:
        return b"%s m %s l %s l h f" % (points(apex), points(first), points(second))
    start, end = toward(first, apex, near), toward(second, apex, near)
    return b"%s m %s l %s l %s l h f" % (points(start), points(first), points(second), points(end))


def line(apex: Point, end: Point, near: float | None, width: float) -> bytes:
    """Stroke ``width`` wide from ``apex`` to ``end``, or with ``near`` from ``near`` of the way back to the apex."""
    start = apex if near is None else toward(end, apex, near)
    return b"%s w %s m %s l S" % (number(width), points(start), points(end))


def worst(cases: Sequence[Case], render: Callable[[pyropdf.Page], pyropdf.Pixmap]) -> int | None:
    """Render each case's global and local page and return the largest difference of a sample, or None if refused."""
    largest = 0
    for size, far, near, named in cases:
        with pyropdf.open(build([(size, far, named), (size, near, named)])) as document:
            try:
                first, second = (bytes(render(page)) for page in document.pages)
            except ValueError:
                return None
        largest = max(largest, *(abs(a - b) for a, b in zip(first, second, strict=True)))
    return largest


def wedges(apexes: Sequence[Point], center: Point, user: Callable[[Point], Point], unit: float) -> list[Case]:
    """Return a wedge and a line from each apex through ``center``, in display space, with ``unit`` points per pixel."""
    cases: list[Case] = []
    for apex in apexes:
        length = max(abs(apex[0] - center[0]), abs(apex[1] - center[1]))
        # The tip lies 48 pixels past the center, and the wedge is 12.5 pixels wide there.
        step = 48 * unit / length
        dx, dy = (center[0] - apex[0]) * step, (center[1] - apex[1]) * step
        tip = (center[0] + dx, center[1] + dy)
        across = (-dy * 6.25 / 48, dx * 6.25 / 48)
        base = (user((tip[0] + across[0], tip[1] + across[1])), user((tip[0] - across[0], tip[1] - across[1])))
        # The local shapes start 128 pixels before the tip, outside the 64 pixel raster.
        near = 2.0 ** -math.floor(math.log2(length / unit / 128))
        cases.append(((0, 0), wedge(user(apex), base, None), wedge(user(apex), base, near), {}))
        # The line is 2 pixels wide.
        far, local = (line(user(apex), user(tip), fraction, 2 * unit) for fraction in (None, near))
        cases.append(((0, 0), far, local, {}))
    return cases


def far_content(distance: float) -> dict[str, int | None]:
    """Return the largest difference per case of *far content* whose far points lie ``distance`` pixels away."""
    center = (32.3125, 32.1875)

    def user(point: Point) -> Point:
        return (point[0], SIZE - point[1])

    apexes = []
    for at in range(DIRECTIONS):
        angle = 2 * math.pi * (at + 0.1) / DIRECTIONS
        dx, dy = math.cos(angle), math.sin(angle)
        apexes.append((center[0] - distance * dx + NUDGE[0], center[1] - distance * dy + NUDGE[1]))
    cases = [((SIZE, SIZE), *case[1:]) for case in wedges(apexes, center, user, 1.0)]
    render: Callable[[pyropdf.Page], pyropdf.Pixmap] = lambda page: page.pixmap()  # noqa: E731
    return {
        "fill and stroke": worst(cases, render),
        "image": worst(images(distance), render),
        "shading": worst(shadings(distance), render),
    }


def images(distance: float) -> list[Case]:
    """Return pages split between a black and a white texel 1/16 pixel past a pixel center, across and down.

    The texels are ``distance`` pixels wide in the global page and 40 in the local one.
    """
    body = (
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray"
        b" /BitsPerComponent 8 /Length 4 >>\nstream\n%s\nendstream"
    )
    split = 32.5 + 1 / 16

    def place(half: float, x: float, y: float) -> bytes:
        return b"q %s 0 0 %s %s %s cm /Im1 Do Q" % (number(2 * half), number(2 * half), number(x), number(y))

    across = {b"/Im1": body % b"\x00\xff\x00\xff"}
    down = {b"/Im1": body % b"\x00\x00\xff\xff"}
    return [
        ((SIZE, SIZE), place(distance, split - distance, 32 - distance), place(40, split - 40, -8), across),
        ((SIZE, SIZE), place(distance, 32 - distance, SIZE - split - distance), place(40, -8, SIZE - split - 40), down),
    ]


def shadings(distance: float) -> list[Case]:
    """Return pages of an axial shading from black to white over one pixel, across and down.

    The shading's coordinates lie ``distance`` pixels away in the global page, which ``cm`` moves back.
    """

    def shading(start: Point, end: Point) -> bytes:
        return b"<< /ShadingType 2 /ColorSpace /DeviceGray /Coords [%s] /Extend [true true] %s >>" % (
            points(start, end),
            GRAY_FUNCTION,
        )

    edge = 32 + 1 / 16
    return [
        (
            (SIZE, SIZE),
            b"q 1 0 0 1 %s 0 cm /Sh1 sh Q" % number(-distance),
            b"/Sh2 sh",
            {
                b"/Sh1": shading((edge + distance, 0), (edge + 1 + distance, 0)),
                b"/Sh2": shading((edge, 0), (edge + 1, 0)),
            },
        ),
        (
            (SIZE, SIZE),
            b"q 1 0 0 1 0 %s cm /Sh1 sh Q" % number(distance),
            b"/Sh2 sh",
            {
                b"/Sh1": shading((0, SIZE - edge - distance), (0, SIZE - edge - 1 - distance)),
                b"/Sh2": shading((0, SIZE - edge), (0, SIZE - edge - 1)),
            },
        ),
    ]


def clips(side: float) -> int | None:
    """Return the largest difference of a clip at the far corner of an A1 page ``side`` pixels long.

    None means that the build refuses the clip.
    """
    width, height = A1
    scale = side / width
    unit = 1 / scale
    center = (width - 32.3125 * unit, height - 31.8125 * unit)

    def user(point: Point) -> Point:
        return (point[0], height - point[1])

    # Next to the page's three other corners and the middles of its far edges.
    apexes = [
        (x + NUDGE[0], y + NUDGE[1])
        for x, y in [(0.0, 0.0), (0.0, height - 1), (width - 1, 0.0), (0.0, height / 2), (width / 2, 0.0)]
    ]
    cases = [(A1, *case[1:]) for case in wedges(apexes, center, user, unit)]
    clip = (width - SIZE * unit, height - SIZE * unit, width, height)
    return worst(cases, lambda page: page.pixmap(matrix=(scale, 0, 0, scale), clip=clip))


def main() -> None:
    """Print the tables."""
    system = f"{platform.system()} {platform.machine()}"
    print(f"pyropdf {pyropdf.__version__}, Python {platform.python_version()}, {system}")
    print()
    print("## Far content\n")
    rows = [(k, far_content(2.0**k)) for k in range(8, 27)]
    names = list(rows[0][1])
    print("| distance (px) | " + " | ".join(names) + " |")
    print("| ---: | " + " | ".join("---:" for _ in names) + " |")
    for k, results in rows:
        print(f"| 2^{k} | " + " | ".join(str(results[name]) for name in names) + " |", flush=True)
    print("\n## Clips\n")
    print("| side (px) | fill and stroke |")
    print("| ---: | ---: |")
    for k in range(12, 27):
        result = clips(2.0**k)
        print(f"| 2^{k} | {'refused' if result is None else result} |", flush=True)
        if result is None:
            break


if __name__ == "__main__":
    main()
