# ruff: noqa: INP001, T201, S603, S607, D103 - a script that runs the renderers and prints.
"""Compare hayro's RGBA raster converted to CMYK with Ghostscript's CMYK raster, and time the conversion.

    python3 evidence/cmyk-raster/compare.py [PROFILE.icc]

PROFILE is the output CMYK profile for both renderers; the default is
Ghostscript's `default_cmyk.icc`, found next to the `gs` executable. The
hayro program is `src/main.rs`, which depends on the released hayro 0.8.0 and
moxcms 0.8.1 and not on pyropdf. Ghostscript renders natively in CMYK: it
keeps DeviceCMYK values and blends in CMYK. Both use the relative
colorimetric intent.

The script prints a Markdown report: for each patch of `patches.pdf` the
mean of its central 20 by 20 pixels at 72 dpi, and the CIE76 colour
difference of the two CMYK values after converting each to sRGB through the
profile. For the patches that paint only DeviceCMYK it adds the lane design
of README.md: hayro renders `lanes-cmy.pdf` and `lanes-k.pdf`, and the CMYK
value is 255 minus the red, green, and blue of the first and the red of the
second. Then it gives the median render and conversion times over 11 runs for page
1 of three documents of `tests/assets/real_world` at 150 dpi.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
PROBE = HERE / "target" / "release" / "cmyk-raster"
DOCUMENTS = ("f1040.pdf", "usrguide.pdf", "wdl6812-manuscript.pdf")
WIDTH, HEIGHT = 660, 180
SRGB_LINEAR_KNEE = 0.04045


def default_profile() -> Path:
    gs = Path(shutil.which("gs") or "gs").resolve()
    found = sorted(gs.parent.parent.glob("share/ghostscript/**/iccprofiles/default_cmyk.icc"))
    if not found:
        sys.exit("pass the output CMYK profile: no default_cmyk.icc next to gs")
    return found[0]


def ghostscript(pdf: Path, profile: Path, out: Path) -> bytes:
    subprocess.run(
        [
            "gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=bitcmyk", "-dGrayValues=256", "-r72",
            f"-sOutputICCProfile={profile}", "-dRenderIntent=1", f"-sOutputFile={out}", str(pdf),
        ],
        check=True,
    )  # fmt: skip
    return out.read_bytes()


def probe(*args: object) -> str:
    return subprocess.run([PROBE, *map(str, args)], check=True, capture_output=True, text=True).stdout


def mean(raster: bytes, patch: dict[str, int]) -> tuple[int, ...]:
    top = HEIGHT - patch["y"] - patch["size"]
    pixels = [
        raster[(y * WIDTH + x) * 4 : (y * WIDTH + x) * 4 + 4]
        for y in range(top + 20, top + 40)
        for x in range(patch["x"] + 20, patch["x"] + 40)
    ]
    return tuple(round(sum(p[c] for p in pixels) / len(pixels)) for c in range(4))


def lab(rgb: tuple[int, ...]) -> tuple[float, float, float]:
    def linear(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= SRGB_LINEAR_KNEE else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = map(linear, rgb)
    xyz = (
        (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047,
        0.2126 * r + 0.7152 * g + 0.0722 * b,
        (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883,
    )
    fx, fy, fz = (t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116 for t in xyz)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(profile: Path, first: tuple[int, ...], second: tuple[int, ...]) -> float:
    lines = probe("to-srgb", profile, ",".join(map(str, first)), ",".join(map(str, second))).split("\n")
    a, b = (lab(tuple(int(v) for v in line.split())) for line in lines[:2])
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5


def main() -> None:
    profile = Path(sys.argv[1]) if len(sys.argv) > 1 else default_profile()
    subprocess.run([sys.executable, HERE / "make_pdf.py"], check=True)
    subprocess.run(["cargo", "build", "--release", "--quiet", "--manifest-path", HERE / "Cargo.toml"], check=True)
    patches = json.loads((HERE / "patches.json").read_text())
    gs_version = subprocess.run(["gs", "--version"], check=True, capture_output=True, text=True).stdout.strip()

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "patches"
        probe("render", HERE / "patches.pdf", 72, profile, out, 1)
        rgba, cmyk = out.with_suffix(".rgba").read_bytes(), out.with_suffix(".cmyk").read_bytes()
        reference = ghostscript(HERE / "patches.pdf", profile, Path(tmp) / "gs.cmyk")
        lanes = []
        for name in ("lanes-cmy", "lanes-k"):
            probe("render", HERE / f"{name}.pdf", 72, profile, Path(tmp) / name, 1)
            lanes.append((Path(tmp) / f"{name}.rgba").read_bytes())

        print(f"Output profile `{profile.name}`, Ghostscript {gs_version}, hayro 0.8.0, moxcms 0.8.1.\n")
        print("| Patch | Paints | hayro RGB | hayro RGB → CMYK | ΔE76 | Lanes CMYK | ΔE76 | Ghostscript CMYK |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for patch in patches:
            converted, native = mean(cmyk, patch), mean(reference, patch)
            lane = "| n/a | n/a"
            if not patch["name"].startswith(("rgb-", "gray-")):
                first, second = mean(lanes[0], patch), mean(lanes[1], patch)
                packed = (255 - first[0], 255 - first[1], 255 - first[2], 255 - second[0])
                lane = f"| {' '.join(map(str, packed))} | {delta_e(profile, packed, native):.1f}"
            print(
                f"| {patch['name']} | `{patch['paints']}` | {' '.join(map(str, mean(rgba, patch)[:3]))} "
                f"| {' '.join(map(str, converted))} | {delta_e(profile, converted, native):.1f} "
                f"{lane} | {' '.join(map(str, native))} |"
            )

        print("\n| Page 1 at 150 dpi | Pixels | Transform build ms | Render ms | RGB → CMYK ms | Conversion / render |")
        print("| --- | --- | --- | --- | --- | --- |")
        for name in DOCUMENTS:
            stats = json.loads(probe("render", ROOT / "tests/assets/real_world" / name, 150, profile, out, 11))
            print(
                f"| {name} | {stats['pixels']:,} | {stats['build_ms']:.1f} | {stats['render_ms']:.1f} "
                f"| {stats['convert_ms']:.1f} | {stats['convert_ms'] / stats['render_ms']:.0%} |"
            )


if __name__ == "__main__":
    main()
