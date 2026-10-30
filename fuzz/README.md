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
- `interpret`: rendering, SVG conversion, text extraction, and content
  items of the first four pages, of a whole file or of a content stream
  that a one-page template with varied resources holds.

The store turns a panic of hayro-syntax into an error, so the targets other
than `interpret` keep Rust's default panic hook instead of libFuzzer's, which
aborts on any panic: only a panic that escapes a target is a crash.
`interpret` keeps libFuzzer's hook, so any panic is a crash, even one that
the store catches (I3, #19). It stays out of CI until the adoption of a
hayro release after 0.8.0 (#7), whose known panics it would find. Panics that the store catches
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
are Flate data, as seeds of `content`, to `fuzz/corpus/`, which git ignores.

## CI and Actions minutes

`.github/workflows/fuzz.yml` runs each target for one minute on pull requests
that change the store, the engine, or the targets, and for fifteen minutes on
Mondays. Each job also installs cargo-fuzz and builds the target, about four
minutes with a warm cache. Linux minutes count once:

- weekly: 5 jobs × (15 + 4) minutes = about 95 minutes, or about 410 a month;
- pull requests: 5 × (1 + 4) = about 25 minutes each, which Rust changes pay.

Against the 2,000 minutes a month of a private repository on GitHub Free
(3,000 on Pro), the weekly run takes about a fifth (or a seventh). Lower
`SECONDS_TO_RUN` or the `paths` of the pull-request trigger if the minutes
run short.
