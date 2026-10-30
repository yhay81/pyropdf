# Rendering profile, darwin-arm64

Measured 2026-10-30 on macOS-27.0.1-arm64-arm-64bit-Mach-O, Python 3.15.0,
pyropdf from this checkout, sampled 10 s per case at 1 ms by `sample`.
Shares are of the samples of the worker's main thread. `profile.py` describes the method.

## Self time per group

Each sample is charged to its leaf frame's crate. Frames inlined into a caller count for the caller.

| Group | bill-hr815 72 dpi | bill-hr815 144 dpi | usrguide 72 dpi | usrguide 144 dpi |
| --- | ---: | ---: | ---: | ---: |
| vello_cpu | 86.1% | 85.6% | 77.3% | 78.8% |
| memcpy and memset | 5.8% | 8.7% | 4.1% | 7.5% |
| hayro-syntax | 1.0% | 0.6% | 4.0% | 2.9% |
| fonts | 0.6% | 0.4% | 3.5% | 2.8% |
| hash maps | 1.9% | 1.3% | 1.7% | 1.3% |
| image and stream decoding | 0.5% | 0.4% | 3.0% | 2.0% |
| malloc and free | 1.1% | 1.2% | 1.8% | 1.6% |
| hayro-interpret | 1.4% | 0.8% | 1.7% | 1.0% |
| Rust std | 0.3% | 0.2% | 0.8% | 0.6% |
| hayro | 0.5% | 0.3% | 0.6% | 0.4% |
| Python and other | 0.2% | 0.4% | 0.4% | 0.5% |
| kernel | 0.2% | 0.1% | 0.5% | 0.2% |
| pyropdf | 0.3% | 0.1% | 0.4% | 0.3% |
| other (smallvec) | 0.1% | 0.1% | 0.2% | 0.2% |
| kurbo | 0.0% | 0.0% | 0.0% | 0.1% |
| other (pxfm) | 0.0% | 0.0% | 0.0% | 0.0% |

## Phases

Inclusive samples of the outermost frame of each phase.

| Phase | bill-hr815 72 dpi | bill-hr815 144 dpi | usrguide 72 dpi | usrguide 144 dpi |
| --- | ---: | ---: | ---: | ---: |
| page raster (pyropdf_engine::render::draw) | 96.9% | 95.1% | 97.1% | 95.5% |
| interpret and encode (hayro::render_into) | 86.6% | 72.3% | 88.6% | 79.0% |
| glyph fills (hayro::Renderer::fill_outline_run) | 81.4% | 68.4% | 74.2% | 68.9% |
| rasterize (vello_cpu Dispatcher::rasterize) | 7.5% | 17.3% | 6.4% | 11.9% |
| take pixels (pyropdf_engine::render::take) | 1.5% | 3.6% | 1.3% | 3.1% |
| RGBA to RGB (pyropdf_engine::render::to_layout) | 0.0% | 0.0% | 0.0% | 0.0% |
| store, bridge, and open (pyropdf) | 0.2% | 0.2% | 1.3% | 1.3% |

## Pages

Best of three renders of each page, in ms, on a document opened once: every page, then the five
where pyropdf spends the most time. PyMuPDF and pyropdf read the samples; "with PNG" is what
`bench.py` times for pyropdf, `to_bytes()`, which encodes a PNG.

### bill-hr815 72 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 80.8 | 375.9 | 4.65 | 580.1 | 7.18 |
| 26 | 0.9 | 4.3 | 5.00 | 6.3 | 7.34 |
| 25 | 0.9 | 4.3 | 4.91 | 6.3 | 7.21 |
| 24 | 0.8 | 4.3 | 5.25 | 6.5 | 8.00 |
| 35 | 0.8 | 4.2 | 5.07 | 6.0 | 7.22 |
| 23 | 0.9 | 4.2 | 4.77 | 6.0 | 6.91 |

### bill-hr815 144 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 139.6 | 638.3 | 4.57 | 1382.4 | 9.90 |
| 23 | 1.4 | 7.3 | 5.21 | 13.9 | 9.86 |
| 22 | 1.3 | 7.1 | 5.41 | 13.6 | 10.42 |
| 25 | 1.5 | 7.1 | 4.79 | 14.4 | 9.71 |
| 9 | 1.4 | 7.1 | 4.96 | 14.3 | 10.01 |
| 26 | 1.4 | 7.0 | 4.97 | 13.5 | 9.63 |

### usrguide 72 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 17.3 | 71.0 | 4.11 | 103.5 | 5.99 |
| 3 | 0.8 | 3.3 | 4.27 | 4.7 | 6.00 |
| 17 | 0.7 | 3.3 | 4.47 | 4.7 | 6.45 |
| 24 | 0.7 | 3.3 | 4.48 | 4.6 | 6.23 |
| 4 | 0.8 | 3.2 | 3.99 | 4.4 | 5.58 |
| 15 | 0.7 | 3.1 | 4.39 | 4.4 | 6.27 |

### usrguide 144 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 26.3 | 112.4 | 4.27 | 234.1 | 8.89 |
| 3 | 1.2 | 5.1 | 4.38 | 10.2 | 8.70 |
| 17 | 1.1 | 5.1 | 4.48 | 10.1 | 8.87 |
| 24 | 1.1 | 5.1 | 4.57 | 10.2 | 9.19 |
| 15 | 1.1 | 4.9 | 4.56 | 9.6 | 9.04 |
| 4 | 1.2 | 4.8 | 4.08 | 9.7 | 8.17 |

## Hottest functions

Self samples, as shares of each case.

### bill-hr815 72 dpi

| Function | Self |
| --- | ---: |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 25.0% |
| `vello_common::strip::render (vello_cpu)` | 21.9% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 17.9% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 7.4% |
| `_platform_memmove (memcpy and memset)` | 4.6% |
| `vello_common::clip::intersect (vello_cpu)` | 3.4% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 2.4% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 1.8% |
| `<vello_common::pixmap::Pixmap>::try_take_rgb8 (vello_cpu)` | 1.5% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.4% |
| `<hashbrown::map::HashMap<alloc::string::String, font_types::glyph_id::GlyphId, rustc_hash::FxBuildHasher>>::get::<str> (hash maps)` | 1.0% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.9% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::init_uncovered_range (vello_cpu)` | 0.9% |
| `__bzero (memcpy and memset)` | 0.9% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::record_fill (vello_cpu)` | 0.6% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:


### bill-hr815 144 dpi

| Function | Self |
| --- | ---: |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 21.2% |
| `vello_common::strip::render (vello_cpu)` | 19.3% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 13.9% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 10.1% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 7.3% |
| `_platform_memmove (memcpy and memset)` | 6.7% |
| `<vello_common::pixmap::Pixmap>::try_take_rgb8 (vello_cpu)` | 3.6% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 2.6% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::init_uncovered_range (vello_cpu)` | 2.2% |
| `vello_common::clip::intersect (vello_cpu)` | 2.0% |
| `__bzero (memcpy and memset)` | 1.5% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.4% |
| `<hashbrown::map::HashMap<alloc::string::String, font_types::glyph_id::GlyphId, rustc_hash::FxBuildHasher>>::get::<str> (hash maps)` | 0.8% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.4% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::record_fill (vello_cpu)` | 0.4% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:


### usrguide 72 dpi

| Function | Self |
| --- | ---: |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 25.0% |
| `vello_common::strip::render (vello_cpu)` | 19.1% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 14.4% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 7.0% |
| `_platform_memmove (memcpy and memset)` | 2.7% |
| `vello_common::clip::intersect (vello_cpu)` | 2.6% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 2.1% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 1.6% |
| `zlib_rs::inflate::inflate_fast_help (image and stream decoding)` | 1.6% |
| `<vello_common::pixmap::Pixmap>::try_take_rgb8 (vello_cpu)` | 1.3% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.0% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.9% |
| `<read_fonts::ps::type1::Type1Font>::new (fonts)` | 0.9% |
| `zlib_rs::inflate::inflate (image and stream decoding)` | 0.9% |
| `<hashbrown::map::HashMap<alloc::string::String, font_types::glyph_id::GlyphId, rustc_hash::FxBuildHasher>>::get::<str> (hash maps)` | 0.8% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:


### usrguide 144 dpi

| Function | Self |
| --- | ---: |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 23.2% |
| `vello_common::strip::render (vello_cpu)` | 17.3% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 13.6% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 7.0% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 6.0% |
| `_platform_memmove (memcpy and memset)` | 5.6% |
| `<vello_common::pixmap::Pixmap>::try_take_rgb8 (vello_cpu)` | 3.1% |
| `vello_common::clip::intersect (vello_cpu)` | 2.2% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 2.1% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::init_uncovered_range (vello_cpu)` | 1.9% |
| `__bzero (memcpy and memset)` | 1.1% |
| `zlib_rs::inflate::inflate_fast_help (image and stream decoding)` | 1.0% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.0% |
| `zlib_rs::inflate::inflate (image and stream decoding)` | 0.6% |
| `<read_fonts::ps::type1::Type1Font>::new (fonts)` | 0.6% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:

