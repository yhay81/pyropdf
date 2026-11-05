# Type 3 colour text at 1.25 em line spacing (#670)

The writer now chooses vertical glyph coordinates that make xpdf's Type 3
font-size estimate agree with the canvas's size. The page's text operators,
line spacing, glyph advances, colours, and painted geometry keep their
meaning. The original 100-emoji page reads in order in all four readers.

## Reproduce

Run from the repository root after `uv sync --locked`:

```powershell
uv run --no-sync --with fonttools==4.68.0 --with pymupdf==1.28.2 --with pypdfium2==5.14.0 python evidence/colr-glyphs/line_order.py --xpdf PATH_TO_XPDF_PDFTOTEXT --output evidence/colr-glyphs/results/line-order.json
```

`--font` defaults to `C:/Windows/Fonts/seguiemj.ttf`. The script selects the
same first 100 painted emoji as `compare.py`, draws ten lines at 24 points
30 points apart, and measures nine candidates. It also measures the
committed CBDT and sbix fixtures, both unchanged and with their advances
increased to 1.5 em. Those wider fixtures reproduce the failure without a
system font. Their pages are 400 points wide so every character fits; the
other pages retain the original 320-by-340-point size.

The baseline removes exactly the current writer's vertical wrapper and
restores its uniform FontMatrix and FontBBox coordinates. This reconstructs
the former writer's representation; the initial investigation also
reproduced the failure on unmodified `e83945c83818939101ec717e49744ce34a0be3ee`.
The `writer` case measures current canvas output without edits. No page
content or font advance changes between candidates.

Results are in [results/line-order.json](results/line-order.json), measured
on Windows 11 on 2026-11-06, with Python 3.14.8, MuPDF 1.28.2,
pypdfium2 5.14.0 (PDFium 156.0.8076.0), and fontTools 4.68.0.
The JSON records font SHA-256 hashes, extracted xpdf text, all four reading
orders, pixel differences, and text geometry differences. The system font
and generated PDFs are not committed.

The xpdf 4.06 tools and source came from the project's official downloads:

- [Windows tools](https://dl.xpdfreader.com/xpdf-tools-win-4.06.zip), SHA-256
  `2b6ca45da794e7854a6468fd6c8063fde62701f001ce03fa4f603eab7e15a0b6`.
- [Source](https://dl.xpdfreader.com/xpdf-4.06.tar.gz), SHA-256
  `1c38f527c46caee0f712386d42a885b96a31ed9ce11904e872559859894d137e`.

## What xpdf measures

In `xpdf/TextOutputDev.cc`, `TextPage::updateFont` (lines 1388-1433)
estimates a Type 3 font's size from a glyph width. It prefers a glyph named
`m`, then a one-letter Latin name, then the first named glyph with a
positive width. pyropdf's generated names use the third case. xpdf divides
that glyph's text-space width by 0.5, then multiplies by
`abs(FontMatrix[3] / FontMatrix[0])`. `GfxFont.cc` line 1397 computes a
Type 3 width as its Widths entry times `FontMatrix[0]`.

Thus the multiplier for these fonts is `2 * first_width * FontMatrix[3]`.
Segoe UI Emoji's first width is 2812 units in a 2048-unit em: the old
uniform matrix makes 24-point text look like 65.90625-point text to this
heuristic. FontBBox and the glyph's d0/d1 operator do not enter it.
Changing descriptor metrics also leaves this size estimate unchanged.

## Candidate results

All candidates retained the text sequence in pyropdf, MuPDF, and PDFium.
The table reports whether xpdf retained it. CBDT and sbix agreed in both
the normal-width and wide-font cases.

| Candidate | Segoe UI Emoji | 1 em bitmaps | 1.5 em bitmaps | Assessment |
| --- | --- | --- | --- | --- |
| Original uniform matrix | No | Yes | No | Reproduces the bug |
| Zero FontBBox | No | Yes | No | Does not affect the heuristic |
| An em-sized FontBBox | No | Yes | No | Also changes MuPDF's text boxes |
| d1 with a bounding box | No | Yes | No | Loses colour in readers |
| FontDescriptor with ink metrics | No | Yes | No | Does not fix ordering |
| Descriptor compensating the size estimate | No | Yes | No | Also uses artificial metrics |
| Uniform 1000-unit coordinates | No | Yes | No | Normalized widths remain the same |
| Compensating vertical coordinates | Yes | Yes | Yes | Selected |
| Current writer | Yes | Yes | Yes | Same outcome as the selected candidate |

The zero and em boxes, d1, and artificial descriptor metrics are diagnostic
controls, not proposed valid colour-font representations. In particular,
d1 describes a shape painted in the caller's colour and excludes ordinary
colour images; it cannot replace d0 for these glyphs. See Adobe's
[PDF Reference 1.7, Table 5.10](https://opensource.adobe.com/dc-acrobat-sdk-docs/pdfstandards/pdfreference1.7old.pdf).

At 72 dpi the current writer's Segoe page is pixel-identical to the
baseline in pyropdf and PDFium. MuPDF changes one channel of one pixel by
one level out of 255. MuPDF and PDFium's text geometry is identical;
pyropdf's maximum geometry difference is `2.85e-14` points. All four bitmap
pages are pixel- and geometry-identical in all three readers. These are
comparisons within each reader, not a claim that the readers render COLR
identically; the colour/performance differences tracked in #671 remain.

## Writer and regression coverage

For a source em `E` and first positive width `W`, the matrix becomes
`[1/E 0 0 1/(2W) 0 0]`. Each glyph's procedure keeps its first-line d0
width and receives a vertical scale `2W/E` after it; FontBBox receives the
same scale. The transforms cancel in the painted geometry, while xpdf's
font-size multiplier becomes one. No dummy glyph, renamed glyph, or
invented bounding box is needed.

The first positive width fixes the coordinates for that 256-glyph font.
Later additions recover the scale from its matrix. If earlier drawings
added only zero-width glyphs, the same transaction adjusts their procedures
once before adding the first positive-width glyph. Every read, allocation,
and write uses the canvas budget, and a refusal discards the transaction.
Explicitly edited procedures outside the writer's format keep their
original coordinate system. Existing fonts that already have positive
widths also retain their chosen coordinates, including older uniform fonts.

`tests/test_canvas_embedded.py` checks COLR v0/v1 and CBDT/sbix text with
pdftotext at both 1 and 1.5 em advances, wherever pdftotext is installed
(CI installs Poppler). Local runs additionally use xpdf 4.06. Tests also
cover later additions, an initial zero-width glyph, unchanged placement
and colours, save/reopen, and undo/redo. Existing tests exercise the 256-code
rollover, other em sizes, transformed glyphs, and atomic budget refusals.
