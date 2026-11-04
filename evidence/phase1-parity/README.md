# Phase 1 parity with frozen pylopdf

The differential evidence for the exit of Phase 1 (DESIGN.md §13): frozen
pylopdf and pyropdf read every corpus document, and pages in CJK fonts that
the file does not embed, and every difference in their outputs is explained
here.

| File | Role |
| --- | --- |
| `run.py PYLOPDF_CHECKOUT` | Builds pylopdf at the freeze commit `ebb01d3c` (#95) with its CJK font extras into its own environment, dumps both engines over `tests/assets/real_world`, `tests/assets/text/nonembedded-latin.pdf`, and `tests/assets/text/nonembedded-cjk.pdf`, and runs `compare.py` |
| `dump.py ENGINE OUT [--fallback-fonts] FILE...` | Writes one engine's outputs per page and each document's Markdown. The page outputs are listed below |
| `compare.py A B` | Classifies each output of each page and writes `report.md` and `results.json` |
| `causes.toml` | Matches each kind of difference to a cause below |
| `report.md` | Pages that are equal per document and output, with the causes of the rest |
| `results.json` | The kind and measurements of every compared output |
| `metadata.json` | The measured pyropdf revision, frozen oracle revision and extras, runtimes, and the hashes of the corpus and of the fallback pages |
| `diagnose_annotations.py ORACLE_PYTHON` | Reproduces the annotation-isolation experiment and writes `annotation-diagnostic.json` |

Per page, `dump.py` records:

- `text`: the text.
- `words`: the words.
- `words_exact`: the words with `exact_bboxes=True`.
- `layout`: the layout dictionary with characters and styles.
- `search`: the search boxes of the page's first 20 distinct words.
- `tables_lines` and `tables_text`: the tables of each strategy.
- `markdown`: the Markdown.
- `render`: a 72-dpi raster.

The corpus renders without fallback fonts in both engines: `dump.py`
turns them off. So does `tests/assets/text/nonembedded-latin.pdf`, whose pages
of #656 are in fonts that the file does not embed, or whose programs do not
load: hayro draws them in Standard 14 substitutes, stretched to the PDF's
widths. `tests/assets/text/nonembedded-cjk.pdf` holds the pages of
#600 in CJK fonts that the file does not embed: MS-Gothic in both descendant
types and both CMaps, MS-Mincho, and fonts of the GB1, CNS1, and Korea1
collections. With `--fallback-fonts`, both engines render it with the faces
of the font extras `jp`, `ko`, `zh-cn`, and `zh-tw`, which `run.py` installs
for pylopdf and pyropdf's dev group installs, in the same versions. The dump
then renders the first page before it reads any page, because frozen pylopdf
fills its faces at its first rendering, and text extraction before that gives
what it gives without them. pyropdf gives the same answer in any order.
`tests/test_parity.py` checks that the report covers the corpus and the
pages in fonts that the files do not embed and leaves no difference
unexplained. When `PYROPDF_PARITY_PYLOPDF` names the Python of
`target/parity/pylopdf-env`, it also redumps three corpus documents and those
pages and checks their kinds against `results.json`.

```bash
python3 evidence/phase1-parity/run.py ~/ghq/github.com/yhay81/pylopdf
PYROPDF_PARITY_PYLOPDF=target/parity/pylopdf-env/bin/python uv run pytest tests/test_parity.py
```

## Kinds of difference

`compare.py` sorts each output that is not equal into one kind:

- `whitespace`: the same words, with different spaces or line breaks.
- `boxes`: the same text and structure, with a number that moved by more
  than 0.01 point.
- `structure`: the same texts, grouped differently.
- `text`: different texts.
- `pixels` or `size`: a raster that is not close, or of another size.
- `error`: one engine raised.

## Causes

The run on 2026-11-04 covers the 15 corpus documents, 156 pages, the 13 pages
of `nonembedded-latin.pdf`, and the 7 pages of `nonembedded-cjk.pdf`, with
pyropdf on hayro 0.9 without its glyph cache and frozen pylopdf on hayro 0.8.
Of the 176 rasters, 175 match byte
for byte and one is close under the stated tolerance, which a cause below
explains. The JSON reading outputs and the Markdown match, except on
page 2 of `patent-us223898.pdf`, which a cause below explains. No output
raises. `metadata.json`
identifies the measured source revision and the hashes of the inputs. The
oracle cache is reused only with a matching stamp of the freeze commit and
the extras and an importable package; an unstamped or different cache is
rebuilt.

### hayro 0.9's glyph cache, turned off

By default (`RenderSettings::glyph_caching`), hayro 0.9 rasterizes a glyph of
text filled in one color once, with `ab_glyph_rasterizer`, and reuses the
bitmap, placed at a quarter of a pixel horizontally and at a whole pixel
vertically. hayro 0.8 filled every glyph where it lies, through vello_cpu. The
run of 2026-11-03, with the cache on, found 11 rasters equal, 7 close, and 145
beyond the tolerance; the worst, `f1040.pdf` page 1, had 8.1% of its pixels
more than 16 away in a channel. The owner chose on #622 that 0.2.2 renders
without the cache, so that a patch release moves no text; #622 records a rough
cost, and #640 is to measure it and decide on a later release. The engine's test
`a_render_fills_glyphs_without_the_glyph_cache` guards the setting.

### Table edges on hayro 0.9

`nics-background-checks-2015-11.pdf`, page 1, differs in 495 pixels by at most
5 of 255, only in the red channel: 457 pixels in column x=33 and 38 in column
x=975, between y=24 and y=482. They are the left and right edges of the page's
table, where its pale cells end at x=33.12 and x=975.36, under thin black rules.
hayro 0.9 blends these partly covered pixels a little differently from hayro
0.8, on the same vello_cpu 0.3.0. Which of its changes does so was not isolated;
its changelog lists SIMD as always on (LaurenzV/hayro#1638) and performance work
on rendering.

### Missing highlight appearance, resolved

The run of 2026-10-28 found `pdfium-links-highlights-annots.pdf`, page 1,
close but not equal at its Highlight annotation without `/AP`: pixel bounds
x=293..348, y=250..261, 0.139% of the page beyond a channel delta of 16.
Frozen pylopdf synthesizes missing appearances for rendering
(`synthesize_missing_appearances_for_render` in
`rust/src/document/rendering.rs`), with a Multiply blend for highlights;
pyropdf then passed annotations to hayro without that synthesis. #169 ported
it (PR #172), and the page renders byte for byte alike on hayro 0.8 and on
hayro 0.9. `annotation-diagnostic.json` finds the two rasters of the page
equal with annotations disabled too.

### Headings above columns

`patent-us223898.pdf`, page 2, differs in its text, words, layout, search,
and Markdown, and so does the document's Markdown. Its heading `UNITED
STATES PATENT OFFICE.` and the line below it span both columns, and their
word gaps fall on the column gutter. Frozen pylopdf splits them into the
columns, as pyropdf did when the run of 2026-10-28 measured the corpus;
since #460, pyropdf keeps them whole above the columns, as PyMuPDF 2.0 does
(`evidence/pymupdf-comparison`). The difference is that improvement: the
words are the same but for `CR` and `C`, which join as `CRC` in the line
`CRC AMP.`, and the page has four fewer spans.

### Exact boxes of fonts that a document does not embed, resolved

The 7 pages of `nonembedded-cjk.pdf` render byte for byte alike with the
faces of the font extras (#600). The run of 2026-11-04 on #613 found that of
their other outputs only the exact word boxes differed, on pages 2 to 7, by up
to 7.28 points: frozen pylopdf's text extraction also draws a font that a
document does not embed in its fallback faces, so `words(exact_bboxes=True)`
measures their outlines, while pyropdf's did not use the faces. #624 measures
those glyphs on the faces, and every output of the 7 pages now matches.

That run attributed the equal page 1 to its `CIDFontType0` descendant font,
wrongly: the dump rendered each page after reading it, so frozen pylopdf,
which fills its faces at its first rendering, read only its first page
without them. The dump now renders first.

pyropdf takes only the ink from the faces. hayro 0.8 stretched a substitute
glyph's outline to the PDF's width, and hayro 0.9 keeps that stretch in the
glyph's transform, so an advance taken through a face would be stretched too.

### Stretched substitutes, resolved

Without faces, the same stretch of a Standard 14 substitute moved the advances
of pyropdf's text on hayro 0.9, which the corpus does not show: no page of it
is in a font that hayro stretches. On the first page of
`nonembedded-latin.pdf`, `Hello` ended at x=106.14, and not at 90.0, and a
space among exact boxes at 125.25, and not at 104.0; a stretch below 1 could
split a word and one above 1 could join two. pyropdf now takes the stretch out
of the advances of text and of `page.content()`'s text runs, and keeps it in
the ink and in the glyphs' matrices, which a rendering draws (#656). Every
output of the 13 pages now matches. `evidence/stretch-reload` measures what
this costs.

Review corrected the original comparator's early equality return for
matching raster metadata: equal dimensions and stride now still compare
actual pixels. Identical errors count as errors, not successful parity,
and incomplete raster bytes fail the comparison. The report test checks
actual corpus page counts and regenerates the report from results/causes.

Numbers are rounded to 3 decimals before comparison, and numeric values
within 0.01 point count as equal. Thus an equal result does not establish
bitwise equality of unrounded floating-point geometry.

## Not compared

- The damaged inputs of `tests/corpus.py`: `tests/test_corpus_records.py`
  compares those with pylopdf's records.
- `page.selection`, `page.svg`, and options other than the defaults above,
  except `exact_bboxes`, `chars`, and `styles`.
- OCR, which needs the models: pylopdf runs without extras here.
