# A colour bitmap face stamped on 100 pages (issue #631)

What does a document pay for a face's CBDT and `sbix` glyphs, drawn as a Type 3
font (DESIGN.md section 7), against the outline text of `evidence/font-subsets`?

## Method

`stamp.py` follows `evidence/font-subsets/stamp.py`: one `page.draw()` per page
on 100 pages of 300 by 100 points, one `canvas.text` at 14 points, saved three
ways, and each page's `page.text()` checked after reopening. "Same" draws five
glyphs on every page; "varying" draws two per page, which use 26 in all.

```bash
uv run --with fonttools python evidence/colour-bitmaps/stamp.py
uv run --with pymupdf --with pypdfium2 python evidence/colour-bitmaps/readers.py
```

The faces are built by the script with the code of
`crates/pyropdf-engine/src/fonts/testdata/colour_faces.py`, since no real CBDT
or `sbix` face is committed: one strike of 109 pixels per em, as Noto Color Emoji
has, whose images are flat shapes in a round mask, about 1 KB of PNG each, and no
outlines. A real face's images cost in proportion to their own size. `stamp.py`
takes the paths of other faces to run.

## Results

Windows 11, AMD64, 2026-11-04, `results.json`. "Saved" is `to_bytes()`,
"dedupe=True" is `to_bytes(prune=True, dedupe=True)`, and "Compressed" adds
`compress=True, object_streams=True`.

| Table | Text | Type 3 fonts | Procedures | Images | Programs | Saved KiB | dedupe=True KiB | Compressed KiB | Face KiB | Draw s | Text |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| CBDT | same | 1 | 5 | 5 | 0 | 42.3 | 25.0 | 8.7 | 26.6 | 0.088 | ok |
| CBDT | varying | 1 | 26 | 26 | 0 | 78.4 | 56.5 | 38.6 | 26.6 | 0.126 | ok |
| `sbix` | same | 1 | 5 | 5 | 0 | 42.3 | 25.0 | 8.7 | 26.5 | 0.099 | ok |
| `sbix` | varying | 1 | 26 | 26 | 0 | 78.4 | 56.5 | 38.6 | 26.5 | 0.123 | ok |

`evidence/font-subsets`, outline text on 100 pages, for comparison:

| Face | Text | Saved KiB | dedupe=True KiB | Compressed KiB |
| --- | --- | ---: | ---: | ---: |
| hebrew | same | 37.1 | 19.7 | 3.5 |
| hebrew | varying | 36.7 | 24.7 | 8.1 |
| latin | same | 45.3 | 27.5 | 6.2 |
| latin | varying | 43.1 | 31.8 | 12.2 |
| japanese | same | 38.7 | 20.1 | 4.0 |
| japanese | varying | 37.2 | 24.6 | 8.2 |

`readers.py`, on a line "RG O R" that mixes colour and outline glyphs in each
committed face:

| Reader | Reads | Paints the red and the green glyph |
| --- | --- | --- |
| pyropdf (hayro) | `RG O R` | yes |
| MuPDF (PyMuPDF) | `RG O R` | yes |
| PDFium (pypdfium2) | `RG O R` | yes |
| `pdftotext` (Git for Windows' xpdf) | `RG O R` | not run |

`pdftoppm` was not installed here; `tests/test_canvas_embedded.py` compares its
colours where it is.

## Findings

- Stamping keeps the cost of a glyph to the first drawing that uses it: 100 pages
  of five glyphs hold five procedures and five images. The pages themselves cost
  what they cost for outline text, and the same text adds about 5 KiB, which is
  the five images and their masks.
- A varying text pays for its 26 images once each: about 1.4 KiB per glyph as
  saved, about what its PNG holds. Outline text of the same kind pays less, since
  an outline is smaller than an image.
- A file with only colour glyphs embeds no font program, so a face of any size
  costs only the glyphs that the document draws.
- `tests/test_canvas_embedded.py` guards the counts: one font per 256 glyphs, and
  each glyph's procedure and image once.
