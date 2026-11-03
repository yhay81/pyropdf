# pylopdf baseline

The measurements and counts behind section 2 of [DESIGN.md](../../DESIGN.md),
which gives the largest size of each workload with pyropdf's result. Each
script runs against an installed pylopdf and takes the files it reads as
arguments.

## Measurements

Two runs on 2026-10-24, on macOS arm64 with CPython 3.14.8 and the pylopdf
0.15.0 extension of a development environment; where they differ, both
results are given. A single fill is the best of seven rounds, and loops ran
once per size in each run. The code paths named in the last column are
unchanged on pylopdf's main at `884af0db`. The larger documents repeat
pylopdf's `tests/assets/real_world/bill-hr815.pdf` (110 pages) through
`doc.pages.extend`, and each is written with `to_bytes()` and opened again
before measuring.

| Workload | Result | Cause in pylopdf |
| --- | --- | --- |
| Rotate every page, then read its text | 110 pages: 0.16 to 0.17 s, against 0.09 to 0.10 s for reading alone. 440 pages: 1.48 to 1.87 s, against 0.29 to 0.32 s. 1,760 pages: 28.68 to 30.41 s, against 1.29 to 1.59 s, 18 to 24 times as long. | Every edit drops hayro's snapshot, the content guard's results, and every derived cache (`invalidate_hayro_pdf_keeping_placements`), and the next read serializes the whole lopdf graph and parses it again (`hayro_view`). |
| Fill one text field | 2.17 to 2.19 ms with 2,626 objects, 6.39 to 6.58 ms with 8,646 objects, when only pages without fields were added | Field edits clone the whole lopdf document so that they can roll back on failure (seven sites in `forms.rs`). |
| Read the value of every field | 7.7 ms for 100 fields, 118 to 126 ms for 400, and 2.18 to 2.24 s for 1,600, while one walk of the form takes 0.07, 0.27 to 0.31, and 1.22 to 1.25 ms | A `Field` handle walks the whole form on every property access to find itself (`Field._facts`). |
| Stamp text in one OpenType face on 100 pages | 100 embedded font programs and 330 KiB. `dedupe=True` merges them only when every page has the same text (29.6 KiB); with different text it leaves 82.7 KiB | Each `page.draw()` commit generates its own krilla document, so each commit embeds its own subset. |

## Structure

Counted on pylopdf's main at `884af0db` (2026-10-24):

- About 5,500 lines of Rust exist because an engine lacks a bound, an API,
  or linear-time behavior, as pylopdf's roadmap counts them. They include
  the nesting guards and the scanners behind them (2,477 lines), the
  reconciliation of how lopdf and hayro read malformed files
  (`object_recovery.rs`, 1,498 lines), and a parser for krilla's output on
  the calling thread (`generated_pdf.rs`, 756 lines).
- Three tokenizers read content streams. hayro-syntax serves rendering,
  extraction, and the content engine, which keeps each operation's bytes
  since yhay81/pylopdf#405. lopdf's content parser serves text
  replacement. And `content_scan.rs` models hayro's tokenizer in order to
  refuse what would crash it.
- 67 call sites choose by hand how much an edit invalidates.
- Page cleanup, structure-tree pruning, and form import are 3,654 lines of
  operation-specific code: `page_cleanup.rs` (740), `structure_prune.rs`
  (945), and `acroform_import.rs` (1,969).
- Incremental save (yhay81/pylopdf#403, 2026-10-24) reads the original
  bytes again and compares every object of the graph with that baseline,
  because no editing method reports what it changed. Transactions and undo
  (yhay81/pylopdf#415, 2026-10-24) keep complete copies of the graph.
- One `#[pymethods]` block holds the 186 methods of `_Document`.
- About 60 `LimitError` codes and 421 hand-placed fallible reservations
  implement the resource policy.
- lopdf stores real numbers as `f32`, so every save rewrites `35.6812345`
  as `35.681236`.
- The development context (`AGENTS.md`) is 249 KB, much of it rules for
  keeping the engines and caches consistent.

On damaged input, hayro alone rendered 103 of 112 inputs (14 files under 8
kinds of damage), while pylopdf with lopdf 0.46 read the first page of 78
(pylopdf's roadmap, evidence log).

## Scripts

| Script | Measures |
| --- | --- |
| `edit_read_loop.py BILL` | Reading every page, and rotating then reading every page, at 110, 440, and 1,760 pages |
| `form_fill_cost.py FORM BILL` | One text-field fill as pages without fields join the document |
| `field_read_cost.py` | Reading every field's value through handles, at 100, 400, and 1,600 fields |
| `font_subsets.py FONT` | Embedded font programs after stamping one face on 1, 10, and 100 pages, with and without `dedupe` |

`BILL` is pylopdf's `tests/assets/real_world/bill-hr815.pdf`, `FORM` its
`tests/assets/real_world/f1040.pdf`, and `FONT` the
`NotoSansHebrew-Variable.ttf` of `pylopdf-fonts-he`. From a pylopdf checkout:

```bash
uv run --no-sync python path/to/edit_read_loop.py tests/assets/real_world/bill-hr815.pdf
```
