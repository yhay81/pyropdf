# Selectable text in hayro-svg (#435)

This directory holds the pre-publication item of #435: what hayro-svg does
with text today, the related upstream discussion, a design for a text
layer, and a draft upstream issue with a patch. Nothing here has been
published in LaurenzV/hayro.

- `probe.rs`, `probe.Cargo.toml`: counts, per page, the glyphs that
  hayro-interpret 0.8.0 passes to a device and what hayro-svg 0.8.0 writes.
- `draft.patch`: the proposed hayro-svg change, against hayro-svg 0.8.0.
- `layer.rs`, `check_layer.py`: build the patched crate, write each page's
  SVG with the layer, parse it with Python's XML parser, and compare the
  layer's text with `Glyph::as_unicode`.
- `run.sh <scratch-dir>`: reproduces `results/`.
- `draft-issue.md`: the upstream issue, to be filed only with the owner's
  go-ahead.

The corpus is `tests/assets/real_world` (15 files, 156 pages).

## Current capability

### hayro-svg 0.8.0 (the version that pyropdf pins)

- `SvgRenderSettings` has one field, `bg_color`. There is no text option.
- `draw_glyph_run` draws each glyph as `<use xlink:href="#gN">` of an
  outline path in `<defs>`, or, for a Type 3 glyph, as `<use>` of its
  interpreted procedure. No `<text>` element is ever written.
- Glyphs drawn with `DrawMode::Invisible` (text rendering mode 3) are
  skipped, so an OCR text layer leaves no trace in the SVG.
- `results/hayro-svg-0.8.0.tsv`: hayro-interpret passes 375,337 glyphs to
  a device; 364,116 are visible and become exactly 364,116 `<use>`
  elements; 11,221 are invisible (all of `patent-us223898.pdf`, an OCR
  scan) and are dropped; 0 `<text>` elements. `Glyph::as_unicode` maps
  375,334 of the 375,337; the 3 unmapped are the Type 3 glyphs of
  `pdfium-type3.pdf`, which have no `ToUnicode`.

So the interpreter already gives what a text layer needs (Unicode per
glyph and its transform); only the SVG writer drops it.

### hayro `main` (04a801de, 2026-11-01)

hayro-svg changed in 3 files since `hayro-svg-v0.8.0` (logging removed,
`RenderCache` no longer `Clone`, tiling pattern opacity); `glyph.rs` is
unchanged, so text output is the same. hayro-interpret on `main` adds
what a text layer can use:

- `Glyph::char_code` and `GlyphRun::writing_mode` (LaurenzV/hayro#1605).
- Glyph-name fallback for Type 3 Unicode (LaurenzV/hayro#1512) and
  `Type3Glyph::advance_width` (LaurenzV/hayro#1678).
- Text drawn only as a clipping path (`7 Tr`) reaches the device as
  `Invisible` (LaurenzV/hayro#1677).
- The marked-content property list as a dictionary, so `ActualText` can
  be read (LaurenzV/hayro#1513, LaurenzV/hayro#1625).

The `main` numbers were not measured: building hayro `main` from an
archive was refused in this session's sandbox. `draft.patch` applies to
`main`'s `hayro-svg/src/lib.rs` (checked with `patch --dry-run`), and the
APIs it calls exist there, but it was compiled and run on 0.8.0 only.

## Related upstream issues and pull requests

None asks for text in hayro-svg. The maintainer's stated scope:

- LaurenzV/hayro#452 (closed): a device that outputs text. The maintainer
  would expose Unicode and position per glyph but not word, line or
  paragraph detection.
- LaurenzV/hayro#1049 (closed PR): structured text extraction. Closed
  because hayro keeps its focus on rendering and leaves anything beyond
  the raw text fragments to clients.
- LaurenzV/hayro#809 (merged): `OutlineGlyph::font_data`, font bytes and
  metadata "for embedding"; `None` for Type 1 fonts.
- Open, from text extraction work: LaurenzV/hayro#1620 (font properties
  of glyph runs), LaurenzV/hayro#1621 (text state and `TJ` adjustments),
  LaurenzV/hayro#1623 (UCS2 CMap Unicode for embedded CID fonts).

A text layer is per-glyph Unicode at per-glyph positions, written by the
renderer as part of its output, which is the raw-fragment level that the
maintainer accepts. Whether it belongs in hayro-svg is still the
maintainer's call, so the draft asks before it offers the patch.

## Design

**Mode.** `SvgRenderSettings::text_layer: bool`, default `false`. Off, the
output is byte-identical to today's: `results/draft-layer.tsv` matches the
0.8.0 sizes on all 156 pages, and pyropdf's path mode stays the default.
On, glyphs are still drawn as paths and an invisible
`<g id="text-layer">` follows all painted content, as pdf.js's text layer
does. Rendering text with real fonts (`<text>` with `@font-face`) is not
proposed: Type 1 fonts have no `font_data`, CFF and subset fonts are
often not loadable by browsers or have no usable `cmap`, and the result
would depend on font availability, where the overlay leaves the visual
output exactly that of path mode.

**Unicode mapping.** Each glyph's text is `Glyph::as_unicode`, hayro's
mapping (`ToUnicode`, then glyph names, then `uniXXXX` names); a
`BfString::String` keeps all its characters. Unmapped glyphs are left out
rather than written as U+FFFD. Characters that XML 1.0 forbids are
dropped and whitespace is written as a space, inside
`xml:space="preserve"`. `ActualText` replacement is left to a follow-up:
on `main` it needs the marked-content stack in hayro-svg. No word or line
detection: spaces appear where the PDF draws space glyphs.

**Font identity.** None in the layer: the text is invisible, and the
generic font only sets the size of selection boxes. A consumer that needs
font identity reads it from the interpreter (`font_cache_key`,
LaurenzV/hayro#1620), not from the SVG.

**Positioning.** Every glyph keeps the transform that its path uses,
`props.transform * glyph.transform()`, flipped to SVG's y-down text.
Consecutive glyphs whose transforms share a linear part form one `<text>`
with that transform and `font-size="1000"` (hayro's glyph space is 1000
units per em), and each character gets its glyph's origin in an `x` list,
plus a `y` list when the glyphs leave the baseline (rise, vertical
writing). Rotated, skewed, and `Tz`-scaled text therefore sits where its
paths are; a change of linear part starts a new element. The extra
characters of a ligature are spread up to the next glyph's origin.
Selection boxes take their widths from the viewer's font, not the PDF's.

**What is recorded.** Only glyph runs drawn while the page itself is
interpreted. hayro-svg interprets masks, tiling patterns and Type 3 glyph
procedures in `with_dummy` or in `finish`; the patch turns recording off
there, so text inside a mask or a pattern cell, or a Type 3 procedure's
own text, never enters the layer. Invisible text (`3 Tr`, and `7 Tr` on
`main`) is recorded. The layer is above clipping and soft masks: clipped
text stays selectable.

**Bounded output.** The layer holds one entry per mapped glyph and one
element per run or change of transform, so it grows linearly with the
glyphs that the interpreter draws, the bound that already applies to the
`<use>` elements. It adds no recursion. Over the corpus it adds 8.6%
(48.3 MB to 52.5 MB), between 0% for pages without text and 21.5% for
`f1040.pdf`. hayro-svg still builds the whole string before returning, so
pyropdf's size limit is checked after conversion as before (#15).

**Escaping.** `xmlwriter` 0.1's `write_text` escapes `<` but not `&`,
which made 4 corpus pages malformed in a first version of the patch; the
patch escapes `&` and `>` itself.

## Checks

`check_layer.py` parses every SVG that the patched crate writes with the
layer on: all 156 pages are well-formed, every `x` list has one entry per
character, and every page's layer text equals the concatenated
`as_unicode` of its glyphs (XML-filtered). `mhlw-doc.pdf` covers Japanese
text and `patent-us223898.pdf` invisible OCR text.

Not checked here, and left to the adoption part of #435: selection and
search in a browser (the in-app browser renders local files only as static
snapshots), rotated and vertical text (not checked against the corpus),
and the comparison with path mode that the issue's third acceptance item
asks for.
