# Hinted renders of subset font programs (issue #18)

hayro 0.9 hints text by default (`RenderSettings::hinting`,
LaurenzV/hayro#1705), which hayro 0.8 did not. Rendered hinted, the font
programs that pyropdf subsets no longer draw as the programs they came from,
while unhinted they draw the same, pixel for pixel. The tests that compare a
document's renders before and after subsetting failed for this reason:
`tests/test_font_subsetting.py` (5 tests), `tests/test_font_subset.py`
(mhlw-doc.pdf, 2), `tests/test_canvas_embedded.py` (the CID-keyed CFF
face), and `tests/test_font_subset_poppler.py`, which compares hayro's
renders of the same kind. This records why, for the owner's choice of what
pyropdf does about it.

## Method

```sh
uv run python evidence/hinted-subsets/run.py
```

- `run.py` writes `tests/assets/real_world/mhlw-doc.pdf` as it is and after
  `Document.subset_fonts`, which keeps every glyph ID of its YuMincho
  TrueType program and empties the outlines of the glyphs that no page
  draws.
- The harness (`src/main.rs`) depends on hayro at the commit that the
  workspace builds against (`Cargo.toml`). It renders every page of both
  documents at 72 dpi on white, with and without hinting, and counts the
  pixels that differ between the two documents and the largest difference
  of a channel.
- The canvas case of `tests/test_canvas_embedded.py` draws Japanese text in
  `NotoSansJP-subset.otf`, a CID-keyed CFF face, which a save compacts into
  a bare CID-keyed CFF program. That document has no file before the save,
  so `run.py` compares pyropdf's own renders before and after the save,
  which hint as pyropdf renders.

## Why the hinted renders differ

hayro hints through skrifa as FreeType's light hinting does
(`hayro-font/src/program/mod.rs`, `hinted_outline`):

- **TrueType outlines** use the auto-hinter, not the program's
  instructions. The auto-hinter derives each script's blue zones and
  standard stem widths from reference characters that it looks up through
  the `cmap`: for Latin, 32 characters such as `T H E Z O C Q S` and `o`;
  for CJK ideographs, 150, such as `他 们 你 來` and `田 囗`
  (`skrifa/generated/generated_autohint_styles.rs`). A subset keeps the
  `cmap` but empties the reference characters that no page draws, so the
  zones and widths change, and with them where every hinted glyph's edges
  snap.
- **CFF outlines** use the program's own hints: the stems of each
  charstring and the blue values of its Private DICT. A save compacts a
  face that pyropdf embeds into a bare CID-keyed CFF whose charstrings are
  drawn from unhinted outlines (`crates/pyropdf-engine/src/fonts/compact.rs`),
  so the compacted program has no hints left. A CFF program that
  `subset_fonts` subsets keeps the charstrings it keeps, hints included.

## Result

`results/windows-x86_64.md`. Hinted, the subset of mhlw-doc.pdf differs from
the original in 5,907 of 500,395 pixels on its first page and 1,349 on its
second, by up to 239 of 255 in a channel; unhinted, in none. The canvas text
differs in 873 of 36,000 pixels after the save when rendered hinted.

What each choice would take:

- **Subsets keep what hinting reads.** A TrueType subset would keep the
  outlines of the auto-hinter's reference characters of every script that
  the program covers, as many as 150 glyphs for a CJK program, and pyropdf
  would carry a copy of skrifa's tables of them. A compacted CFF program
  would keep each charstring's hints and the Private DICT's blue values,
  which needs a charstring rewriter instead of drawing outlines.
- **The tests compare unhinted renders.** pyropdf renders hinted, and the
  tests render through a switch that turns hinting off, which `page.pixmap`
  does not have today.
- **pyropdf renders unhinted**, as it did with hayro 0.8: one setting in
  `crates/pyropdf-engine/src/render/mod.rs`, and the tests stay as they
  are. The owner chose this for 0.2.2 on 2026-11-04 (#18).
