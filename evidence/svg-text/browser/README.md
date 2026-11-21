# Browser selection in the proposed SVG text layer

[hayro#1739](https://github.com/LaurenzV/hayro/pull/1739) adds an optional transparent Unicode text layer above the existing glyph paths. This probe uses the upstream implementation at `623a2508d385852ebfc896cee16cd683996f34b6`. It checks what browser users can find and select before pyropdf adopts an upstream release. [pyropdf#435](https://github.com/yhay81/pyropdf/issues/435)

## Results

On macOS, Chromium 159.0.8154.6, Firefox 159.0 and WebKit 27.2 found and selected all 14 non-Hebrew queries per browser, plus the visual-order Hebrew control. These cover simple and substitute fonts, OCR text, escaped XML characters, a ligature, Japanese, rotation and shear. Double-clicking fixed points on the PDF page selected the intended word in all 12 horizontal, rotated, sheared and invisible controls. Each point intersected a glyph of that word. Each browser's six screenshot pairs had identical decoded RGBA pixels. The path-only controls contained zero SVG text elements and gave no match for the first query on each page. [Per-query results](results.tsv)

The embedded Hebrew fixture spells `שָׁלוֹם עולם` and paints its glyphs in visual order. All three browsers found the emitted string `םלוע םֹולָׁש`. Searching for the logical Hebrew phrase returned no match in any browser. The upstream layer preserves glyph order, so a release claim covering logical right-to-left search needs further work. [Fixture description](../../../tests/assets/text/README.md)

We shifted just the transparent layer by 200 SVG units right and 65 down. None of the twelve fixed points intersected the target word after the shift. Double-click still selected that word at 2 of four points in Chromium, 0 in Firefox and 0 in WebKit. Browser selection can extend to nearby text, so the probe checks both the glyph hit and the selected word. The path-only files selected none of the four words at the same points.

The browser chooses the font used for the invisible selection layer. On the substituted CJK page the selection for `計画地` measured 120.00 CSS pixels wide in Chromium and WebKit and 125.22 in Firefox at device scale 1. Horizontal, rotated and sheared selection boxes also varied across engines. The screenshots compare each browser with its own path-only rendering at a 900 × 800 viewport.

## Reproduce

Run from a checkout of pyropdf with Git, Cargo, Node.js and npm installed. The command installs the three browsers in Playwright’s usual cache, builds the upstream converter in the scratch directory and writes `browser.tsv` there. Set `PLAYWRIGHT_BROWSERS_PATH` to choose a different browser cache.

```sh
scratch=$(mktemp -d)
bash evidence/svg-text/browser/run.sh "$scratch"
```

The two modes use the same input page, interpreter settings and renderer build. The browser performs a case-sensitive find and reads back its selection. For the pointer tests, the four PDF coordinates come from the fixture’s text matrices and 18-point Helvetica advances. The script flips the PDF y coordinate using the 180-point page height and maps the point through the root SVG viewport. It examines the transparent text element only to check whether a glyph intersects that independently chosen point. Screenshots are taken after fonts load and the browser has completed two animation frames. The probe compares every decoded pixel before running queries and records the browser's range bounds in CSS pixels. On a different host the installed fallback fonts can change these bounds.

`positions.pdf` is a 300 × 180 point Helvetica page with four text matrices or rendering modes. It was written for this probe. The other inputs are the upstream Unicode and OCR fixtures and the three [pyropdf text fixtures](../../../tests/assets/text/README.md). Playwright Core 1.65.1 pins the browser builds and pngjs 7.0.0 decodes the screenshots.
