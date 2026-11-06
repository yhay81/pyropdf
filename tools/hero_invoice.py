"""Build the invoice that the home page shows, with pyropdf itself.

pyropdf draws an invoice, runs the home page's example on it (highlight
every "total", save incrementally), and renders both versions. The home
page shows the renders, so what a visitor sees is pyropdf's own output.
Run it after a change to the example or to rendering:

    uv run python tools/hero_invoice.py
"""

from __future__ import annotations

from pathlib import Path

import pyropdf
from pyropdf import Color, Font, Stroke

OUT = Path(__file__).resolve().parent.parent / "docs" / "en" / "assets" / "hero"
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

        canvas.text("Payment within 30 days. Thank you.", (MARGIN, HEIGHT - 34), font=regular, size=7.5, fill=SOFT)
    data = doc.to_bytes()
    doc.close()
    return data


def _right_text(  # noqa: PLR0913 - the text options of Canvas.text, and its right edge.
    canvas: pyropdf.Canvas, text: str, right: float, y: float, *, font: Font, size: float, fill: Color
) -> None:
    """Draw `text` with its end at `right`."""
    canvas.text(text, (right - font.text_width(text, size), y), font=font, size=size, fill=fill)


def main() -> None:
    """Write the invoice, the reviewed invoice, and their renders."""
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "invoice.pdf").write_bytes(draw_invoice())
    with pyropdf.open(OUT / "invoice.pdf") as doc:
        doc.pages[0].pixmap(dpi=DPI).save(OUT / "invoice.png")

        # The home page's example, as it is written there.
        page = doc.pages[0]
        with doc.transaction("highlight totals"):
            hits = page.search("total")
            page.annotations.add(pyropdf.Highlight(hits))
        doc.save(OUT / "reviewed.pdf", incremental=True)
        page.pixmap(dpi=DPI).save(OUT / "reviewed.png")
    for name in ("invoice.pdf", "invoice.png", "reviewed.pdf", "reviewed.png"):
        print(f"{name}: {(OUT / name).stat().st_size:,} bytes")  # noqa: T201


if __name__ == "__main__":
    main()
