# Draft follow-up comment for LaurenzV/hayro#1582

Confirmed on `3de8bb0401f0410b71f37a52524b7607433ace13` with vello_cpu 0.3.0.
Reducing a COLR Type 3 glyph led to the same duplicate-coverage problem.

The [1,269-byte fixture](https://github.com/yhay81/pyropdf/blob/main/evidence/colr-glyphs/rendering-fixtures/clipped-shading.pdf)
has three identical fractional trapezoid clips over magenta: white `sh`, white
fill through a luminosity mask painted by white `sh`, and white fill.
It uses no fonts or radial gradients. At 72 dpi, corresponding bottom-edge
pixels `(10,35)`, `(50,35)`, `(90,35)`, from the upper left, are:

| Reader | `sh` | `sh` mask | Fill |
| --- | --- | --- | --- |
| hayro | 221,143,210 | 221,143,210 | 236,191,229 |
| MuPDF 1.28.2 | 234,187,227 | 234,187,227 | 234,187,227 |
| PDFium 156.0.8076.0 | 235,191,229 | 235,191,229 | 235,191,229 |

Rounding out the `sh` fill bounds makes all three hayro samples 236,191,229.
The green channel exposes 0.75 squared versus 0.75 coverage. Using a
trapezoid avoids the reference readers' axis-aligned clip rounding shortcuts.

The [evidence and reproducer](https://github.com/yhay81/pyropdf/blob/main/evidence/colr-glyphs/rendering.md)
also relate the effect to the white tooth edge in a real COLR glyph. Its
overall comparison includes a separate native-gradient sampling issue;
rounding the fill bounds does not resolve every pixel difference.

This is additional reproduction evidence, not a claim that the patch is ready
for merging. I have not repeated the full upstream reference-image suite or
the regression analysis in the original report.
