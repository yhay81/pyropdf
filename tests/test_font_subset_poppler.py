"""Cross-renderer checks for save-time CFF subsetting (#482)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import pyropdf


def _ink_box(samples: bytes, width: int, y0: int, y1: int) -> tuple[int, int, int, int]:
    dark = [i for i in range(y0 * width, y1 * width) if samples[i] < 128]
    assert dark
    return (
        min(i % width for i in dark),
        min(i // width for i in dark),
        max(i % width for i in dark),
        max(i // width for i in dark),
    )


@pytest.mark.skipif(
    shutil.which("pdftoppm") is None or shutil.which("pdftotext") is None,
    reason="poppler's pdftoppm and pdftotext are required",
)
def test_cff_subset_after_repeated_saves_matches_poppler(tmp_path: Path) -> None:
    font_path = (
        Path(__file__).resolve().parent.parent / "crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf"
    )
    font = pyropdf.Font.from_file(font_path)
    document = pyropdf.Document()
    page = document.pages.new((300, 120))
    with page.draw() as canvas:
        canvas.text("日本語、かな。", (20, 40), font=font, size=24)

    first_live = bytes(page.pixmap(dpi=72))
    first = document.to_bytes()
    with page.draw() as canvas:
        canvas.text("AVA 123", (20, 90), font=font, size=24)
    second_live = bytes(page.pixmap(dpi=72))
    second = document.to_bytes()

    for index, (data, expected, live, rows) in enumerate(
        [
            (first, ["日本語、かな。"], first_live, [(0, 60)]),
            (second, ["日本語、かな。", "AVA 123"], second_live, [(0, 60), (60, 120)]),
        ]
    ):
        saved = pyropdf.open(data)
        (saved_font,) = saved.pages[0].fonts()
        assert saved_font.program == "cff"
        assert bytes(saved.pages[0].pixmap(dpi=72)) == live
        pdf_path = tmp_path / f"stage-{index}.pdf"
        pdf_path.write_bytes(data)
        text = subprocess.run(  # noqa: S603 - fixed executable and a test-owned file.
            [shutil.which("pdftotext") or "pdftotext", str(pdf_path), "-"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
        assert [line for line in text.splitlines() if line.strip()] == expected
        pgm = subprocess.run(  # noqa: S603 - fixed executable and a test-owned file.
            [shutil.which("pdftoppm") or "pdftoppm", "-gray", "-r", "72", "-singlefile", str(pdf_path)],
            check=True,
            capture_output=True,
        ).stdout
        header = pgm.split(maxsplit=4)
        width, height = int(header[1]), int(header[2])
        poppler = pgm[-width * height :]
        hayro = saved.pages[0].pixmap(dpi=72).to_gray()
        assert (width, height) == (hayro.width, hayro.height)
        for y0, y1 in rows:
            boxes = _ink_box(poppler, width, y0, y1), _ink_box(bytes(hayro), width, y0, y1)
            assert all(abs(a - b) <= 2 for a, b in zip(*boxes, strict=True)), boxes
            hayro_samples = bytes(hayro)
            poppler_ink = {i for i in range(y0 * width, y1 * width) if poppler[i] < 128}
            hayro_ink = {i for i in range(y0 * width, y1 * width) if hayro_samples[i] < 128}
            overlap = len(poppler_ink & hayro_ink) / len(poppler_ink | hayro_ink)
            assert overlap >= 0.6, overlap
