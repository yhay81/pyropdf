# Other instances of a variable face, and colour glyphs (issue #598)

The measurements behind the recommendation in #598: what each option for
canvas text costs, and what text extraction then reads. Nothing here is
built into pyropdf; each option exists only as a prototype in this directory.

## Method

```bash
cargo build --release --locked --manifest-path evidence/font-instances-colour/instance/Cargo.toml
uv run --with fonttools --with pypdfium2 --with pymupdf python evidence/font-instances-colour/measure_instances.py PROBE results/instances.json
uv run --with fonttools --with pypdfium2 --with pymupdf python evidence/font-instances-colour/measure_colour.py results/colour.json OUTPUT_FOLDER
uv run --with fonttools python evidence/font-instances-colour/colr_features.py C:/Windows/Fonts/seguiemj.ttf results/colr-features.json
uv run --with pypdfium2 --with pymupdf python evidence/font-instances-colour/control.py ARABIC_FACE results/control.json
```

- `instance/` writes a static TrueType program for one instance with the
  skrifa and harfrust that pyropdf already builds against: 367 lines. Every
  glyph keeps its ID, composites are flattened, and instructions go.
- `measure_instances.py` loads that program with `Font.from_bytes`, stamps 100
  pages, and saves. It compares every glyph with fontTools' instancer as an
  oracle. It also draws the same glyphs as paths in `/ActualText`, inline and
  as Form XObjects.
- `measure_colour.py` reports what the canvas does today with a colour face,
  then paints eight COLR v1 emoji with a Type 3 prototype and with Forms in
  `/ActualText`, and one image glyph with a soft mask.
- Four readers read each file: `page.text()`, xpdf 4.06's `pdftotext`, MuPDF,
  and PDFium. hayro, MuPDF, and PDFium render it.

The faces are those that Windows 11 installs, plus pyropdf's own Arabic test
face. Fonts and the renders of them are not committed. AMD Ryzen 5 3600XT,
Windows 11, 2026-11-04.

## Other instances

| Face | Instance | Glyphs | Face KiB | Static KiB | Instancing ms | fontTools s | Glyphs off from fontTools | Saved program KiB | 100 pages KiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Bahnschrift | wght 700 | 961 | 353 | 152 | 7.2 | 0.67 | 0 | 1.24 | 43.4 |
| Bahnschrift | wght 300, wdth 75 | 961 | 353 | 148 | 9.2 | 0.32 | 0 | 1.21 | 43.4 |
| Segoe UI Variable | wght 700 | 2530 | 1808 | 412 | 20.6 | 2.93 | 0 | 1.30 | 40.8 |
| Sitka | wght 700 | 1185 | 504 | 227 | 10.7 | 1.21 | 0 | 1.84 | 44.1 |
| Noto Sans JP | wght 400 | 17936 | 9366 | 5574 | 313.1 | 11.88 | 0 | 2.05 | 64.5 |
| Noto Sans JP | wght 700 | 17936 | 9366 | 5564 | 368.5 | 12.02 | 0 | 2.04 | 64.5 |
| Noto Sans Arabic subset | wght 700 | 56 | 39 | 10 | 0.7 | 0.05 | 0 | 0.79 | 38.0 |

"Off" counts glyphs whose advance or bounds differ by more than one unit, or
whose area differs by more than 1%. A page of the prototype's program and one
of fontTools' differ in 3 to 42 bytes of the raster at 144 dpi, and a page at
the default instance differs in 1890 to 11232, so the instance does draw.

- **The program is cheap enough to write at load.** The largest face takes
  about a third of a second, so the provisional program with all its glyphs
  (DESIGN.md section 7) can be the instance's, and everything downstream
  treats it as a static face. A save subsets it as any other: two weights of
  one face in one document make two programs and read back correctly.
- **Shaping needs the source.** harfrust takes the instance's axes directly.
  Shaping the prototype's static program, which copies `GPOS` as it is, gave
  4 of 23 glyphs of the Noto Sans JP text other advances than shaping the
  source at the instance. Kerning has variation deltas, so a face must keep
  the source for shaping.
- **The default instance is sometimes Thin.** `NotoSansJP-VF.ttf`, which
  Windows 11 installs, has its default at wght 100, and `Font.from_file(path,
  weight=400)` is refused today, so pyropdf can draw that face only in Thin.
  pylopdf's `with_weight` drew these (its `ar`, `he`, `hi`, and `th` extras
  are variable; `_regular_instance` exists because Noto Sans Hebrew's default
  is Thin).
- **Not covered.** CFF2 stays refused: it has no PDF font type, and the
  prototype writes only `glyf`. The prototype also has no budget. A real
  instancer must charge its output, because flattening composites can
  amplify, and must refuse coordinates that deltas push outside `i16`.

## What each option reads back

100 pages of one line; sizes are `to_bytes()` for pyropdf's writer and a plain
hand-written file for the prototypes, so compare them as orders of magnitude.

| Case | Option | pyropdf | xpdf | MuPDF | PDFium | `page.content()` | KiB |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| Instance | Static program | text | text | text | text | `TextRun` | 38 to 65 |
| Instance | Paths in `/ActualText`, inline | nothing | nothing | nothing | nothing | `Path` | 175 to 452 |
| Instance | Paths in `/ActualText`, Forms | nothing | nothing | nothing | nothing | `Path` | 42 to 66 |
| Colour | Type 3 | text | text | text | text | `TextRun` | 134 |
| Colour | Forms in `/ActualText` | nothing | nothing | nothing | nothing | `Path` | 153 |

The empty results are the readers' rule, not a fault of these files.
`/ActualText` around a real glyph reads as the replacement in xpdf, MuPDF, and
PDFium (`results/control.json`), so they honour it only where a glyph is shown.
`page.text()` does not read `/ActualText` at all. Acrobat was not tried.

## Colour glyphs

### What the canvas does today

| Face | Result |
| --- | --- |
| Segoe UI Emoji (COLR v1 with fallback outlines) | Draws the monochrome fallback; all four readers read the text |
| COLR v1, empty outline | **Draws nothing, and `page.text()` reads the text** |
| `sbix`, empty outline | **Draws nothing, and `page.text()` reads the text** |
| `SVG `, empty outline | **Draws nothing, and `page.text()` reads the text** |
| Bitmaps, no `glyf` | Refused: "the face has no outlines" |

The empty-outline faces are built by the script. A blank glyph with readable
text is a silent failure, whatever the choice for colour.

### What drawing COLR takes

Segoe UI Emoji's 3487 colour glyphs, by the hardest thing each needs from a
PDF writer (`results/colr-features.json`):

| Needs | Glyphs | Share |
| --- | ---: | ---: |
| Solid layers only | 6 | 0.2% |
| Opaque shadings | 4 | 0.1% |
| Shadings with alpha stops (soft masks) | 3406 | 97.7% |
| `PaintComposite` (transparency groups, blend modes) | 71 | 2.0% |

A glyph has 61 `PaintGlyph` layers at the median, 119 at the 95th percentile,
and 857 at most. One of the eight sample glyphs is 163 KB of operators.

The Type 3 prototype paints shadings and transforms but ignores alpha stops
(218 in the eight glyphs) and `PaintComposite`, so its emoji come out too dark:
hayro, MuPDF, and PDFium agree with each other, not with the font. The
canvas has no writer for any of this yet: `a canvas fills with a Color;
pattern paints cannot be drawn yet`.

An image with a soft mask inside a Type 3 glyph renders in all three and reads
as text in all four readers, so a bitmap face needs only the Type 3 writer and
the canvas's image objects.

### Code and dependencies

| Option | Code, measured against | New crates in `Cargo.lock` | Fuzzing |
| --- | --- | ---: | --- |
| Instance: static program | 367-line prototype, without budget or tests | 0 | Extend `fuzz/fuzz_targets/fonts.rs` |
| Instance: `subsetter` with `variable-fonts` | The crate, unbudgeted; it renumbers glyphs | 7, with another skrifa and read-fonts | Extend `fonts.rs` |
| Instance: paths in `/ActualText` | Small, but it reads as nothing | 0 | None |
| Colour: refuse a blank colour glyph | Tens of lines | 0 | Extend `fonts.rs` |
| Colour: Type 3, bitmaps | krilla 0.8.2: `type3.rs` 514, `glyph/bitmap.rs` 81, `glyph/mod.rs` 205 | 0 | New target |
| Colour: Type 3, COLR | krilla adds `glyph/colr.rs` 581, `shading_function.rs` 865, `shading_pattern.rs` 60, `mask.rs` 128 | 0 | New target; skrifa bounds paint cycles and depth, the budget must bound output |
| Colour: Type 3, `SVG ` | `usvg` | 23 | XML from the input, with no hook for the budget |
| Colour: Forms in `/ActualText` | As Type 3 without the font | 0 | New target, and it reads as nothing |

Crate counts compare `cargo tree -e normal` of `subsetter` 0.2.6 and `usvg`
0.47.0 with the workspace's `Cargo.lock`. A Type 3 font holds 256 codes, so
more distinct colour glyphs need more font dictionaries, and a run that mixes
colour and plain glyphs switches fonts.

## Found on the way

`results/control.json`: at the default instance, so unrelated to this issue,
MuPDF and PDFium read pyropdf's Arabic test word with a stray `&`. The shaper
splits ب into a base and a dot glyph in one cluster; only the base gets a
ToUnicode entry, and those readers fall back to CID 0x26 as a character.
`page.text()` warns that it skipped one glyph.
