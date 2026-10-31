# One embedded face stamped on 100 pages (issues #481, #482)

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

Apple M2 Pro, 2026-10-31, `results.json`. A save subsets each face's
program to the glyphs that its `W` array lists, renumbered (#482):
"Saved" is `to_bytes()`, "dedupe=True" is `to_bytes(prune=True,
dedupe=True)`, as krilla's baseline saved, and "Compressed" adds
`compress=True, object_streams=True`. "Face" is the loaded program and
"Saved program" the one in the file.

| Face | Text | Programs | Type0 fonts | Saved KiB | dedupe=True KiB | Compressed KiB | Face KiB | Saved program KiB | Draw s | Text |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| hebrew | same | 1 | 1 | 37.1 | 19.7 | 3.5 | 110.0 | 0.71 | 0.033 | ok |
| hebrew | varying | 1 | 1 | 36.7 | 24.7 | 8.1 | 110.0 | 2.12 | 0.033 | ok |
| latin | same | 1 | 1 | 45.3 | 27.5 | 6.2 | 525.4 | 5.52 | 0.030 | ok |
| latin | varying | 1 | 1 | 43.1 | 31.8 | 12.2 | 525.4 | 8.87 | 0.032 | ok |
| japanese | same | 1 | 1 | 38.7 | 20.1 | 4.0 | 4394.4 | 1.36 | 0.099 | ok |
| japanese | varying | 1 | 1 | 37.2 | 24.6 | 8.2 | 4394.4 | 3.12 | 0.102 | ok |

krilla through pylopdf 0.16.0 with `dedupe=True`, from
`evidence/embedded-text` (100 pages; its Latin face is Pillow's smaller
test font, so the Latin row favours krilla):

| Face | Same KiB | Varying KiB |
| --- | ---: | ---: |
| hebrew | 29.6 | 82.8 |
| latin | 32.3 | 154.5 |
| japanese | 29.9 | 68.3 |

Before #482, without subsetting, the saved sizes were 106.6 and 104.8 KiB
(Hebrew), 297.6 and 295.2 (Latin), and 3818.1 and 3814.9 (Japanese).

Other checks, on one page drawing "שָׁלוֹם עולם", "Hello, office ffi AVA",
and "日本語のテキスト。" in the three faces and saved:

- hayro (`page.pixmap`) and poppler 26 (`pdftoppm`) draw the same pixels
  to the eye, with the Hebrew points on their letters and the "ffi"
  ligature. `pdffonts` lists two subset CID TrueType fonts and one subset
  CID Type 0C font.
- `page.text()` returns each line in logical order. poppler's `pdftotext`
  returns the Latin and Japanese lines unchanged and the Hebrew line in
  logical order, with the shin dot before its letter.
- The Arabic subset face draws the dot of "ب" as a glyph of its own without
  text; it gets no ToUnicode entry, and `page.text()` warns that it skipped
  one unmapped glyph while returning the text.

## Findings

1. **One program for 100 stamps holds** in every row, against krilla's 100
   without `dedupe=True` (`evidence/embedded-text`).
2. **After a save, every face is smaller than krilla's `dedupe=True`**, in
   every row, at the same options; the saved program is 0.7 to 8.9 KiB, and
   the rest of each file is the 100 pages.
3. A TrueType face keeps its outlines, metrics, and instructions, renumbered
   with a `CIDToGIDMap`; a CFF face becomes a CID-keyed CFF without hints,
   whose charset maps each new glyph to the CID that the content shows.
   Dropping CFF hints is an intended difference from krilla, which keeps
   them.
