# ruff: noqa: INP001, T201
"""Measure bounded Pixmap loading on the baseline and patched extension (#697)."""

from __future__ import annotations

import io
import json
import platform
import random
import statistics
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from pyropdf import Pixmap

if TYPE_CHECKING:
    from collections.abc import Callable


def median_ms(call: Callable[[], object]) -> float:
    """Return the median milliseconds per call over nine batches of 20."""
    call()
    batches = []
    for _ in range(9):
        start = time.perf_counter()
        for _ in range(20):
            call()
        batches.append((time.perf_counter() - start) * 1000 / 20)
    return round(statistics.median(batches), 4)


def main() -> None:
    """Measure a large raw image and an indexed TIFF that needs an input copy."""
    random_bytes = random.Random(697).randbytes  # noqa: S311 - Reproducible image samples.
    ppm = b"P6\n1024 1024\n255\n" + random_bytes(1024 * 1024 * 3)
    palette = Image.frombytes("P", (256, 256), random_bytes(256 * 256))
    palette.putpalette(bytes(range(256)) * 3)
    output = io.BytesIO()
    palette.save(output, "TIFF")
    tiff = output.getvalue()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "image.ppm"
        path.write_bytes(ppm)
        result = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "ppm_bytes": len(ppm),
            "tiff_bytes": len(tiff),
            "ppm_buffer_ms": median_ms(lambda: Pixmap.from_bytes(ppm)),
            "ppm_file_ms": median_ms(lambda: Pixmap.open(path)),
            "tiff_palette_ms": median_ms(lambda: Pixmap.from_bytes(tiff)),
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
