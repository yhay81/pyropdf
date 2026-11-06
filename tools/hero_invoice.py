"""Build what the home page's hero shows, with pyropdf itself.

pyropdf draws an invoice. The hero's four snippets, read from
docs/en/index.md as they are written there, then run in turn: the first
reads the invoice, and each of the others edits the last one's output and
saves it incrementally, so reviewed.pdf keeps every revision. Every
revision is rendered by pyropdf. The hero shows those renders, the boxes
of the words that page.words() finds, and what the first snippet prints.
Run it after a change to the snippets, the invoice, or rendering:

    uv run python tools/hero_invoice.py
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import tempfile
from pathlib import Path

import pyropdf
from pyropdf import Color, Font, Stroke

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "en" / "assets" / "hero"
HOME = ROOT / "docs" / "en" / "index.md"
SNIPPET = re.compile(r'^```python title="(\w+\.py)"\n(.*?)^```', re.MULTILINE | re.DOTALL)
STEPS = ("read.py", "annotate.py", "fill.py", "stamp.py")
OUTPUT_LINES = 9

WIDTH, HEIGHT = 340, 442
MARGIN = 30
INK = Color(0.16, 0.14, 0.11)
SOFT = Color(0.45, 0.41, 0.35)
RULE = Color(0.86, 0.83, 0.78)
EMBER = Color(0.79, 0.26, 0.10)
DPI = 224

ROWS = (
    ("Design review", "1,200.00"),
    ("Implementation", "3,400.00"),
    ("Support, 12 months", "600.00"),
)


def draw_invoice() -> bytes:
    """Draw the invoice in the Standard 14 fonts and return its bytes."""
    regular = Font.standard("Helvetica")
    bold = Font.standard("Helvetica-Bold")
    doc = pyropdf.Document()
    page = doc.pages.new((WIDTH, HEIGHT))
    right = WIDTH - MARGIN
    with page.draw() as canvas:
        canvas.rect((0, 0, WIDTH, 5), fill=EMBER)
        canvas.text("Harbor Lane Studio", (MARGIN, 40), font=bold, size=9, fill=INK)
        _right_text(canvas, "No. 2026-118", right, 40, font=regular, size=8, fill=SOFT)
        _right_text(canvas, "6 November 2026", right, 51, font=regular, size=8, fill=SOFT)
        canvas.text("Invoice", (MARGIN, 92), font=bold, size=28, fill=INK)
        canvas.text("Billed to Kestrel Analytics", (MARGIN, 112), font=regular, size=8.5, fill=SOFT)

        top = 150
        canvas.text("Description", (MARGIN, top), font=bold, size=7.5, fill=SOFT)
        _right_text(canvas, "Amount", right, top, font=bold, size=7.5, fill=SOFT)
        canvas.line((MARGIN, top + 7), (right, top + 7), stroke=Stroke(paint=INK, width=0.8))
        y = top + 26
        for name, amount in ROWS:
            canvas.text(name, (MARGIN, y), font=regular, size=10, fill=INK)
            _right_text(canvas, amount, right, y, font=regular, size=10, fill=INK)
            canvas.line((MARGIN, y + 10), (right, y + 10), stroke=Stroke(paint=RULE, width=0.6))
            y += 27
        canvas.text("Total", (MARGIN, y + 6), font=bold, size=13, fill=INK)
        _right_text(canvas, "GBP 5,200.00", right, y + 6, font=bold, size=13, fill=INK)

        # Where fill.py puts its field.
        canvas.text("Approved by", (MARGIN, 310), font=regular, size=7.5, fill=SOFT)
        canvas.line((MARGIN, 335), (170, 335), stroke=Stroke(paint=RULE, width=0.6))

        canvas.text("Payment within 30 days. Thank you.", (MARGIN, HEIGHT - 34), font=regular, size=7.5, fill=SOFT)
    data = doc.to_bytes()
    doc.close()
    return data


def _right_text(  # noqa: PLR0913 - the text options of Canvas.text, and its right edge.
    canvas: pyropdf.Canvas, text: str, right: float, y: float, *, font: Font, size: float, fill: Color
) -> None:
    """Draw `text` with its end at `right`."""
    canvas.text(text, (right - font.text_width(text, size), y), font=font, size=size, fill=fill)


def snippets() -> dict[str, str]:
    """Return the hero's snippets by title, as the home page writes them."""
    home = HOME.read_text(encoding="utf-8")
    start = home.index('<div class="pp-reel"')
    hero = home[start : home.index("</section>", start)]
    found = dict(SNIPPET.findall(hero))
    missing = [name for name in STEPS if name not in found]
    if missing:
        msg = f"the hero has no snippet titled {', '.join(missing)}"
        raise SystemExit(msg)
    return found


def run(code: str, name: str, work: Path) -> str:
    """Run a snippet in `work`, where input.pdf is, and return what it prints."""
    printed = io.StringIO()
    previous = Path.cwd()
    os.chdir(work)
    try:
        with contextlib.redirect_stdout(printed):
            exec(compile(code, name, "exec"), {"__name__": "__main__"})  # noqa: S102 - the home page's own snippets.
    finally:
        os.chdir(previous)
    return printed.getvalue()


def render(pdf: Path, png: Path) -> None:
    """Render the first page of `pdf` to `png`."""
    with pyropdf.open(pdf) as doc:
        doc.pages[0].pixmap(dpi=DPI).save(png)


def word_boxes(pdf: Path) -> str:
    """Return an SVG of the boxes of the words that page.words() finds."""
    with pyropdf.open(pdf) as doc:
        boxes = [word.bbox for word in doc.pages[0].words()]
    rects = "".join(
        f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" height="{y1 - y0:.1f}" rx="1"/>'
        for x0, y0, x1, y1 in boxes
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}">'
        f'<g fill="rgb(201 67 26 / 10%)" stroke="rgb(201 67 26)" stroke-width="0.6">{rects}</g></svg>\n'
    )


def main() -> None:
    """Write the invoice, its revisions' renders, the word boxes, and the read output."""
    code = snippets()
    OUT.mkdir(parents=True, exist_ok=True)
    invoice = OUT / "invoice.pdf"
    invoice.write_bytes(draw_invoice())
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        shutil.copyfile(invoice, work / "input.pdf")
        printed = run(code["read.py"], "read.py", work).splitlines()
        if len(printed) > OUTPUT_LINES:
            printed = [*printed[: OUTPUT_LINES - 1], "…"]
        (OUT / "read.txt").write_text("\n".join(printed) + "\n", encoding="utf-8", newline="\n")
        (OUT / "words.svg").write_text(word_boxes(invoice), encoding="utf-8", newline="\n")
        render(invoice, OUT / "revision-1.png")
        for number, name in enumerate(STEPS[1:], start=2):
            run(code[name], name, work)
            shutil.move(work / "output.pdf", work / "input.pdf")
            render(work / "input.pdf", OUT / f"revision-{number}.png")
        shutil.copyfile(work / "input.pdf", OUT / "reviewed.pdf")
    for path in sorted(OUT.iterdir()):
        print(f"{path.name}: {path.stat().st_size:,} bytes")  # noqa: T201


if __name__ == "__main__":
    main()
