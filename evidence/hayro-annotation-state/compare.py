# ruff: noqa: INP001, T201, S101, S603, S607, D103 - a script that runs the two renderers and prints.
"""Render both files with hayro alone and with Ghostscript, and print the colors of the annotation's box.

    python3 evidence/hayro-annotation-state/compare.py

`make_pdf.py` writes the files first. The hayro program is `src/main.rs`, which
depends on the released hayro 0.8.0 and not on pyropdf. Both renderers draw at
72 dpi on white and count the pixels of the box x 20 to 60, y 140 to 160 from
the top, where the annotation's appearance is.
"""

import re
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
FILES = ("stray-q.pdf", "balanced.pdf")
HIGH, LOW = 200, 60
NAMES = ("red", "green", "blue", "black")


def classify(r: int, g: int, b: int) -> int | None:
    if r > HIGH and g < LOW and b < LOW:
        return 0
    if g > HIGH and r < LOW and b < LOW:
        return 1
    if b > HIGH and r < LOW and g < LOW:
        return 2
    if r < LOW and g < LOW and b < LOW:
        return 3
    return None


def ghostscript(pdf: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "page.ppm"
        subprocess.run(
            ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=ppmraw", "-r72", f"-sOutputFile={out}", str(pdf)],
            check=True,
        )
        data = out.read_bytes()
    header = re.match(rb"P6\s+(?:#[^\n]*\n\s*)*(\d+)\s+(\d+)\s+255\s", data)
    assert header
    width = int(header[1])
    pixels = data[header.end() :]
    counts = [0, 0, 0, 0]
    for y in range(140, 160):
        for x in range(20, 60):
            at = (y * width + x) * 3
            kind = classify(pixels[at], pixels[at + 1], pixels[at + 2])
            if kind is not None:
                counts[kind] += 1
    return ", ".join(f"{n} {name}" for n, name in zip(counts, NAMES, strict=True)) + " pixels"


def hayro(pdf: Path) -> str:
    run = subprocess.run(
        ["cargo", "run", "-q", "--release", "--manifest-path", str(HERE / "Cargo.toml"), "--", str(pdf)],
        check=True,
        capture_output=True,
        text=True,
    )
    return run.stdout.removeprefix("annotation box: ").strip()


for name in FILES:
    pdf = HERE / name
    print(name)
    print("  hayro:      ", hayro(pdf))
    print("  ghostscript:", ghostscript(pdf))
