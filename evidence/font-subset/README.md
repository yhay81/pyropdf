# Font subsetting

The measurement for #364: `doc.subset_fonts()` shrinks documents whose
embedded programs hold more glyphs than the pages draw.

## Method

`measure.py` opens each corpus document, saves it in full, subsets its
fonts, and saves it in full again. The subset keeps every glyph ID and
empties the outlines that no page draws, so the saved sizes differ only by
the font programs and their new subset tags. Seconds time
`subset_fonts()` alone.

## Results

Apple M2 Pro, 2026-10-29:

| Document | Saved before | Saved after | Change | Programs subset | Glyphs kept | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| bill-hr815.pdf | 360,080 | 360,080 | +0.0% | 0 | - | 0.03 |
| bunka-kokugo-series-019-p4.pdf | 131,036 | 131,036 | +0.0% | 0 | - | 0.00 |
| f1040.pdf | 433,759 | 433,759 | +0.0% | 0 | - | 0.05 |
| mhlw-doc.pdf | 348,922 | 126,024 | -63.9% | 1 | 233/23398 | 0.01 |
| nics-background-checks-2015-11.pdf | 90,461 | 90,461 | +0.0% | 0 | - | 0.00 |
| patent-us223898.pdf | 319,926 | 319,926 | +0.0% | 0 | - | 0.00 |
| pdf20-simple.pdf | 4,912 | 4,912 | +0.0% | 0 | - | 0.00 |
| pdfium-jpx-lzw.pdf | 1,224 | 1,224 | +0.0% | 0 | - | 0.00 |
| pdfium-links-highlights-annots.pdf | 2,099 | 2,099 | +0.0% | 0 | - | 0.00 |
| pdfium-ocg-invisible.pdf | 1,114 | 1,114 | +0.0% | 0 | - | 0.00 |
| pdfium-smask-blend.pdf | 1,448 | 1,448 | +0.0% | 0 | - | 0.00 |
| pdfium-type3.pdf | 1,191 | 1,191 | +0.0% | 0 | - | 0.00 |
| senate-expenditures.pdf | 53,486 | 36,868 | -31.1% | 3 | 24/42, 5/13, 43/77 | 0.00 |
| usrguide.pdf | 594,758 | 594,758 | +0.0% | 0 | - | 0.03 |
| wdl6812-manuscript.pdf | 377,721 | 377,721 | +0.0% | 0 | - | 0.00 |

mhlw-doc.pdf embeds the whole YuMincho program (23,398 glyphs) and keeps 233
of them with .notdef; senate-expenditures.pdf's programs carry subset tags but still
hold glyphs that no page draws. Every other program in the corpus is a
Type 1 program, already holds only the glyphs drawn (bill-hr815.pdf's
Type1C programs, where the rewritten CFF would grow by its 5-byte offsets,
so it stays as it is), or is reached from form default resources.

## Running

```bash
uv run python evidence/font-subset/measure.py
```
