# Phase 1 parity with frozen pylopdf

The differential evidence for the exit of Phase 1 (DESIGN.md §13): frozen
pylopdf and pyropdf read every corpus document, and every difference in
their outputs is explained here.

| File | Role |
| --- | --- |
| `run.py PYLOPDF_CHECKOUT` | Builds pylopdf at the freeze commit `ebb01d3c` (#95) into its own environment, dumps both engines over `tests/assets/real_world`, and runs `compare.py` |
| `dump.py ENGINE OUT FILE...` | Writes one engine's outputs per page and each document's Markdown. The page outputs are listed below |
| `compare.py A B` | Classifies each output of each page and writes `report.md` and `results.json` |
| `causes.toml` | Matches each kind of difference to a cause below |
| `report.md` | Pages that are equal per document and output, with the causes of the rest |
| `results.json` | The kind and measurements of every compared output |

Per page, `dump.py` records:

- `text`: the text.
- `words`: the words.
- `words_exact`: the words with `exact_bboxes=True`.
- `layout`: the layout dictionary with characters and styles.
- `search`: the search boxes of the page's first 20 distinct words.
- `tables_lines` and `tables_text`: the tables of each strategy.
- `markdown`: the Markdown.
- `render`: a 72-dpi raster.

Both engines run without fallback fonts: pylopdf is installed without
extras, and pyropdf has none. `tests/test_parity.py` checks that the report
covers the corpus and leaves no difference unexplained. When
`PYROPDF_PARITY_PYLOPDF` names the Python of `target/parity/pylopdf-env`, it
also redumps two documents and checks their kinds against `results.json`.

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

None. On 2026-10-27, with pyropdf at the head of the stack through #148, every
compared output of every page of all 14 documents (155 pages) was identical:
the dumps of the two engines were byte-for-byte equal, and so was every
raster. No output raised in either engine. `causes.toml` therefore lists no
cause. A later difference gets a cause there and a paragraph here, or a fix.

Numbers are rounded to 3 decimals before they are compared, so the report
says nothing about differences below 0.0005 point.

## Not compared

- The damaged inputs of `tests/corpus.py`: `tests/test_corpus_records.py`
  compares those with pylopdf's records.
- `page.selection`, `page.svg`, and options other than the defaults above,
  except `exact_bboxes`, `chars`, and `styles`.
- OCR, which needs the models: pylopdf runs without extras here.
