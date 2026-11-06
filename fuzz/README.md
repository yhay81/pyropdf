# Fuzz targets

cargo-fuzz targets of the store and the engine (DESIGN.md section 12, F9):

- `index`: indexing and repair of any bytes, and every object of an index
  that opens.
- `value`: a value that parses writes, keeping lexemes and canonically,
  bytes that parse to the same value.
- `save`: edits that the input's last bytes choose, then full and
  incremental saves, which reopen and read the written values back.
- `bridge`: the page index, the hayro bridge, and a small render of the
  first page.
- `content`: content streams split into contiguous operations whose bytes
  and tail are the input.
- `fonts`: embedded faces that load from any bytes, shape a text, look for
  the colour glyphs that drawing refuses, measure its glyphs, and compact
  to them at save; bytes that do not load are a
  typed error, and any bytes compact or refuse (#482).
- `bitmaps`: faces that load from any bytes draw their PNG strikes at three
  sizes into Type 3 fonts, which read back the codes that they gave (#631).
  Decoding a PNG is `Canvas.image`'s path, which the Python tests cover.
- `colr`: faces that load from any bytes paint their COLR glyphs into one
  transaction under the web profile's budget, whole or not at all as the
  check says, and Type 3 fonts read back the codes that they gave (#633).
- `interpret`: rendering, SVG conversion, text extraction, and content
  items of the first four pages, of a whole file or of a content stream
  that a one-page template with varied resources holds.

The store turns a panic of hayro-syntax into an error, so the targets other
than `interpret` keep Rust's default panic hook instead of libFuzzer's, which
aborts on any panic: only a panic that escapes a target is a crash.
`interpret` keeps libFuzzer's hook, so any panic is a crash, even one that
the store catches (I3, #19). It runs in CI with the adopted hayro 0.9-based
dependency. Panics that the store catches
still print their message, and are the material of hayro's I3 (#19); the
`value` target's first run found `[ 2 + R ]` in an array.

## Running

```sh
python3 -I fuzz/seeds.py
cd fuzz
cargo +nightly fuzz run index corpus/index -- -max_total_time=60
```

`seeds.py` writes the corpus, its damaged inputs, and the encrypted
fixtures as seeds of `index`, `save`, and `bridge`, their uncompressed
objects as seeds of `value`, and their content streams, inflated where they
are Flate data, as seeds of `content`, and the engine's subset test faces
as seeds of `fonts`, and its colour faces as seeds of `bitmaps` and `colr`, to
`fuzz/corpus/`, which git ignores.

## CI and Actions minutes

`.github/workflows/fuzz.yml` runs each target for one minute on pull requests
and pushes to main that change the store, the engine, or the targets, and for fifteen minutes on
Mondays. Each job also installs cargo-fuzz and builds the target, about four
minutes with a warm cache. Linux minutes count once:

- weekly: 9 jobs × (15 + 4) minutes = about 170 minutes, or about 740 a month;
- pull requests and pushes to main: 9 × (1 + 4) = about 45 minutes each,
  which Rust changes pay twice: once on the pull request and once on main.

Against the 2,000 minutes a month of a private repository on GitHub Free
(3,000 on Pro), the weekly run takes about 37% (or 25%). Lower
`SECONDS_TO_RUN` or the `paths` of the pull-request trigger if the minutes
run short.
