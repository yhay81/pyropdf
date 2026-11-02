<!--
Draft for LaurenzV/hayro (yhay81/pyropdf#435). Not filed: filing needs
the owner's go-ahead. Title:

hayro-svg: an optional invisible text layer, so converted text can be selected
-->

Hi! hayro-svg draws every glyph as a path (`<use>` of an outline in
`<defs>`), and it skips glyphs drawn with `DrawMode::Invisible`. The
resulting SVG looks right, but its text can't be selected, searched or
copied in a browser, and the text of OCR'd scans (`3 Tr`) disappears
entirely.

Would you accept an opt-in text layer in hayro-svg? I'd like to ask
before sending a pull request, since I know you prefer to keep hayro
focused on rendering (#452, #1049).

## What it would do

A new `SvgRenderSettings::text_layer: bool`, `false` by default. When it
is off the output is unchanged, byte for byte. When it is on, glyphs are
still drawn as paths exactly as now, and an invisible
`<g id="text-layer" fill-opacity="0" xml:space="preserve">` is written
after all painted content, the way pdf.js lays text over its canvas:

```xml
<text transform="matrix(…)" font-size="1000" x="0 556 1112 1390">Text</text>
```

- The text of each glyph is `Glyph::as_unicode`; glyphs without a mapping
  are left out. No word, line or layout detection: only the per-glyph
  text and position that the interpreter already exposes.
- The transform is the one the glyph's path uses (`props.transform *
  glyph.transform()`, flipped for SVG's y-down text), so rotated, skewed
  and scaled text lands on its paths. Consecutive glyphs that share a
  linear part form one `<text>` with an `x` list of glyph origins (plus a
  `y` list when they leave the baseline); a change starts a new element.
- Only the page's own content is recorded: recording is off inside
  `with_dummy` and `finish`, so masks, tiling patterns and Type 3 glyph
  procedures add nothing.
- Invisible glyphs are recorded, which is what makes OCR'd scans
  searchable.
- The layer grows linearly with the drawn glyphs and adds no recursion.

Real fonts (`<text>` with `@font-face`) aren't proposed: Type 1 fonts
have no font data, CFF and subset fonts often don't load in browsers, and
the output would then depend on font availability. The overlay leaves the
visual result identical to today's.

## Measurements

A patch of about 170 lines (a new `text.rs` and a few lines in `lib.rs`),
against hayro-svg 0.8.0, on 156 pages of 15 real-world PDFs:

- With the option off, every page's SVG is identical to 0.8.0's.
- With it on, all pages are well-formed XML, and the text of each page's
  layer, read back with an independent XML parser, equals the
  concatenated `as_unicode` of its glyphs, including Japanese text and an
  OCR scan whose 11,221 glyphs are all invisible.
- Output grows by 8.6% overall (0% for pages without text, at most 21.5%).

One pitfall I hit: `xmlwriter` 0.1's `write_text` escapes `<` but not `&`,
so the patch escapes `&` and `>` itself.

The patch also applies to `main`'s `lib.rs`. `ActualText` (now reachable
through the marked-content properties, #1625) could replace the text of
its span in a follow-up.

If this sounds acceptable, I'll open a PR against `main` with tests; if
you'd rather not have it in hayro-svg, that's fine too, and I'd build the
layer on our side from a device instead.
