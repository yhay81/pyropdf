# Font subsetting and compaction, before and after issue #676

#676 binds pyropdf-engine to `crates/clippy.toml`, so that every
collection that grows from input either grows through the budget or names
what bounds it (DESIGN.md F6 and section 9). Most of its changes are
`#[expect(clippy::disallowed_methods, reason = "...")]` attributes, which
change no code. Two of the hot paths that the issue names changed:

- **Subsetting** (`fonts::subset`, `fonts/sfnt.rs` and `fonts/cff.rs`): a
  program whose tables, glyph outlines, or CID-keyed Private DICTs overlap
  would be copied once per reference, up to 65,535, 32,768, and 256 times
  over. The subsetter now sums what it copies and leaves such a program
  whole once the sum passes what the program holds, and it checks that the
  records, INDEX offsets, and `loca` offsets are present before their
  counts size anything.
- **Compaction** (`fonts::compact::compact`, at save): a CFF face's
  charstrings inline the subroutines and `seac` components that they call,
  up to read-fonts' 1,000,000 operations per glyph. They now stop at the
  program's length, past which no result is smaller than the program, which
  the settler then keeps whole.

Neither changes what a save or `subset_fonts` writes for a face whose
structures do not overlap. This measures what the added checks cost.

## Method

```bash
uv run python evidence/font-growth-676/run.py FACE...
```

`run.py` builds the harness (`src/main.rs`) in release twice, against a
temporary worktree of the base commit and against this checkout, and runs
the two builds in turn for five rounds. For each face the harness keeps the
first 256 glyphs, as Latin text draws, and every 16th glyph after them, and
times `fonts::subset` and `fonts::compact::compact` with that set: at least
15 runs and half a second each, of which it reports the median. The table
gives the median of the five rounds' medians, and the result's byte count,
which is the same before and after for every face here.

The faces are two of pyropdf's test faces, four from the measuring
machine's `C:\Windows\Fonts`, and pylopdf-fonts-jp's
`NotoSansJP-Regular.otf`; only pyropdf's are committed.

- `NotoSansJP-subset.otf` and `NotoSansHebrew-subset.ttf`:
  `crates/pyropdf-engine/src/fonts/testdata/`.
- `arial.ttf`, `segoeui.ttf`, `ARIALUNI.ttf` (TrueType), and
  `Xolonium-Regular.otf` (a name-keyed CFF): `C:\Windows\Fonts`.
- `NotoSansJP-Regular.otf` (a CID-keyed CFF of 17,936 glyphs): pylopdf's
  `fonts/pylopdf-fonts-jp/src/pylopdf_fonts_jp/`.

## Results

AMD Ryzen 5 3600XT, Windows 11, 2026-11-05, base 0bae7fd and head
33551e2, `results/windows-x86_64.md`. Times are medians in microseconds.

| Face | Operation | Glyphs | Kept | Face bytes | Result bytes | Base µs | Head µs | Change |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NotoSansJP-subset.otf | subset | 99 | 99 | 15,240 | 15,344 | 70 | 72 | +2.9% |
| NotoSansJP-subset.otf | compact | 99 | 99 | 15,240 | 12,450 | 229 | 231 | +0.9% |
| NotoSansHebrew-subset.ttf | subset | 13 | 13 | 6,408 | 6,448 | 12 | 12 | +0.0% |
| NotoSansHebrew-subset.ttf | compact | 13 | 13 | 6,408 | 5,176 | 10 | 10 | +0.0% |
| arial.ttf | subset | 4,652 | 531 | 1,045,956 | 492,544 | 1,118 | 1,091 | -2.4% |
| arial.ttf | compact | 4,652 | 531 | 1,045,956 | 150,748 | 281 | 273 | -2.8% |
| segoeui.ttf | subset | 5,395 | 578 | 960,076 | 433,484 | 879 | 888 | +1.0% |
| segoeui.ttf | compact | 5,395 | 578 | 960,076 | 102,220 | 201 | 196 | -2.5% |
| ARIALUNI.ttf | subset | 50,386 | 3,390 | 23,449,860 | 2,471,392 | 7,334 | 7,343 | +0.1% |
| ARIALUNI.ttf | compact | 50,386 | 3,390 | 23,449,860 | 1,656,464 | 4,021 | 3,965 | -1.4% |
| Xolonium-Regular.otf | subset | 847 | 293 | 230,760 | 205,372 | 436 | 455 | +4.4% |
| Xolonium-Regular.otf | compact | 847 | 293 | 230,760 | 28,660 | 412 | 422 | +2.4% |
| NotoSansJP-Regular.otf | subset | 17,936 | 1,361 | 4,533,028 | 948,080 | 2,308 | 2,234 | -3.2% |
| NotoSansJP-Regular.otf | compact | 17,936 | 1,361 | 4,533,028 | 393,221 | 5,265 | 5,296 | +0.6% |

## Reading

No face is more than 4.4% slower, and the differences fall on both sides
of zero: the added sums and presence checks cost no more than this
measurement resolves. The original harness compared result lengths;
the review follow-up below also verifies SHA-256 digests.
The largest absolute difference is 74 µs, `NotoSansJP-Regular.otf`'s
subset, which is faster after.

## Review follow-up

Review of #686 found two remaining ways to grow beyond the stated bound:

- Empty duplicate sfnt table records pass the sum of input table lengths,
  but subsetting replaced each duplicate `glyf`, `loca`, or `CFF ` record
  with the first rebuilt table. `sfnt::tables` now refuses duplicate tags
  before rewriting. The tag set holds one entry per present directory
  record, and compacting no longer needs to check duplicates again.
- A CFF charstring that reached its byte limit still appended stem-operator
  positions after `put` stopped writing. It now records a position only
  after the operator fits, so later callbacks cannot grow this bookkeeping.

`duplicate_table_tags_cannot_multiply_rebuilt_tables` and
`an_overflowing_charstring_stops_recording_stem_operators` both fail on
the original PR head, a0f50a5; all 100 font tests pass with the corrections.

The harness now prints each result's SHA-256 outside the timed loop, and
`run.py` rejects differing digests or lengths in every round. This checks
byte identity instead of inferring it from matching sizes. Repeat the
comparison against the original PR head with:

```bash
uv run python evidence/font-growth-676/run.py --base a0f50a5 FACE...
```

Alternatively, build the updated harness against each source version with
`cargo build --release --locked --manifest-path evidence/font-growth-676/Cargo.toml`,
save each executable, and pass `--binaries BASE_EXE HEAD_EXE --base a0f50a5`.
This reuses existing builds without creating another worktree. The same
seven faces and five alternating rounds are used for the review result
in `results/windows-x86_64-review.md`.

Against a0f50a5, the corrected engine at ec99091 writes matching SHA-256
digests for all 14 font/operation pairs in every round. Timing changes
range from -14.8% to +5.0%, with a largest slowdown of 51 microseconds.
These small measurements include run variation; they do not establish a
speedup from the additional checks.

## Ruled-table extraction

`tables.py` measures the cell-text charge added by #676, using installed
builds before the PR (ccf8341) and after the corrections (ec99091):

```bash
python evidence/font-growth-676/tables.py --pythons BASE_PYTHON HEAD_PYTHON \
  --base ccf8341 --head ec99091
```

Prepare both interpreters with `uv sync --locked` in their checkouts. The
script uses the existing PDF fixture builder from `tests/test_tables.py`
to make complete 2-by-2 and 10-by-4 grids, with distinct labels in every
cell. Each extraction starts with a fresh document, so the derived-result
cache cannot hide the changed path. Opening, closing, validation, and
hashing are outside the timed interval. Each sample makes at least 25
extractions and runs for at least half a second; five rounds alternate
the two interpreters. Every result is checked for all expected labels,
and hashes of the extracted text and geometry must agree.

`results/windows-x86_64-tables.md` records 91.5 to 93.0 microseconds for
the 2-by-2 grid (+1.6%) and 263.9 to 262.9 microseconds for the 10-by-4
grid (-0.4%). The digests match in every round. This measures complete
table extraction, not the isolated cost of one budget charge.
