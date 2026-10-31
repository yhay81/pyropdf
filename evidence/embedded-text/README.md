# Embedded-font text without krilla (route C of #466)

Can pyropdf shape text and write embedded Unicode and OpenType fonts itself,
with one font program per document however many drawings use the face?

## Method

`probe/` is a standalone crate (not a workspace member) that does what
route C needs, in 365 lines:

- shapes each text with harfrust 0.15 (the HarfBuzz port on the fontations
  crates that hayro and skrifa already use), with script and direction
  guessed per text;
- registers the face once per document: one Type0 font with Identity-H
  encoding, one CIDFontType2 (TrueType) or CIDFontType0 (CFF) font with
  CID = GID, one descriptor, one program, and one ToUnicode CMap, shared by
  every page;
- writes each page's text as a `TJ` with the shaper's advances and offsets,
  and `Ts` for vertical offsets;
- maps each glyph to Unicode by cluster: when every glyph of a cluster is the
  nominal glyph of one of its characters (a base and its marks), each glyph
  maps to its own character; otherwise, as in a ligature, the first glyph
  maps to the whole cluster. A glyph met again with a different text is
  counted as a conflict, which would need `ActualText`;
- embeds the whole program, as the provisional font object of DESIGN.md
  section 7 holds it, optionally pruned (`PRUNE=1`) to the tables a PDF
  consumer reads (ISO 32000-2 9.9): `glyf`, `loca`, `head`, `hhea`, `hmtx`,
  `maxp`, `cvt `, `fpgm`, `prep` for TrueType, and `CFF `, `head`, `hhea`,
  `hmtx` for CFF. GSUB, GPOS, GDEF, `gvar`, and the other variation tables
  are dropped; a variable face keeps its default instance.

`measure.py` runs the probe, opens the result with pyropdf, checks that every
page's text holds the drawn characters and that the first page has ink,
subsets with `doc.subset_fonts()` (#364, which keeps glyph IDs and empties
the outlines of unused glyphs), saves, and counts the font programs.
`krilla_baseline.py` draws the same stamps with pylopdf 0.16.0, through
krilla's public API, one `page.draw()` per page.

```bash
uv run python evidence/embedded-text/measure.py HEBREW LATIN JAPANESE
uv run --no-project --with pylopdf==0.16.0 python -I evidence/embedded-text/krilla_baseline.py HEBREW LATIN JAPANESE
```

The faces are not committed: HEBREW is pylopdf-fonts-he's
`NotoSansHebrew-Variable.ttf` (470 glyphs, the face of DESIGN.md section
2.1), LATIN is `NotoSans-Regular.ttf` (static TrueType, 3,246 glyphs), and
JAPANESE is `NotoSansJP-Regular.otf` (CID-keyed CFF, 17,850 glyphs), the
last two from Pillow 13.0.0's test fonts. "Same" text draws one string on
every page; "varying" draws two letters per page, as
`evidence/pylopdf-baseline/font_subsets.py` does.

## Results

Apple M2 Pro, 2026-10-31. Route C, saved by pyropdf after
`doc.subset_fonts()`:

| Face | Pages | Text | Pruned | Glyphs | Programs | Saved KiB | Kept/glyphs | Write s | Subset s | Text | Ink |
| --- | ---: | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | --- | --- |
| hebrew | 1 | same | no | 6 | 1 | 57.6 | 7/470 | 0.006 | 0.006 | ok | ok |
| hebrew | 1 | same | yes | 6 | 1 | 3.7 | 7/470 | 0.001 | 0.001 | ok | ok |
| hebrew | 100 | same | no | 6 | 1 | 94.0 | 7/470 | 0.004 | 0.006 | ok | ok |
| hebrew | 100 | same | yes | 6 | 1 | 40.2 | 7/470 | 0.002 | 0.004 | ok | ok |
| hebrew | 100 | varying | no | 22 | 1 | 89.1 | 23/470 | 0.004 | 0.006 | ok | ok |
| hebrew | 100 | varying | yes | 22 | 1 | 35.1 | 23/470 | 0.001 | 0.004 | ok | ok |
| latin | 1 | same | no | 9 | 1 | 63.4 | 10/3246 | 0.013 | 0.005 | ok | ok |
| latin | 1 | same | yes | 9 | 1 | 12.0 | 10/3246 | 0.010 | 0.003 | ok | ok |
| latin | 100 | same | no | 9 | 1 | 105.9 | 10/3246 | 0.013 | 0.008 | ok | ok |
| latin | 100 | same | yes | 9 | 1 | 54.5 | 10/3246 | 0.011 | 0.006 | ok | ok |
| latin | 100 | varying | no | 26 | 1 | 95.9 | 27/3246 | 0.013 | 0.008 | ok | ok |
| latin | 100 | varying | yes | 26 | 1 | 44.3 | 27/3246 | 0.011 | 0.006 | ok | ok |
| japanese | 1 | same | no | 9 | 1 | 411.3 | 10/17850 | 0.107 | 0.043 | ok | ok |
| japanese | 1 | same | yes | 9 | 1 | 288.5 | 10/17850 | 0.102 | 0.035 | ok | ok |
| japanese | 100 | same | no | 9 | 1 | 447.0 | 10/17850 | 0.105 | 0.043 | ok | ok |
| japanese | 100 | same | yes | 9 | 1 | 324.3 | 10/17850 | 0.096 | 0.039 | ok | ok |
| japanese | 100 | varying | no | 17 | 1 | 442.2 | 18/17850 | 0.108 | 0.043 | ok | ok |
| japanese | 100 | varying | yes | 17 | 1 | 319.2 | 18/17850 | 0.099 | 0.038 | ok | ok |

krilla through pylopdf 0.16.0, saved by pylopdf:

| Face | Pages | Text | Programs | Saved KiB | dedupe=True KiB |
| --- | ---: | --- | ---: | ---: | ---: |
| hebrew | 1 | same | 1 | 3.6 | 3.6 |
| hebrew | 100 | same | 100 | 330.2 | 29.6 |
| hebrew | 100 | varying | 100 | 290.2 | 82.8 |
| latin | 1 | same | 1 | 6.3 | 6.3 |
| latin | 100 | same | 100 | 600.7 | 32.3 |
| latin | 100 | varying | 100 | 527.3 | 154.5 |
| japanese | 1 | same | 1 | 3.9 | 3.9 |
| japanese | 100 | same | 100 | 359.3 | 29.9 |
| japanese | 100 | varying | 100 | 266.4 | 68.3 |

pyropdf after #482, the same stamps through `page.draw()`
(`pyropdf_stamps.py`, same faces), with the save's subsetting:

```bash
uv run python evidence/embedded-text/pyropdf_stamps.py HEBREW LATIN JAPANESE
```

| Face | Pages | Text | Saved KiB | dedupe=True KiB | + object streams KiB |
| --- | ---: | --- | ---: | ---: | ---: |
| hebrew | 1 | same | 2.8 | 2.7 | 2.4 |
| hebrew | 100 | same | 36.9 | 19.7 | 3.8 |
| hebrew | 100 | varying | 36.5 | 24.6 | 8.6 |
| latin | 1 | same | 6.0 | 6.0 | 5.6 |
| latin | 100 | same | 40.7 | 22.9 | 7.0 |
| latin | 100 | varying | 36.7 | 25.5 | 9.6 |
| japanese | 1 | same | 3.2 | 3.2 | 2.8 |
| japanese | 100 | same | 38.8 | 20.1 | 4.2 |
| japanese | 100 | varying | 37.0 | 24.6 | 8.6 |

With `dedupe=True` on both sides, every row is smaller than krilla's:
100 stamps of the same text save to 19.7 to 22.9 KiB against krilla's
29.6 to 32.3, and varying text to 24.6 to 25.5 KiB against 68.3 to 154.5.
Without `dedupe`, the 100 identical content streams stay separate objects.

Route C's 100-page rows carry about 0.37 KiB per page of page objects and
uncompressed content streams that neither writer's font changes; the probe
does not compress content.

Other checks:

- hayro (`page.pixmap`) and poppler (`pdftoppm`) draw the Hebrew points and
  the Japanese CFF text correctly. poppler's `pdftotext` extracts every
  sample in order. The CFF face is CID-keyed with an identity charset, so
  CID = GID held; a CID-keyed face whose charset is not the identity would
  not.
- The samples had no ToUnicode conflicts. The face without Japanese glyphs
  drew .notdef, which gets no ToUnicode entry.
- pyropdf's extractor splits a right-to-left line with combining marks into
  two lines, for krilla's output as for the probe's: #469. With krilla's
  mapping (the first glyph of a cluster carries the whole cluster) poppler
  also attaches the holam to the wrong letter; the per-glyph mapping above
  does not.

## Findings

1. **One program for 100 stamps holds.** One font resource per document,
   referenced from every page, embeds one program in every row, with the
   same or varying text, against krilla's 100. With pruning, 100 varying
   Hebrew stamps save to 35.1 KiB against krilla's 290.2 KiB (82.8 KiB with
   `dedupe=True`), and Latin to 44.3 KiB against 527.3 (154.5).
2. **The subset at save must prune tables.** `fonts::subset` empties
   outlines only. Without pruning, the variable Hebrew face keeps 51 KB of
   `gvar` and 20 KB of GPOS, and a one-page stamp is 57.6 KiB instead of
   3.7. The pruned list is fixed by ISO 32000-2 9.9.
3. **Large faces need glyph compaction, still without rewriting content.**
   Keeping glyph IDs leaves an entry per glyph in `loca`, `hmtx`, the CFF
   CharStrings and charset: 10 Japanese glyphs save to 288.5 KiB, against
   krilla's 3.9 KiB, which renumbers. The content need not change: content
   codes stay the original glyph IDs as CIDs, and the save maps them to the
   compacted program through `CIDToGIDMap` (TrueType; for 17,851 CIDs and 18
   glyphs the map is 35,702 bytes raw and 116 bytes with Flate) or through
   the CFF charset of a CID-keyed program (CFF). DESIGN.md section 7's rule
   "glyph IDs stay stable, so content written earlier needs no rewrite"
   therefore holds for CIDs, not program glyph IDs.
4. **Shaping is the caller's either way.** krilla 0.8.2 shapes with
   rustybuzz inside `Surface::draw_text` (`src/text/shape.rs`, 102 lines);
   `draw_glyphs` takes glyphs shaped elsewhere. The probe uses harfrust 0.15,
   which depends on read-fonts 0.46. The engine and hayro-interpret both use
   skrifa 0.46.2 and read-fonts 0.43.3, so updating the engine's skrifa alone
   would not remove that older dependency. harfrust 0.13.3 uses read-fonts
   0.43.3 and can share it; the production version should follow hayro's
   read-fonts version. harfrust bounds its own work (`budget.rs`); pyropdf
   polls its budget per text run.
5. **pdf-writer is not needed.** The canvas is written in Python and adds
   its content and objects through the store (`python/pyropdf/_canvas.py`),
   and the font dictionaries are store values (F1). What pdf-writer would
   add is byte output for the content operators, which `standard/ops.rs`
   and the canvas already write, and the ToUnicode CMap, about 60 lines.
6. **What ToUnicode cannot hold needs ActualText.** ToUnicode maps a glyph,
   not an occurrence; a glyph that two clusters map differently, such as a
   contextual form shared by two letters, needs `/ActualText` on its
   marked content. Counting conflicts as the probe does finds them.

## Conclusion

Route C is adoptable. Shaping is a dependency (harfrust), not pyropdf code;
the font writer is small; subsetting exists and needs two extensions; and
one program per face per document follows from registering the face in the
document rather than in a drawing.

Size of the production work, judged from the probe and krilla's text
module (3,123 lines, most of them for Type 3, COLR, SVG, and bitmap glyphs,
which route C leaves out at first):

| Part | Where | Rust lines |
| --- | --- | ---: |
| Shaping runs: harfrust, bidi and script runs, cluster-to-text, budgets | `pyropdf-engine/src/text/` or `fonts/shape.rs` | 250 to 350 |
| Font resource: Type0, CIDFont, descriptor, `W`, ToUnicode, ActualText spans, provisional object | `pyropdf-engine/src/fonts/embed.rs` | 300 to 450 |
| Subset at save: table pruning, compaction with `CIDToGIDMap` or CFF charset | `fonts/sfnt.rs`, `fonts/cff.rs` | 250 to 350 |
| Python `Font` from a file, canvas text with it | `_fonts.py`, `_canvas.py`, binding | 250 to 350 (Python) |

New dependency: harfrust, using a release compatible with hayro's read-fonts.
The probe used harfrust 0.15; 0.13.3 shares hayro's current read-fonts 0.43.3.
No pdf-writer, rustybuzz, or subsetter.

Fuzzing is needed, because a user's font file is untrusted input to code
that pyropdf owns: the table walk of pruning and compaction, the CFF
charset rewrite, and the reading of metrics. Two targets: `embed` (font
bytes and text → shape, register, write, subset; never panics, and the
result parses and draws through hayro) and `subset` (font bytes and a glyph
set → pruned, compacted program that read-fonts reads back with the same
outlines for the kept glyphs). read-fonts and skrifa are fuzzed upstream in
OSS-Fuzz; harfrust's fuzzing was not checked.
