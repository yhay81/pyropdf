# Bounding interpretation before hayro's budgeting (issue #678)

Option B of #678 walks a page's content, and the streams that it invokes,
before hayro interprets it, and charges `interpretation_fuel` per operator
times the invocations. What does that walk cost per page on the corpus, and
which pages would candidate limits refuse?

## Method

```bash
cargo run --release --locked --manifest-path evidence/interpretation-fuel/Cargo.toml -- evidence/interpretation-fuel/results windows-x86_64 tests/assets/real_world/*.pdf
```

- `src/walk.rs` is the prototype of B. It tokenizes with pyropdf's content
  syntax (`content::Operations`, #93) every stream that hayro 0.9 interprets
  for a page: the content, the form XObjects that `Do` draws, the appearances
  of annotations that are neither hidden nor `NoView`, soft-mask groups, the
  cells of tiling patterns, and Type 3 glyph procedures, each with the
  resources that hayro gives it. It memoizes each stream with its resources,
  so that it tokenizes each once, and multiplies the counts by the
  invocations. It is iterative, stops at hayro's 50 nested interpretations,
  and saturates on a stream that invokes itself. Its module documentation
  lists where it stops short of a product walk.
- `src/main.rs` measures every page of each document, best of five runs, each
  run on fresh bridges: the walk with an empty memo, as one render of the page
  would run it; hayro-syntax's tokenizer alone over the page's content, which
  any walk costs at least; `render::raster` at 72 dpi; and
  `extract::page_text`.

Not measured here: synthetic costly pages, which the issue's first
acceptance box also asks for, and a check of the walk's counts against
hayro's own interpretation.

AMD Ryzen 5 3600XT (6 cores), 32 GB, Windows 11, 2026-11-05, with other
programs keeping 35 to 40% of the CPU busy. Four runs gave totals for
rendering from 896 to 1,270 ms, and the walk from 4.5% to 4.9% of them.

## Results

`results/windows-x86_64.md`, with every page in the `.json` beside it.

| Corpus, 156 pages | Milliseconds |
| --- | ---: |
| `render::raster`, 72 dpi | 1,116.7 |
| `page_text` | 367.5 |
| The walk | 53.6 (4.8% of rendering, 14.6% of text) |
| The page's tokens alone | 9.3 |

- Per page, the walk costs a median 2.4% of the render, 8.3% at the 90th
  percentile, and 58.9% at most, on nics-background-checks-2015-11, whose one
  content stream holds 9,205 operators in 104 KiB. There and on
  senate-expenditures, the walk takes longer than the page's text.
- Most of the walk's time is `Operations::parse`, which builds every operand
  as a value: tokenizing the pages' content alone takes a sixth of it. A walk
  that reads only the operands of the operators that invoke streams would
  cost close to the tokenizer.
- The largest page runs 11,562 operators and the median 223. No page invokes
  more than 5 streams, none invokes itself, and every Type 3 code maps to a
  procedure. No limit from 16,384 operators up refuses a page: the corpus
  holds no page that could set a limit.
