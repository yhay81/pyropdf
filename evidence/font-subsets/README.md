# One embedded face stamped on 100 pages (issue #481)

Does pyropdf's canvas write one font program however many drawings use a
face (DESIGN.md section 7)?

## Method

`stamp.py` loads each face with `Font.from_file`, draws one line with
`canvas.text` on each of 100 pages, one `page.draw()` per page, saves,
reopens, counts the font programs and Type0 fonts, and checks every page's
`page.text()`. "Same" draws one string on every page; "varying" draws two
letters per page, as `evidence/embedded-text` does.

```bash
uv run python evidence/font-subsets/stamp.py HEBREW LATIN JAPANESE
```

The faces are not committed: HEBREW is pylopdf-fonts-he's
`NotoSansHebrew-Variable.ttf`, LATIN is TeX Live 2026's
`NotoSans-Regular.ttf` (a larger build than Pillow's test font that
`evidence/embedded-text` used), and JAPANESE is pylopdf-fonts-jp's
`NotoSansJP-Regular.otf` (CID-keyed CFF).

## Results

Apple M2 Pro, 2026-10-31, `results.json`. Sizes are before subsetting,
which is issue #482: until a save subsets it, the program holds every glyph.

| Face | Text | Programs | Type0 fonts | Saved KiB | Program KiB | Draw s | Text |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| hebrew | same | 1 | 1 | 106.6 | 110.0 | 0.034 | ok |
| hebrew | varying | 1 | 1 | 104.8 | 110.0 | 0.033 | ok |
| latin | same | 1 | 1 | 297.6 | 525.4 | 0.031 | ok |
| latin | varying | 1 | 1 | 295.2 | 525.4 | 0.032 | ok |
| japanese | same | 1 | 1 | 3818.1 | 4394.4 | 0.102 | ok |
| japanese | varying | 1 | 1 | 3814.9 | 4394.4 | 0.106 | ok |

Other checks, on one page drawing "שָׁלוֹם עולם", "Hello, office ffi AVA",
and "日本語のテキスト。" in the three faces:

- hayro (`page.pixmap`) and poppler 26 (`pdftoppm`) draw the same pixels
  to the eye, with the Hebrew points on their letters and the "ffi"
  ligature.
- `page.text()` returns each line in logical order. poppler's `pdftotext`
  returns the Latin and Japanese lines unchanged and the Hebrew line in
  logical order, with the shin dot before its letter.
- The Arabic subset face draws the dot of "ب" as a glyph of its own without
  text; it gets no ToUnicode entry, and `page.text()` warns that it skipped
  one unmapped glyph while returning the text.

## Findings

1. **One program for 100 stamps holds** in every row, against krilla's 100
   without `dedupe=True` (`evidence/embedded-text`).
2. The saved size is the whole program until #482 subsets it at save; the
   comparison with krilla's `dedupe=True` sizes belongs there.
