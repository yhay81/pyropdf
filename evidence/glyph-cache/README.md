# hayro's glyph cache on and off (issue #640)

Should a later release render through hayro 0.9's glyph cache
(`RenderSettings::glyph_caching`, LaurenzV/hayro#1706) by default, or offer
it as an option? #622 turned it off for 0.2.2, so that a patch release moves
no text, with a rough ratio of 1.8 measured in the engine's test profile.
This is the benchmark in release mode.

## Method

```bash
cargo run --release --locked --manifest-path evidence/glyph-cache/Cargo.toml -- evidence/glyph-cache/results windows-x86_64 tests/assets/real_world/*.pdf
RUNS=9 DPIS=300 QUALITY_DPIS= DISTINCT_SIZES=1000 cargo run --release --locked --manifest-path evidence/glyph-cache/Cargo.toml -- evidence/glyph-cache/results windows-x86_64-300-rerun tests/assets/real_world/*.pdf
```

`src/main.rs` draws each page as `pyropdf_engine::render::draw` draws it for
`page.pixmap(dpi=...)`: through the store's bridge, with hayro's default
interpreter settings and every annotation, unhinted, on white, taking the RGB
samples. The two variants differ only in `glyph_caching`. The product's
`render::raster`, which renders without the cache, is timed beside them, and
its samples equal the replica's without the cache on all 156 pages at 72 and
144 dpi.

- **Time**: every page of each document in order, with one `RenderCache`
  per document, as `render::raster` keeps one per thread for the version that
  it rendered last. The variants take turns within each run; a document's
  time is the best of the runs (5, or 9 for the rerun).
- **Memory**: a counting allocator records what a document's pages allocate
  at their peak and what the cache still holds after the last page.
- **Pixels**: at 72 and 144 dpi, each page is drawn both ways and compared
  with the page drawn without the cache at four times the resolution and
  averaged over each 4 by 4 block, which approximates the area that each
  shape covers in a pixel. A pixel is far when a channel differs by more than
  16, as `evidence/phase1-parity` counts it.
- **Distinct sizes**: one page of 20,000 glyphs, each at a size of its own,
  where the cache can reuse nothing.
- **Positions**: one page of 2,000 glyphs whose origins lie where the cache
  places glyphs (a whole pixel vertically, a quarter of a pixel horizontally),
  and the same page moved half a pixel up and an eighth to the right.

AMD Ryzen 5 3600XT (6 cores), 32 GB, Windows 11, 2026-11-05. The machine was
not idle: other programs kept 35 to 40% of its CPU busy. At 72 and 144 dpi
`render::raster`, which does the replica's work without the cache, stays
within 3% of it, so the ratios hold. At 300 dpi it differs by up to 18%, and
the two runs disagree.

## Results

`results/windows-x86_64.md`, with the raw times in the `.json` beside it, and
`results/windows-x86_64-300-rerun.md`. Corpus totals, in milliseconds:

| | 72 dpi | 144 dpi | 300 dpi | 300 dpi, rerun |
| --- | ---: | ---: | ---: | ---: |
| Off | 1,646.8 | 3,755.5 | 6,515.6 | 9,375.2 |
| On | 1,076.1 | 2,622.5 | 5,814.9 | 7,224.8 |
| Off / on | **1.53** | **1.43** | 1.12 | 1.30 |

- Text-heavy documents gain the most: bill-hr815 1.64 and usrguide 1.68 at
  72 dpi. Pages with little text gain nothing.
- **Pixels.** 145 of the 156 pages change. Against the reference, the mean
  channel error goes from 0.643 to 2.511 at 72 dpi and from 0.391 to 1.392 at
  144 dpi; on the pixels that the cache changes, from 6.74 to 30.68 at 72 dpi.
  Far pixels go from 1.38% to 4.90% of the corpus at 72 dpi.
- **Where the error comes from.** On the aligned page the cache is closer to
  the reference than vello_cpu's fill (0.235 against 1.576); on the page moved
  by half a pixel it is far worse (9.101 against 1.688). The cache rasterizes
  glyphs well, but it rounds their origins to a whole pixel vertically
  (`hayro/src/glyph_cache.rs`: `y = f.round()`), which moves text by up to
  half a pixel.
- **Memory.** One document's cache holds at most 3.6 MiB more at 300 dpi
  (usrguide); the documents' caches together hold 10.8 MiB instead of 8.9 at
  72 dpi and 21.4 MiB at 300 dpi. On the page of distinct sizes, the cache
  holds 297.8 MiB after the page, about 15 KiB per glyph size, where the fill
  holds 0.8 MiB, and the page takes 1,170 ms instead of 335. Nothing evicts or
  bounds the cache, and the budget does not see it: it lives as long as the
  thread's `RenderCache` for the version.
