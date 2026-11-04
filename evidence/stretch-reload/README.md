# The cost of loading a font again for its stretch

Text extraction takes hayro's stretch of a substitute glyph out of the glyph's
advance (#656). Only `hayro_font::Font::glyph_stretch` tells the stretch, of a
font that `hayro-font` loads, and `hayro_interpret` does not share the font it
loaded, so `crates/pyropdf-engine/src/extract/stretch.rs` loads it again.

`#656`'s notes proposed loading a font again only when its dictionary names no
font program. That misses a program that hayro does not read: hayro then draws
the same stretched substitute, and pages 9 to 11 of
`tests/assets/text/nonembedded-latin.pdf` read with stretched advances. So a
run's glyphs decide instead: a font loads when the x basis of a glyph's
transform is not the length of its y basis, which a stretch leaves, and which
`Tz` and a text matrix that is no similarity leave too. Those then load a font
that tells a stretch of 1.

`time_words.py` compares three builds of that decision in one extension, which
`timing-toggle.patch` selects by an environment variable: `off` leaves the
stretch in, `on` is the filter as merged, and `always` loads every font of a
page again.

```bash
git apply evidence/stretch-reload/timing-toggle.patch
uv sync
uv run --no-sync python evidence/stretch-reload/time_words.py
```

`results.txt` holds the run on 2026-11-04, on Windows 11, in the release profile,
with the best of 7 rounds per document. A page's interpretation is its own, so
each page loads its fonts again.

## What it shows

- `always` costs what the issue feared: +39.3% on `usrguide`, +47.3% on
  `mhlw-doc`, and +15.1% on `nics-background-checks-2015-11`, whose fonts are
  embedded. This is why a font does not simply load every time.
- `on` is not told apart from `off`. Its total is -2.7% in this run and was +1.0%
  in the run before it, and a document can swing by about 10% either way:
  `mhlw-doc` reads +10.8% here, and no run of it passes the filter, so it loads
  nothing and that is noise.

## What it does not show

No corpus document sets text in an embedded font with `Tz` or an anisotropic
text matrix across its pages at a scale where the load would show. Such a
document would pay about what `always` pays, once per font and page. That is an
estimate from the `always` column, not a measurement.
