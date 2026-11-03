# Phase 1 parity with frozen pylopdf

The differential evidence for the exit of Phase 1 (DESIGN.md §13): frozen
pylopdf and pyropdf read every corpus document, and pages in CJK fonts that
the file does not embed, and every difference in their outputs is explained
here.

| File | Role |
| --- | --- |
| `run.py PYLOPDF_CHECKOUT` | Builds pylopdf at the freeze commit `ebb01d3c` (#95) with its CJK font extras into its own environment, dumps both engines over `tests/assets/real_world` and `tests/assets/text/nonembedded-cjk.pdf`, and runs `compare.py` |
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
turns them off. `tests/assets/text/nonembedded-cjk.pdf` holds the pages of
#600 in CJK fonts that the file does not embed: MS-Gothic in both descendant
types and both CMaps, MS-Mincho, and fonts of the GB1, CNS1, and Korea1
collections. With `--fallback-fonts`, both engines render it with the faces
of the font extras `jp`, `ko`, `zh-cn`, and `zh-tw`, which `run.py` installs
for pylopdf and pyropdf's dev group installs, in the same versions.
`tests/test_parity.py` checks that the report covers the corpus and the
fallback pages and leaves no difference unexplained. When
`PYROPDF_PARITY_PYLOPDF` names the Python of `target/parity/pylopdf-env`, it
also redumps three corpus documents and the fallback pages and checks their
kinds against `results.json`.

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

The run on 2026-11-04 covers the 14 corpus documents, 155 pages, and the 7
pages of `nonembedded-cjk.pdf`. Of the 162 rasters, 161 match byte for byte,
the 7 fallback pages among them, and one is close under the stated
tolerance. The JSON reading outputs and the Markdown match, except on page 2
of `patent-us223898.pdf` and in the exact word boxes of the fallback pages,
which the causes below explain. No output raises. `metadata.json` identifies
the measured source revision and the hashes of the inputs. The oracle cache
is reused only with a matching stamp of the freeze commit and the extras and
an importable package; an unstamped or different cache is rebuilt.

### Missing highlight appearance

`pdfium-links-highlights-annots.pdf`, page 1, differs only at its Highlight
annotation without `/AP`: pixel bounds x=293..348, y=250..261, within the
annotation's PDF rectangle [293, 530, 349, 542]. Pixels with a channel delta
over 16 occupy 0.139% of the page (the threshold is 0.5%); the mean maximum
channel delta over all pixels is 0.295. Disabling annotations makes the
page byte-identical, as `annotation-diagnostic.json` records with hashes.

Frozen pylopdf synthesizes missing appearances in
`rust/src/document/rendering.rs` via `synthesize_missing_appearances_for_render`;
its `rust/src/annotation_appearance.rs` gives highlights a Multiply blend.
The measured pyropdf version passes annotations to hayro without that
synthesis. The port is tracked by #169 / PR #172. This is an explained,
within-tolerance difference, rather than byte-identical rendering.

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

### Exact boxes of fonts that a document does not embed

The 7 pages of `nonembedded-cjk.pdf` render byte for byte alike, with the
faces of the font extras (#600). Only their exact word boxes differ, on
pages 2 to 7, by up to 7.28 points: frozen pylopdf's text extraction also
draws a font that a document does not embed in its fallback faces, so
`words(exact_bboxes=True)` measures their outlines, while pyropdf's text
extraction does not use the fallback faces, which #600 leaves out, and
gives the boxes that it gives without them. On page 1, whose descendant
font is a `CIDFontType0`, pylopdf gives those boxes too. The text, the
other boxes, and the layout match on every page.

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
