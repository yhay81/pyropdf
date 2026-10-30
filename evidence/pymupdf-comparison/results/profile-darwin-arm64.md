# Rendering profile, darwin-arm64

Measured 2026-10-30 on macOS-27.0.1-arm64-arm-64bit-Mach-O, Python 3.15.0,
pyropdf from this checkout, sampled 10 s per case at 1 ms by `sample`.
Shares are of the samples of the worker's main thread. `profile.py` describes the method.

## Self time per group

Each sample is charged to its leaf frame's crate. Frames inlined into a caller count for the caller.

| Group | bill-hr815 72 dpi | bill-hr815 144 dpi | usrguide 72 dpi | usrguide 144 dpi |
| --- | ---: | ---: | ---: | ---: |
| vello_cpu | 77.4% | 66.7% | 72.1% | 65.5% |
| pyropdf | 9.7% | 20.5% | 7.6% | 18.2% |
| memcpy and memset | 6.0% | 8.0% | 4.4% | 6.5% |
| hayro-syntax | 0.9% | 0.5% | 3.6% | 2.3% |
| fonts | 0.5% | 0.3% | 3.4% | 2.1% |
| malloc and free | 1.0% | 1.2% | 1.7% | 1.3% |
| image and stream decoding | 0.5% | 0.2% | 2.8% | 1.3% |
| hash maps | 1.9% | 1.0% | 1.2% | 0.6% |
| hayro-interpret | 1.2% | 0.7% | 1.4% | 0.7% |
| hayro | 0.6% | 0.3% | 0.6% | 0.4% |
| Rust std | 0.3% | 0.2% | 0.7% | 0.5% |
| Python and other | 0.1% | 0.4% | 0.2% | 0.2% |
| kernel | 0.1% | 0.1% | 0.3% | 0.3% |
| other (smallvec) | 0.1% | 0.1% | 0.2% | 0.0% |
| kurbo | 0.0% | 0.0% | 0.0% | 0.1% |
| other (pxfm) | 0.0% | 0.0% | 0.0% | 0.0% |

## Phases

Inclusive samples of the outermost frame of each phase.

| Phase | bill-hr815 72 dpi | bill-hr815 144 dpi | usrguide 72 dpi | usrguide 144 dpi |
| --- | ---: | ---: | ---: | ---: |
| page raster (pyropdf_engine::render::draw) | 97.0% | 95.4% | 97.5% | 96.6% |
| interpret and encode (hayro::render_into) | 79.6% | 60.1% | 83.4% | 67.4% |
| glyph fills (hayro::Renderer::fill_outline_run) | 74.5% | 57.2% | 70.1% | 59.6% |
| rasterize (vello_cpu Dispatcher::rasterize) | 7.1% | 13.1% | 6.2% | 9.9% |
| store, bridge, and open (pyropdf) | 0.2% | 0.2% | 1.2% | 0.7% |

## Pages

Best of three renders of each page, in ms, on a document opened once: every page, then the five
where pyropdf spends the most time. PyMuPDF and pyropdf read the samples; "with PNG" is what
`bench.py` times for pyropdf, `to_bytes()`, which encodes a PNG.

### bill-hr815 72 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 85.7 | 418.1 | 4.88 | 619.2 | 7.22 |
| 25 | 1.0 | 4.8 | 5.01 | 6.7 | 7.00 |
| 9 | 0.9 | 4.6 | 5.03 | 6.5 | 7.03 |
| 23 | 0.9 | 4.6 | 4.92 | 6.7 | 7.11 |
| 35 | 0.9 | 4.5 | 4.98 | 6.5 | 7.18 |
| 7 | 0.9 | 4.5 | 5.19 | 6.4 | 7.33 |

### bill-hr815 144 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 145.6 | 817.3 | 5.61 | 1549.4 | 10.64 |
| 9 | 1.5 | 8.8 | 5.93 | 15.9 | 10.75 |
| 25 | 1.5 | 8.7 | 5.72 | 16.1 | 10.53 |
| 7 | 1.5 | 8.6 | 5.75 | 15.4 | 10.28 |
| 26 | 1.4 | 8.6 | 5.91 | 15.7 | 10.81 |
| 95 | 1.5 | 8.5 | 5.85 | 15.1 | 10.40 |

### usrguide 72 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 17.5 | 78.8 | 4.50 | 111.8 | 6.38 |
| 17 | 0.7 | 3.6 | 4.88 | 4.8 | 6.48 |
| 3 | 0.8 | 3.5 | 4.37 | 4.9 | 6.11 |
| 4 | 0.8 | 3.5 | 4.24 | 4.8 | 5.84 |
| 24 | 0.8 | 3.4 | 4.55 | 4.8 | 6.33 |
| 15 | 0.7 | 3.4 | 4.77 | 4.7 | 6.58 |

### usrguide 144 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 26.1 | 139.9 | 5.36 | 262.7 | 10.06 |
| 17 | 1.1 | 6.2 | 5.64 | 11.0 | 10.06 |
| 4 | 1.2 | 6.1 | 5.26 | 11.0 | 9.45 |
| 3 | 1.1 | 6.1 | 5.31 | 11.4 | 9.91 |
| 24 | 1.1 | 5.8 | 5.20 | 11.0 | 9.85 |
| 22 | 1.1 | 5.8 | 5.45 | 9.9 | 9.34 |

## Hottest functions

Self samples, as shares of each case.

### bill-hr815 72 dpi

| Function | Self |
| --- | ---: |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 23.1% |
| `vello_common::strip::render (vello_cpu)` | 20.3% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 16.1% |
| `pyropdf_engine::render::draw (pyropdf)` | 9.6% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 6.8% |
| `_platform_memmove (memcpy and memset)` | 5.0% |
| `vello_common::clip::intersect (vello_cpu)` | 2.7% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 1.9% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 1.8% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.4% |
| `<hashbrown::map::HashMap<alloc::string::String, font_types::glyph_id::GlyphId, rustc_hash::FxBuildHasher>>::get::<str> (hash maps)` | 1.2% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::init_uncovered_range (vello_cpu)` | 1.1% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.7% |
| `__bzero (memcpy and memset)` | 0.6% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::record_fill (vello_cpu)` | 0.6% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:

- `mod.rs:361`: 9.6%

### bill-hr815 144 dpi

| Function | Self |
| --- | ---: |
| `pyropdf_engine::render::draw (pyropdf)` | 20.3% |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 18.4% |
| `vello_common::strip::render (vello_cpu)` | 14.8% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 12.0% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 6.7% |
| `_platform_memmove (memcpy and memset)` | 6.1% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 5.6% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 2.3% |
| `vello_common::clip::intersect (vello_cpu)` | 2.1% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::init_uncovered_range (vello_cpu)` | 2.1% |
| `__bzero (memcpy and memset)` | 1.4% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.0% |
| `<hashbrown::map::HashMap<alloc::string::String, font_types::glyph_id::GlyphId, rustc_hash::FxBuildHasher>>::get::<str> (hash maps)` | 0.4% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.4% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::record_fill (vello_cpu)` | 0.4% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:

- `mod.rs:361`: 20.3%
- `mod.rs:352`: 0.0%
- `mod.rs:362`: 0.0%

### usrguide 72 dpi

| Function | Self |
| --- | ---: |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 23.4% |
| `vello_common::strip::render (vello_cpu)` | 18.2% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 13.7% |
| `pyropdf_engine::render::draw (pyropdf)` | 7.1% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 6.4% |
| `_platform_memmove (memcpy and memset)` | 3.1% |
| `vello_common::clip::intersect (vello_cpu)` | 2.4% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 2.2% |
| `zlib_rs::inflate::inflate_fast_help (image and stream decoding)` | 1.8% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 1.6% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.2% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.9% |
| `<read_fonts::ps::type1::Type1Font>::new (fonts)` | 0.8% |
| `zlib_rs::inflate::inflate (image and stream decoding)` | 0.7% |
| `__bzero (memcpy and memset)` | 0.6% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:

- `mod.rs:361`: 7.1%

### usrguide 144 dpi

| Function | Self |
| --- | ---: |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 20.0% |
| `pyropdf_engine::render::draw (pyropdf)` | 18.2% |
| `vello_common::strip::render (vello_cpu)` | 15.3% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 12.0% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 6.2% |
| `_platform_memmove (memcpy and memset)` | 4.8% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 4.7% |
| `vello_common::clip::intersect (vello_cpu)` | 1.9% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 1.7% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::init_uncovered_range (vello_cpu)` | 1.4% |
| `__bzero (memcpy and memset)` | 1.1% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.0% |
| `zlib_rs::inflate::inflate_fast_help (image and stream decoding)` | 0.7% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.4% |
| `<read_fonts::ps::type1::Type1Font>::new (fonts)` | 0.4% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:

- `mod.rs:361`: 18.2%
