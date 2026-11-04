# COLR glyphs drawn as a Type 3 font (issue #633)

Does canvas text paint every COLR glyph of a real colour face, or refuse it as
DESIGN.md section 7 says, and what do the drawings cost and look like?

## Method

```bash
cargo run --release --locked --manifest-path evidence/colr-glyphs/paint/Cargo.toml -- C:/Windows/Fonts/seguiemj.ttf > evidence/colr-glyphs/results/segoe-ui-emoji.json
uv run --with "blackrenderer[skia]" --with fonttools --with pymupdf --with pypdfium2 python evidence/colr-glyphs/compare.py C:/Windows/Fonts/seguiemj.ttf OUTPUT_FOLDER > evidence/colr-glyphs/results/compare.json
```

- `paint/` checks and paints every glyph of the face with the engine's
  `fonts::colr` painter, each in a document of its own under the default
  limits, as canvas text does. It counts what the check says of each glyph
  and whether the paint agrees, and what each painted glyph writes: its
  objects, the bytes of its streams as written (compressed where that is
  shorter), and the time of its check and paint.
- `compare.py` draws each of eight emoji (those of #598) alone at 64 points
  and renders the page at 144 dpi in hayro, MuPDF, and PDFium. blackrenderer,
  a COLR v1 renderer on Skia that shares no code with pyropdf, renders the
  same glyph at the same place as the reference. A pixel's difference is the
  largest of its three channels, over white, among the pixels that the
  reference inks. It then draws one page of 100 distinct emoji, times it,
  saves it three ways, renders it, and reads it back with four readers.

Segoe UI Emoji is the face that Windows 11 installs; it is not committed.
AMD Ryzen 5 3600XT, Windows 11, 2026-11-04.

## Every glyph

`results/segoe-ui-emoji.json`:

| Glyphs | Colour glyphs | Paint | Fallback outline | Refuse | Check and paint disagree |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 64,883 | 3,487 | 3,487 | 0 | 0 | 0 |

| Per painted glyph | Median | 95th percentile | Most |
| --- | ---: | ---: | ---: |
| Objects | 214 | 412 | 975 |
| Stream bytes | 5,903 | 12,616 | 170,171 |
| Check and paint, ms | 5.4 | 14.6 | 138.1 |

All 3,487 glyphs paint: the face uses solid colours, linear and radial
gradients, transforms, and composite modes that PDF expresses, and no sweep
gradient. The 97.7% of #627 whose gradients have alpha paint through soft
masks. Their objects are mostly the shadings, functions, and soft masks of
those gradients, which equal gradients share, among glyphs too. The glyph of
170 KB is the largest of #627's samples, 163 KB of operators there.

## Against another renderer

RESULTS

## A page of 100 emoji

PAGE

## Findings

FINDINGS
