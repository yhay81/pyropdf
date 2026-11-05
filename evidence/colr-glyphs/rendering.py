# ruff: noqa: INP001, T201, S603, S101 - a measured experiment on generated PDFs.
"""Reduce COLR shading edges and soft-mask costs for issue #671.

Build ``hayro-probe`` first, then run this with PyMuPDF and pypdfium2::

    uv run --no-sync --with pymupdf==1.28.2 --with pypdfium2==5.14.0
        python evidence/colr-glyphs/rendering.py --upstream PROBE --output DIRECTORY

``--diagnostic PROBE`` compares hayro with the bounds and tracing patches.
``--aligned PROBE`` also applies the native-gradient-origin patch.
``--font FACE`` also reproduces the original emoji measurements; it
needs blackrenderer[skia] and fonttools. Fonts are never copied into evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf
import pypdfium2

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable

UPSTREAM = "3de8bb0401f0410b71f37a52524b7607433ace13"
POINTS = ((10, 35), (50, 35), (90, 35), (10, 34), (50, 34), (90, 34))


def stream(data: str, entries: str = "") -> bytes:
    """Make an uncompressed stream with its exact byte length."""
    encoded = data.encode("ascii")
    return f"<< {entries} /Length {len(encoded)} >>\nstream\n".encode() + encoded + b"\nendstream"


def pdf(objects: list[str | bytes]) -> bytes:
    """Write a small, deterministic PDF independently of pyropdf's writer."""
    result = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        encoded = obj.encode("ascii") if isinstance(obj, str) else obj
        result.extend(f"{number} 0 obj\n".encode() + encoded + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)


def clipped_shading() -> bytes:
    """Paint identical trapezoids with sh, a white sh soft mask, and a fill."""
    contents = ["0.7 0 0.6 rg 0 0 120 40 re f\n"]
    for x, paint in ((0, "/S sh"), (40, "/M gs 1 g 0 0 40 40 re f"), (80, "1 g 0 0 40 40 re f")):
        contents.append(f"q 1 0 0 1 {x} 0 cm\n4.25 4.25 m 28.75 4.25 l 26.75 28.75 l 6.25 28.75 l h W n\n{paint} Q\n")
    return pdf(
        [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 120 40] /Contents 4 0 R "
                "/Resources << /Shading << /S 5 0 R >> /ExtGState << /M 6 0 R >> >> >>"
            ),
            stream("".join(contents)),
            (
                "<< /ShadingType 2 /ColorSpace /DeviceGray /Coords [0 0 40 0] "
                "/Function << /FunctionType 2 /Domain [0 1] /C0 [1] /C1 [1] /N 1 >> /Extend [true true] >>"
            ),
            "<< /SMask << /S /Luminosity /G 7 0 R >> >>",
            stream(
                "/S sh",
                "/Type /XObject /Subtype /Form /BBox [0 0 40 40] "
                "/Group << /S /Transparency /CS /DeviceGray >> /Resources << /Shading << /S 5 0 R >> >>",
            ),
        ]
    )


def small_masks(size: int, count: int, *, masked: bool) -> bytes:
    """Paint count 8 by 8 pt squares, using one translated luminosity mask."""
    contents = []
    for index in range(count):
        x, y = 4 + 10 * (index % 8), 4 + 10 * (index // 8)
        paint = "/M gs 0 g" if masked else "0.5 g"
        contents.append(f"q 1 0 0 1 {x} {y} cm {paint} 0 0 8 8 re f Q\n")
    return pdf(
        [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size} {size}] /Contents 4 0 R "
                "/Resources << /ExtGState << /M 5 0 R >> >> >>"
            ),
            stream("".join(contents)),
            "<< /SMask << /S /Luminosity /G 6 0 R >> >>",
            stream(
                "0.5 g 0 0 8 8 re f",
                "/Type /XObject /Subtype /Form /BBox [0 0 8 8] "
                "/Group << /S /Transparency /CS /DeviceGray >> /Resources << >>",
            ),
        ]
    )


def radial_gradient() -> bytes:
    """Compare extended native gradients with the unextended texture path."""
    contents = ["0 g 0 0 128 32 re f\n"]
    for x, paint in (
        (0, "/S0 sh"),
        (32, "/S1 sh"),
        (64, "/M0 gs 1 g 0 0 32 32 re f"),
        (96, "/M1 gs 1 g 0 0 32 32 re f"),
    ):
        contents.append(f"q 1 0 0 1 {x} 0 cm 0 0 32 32 re W n {paint} Q\n")
    shade = (
        "<< /ShadingType 3 /ColorSpace /DeviceGray /Coords [16 16 8 16 16 12] "
        "/Function << /FunctionType 2 /Domain [0 1] /C0 [0] /C1 [1] /N 1 >> /Extend "
    )
    group = "/Type /XObject /Subtype /Form /BBox [0 0 32 32] /Group << /S /Transparency /CS /DeviceGray >> "
    return pdf(
        [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 128 32] /Contents 4 0 R "
                "/Resources << /Shading << /S0 5 0 R /S1 6 0 R >> /ExtGState << /M0 7 0 R /M1 8 0 R >> >> >>"
            ),
            stream("".join(contents)),
            shade + "[true true] >>",
            shade + "[false false] >>",
            "<< /SMask << /S /Luminosity /G 9 0 R >> >>",
            "<< /SMask << /S /Luminosity /G 10 0 R >> >>",
            stream("/S sh", group + "/Resources << /Shading << /S 5 0 R >> >>"),
            stream("/S sh", group + "/Resources << /Shading << /S 6 0 R >> >>"),
        ]
    )


def timed(render: Callable[[], object], repeats: int) -> dict[str, object]:
    """Time fresh-document renders after one warmup; keep destruction untimed."""
    rendered = render()
    samples = []
    for _ in range(repeats):
        del rendered
        started = time.perf_counter()
        rendered = render()
        samples.append(time.perf_counter() - started)
    del rendered
    return {"seconds": samples, "median_seconds": statistics.median(samples)}


def renderers(data: bytes, scale: float) -> dict[str, Callable[[], object]]:
    """Return independent fresh-document render calls over white."""

    def hayro() -> object:
        return pyropdf.open(data).pages[0].pixmap(dpi=72 * scale)

    def mupdf() -> object:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            return document[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)

    def pdfium() -> object:
        with pypdfium2.PdfDocument(data) as document:
            page = document[0]
            try:
                return page.render(scale=scale)
            finally:
                page.close()

    return {"pyropdf_hayro": hayro, "mupdf": mupdf, "pdfium": pdfium}


def upstream(probe: Path, source: Path, image: Path, repeats: int, scale: float) -> dict[str, object]:
    """Run the standalone upstream renderer; process startup and PNG saving are untimed."""
    result = subprocess.run(
        [str(probe), str(source), str(image), str(repeats), str(scale)],
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(result.stdout)
    values["median_seconds"] = statistics.median(values["seconds"])
    return values


def image_bytes(render: object, path: Path) -> tuple[bytes, int]:
    """Save a renderer result and return RGB samples and width, outside timing."""
    if isinstance(render, pyropdf.Pixmap):
        rgb = render.to_rgb()
        rgb.save(path)
        return bytes(rgb), rgb.width
    if isinstance(render, pymupdf.Pixmap):
        render.save(str(path))
        return render.samples, render.width
    image = render.to_pil().convert("RGB")
    image.save(path)
    return image.tobytes(), image.width


def pixels(data: bytes, width: int, points: tuple[tuple[int, int], ...]) -> list[list[int]]:
    """Read pixel coordinates measured from the upper left."""
    return [list(data[(y * width + x) * 3 : (y * width + x) * 3 + 3]) for x, y in points]


def measure(  # noqa: PLR0913 - one fixture's output, samples, and optional diagnostics.
    data: bytes,
    name: str,
    args: argparse.Namespace,
    *,
    scale: float = 1,
    points: tuple[tuple[int, int], ...] = (),
    include_aligned: bool = False,
) -> dict[str, object]:
    """Save one PDF and record timings and samples for all independent readers."""
    source = args.output / f"{name}.pdf"
    source.write_bytes(data)
    readings = {}
    for reader, render in renderers(data, scale).items():
        reading = timed(render, args.repeats)
        rgb, width = image_bytes(render(), args.output / f"{name}-{reader}.png")
        reading["pixels"] = pixels(rgb, width, points)
        readings[reader] = reading
    probes = [("upstream_hayro", args.upstream), ("diagnostic_hayro", args.diagnostic)]
    if include_aligned:
        probes.append(("aligned_hayro", args.aligned))
    for label, probe in probes:
        if probe is None:
            continue
        image = args.output / f"{name}-{label}.png"
        reading = upstream(probe, source, image, args.repeats, scale)
        pixmap = pyropdf.Pixmap.from_bytes(image.read_bytes()).to_rgb()
        reading["pixels"] = pixels(bytes(pixmap), pixmap.width, points)
        readings[label] = reading
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "points": points, "readers": readings}


def trace_masks(source: Path, args: argparse.Namespace) -> dict[str, object]:
    """Count mask rasterizations in a separate, untimed instrumented run."""
    result = subprocess.run(
        [
            str(args.diagnostic),
            str(source),
            str(args.output / "trace.png"),
            "1",
            "2" if "emoji" in source.stem else "1",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "COLR_TRACE_MASKS": "1"},
    )
    dimensions = [tuple(map(int, line.split()[1:])) for line in result.stderr.splitlines() if line.startswith("MASK ")]
    # The probe renders twice: one warmup and one measured iteration.
    if not dimensions or len(dimensions) % 2:
        message = "The diagnostic probe must include trace-soft-masks.patch"
        raise RuntimeError(message)
    return {
        "rasterizations_per_render": len(dimensions) // 2,
        "dimensions": sorted(set(dimensions)),
        "mask_pixels_per_render": sum(width * height for width, height in dimensions) // 2,
    }


def emoji(args: argparse.Namespace) -> dict[str, object]:
    """Relate the synthetic reductions to the original COLR glyph and page."""
    import compare  # noqa: PLC0415 - optional blackrenderer/fonttools dependencies.

    face = pyropdf.Font.from_file(args.font)
    document = pyropdf.Document()
    page = document.pages.new((80, 80))
    with page.draw() as canvas:
        canvas.text(chr(0x1F600), (8, 64), font=face, size=64)
    glyph = compare.TTFont(args.font).getBestCmap()[0x1F600]
    reference = compare._reference(compare.BlackRendererFont(args.font), glyph, 2)  # noqa: SLF001
    compare._save(args.output / "emoji-blackrenderer.png", reference)  # noqa: SLF001
    grinning = measure(
        document.to_bytes(),
        "emoji",
        args,
        scale=2,
        points=((71, 89), (100, 89), (100, 100), (110, 110)),
        include_aligned=True,
    )
    for reader, reading in grinning["readers"].items():
        image = pyropdf.Pixmap.from_bytes((args.output / f"emoji-{reader}.png").read_bytes()).to_rgb()
        rendered = compare._pixels(bytes(image), 3)  # noqa: SLF001
        reading["against_blackrenderer"] = compare._differences(rendered, reference)  # noqa: SLF001
        reading["mouth_interior_max_channel_delta"] = max(
            abs(rendered[y * 160 + x][channel] - reference[y * 160 + x][channel])
            for x in range(80, 131)
            for y in range(95, 106)
            for channel in range(3)
        )
    print("emoji colour", flush=True)
    text = compare._painted_emoji(face, args.font)  # noqa: SLF001
    document = pyropdf.Document()
    page = document.pages.new((320, 340))
    with page.draw() as canvas:
        for index in range(10):
            canvas.text(text[index * 10 : (index + 1) * 10], (10, 30 + index * 30), font=face, size=24)
    data = document.to_bytes()
    full = measure(data, "emoji-100", args, scale=2)
    full["objects"] = len(document.objects)
    print("emoji page", flush=True)
    # This changes the appearance: it is only an attribution experiment, never a fix.
    masks = 0
    with document.transaction():
        for number in list(document.objects):
            obj = document.objects[number]
            if isinstance(obj, pyropdf.Dictionary) and isinstance(obj.get("SMask"), pyropdf.Dictionary):
                obj["SMask"] = pyropdf.Name("None")
                masks += 1
    without_masks = measure(document.to_bytes(), "emoji-100-no-masks", args, scale=2)
    print("emoji page without masks", flush=True)
    result = {
        "font_sha256": hashlib.sha256(args.font.read_bytes()).hexdigest(),
        "font_name": face.name,
        "oracle_versions": {
            name: importlib.metadata.version(name) for name in ("blackrenderer", "skia-python", "fonttools")
        },
        "grinning": grinning,
        "page": full,
        "without_masks": without_masks,
        "mask_dictionaries": masks,
    }
    if args.diagnostic:
        result["trace"] = trace_masks(args.output / "emoji-100.pdf", args)
    return result


def validate(results: dict[str, object]) -> None:
    """Check that the fixtures exercise equivalent painting inside each reader."""
    edges = results["clipped_shading"]["readers"]
    for name in ("mupdf", "pdfium"):
        shading, mask, fill, *_ = edges[name]["pixels"]
        assert shading == mask == fill, (name, "equivalent edge paint")
    if "diagnostic_hayro" in edges:
        shading, mask, fill, *_ = edges["diagnostic_hayro"]["pixels"]
        assert shading == mask == fill, "bounds patch must remove duplicate coverage"
    gradient = results["radial_gradient"]["readers"]
    for name in ("mupdf", "pdfium"):
        native_right, native_left, sampled_right, sampled_left, *mask = gradient[name]["pixels"]
        assert native_right == sampled_right, (name, "right sample inside domain")
        assert native_left == sampled_left, (name, "left sample inside domain")
        assert mask[:2] == mask[2:], (name, "mask extension inside domain")
    if "aligned_hayro" in gradient:
        assert all(pixel == [96, 96, 96] for pixel in gradient["aligned_hayro"]["pixels"])
    for size in (128, 512, 1024):
        for count in (1, 8, 32):
            masked = results["soft_masks"][f"mask-{size}-{count}"]["readers"]
            plain = results["soft_masks"][f"fill-{size}-{count}"]["readers"]
            for reader, reading in masked.items():
                fill_pixel, background = reading["pixels"]
                assert background == [255, 255, 255], (reader, "outside mask")
                assert all(abs(value - 128) <= 1 for value in fill_pixel), (reader, "inside mask")
                control = plain[reader]["pixels"][0]
                assert all(abs(a - b) <= 1 for a, b in zip(fill_pixel, control, strict=True))


def main() -> None:
    """Record the edge reduction and page-area/mask-count scaling matrix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path)
    parser.add_argument("--aligned", type=Path)
    parser.add_argument("--font", type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    results = {
        "upstream_commit": UPSTREAM,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": {name: importlib.metadata.version(name) for name in ("pyropdf", "pymupdf", "pypdfium2")},
        "pdfium": str(pypdfium2.PDFIUM_INFO),
        "method": (
            "one warmup, fresh document/cache per sample, in-memory input, exclude PNG encoding and process startup"
        ),
        "clipped_shading": measure(clipped_shading(), "clipped-shading", args, points=POINTS),
        "radial_gradient": measure(
            radial_gradient(),
            "radial-gradient",
            args,
            points=tuple((x + offset, 15) for offset in (0, 32, 64, 96) for x in (25, 6)),
            include_aligned=True,
        ),
        "soft_masks": {},
    }
    for size in (128, 512, 1024):
        for count in (1, 8, 32):
            for masked in (False, True):
                name = f"{'mask' if masked else 'fill'}-{size}-{count}"
                results["soft_masks"][name] = measure(
                    small_masks(size, count, masked=masked), name, args, points=((8, size - 8), (0, 0))
                )
                print(name, flush=True)
    if args.diagnostic:
        results["mask_trace"] = trace_masks(args.output / "mask-1024-32.pdf", args)
    if args.font:
        results["emoji"] = emoji(args)
    validate(results)
    results["fixture_checks"] = "passed: equivalent edges, gradient domain, and masked/control sample colours"
    (args.output / "rendering.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
